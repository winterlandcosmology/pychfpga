"""Correlator (UCORR) packet processor: integration plus HDF5 capture on emits."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Union

import numpy as np

from wtl import log

from pychfpga.common.state import State
from pychfpga.common.ucorr_packet_processor import UCorrPacketProcessor


class CorrDataProcessor(UCorrPacketProcessor):
    """UCORR packet integration plus correlator HDF5 capture (raw_acq REST path)."""

    def __init__(self, packet_receiver, config_getter, receive_window=3, verbose=0):
        if config_getter is None:
            raise TypeError("config_getter is required")
        self._config_getter = config_getter
        self.log = log.get_logger(self)
        self.recv = packet_receiver
        self.hdf5_file = None
        self.hdf5_start_time = None
        self.corr_udp_cookie_packets_total = 0
        self.corr_udp_packets_total = 0
        self.corr_current_udp_packets = 0
        self.corr_integrations_emitted = 0
        self.corr_hdf5_integrations_written = 0
        self.corr_max_processing_time = 0
        self.corr_current_processing_time = 0
        self.metrics_corr_packet_length_error = 0
        self.frame0_irigb_time = 0
        super().__init__(
            packet_receiver,
            config_getter=config_getter,
            receive_window=receive_window,
            verbose=verbose,
        )

    def filter_packets(self, n):
        """Count UCORR cookie matches, then delegate to the base filter."""
        if getattr(self, 'buf_cookie', None) is not None:
            self.corr_udp_cookie_packets_total += int(
                np.sum(self.buf_cookie[:n] == self.CORR_PACKET_COOKIE))
        bix = super().filter_packets(n)
        if bix is not None and bix.size:
            n_matched = int(bix.size)
            self.corr_udp_packets_total += n_matched
            self.corr_current_udp_packets += n_matched
        return bix

    def consume(self, result):
        """Append correlator HDF5 rows from a ``get_formatted_data()`` result.

        Args:
            result: ``(integ_ts, data, counts, sats)`` tuple, or None.

        Returns:
            The same ``result`` argument (for chaining).
        """
        if result is not None:
            self.corr_integrations_emitted += len(result[0])
            if self.hdf5_file is not None:
                self._write_hdf5_from_result(result)
        return result

    def after_process_udp_packets(self, elapsed):
        """Update correlator timing metrics after UDP assembly."""
        self.corr_max_processing_time = max(elapsed, self.corr_max_processing_time)
        self.corr_current_processing_time += elapsed

    def _hdf5_writer_geometry(self):
        """Return HDF5CorrWriter geometry from the firmware corr config on this processor."""
        if self.NBINS is None or self.NCHAN is None:
            raise RuntimeError(
                f'{self!r}: correlator geometry is not configured; '
                'start raw_acq with firmware corr config first')
        cfg = self.config
        engine = cfg.capture_engine if cfg.has_attr('capture_engine') else 'UCORR'
        geom = dict(
            capture_engine=engine,
            n_freq_bins=int(self.NBINS),
            number_of_correlated_inputs=int(self.NCHAN),
            sample_freq=float(cfg.sample_freq_mhz if cfg.has_attr('sample_freq_mhz') else 800.),
        )
        if engine == 'ICE':
            geom['number_of_correlators'] = int(cfg.number_of_correlators)
            geom['number_of_bins_per_frame'] = int(cfg.number_of_bins_per_frame)
        else:
            geom['number_of_correlators'] = int(cfg.NCORRS if cfg.has_attr('NCORRS') else 1)
        return geom

    def _write_hdf5_from_result(self, result):
        """Append completed UCORR integration rows to the correlator HDF5 file."""
        integ_ts, data, counts, sats = result
        n = len(integ_ts)
        self.corr_hdf5_integrations_written += n
        soft = max(1, int(self.soft_integ_period or 1))
        firm = max(1, int(self.firmware_integ_period or 1))
        for i in range(n):
            it = int(integ_ts[i])
            fpga_frame_number = it * soft * firm
            irigb_time = fpga_frame_number * 2560 + self.frame0_irigb_time
            row_data = data[i]
            row_sat = sats[i]
            row_count = counts[i]
            # UID 0: UCORR path uses a single logical stream per receiver instance.
            self.hdf5_file.write(0, it, fpga_frame_number, irigb_time, row_data, row_count, row_sat)

    def stop(self):
        """Stop correlator data capture (but data is still processed for metrics)"""
        if self.hdf5_file:
            self.hdf5_file.close()
            self.hdf5_file = None

    def start_corr_hdf5(self,
                        base_dir=None,
                        base_filename=None,
                        capture_duration=60,
                        capture_n_inputs=4,
                        autocorr_only=False,
                        elements_per_file=256,
                        frame0_irigb_time=0,
                        software_integration_period=None,
                        ):
        """ Start the capture of data in a HDF5 file.
        """
        if software_integration_period is not None:
            self.set_soft_integ_period(software_integration_period)
        self.update_config()

        if self.hdf5_file:
            self.stop_corr_hdf5()
            # raise RuntimeError('HDF5 dataWriter is already running')

        self.log.info(
            f'{self!r}: Starting correlator HDF5 data writer with '
            f'base_dir={base_dir}, base_filename={base_filename}, '
            f'capture_duration={capture_duration} (type={type(capture_duration)}), '
            f'n_inputs={capture_n_inputs}, elements_per_file={elements_per_file}, '
            f'soft_integ={self.soft_integ_period}, firm_integ={self.firmware_integ_period}, '
            f'irigb_time={frame0_irigb_time}')

        self.frame0_irigb_time = frame0_irigb_time

        base_dir = base_dir or '%(run_folder)s/corr'
        base_filename = base_filename or '%(elapsed_seconds)08d_%(file_number)04d.h5'

        self.capture_n_inputs = capture_n_inputs
        self.elements_per_file = elements_per_file

        self.hdf5_start_time = time.time()  # used to keep track of how long the disk capture has been running

        # self.hdf5_refresh_time = capture_refresh_time

        # Schedule for the acquisition to stop if capture_ducation is non-zero
        if capture_duration:
            self.log.info(f'{self!r}: HDF5 correlator data writer will be stopped in {capture_duration} seconds')
            asyncio.get_event_loop().call_later(capture_duration, self.stop_corr_hdf5)

        # Create the target folder
        self.hdf5_start_time = time.time()
        hdf5_start_isotime = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.hdf5_start_time))
        extra_fields = dict(
            hdf5_start_time=hdf5_start_isotime)

        self.hdf5_base_dir = self.pr.expand_path(base_dir, extra_fields)

        try:
            os.makedirs(self.hdf5_base_dir)
        except Exception:
            self.log.warning(f"{self!r}: couldn't make directory '{self.hdf5_base_dir}'. Using current directory.")
            self.hdf5_base_dir = './'

        try:
            from .hdf5_corr_writer import HDF5CorrWriter

            self.hdf5_file = HDF5CorrWriter(
                base_dir=self.hdf5_base_dir,
                filename=base_filename,
                elements_per_file=self.elements_per_file,
                capture_n_inputs=self.capture_n_inputs,
                autocorr_only=autocorr_only,
                **self._hdf5_writer_geometry(),
            )
            self.log.info(f'{self!r}: Correlator HDF5 data writer is started')
        except Exception as e:
            self.log.error(
                f'{self!r}: Could not open correlator file {self.hdf5_base_dir}/{base_filename}. '
                f'Error is\n{e!r}')

    def stop_corr_hdf5(self):
        if not self.hdf5_file:
            raise RuntimeError(f'{self!r}: Correlator HDF5 dataWriter is not running. Cannot stop it.')
        self.log.info(f'{self!r}: Stopping Correlator HDF5 data writer')
        hdf5_file = self.hdf5_file
        self.hdf5_file = None  # Stop the thread from using the file before we close it
        hdf5_file.close()
        self.hdf5_start_time = None

    async def get_metrics_async(self, metrics):
        """ Add the correlator metrics to the specified `Metrics` object

        Parameters:

            metrics (Metrics): Metrics object to which the metrics will be added.

        """

        metrics.add('raw_acq_corr_udp_cookie_packets', value=self.corr_udp_cookie_packets_total)
        metrics.add('raw_acq_corr_udp_packets', value=self.corr_udp_packets_total)
        metrics.add('raw_acq_corr_integrations_emitted', value=self.corr_integrations_emitted)
        metrics.add('raw_acq_corr_hdf5_integrations_written', value=self.corr_hdf5_integrations_written)
        # Legacy alias: previously counted HDF5 rows only (misleading when writer failed).
        metrics.add('raw_acq_processed_corr_packets', value=self.corr_hdf5_integrations_written)
        metrics.add('raw_acq_corr_max_processing_time', value=self.corr_max_processing_time)
        self.corr_max_processing_time = 0
        metrics.add(
            'raw_acq_corr_avg_processing_time',
            value=(
                self.corr_current_processing_time / self.corr_current_udp_packets
                if self.corr_current_udp_packets else 0),
        )
        self.corr_current_processing_time = 0
        self.corr_current_udp_packets = 0
        if self.hdf5_file:
            metrics.add('raw_acq_corr_hdf5_file_number', value=self.hdf5_file.file_number)
            metrics.add('raw_acq_corr_hdf5_current_sample_in_file', value=self.hdf5_file.n)
            metrics.add('raw_acq_corr_hdf5_current_total_samples', value=self.hdf5_file.n_total)

        return

    def stop_adc_hdf5(self):
        """Alias used by legacy REST endpoint name."""
        return self.stop_corr_hdf5()


# Backward-compatible name from the integration PR.
UCorrAcqPacketProcessor = CorrDataProcessor
