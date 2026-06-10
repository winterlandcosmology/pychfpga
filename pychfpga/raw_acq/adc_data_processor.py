"""ADC / FUNCGEN raw timestream packet processor (cookie 0xA0)."""

from __future__ import annotations

import asyncio
import datetime
import os
import threading
import time

import numpy as np

from wtl import log

from pychfpga.common.state import State

PACKET_COOKIE_ADC = 0xA0

_BYTES_PER_SAMPLE_TO_DTYPE = {
    1: np.dtype("i1"),
    2: np.dtype("i2"),
    4: np.dtype("i4"),
    8: np.dtype("i8"),
}


class ADCDataProcessor:
    """ADC / FUNCGEN sink (source 0 / cookie 0xA0) on a shared raw processor."""

    def __init__(
        self,
        packet_receiver,
        raw_processor,
        config=None,
        stream_ids=(),
        metrics_refresh_time=10,
        adc_rms_refresh_count=100,
        **kwargs,
    ) -> None:
        kwargs.pop("metrics_refresh_time", None)
        kwargs.pop("adc_rms_refresh_count", None)
        kwargs.pop("fft_offset_encoding", None)
        kwargs.pop("receive_window", None)
        kwargs.pop("verbose", None)
        kwargs.pop("decode_strict", None)
        self.raw = raw_processor
        self.recv = packet_receiver
        self.config = State(config).deepcopy() if config is not None else State()
        if stream_ids and not self.config.get("stream_ids"):
            self.config.stream_ids = tuple(stream_ids)
        self.log = log.get_logger(self)
        self.lock = threading.RLock()
        self.processed_packets = 0
        self._sync_stream_id_helpers()
        self._init_adc_state(metrics_refresh_time, adc_rms_refresh_count)
        self._sync_adc_hdf5_layout()

    @property
    def NCHAN(self):
        return self.raw.NCHAN

    @property
    def stream_ids(self):
        return self.raw.stream_ids

    @property
    def expected_packets(self):
        return self.raw.expected_packets

    @staticmethod
    def storage_dtype_from_config(state) -> np.dtype:
        """Resolve HDF5/metrics sample dtype from ``storage_dtype`` or ``bytes_per_sample``."""
        if state.get("storage_dtype") is not None:
            return np.dtype(state.storage_dtype)
        bps = int(state.bytes_per_sample)
        try:
            return _BYTES_PER_SAMPLE_TO_DTYPE[bps]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported bytes_per_sample={bps!r}; set storage_dtype explicitly "
                "(e.g. 'i2', 'i4', 'c8')."
            ) from exc

    def _sync_adc_hdf5_layout(self) -> None:
        """Sample dtype and length per assembled frame for HDF5 and metrics."""
        state = self.config
        self.adc_storage_dtype = self.storage_dtype_from_config(state)
        bps = int(state.get("bytes_per_sample") or self.raw.BYTES_PER_SAMPLE)
        burst_bytes = int(self.raw.PKT_FRAMES_PER_BURST) * int(self.raw.BYTES_PER_FRAME)
        if np.issubdtype(self.adc_storage_dtype, np.complexfloating):
            self.adc_samples_per_frame = burst_bytes // (2 * bps)
        else:
            self.adc_samples_per_frame = burst_bytes // bps

    def update_config(self, config=None) -> None:
        nchan_before = getattr(self, "NCHAN", 0)
        if config is not None:
            self.config.update(config)
        self._sync_adc_hdf5_layout()
        self._sync_stream_id_helpers()
        if self.NCHAN == nchan_before or not hasattr(self, "metrics_refresh_time"):
            return
        mrt = self.metrics_refresh_time
        arr = self.adc_rms_refresh_count
        hdf5_file, hdf5_aux_file = self.hdf5_file, self.hdf5_aux_file
        self._init_adc_state(mrt, arr)
        self.hdf5_file, self.hdf5_aux_file = hdf5_file, hdf5_aux_file

    def _sync_stream_id_helpers(self) -> None:
        ids = tuple(int(s) for s in getattr(self, "stream_ids", ()) or ())
        self.stream_id = np.array(ids, dtype=np.uint16)
        self.sid_map = {sid: ix for ix, sid in enumerate(ids)}
        self.chan_ids = [self.unpack_stream_id(sid) for sid in self.stream_id] if ids else []

    @staticmethod
    def unpack_stream_id(stream_id):
        crate = (int(stream_id) >> 8) & 0xF
        slot = (int(stream_id) >> 4) & 0xF
        chan = int(stream_id) & 0xF
        return crate, slot, chan

    def _init_adc_state(self, metrics_refresh_time=10, adc_rms_refresh_count=100):
        """Metrics, HDF5 capture, and averaged RMS state fed from assembled frames."""
        self.adc_processed_packets = 0
        self.adc_max_processing_time = 0
        self.adc_current_processing_time = 0
        self.adc_current_processed_packets = 0
        self.adc_hdf5_max_processing_time = 0
        self.adc_metrics_max_processing_time = 0
        self.adc_total_hdf5_processing_time = 0
        self.adc_rms_max_processing_time = 0
        self.adc_frames = np.zeros(self.NCHAN, dtype=np.uint32)
        self.start_time = time.time()
        self.metrics_refresh_time = metrics_refresh_time
        self.metrics_last_time = np.zeros(self.NCHAN, dtype=np.float64)
        self.metrics_updated = np.zeros(self.NCHAN, dtype=np.int8)
        self.metrics_rms = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_min = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_max = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_mean = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_maxdiff = np.zeros(self.NCHAN, dtype=np.float32)
        self.metrics_incomplete_frames = np.zeros(self.NCHAN, dtype=np.int32)
        self.metrics_ramp_error_count = np.zeros(self.NCHAN, dtype=np.float32)
        self.hdf5_write_time = 0
        self.hdf5_start_time = None
        self.hdf5_base_dir = None
        self.hdf5_file = None
        self.hdf5_refresh_time = 30
        self.hdf5_last_time = np.zeros(self.NCHAN, dtype=np.float64)
        self.hdf5_block_writes = 0
        self.hdf5_capture_aux_enable = False
        self.hfd5_aux_base_dir = None
        self.hdf5_aux_file = None
        self.hdf5_aux_refresh_time = 1
        self.hdf5_aux_stream_ids = [6]
        self.hdf5_aux_last_time = np.zeros(self.NCHAN, dtype=np.float64)
        self.adc_rms_rlock = threading.RLock()
        self.adc_rms_refresh_count = adc_rms_refresh_count
        self.adc_rms = np.zeros(self.NCHAN, dtype=np.float32)
        self.adc_rms_timestamp = np.zeros(self.NCHAN, dtype=np.float64)
        self.adc_rms_buffer = np.zeros(self.NCHAN, dtype=np.float32)
        self.adc_rms_frame_count = np.zeros(self.NCHAN, dtype=np.int32)
        self.adc_rms_updated = np.zeros(self.NCHAN, dtype=bool)
        self.chan_number_mismatch_count = 0
        self.crate_number_mismatch_count = 0
        self.slot_number_mismatch_count = 0

    def _channel_frame(self, wire_row) -> np.ndarray:
        """Decode one assembled channel row using the shared raw processor."""
        wire_bytes = int(self.raw.PKT_FRAMES_PER_BURST) * int(self.raw.BYTES_PER_FRAME)
        raw = np.ascontiguousarray(wire_row).reshape(-1).view(np.uint8)[:wire_bytes]
        shaped = raw.reshape(1, 1, raw.size)
        _, formatted, _ = self.raw.format_data(shaped, source=0)
        return np.asarray(formatted[0, 0], dtype=self.adc_storage_dtype)

    def _process_adc_emitted(self, ts, data, counts) -> None:
        """Feed HDF5, metrics, and averaged RMS from one ``get_formatted_data(0)`` emission."""
        n_out = ts.shape[0]
        packet_inc = int(counts[:n_out].sum())
        self.processed_packets += packet_inc
        self.adc_processed_packets += packet_inc
        self.adc_current_processed_packets += packet_inc

        for tix in range(n_out):
            timestamp = np.uint64(ts[tix])
            for cix in range(self.NCHAN):
                n_pkt = int(counts[tix, cix])
                if n_pkt < self.expected_packets:
                    self.metrics_incomplete_frames[cix] += 1
                    continue
                frame = np.asarray(data[tix, cix], dtype=self.adc_storage_dtype)
                self.adc_frames[cix] += 1

                t0 = time.time()
                self._update_adc_metrics(cix, frame, t0)
                t1 = time.time()
                self._write_adc_hdf5(cix, timestamp, frame, t0)
                t2 = time.time()
                self._update_adc_rms(cix, frame)
                t3 = time.time()

                self.adc_metrics_max_processing_time = max(t1 - t0, self.adc_metrics_max_processing_time)
                self.adc_total_hdf5_processing_time += t2 - t1
                self.adc_hdf5_max_processing_time = max(t2 - t1, self.adc_hdf5_max_processing_time)
                self.adc_rms_max_processing_time = max(t3 - t2, self.adc_rms_max_processing_time)

    def _update_adc_metrics(self, cix, frame, t0):
        if (t0 - self.metrics_last_time[cix]) < self.metrics_refresh_time:
            return
        self.metrics_last_time[cix] = t0
        self.metrics_updated[cix] = True
        self.metrics_mean[cix] = np.mean(frame)
        self.metrics_rms[cix] = np.sqrt(np.mean((frame.astype(np.float32) - self.metrics_mean[cix]) ** 2))
        self.metrics_min[cix] = np.min(frame)
        self.metrics_max[cix] = np.max(frame)
        if frame.size > 1:
            self.metrics_maxdiff[cix] = np.max(np.abs(np.diff(frame.astype(np.int16))))

    def _write_adc_hdf5(self, cix, timestamp, frame, t0):
        def maybe_write(hdf5_file, hdf5_last_time, hdf5_refresh_time, stream_id_filter=None):
            if not hdf5_file:
                return
            if stream_id_filter is not None and int(self.stream_id[cix]) not in stream_id_filter:
                return
            if (t0 - hdf5_last_time[cix]) <= hdf5_refresh_time:
                return
            hdf5_last_time[cix] = t0
            flags = np.zeros(1, dtype=np.uint8)
            hdf5_file.write(
                np.asarray([timestamp], dtype=np.uint64),
                np.asarray([self.stream_id[cix]], dtype=np.uint16),
                flags,
                frame.reshape(1, -1),
            )
            self.hdf5_block_writes += 1

        maybe_write(self.hdf5_file, self.hdf5_last_time, self.hdf5_refresh_time)
        if self.hdf5_capture_aux_enable:
            maybe_write(
                self.hdf5_aux_file,
                self.hdf5_aux_last_time,
                self.hdf5_aux_refresh_time,
                stream_id_filter=self.hdf5_aux_stream_ids,
            )

    def _update_adc_rms(self, cix, frame):
        self.adc_rms_buffer[cix] += np.var(frame.astype(np.float64))
        self.adc_rms_frame_count[cix] += 1
        if self.adc_rms_frame_count[cix] != self.adc_rms_refresh_count:
            return
        with self.adc_rms_rlock:
            self.adc_rms[cix] = np.sqrt(self.adc_rms_buffer[cix] / self.adc_rms_frame_count[cix])
            self.adc_rms_timestamp[cix] = time.time()
            self.adc_rms_frame_count[cix] = 0
            self.adc_rms_buffer[cix] = 0
            self.adc_rms_updated[cix] = True

    def start_adc_hdf5(self,
                       base_dir,
                       base_filename,
                       capture_duration=60,
                       capture_refresh_time=0,
                       elements_per_file=2048 * 64,
                       capture_aux_enable=False,
                       aux_base_dir='./',
                       aux_base_filename='{file_number:06d}_aux.h5',
                       capture_aux_refresh_time=0,
                       capture_aux_stream_ids=[6],
                       aux_elements_per_file=2048 * 64):
        if self.hdf5_file:
            self.stop_adc_hdf5()
        if self.hdf5_aux_file:
            self.stop_adc_hdf5()
        self.log.info(
            f'{self!r}: Starting HDF5 raw data data writer '
            f'with base_dir={base_dir}, base_filename={base_filename}, '
            f'capture_duration={capture_duration} (type={type(capture_duration)}), '
            f'elements_per_file={elements_per_file}')

        self.hdf5_capture_aux_enable = capture_aux_enable
        self.elements_per_file = elements_per_file
        self.hdf5_start_time = time.time()
        self.hdf5_refresh_time = capture_refresh_time

        if self.hdf5_capture_aux_enable is True:
            self.log.info(
                f'{self!r}: Starting HDF5 raw data data writer for auxillary channel '
                f'with base_dir={aux_base_dir}, base_filename={aux_base_filename}, '
                f'capture_duration={capture_duration} (type={type(capture_duration)}), '
                f'elements_per_file={aux_elements_per_file}')
            self.hdf5_aux_refresh_time = capture_aux_refresh_time
            self.hdf5_aux_stream_ids = capture_aux_stream_ids
            self.aux_elements_per_file = aux_elements_per_file

        if capture_duration:
            self.log.info(f'{self!r}: HDF5 raw data data writer will be stopped in {capture_duration} seconds')
            asyncio.get_event_loop().call_later(capture_duration, self.stop_adc_hdf5)

        extra_fields = dict(
            hdf5_start_time=datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))

        self.hdf5_base_dir = self.pr.expand_path(base_dir, extra_fields)
        try:
            os.makedirs(self.hdf5_base_dir, exist_ok=True)
        except OSError as exc:
            self.log.warning(
                f"{self!r}: couldn't make directory '{self.hdf5_base_dir}' ({exc}). "
                "Using current directory."
            )
            self.hdf5_base_dir = './'

        from .hdf5_raw_writer import HDF5RawWriter

        self.hdf5_file = HDF5RawWriter(
            base_dir=self.hdf5_base_dir,
            filename=base_filename,
            elements_per_file=self.elements_per_file,
            storage_dtype=self.adc_storage_dtype,
            samples_per_frame=self.adc_samples_per_frame,
        )

        if self.hdf5_capture_aux_enable is True:
            self.hdf5_aux_base_dir = self.pr.expand_path(aux_base_dir, extra_fields)
            try:
                os.makedirs(self.hdf5_aux_base_dir, exist_ok=True)
            except OSError as exc:
                self.log.warning(
                    f"{self!r}: couldn't make directory '{self.hdf5_aux_base_dir}' ({exc}). "
                    "Using current directory."
                )
                self.hdf5_aux_base_dir = './'

            self.hdf5_aux_file = HDF5RawWriter(
                base_dir=self.hdf5_aux_base_dir,
                filename=aux_base_filename,
                elements_per_file=self.aux_elements_per_file,
                storage_dtype=self.adc_storage_dtype,
                samples_per_frame=self.adc_samples_per_frame,
            )

    def stop_adc_hdf5(self):
        if not self.hdf5_file:
            raise RuntimeError(f'{self!r}: HDF5 dataWriter is not running. Cannot stop it.')
        self.log.info(f'{self!r}: Stopping HDF5 data writer')
        hdf5_file = self.hdf5_file
        self.hdf5_file = None
        hdf5_file.close()

        if self.hdf5_capture_aux_enable is True:
            if not self.hdf5_aux_file:
                raise RuntimeError(f'{self!r}: Auxiliary HDF5 dataWriter is not running. Cannot stop it.')
            self.log.info(f'{self!r}: Stopping auxiliary HDF5 data writer')
            hdf5_aux_file = self.hdf5_aux_file
            self.hdf5_aux_file = None
            hdf5_aux_file.close()

        self.hdf5_start_time = None
        write_rate = ((self.adc_hdf5_max_processing_time * 1000. / self.hdf5_block_writes)
                      if self.hdf5_block_writes else 0)
        self.log.info(
            f'{self!r}: Write {self.hdf5_block_writes} data blocks in {self.adc_hdf5_max_processing_time:.3f} s total'
            f'({write_rate:.0f} ms/write)')

    async def get_adc_rms_async(self):
        """Return latest averaged ADC RMS values per configured channel."""
        with self.adc_rms_rlock:
            return list(zip(
                self.chan_ids,
                self.adc_rms_timestamp.tolist(),
                self.adc_rms.tolist()))

    async def get_data_async(self):
        """Deprecated full-frame capture hook (not implemented for assembled capture)."""
        raise NotImplementedError(
            "get_data_async is not supported; use process_packets() assembled output instead.")

    def consume(self, result):
        """Update ADC HDF5 / metrics / RMS from a ``get_formatted_data(0)`` result."""
        t0 = time.time()
        if result is not None:
            ts, data, counts, _indices = result
            self._process_adc_emitted(ts, data, counts)
        t1 = time.time()
        self.adc_max_processing_time = max(t1 - t0, self.adc_max_processing_time)
        self.adc_current_processing_time += t1 - t0
        return result

    def flush(self, flush_timeout=None):
        """No-op; assembly flush is handled by the shared raw processor."""

    def stop(self):
        if self.hdf5_file:
            self.stop_adc_hdf5()

    async def get_metrics_async(self, metrics):
        """Gather raw acquisition metrics for the ADC path."""
        metrics.add('raw_acq_hdf5_run_time', value=(
            0 if self.hdf5_start_time is None else time.time() - self.hdf5_start_time))

        metrics.add('raw_acq_hdf5_write_time', value=self.hdf5_write_time)
        self.hdf5_write_time = 0
        if self.hdf5_file:
            metrics.add('raw_acq_hdf5_n_elements', value=self.hdf5_file.nn + self.hdf5_file.n)
            metrics.add('raw_acq_hdf5_n_elements_max', value=self.hdf5_file.elements_per_file)
            metrics.add('raw_acq_hdf5_number_of_files', value=self.hdf5_file.file_number)

        await asyncio.sleep(0)

        metrics.add('raw_acq_processed_packets', value=self.processed_packets)
        metrics.add('raw_acq_processed_adc_packets', value=self.adc_processed_packets)
        metrics.add('raw_acq_hdf5_data_block_writes', value=self.hdf5_block_writes)

        with self.lock:
            adc_max_processing_time = self.adc_max_processing_time
            adc_metrics_max_processing_time = self.adc_metrics_max_processing_time
            adc_hdf5_max_processing_time = self.adc_hdf5_max_processing_time
            adc_rms_max_processing_time = self.adc_rms_max_processing_time
            self.adc_max_processing_time = 0
            self.adc_metrics_max_processing_time = 0
            self.adc_hdf5_max_processing_time = 0
            self.adc_rms_max_processing_time = 0

        metrics.add('raw_acq_adc_max_processing_time', value=adc_max_processing_time)
        metrics.add('raw_acq_adc_metrics_max_processing_time', value=adc_metrics_max_processing_time)
        metrics.add('raw_acq_adc_hdf5_max_processing_time', value=adc_hdf5_max_processing_time)
        metrics.add('raw_acq_adc_rms_max_processing_time', value=adc_rms_max_processing_time)

        with self.lock:
            adc_avg_processing_time = (
                (self.adc_current_processing_time * 1.0 * self.NCHAN / self.adc_current_processed_packets)
                if self.adc_current_processed_packets else 0)
            self.adc_current_processing_time = 0
            self.adc_current_processed_packets = 0

        metrics.add('raw_acq_adc_avg_processing_time', value=adc_avg_processing_time)

        await asyncio.sleep(0)

        cix, = np.where(self.metrics_updated)

        for ix in cix:
            with self.lock:
                adc_frames = self.adc_frames[ix]
                metrics_rms = self.metrics_rms[ix]
                metrics_min = self.metrics_min[ix]
                metrics_max = self.metrics_max[ix]
                metrics_mean = self.metrics_mean[ix]
                metrics_maxdiff = self.metrics_maxdiff[ix]
                metrics_ramp_error_count = self.metrics_ramp_error_count[ix]
                metrics_incomplete = self.metrics_incomplete_frames[ix]

            crate, slot, chan = self.unpack_stream_id(self.stream_id[ix])
            metrics.add('raw_acq_adc_frames', value=adc_frames, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_rms', value=metrics_rms, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_min', value=metrics_min, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_max', value=metrics_max, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_mean', value=metrics_mean, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_max_diff', value=metrics_maxdiff, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_ramp_errors', value=metrics_ramp_error_count, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_adc_packet_length_error',
                        value=metrics_incomplete, crate=crate, slot=slot, chan=chan)
            time.sleep(0.0001)
            await asyncio.sleep(0)
        self.metrics_updated[cix] = False

        with self.adc_rms_rlock:
            cix, = np.where(self.adc_rms_updated)
            if cix.size > 0:
                c_stream_id = self.stream_id[cix]
                c_adc_rms = self.adc_rms[cix]
                self.adc_rms_updated[cix] = False
            else:
                c_stream_id = []
                c_adc_rms = []

        for csid, crms in zip(c_stream_id, c_adc_rms):
            crate, slot, chan = self.unpack_stream_id(csid)
            metrics.add('raw_acq_adc_averaged_rms', value=crms, crate=crate, slot=slot, chan=chan)
            await asyncio.sleep(0)

        metrics.add('raw_acq_run_time', value=0 if self.start_time is None else time.time() - self.start_time)

        metrics.add('raw_acq_chan_mismatch', value=self.chan_number_mismatch_count)
        metrics.add('raw_acq_crate_mismatch', value=self.crate_number_mismatch_count)
        metrics.add('raw_acq_slot_mismatch', value=self.slot_number_mismatch_count)
