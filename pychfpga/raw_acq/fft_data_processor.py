"""FFT / SCALER spectrum packet processor (cookie 0xA1)."""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np

from pychfpga.common.state import State

SOURCE_INDEX = 1

from wtl import log

PACKET_COOKIE_FFT = 0xA1


class FFTDataProcessor:
    """FFT / SCALER spectrum sink (source 1 / cookie 0xA1)."""

    def __init__(
        self,
        packet_receiver,
        raw_processor,
        config=None,
        stream_ids=(),
        **kwargs,
    ):
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
        nchan = getattr(self.raw, "NCHAN", len(self.config.get("stream_ids") or ()))
        if nchan:
            self._init_fft_state(nchan=nchan)

    @property
    def NCHAN(self):
        return self.raw.NCHAN

    @property
    def stream_ids(self):
        return self.raw.stream_ids

    @property
    def expected_packets(self):
        return self.raw.expected_packets

    def update_config(self, config=None):
        if config is not None:
            self.config.update(config)
        nchan_before = getattr(self, "NCHAN", 0)
        n_bins_before = getattr(self, "_fft_n_bins", 1)
        self._sync_stream_id_helpers()
        self._apply_fft_offset_encoding_from_config()
        if not hasattr(self, "fft_rms_buffer"):
            self._init_fft_state(nchan=self.NCHAN)
            return
        if self.NCHAN != nchan_before:
            self._init_fft_state(nchan=self.NCHAN)
        elif self._fft_n_bins != n_bins_before:
            self._alloc_fft_rms_buffers(self.NCHAN, self._fft_n_bins)

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

    def _apply_fft_offset_encoding_from_config(self) -> None:
        enc = bool(self.config.get("fft_offset_encoding", True))
        self.fft_offset_encoding_mask = -128 if enc else 0

    def _init_fft_state(self, nchan=None):
        """FFT / scaler RMS acquisition state fed from assembled frames."""
        if nchan is None:
            nchan = self.NCHAN
        self.fft_processed_packets = 0
        self.fft_max_processing_time = 0
        self.fft_current_processing_time = 0
        self.fft_current_processed_packets = 0
        self.metrics_incomplete_frames = np.zeros(nchan, dtype=np.int32)
        self._apply_fft_offset_encoding_from_config()
        self.fft_lock = threading.RLock()
        self._fft_n_bins = 1
        self._alloc_fft_rms_buffers(nchan, self._fft_n_bins)

    def _alloc_fft_rms_buffers(self, nchan, n_bins):
        self._fft_n_bins = int(n_bins)
        self.fft_rms_started = np.zeros(nchan, dtype=bool)
        self.fft_rms_done = np.zeros(nchan, dtype=np.int8)
        self.fft_target_bank = np.zeros(nchan, dtype=np.int8)
        self.fft_rms_buffer = np.zeros((nchan, self._fft_n_bins), dtype=np.int32)
        self.fft_rms_current = np.zeros((nchan, self._fft_n_bins), dtype=np.int32)
        self.fft_n_frames = np.zeros(nchan, dtype=np.int32)
        self.fft_rms_average = np.zeros(nchan, dtype=np.int32) + 100
        self.fft_rms = np.zeros((nchan, self._fft_n_bins), dtype=np.float32)
        self.fft_overflow = np.zeros((nchan, self._fft_n_bins), dtype=np.int32)
        self.fft_metrics_updated = np.zeros(nchan, dtype=np.int8)

    def _spectrum_bin_powers(self, data_row) -> np.ndarray:
        """Per-bin power vector from one assembled channel row."""
        row = np.asarray(data_row)
        if np.iscomplexobj(row):
            flat = row.reshape(-1)
            return (flat.real.astype(np.int32) ** 2 + flat.imag.astype(np.int32) ** 2).astype(np.int32)
        raw = row.view(np.int8).reshape(-1) if row.dtype != np.int8 else row.reshape(-1)
        mask = self.fft_offset_encoding_mask
        re = ((raw[::2] ^ mask) >> 4).astype(np.int32)
        im = ((raw[1::2] ^ mask) >> 4).astype(np.int32)
        return re * re + im * im

    def _process_fft_emitted(self, ts, data, counts, indices):
        """Update FFT RMS acquisition and metrics from one ``get_formatted_data`` emission."""
        n_out = ts.shape[0]
        packet_inc = int(counts[:n_out].sum())
        self.processed_packets += packet_inc
        self.fft_processed_packets += packet_inc
        self.fft_current_processed_packets += packet_inc

        for tix in range(n_out):
            for cix in range(self.NCHAN):
                if int(counts[tix, cix]) < self.expected_packets:
                    self.metrics_incomplete_frames[cix] += 1
                    continue
                powers = self._spectrum_bin_powers(data[tix, cix])
                n_bins = powers.size
                if n_bins != self._fft_n_bins:
                    with self.fft_lock:
                        self._alloc_fft_rms_buffers(self.NCHAN, n_bins)

                with self.fft_lock:
                    self.fft_rms_current[cix] = powers
                    self.fft_metrics_updated[cix] = True

                    if not self.fft_rms_started[cix] or self.fft_rms_done[cix]:
                        continue
                    self.fft_rms_buffer[cix] += powers
                    self.fft_n_frames[cix] += 1
                    if self.fft_n_frames[cix] != self.fft_rms_average[cix]:
                        continue
                    self.fft_rms_done[cix] = True
                    self.fft_rms[cix] = np.sqrt(
                        self.fft_rms_buffer[cix].astype(np.float32) / self.fft_n_frames[cix])
                    self.fft_rms_buffer[cix] = 0

    async def start_fft_rms_async(self, stream_ids, target_gain_bank, number_of_frames=100):
        """Start averaged RMS acquisition for the given stream IDs and scaler bank."""
        ix = [self.sid_map[int(sid)] for sid in stream_ids if int(sid) in self.sid_map]
        if not ix:
            return
        with self.fft_lock:
            self.fft_target_bank[ix] = target_gain_bank
            self.fft_rms_average[ix] = number_of_frames
            self.fft_n_frames[ix] = 0
            self.fft_rms_buffer[ix] = 0
            self.fft_overflow[ix] = 0
            self.fft_rms_done[ix] = False
            self.fft_rms_started[ix] = True

    async def get_fft_rms_async(self, all_done=True):
        """Return FFT RMS data products that are ready."""
        with self.fft_lock:
            ix = np.logical_and(self.fft_rms_started, self.fft_rms_done)
            sid = self.stream_id[ix]
            rms = self.fft_rms[ix]
            self.fft_rms_started[ix] = False
        self.log.info(f'{self!r}: get_fft_rms returned FFT RMS vectors from {sid.size} channels')
        return sid, rms

    def consume(self, result):
        """Update FFT RMS / metrics from a ``get_formatted_data(1)`` result.

        Args:
            result: ``(ts, data, counts, indices)`` tuple, or None.

        Returns:
            The same ``result`` argument (for chaining).
        """
        t0 = time.time()
        if result is not None:
            self._process_fft_emitted(*result)
        t1 = time.time()
        self.fft_max_processing_time = max(t1 - t0, self.fft_max_processing_time)
        self.fft_current_processing_time += t1 - t0
        return result

    def flush(self, flush_timeout=None):
        """No-op; assembly flush is handled by the shared raw processor."""

    async def get_metrics_async(self, metrics):
        """Gather raw acquisition metrics for the FFT path."""
        metrics.add('raw_acq_processed_packets', value=self.processed_packets)
        metrics.add('raw_acq_processed_fft_packets', value=self.fft_processed_packets)

        with self.lock:
            fft_max_processing_time = self.fft_max_processing_time
            self.fft_max_processing_time = 0

        metrics.add('raw_acq_fft_max_processing_time', value=fft_max_processing_time)

        with self.lock:
            fft_avg_processing_time = (
                (self.fft_current_processing_time * 1.0 * self.NCHAN / self.fft_current_processed_packets)
                if self.fft_current_processed_packets else 0)
            self.fft_current_processing_time = 0
            self.fft_current_processed_packets = 0

        metrics.add('raw_acq_fft_avg_processing_time', value=fft_avg_processing_time)

        await asyncio.sleep(0)

        cix, = np.where(self.fft_metrics_updated)
        for ix in cix:
            with self.fft_lock:
                stream_id = self.stream_id[ix]
                fft_rms_current = self.fft_rms_current[ix]
                metrics_incomplete = self.metrics_incomplete_frames[ix]
                fft_scaler_overflows = self.fft_overflow[ix, 1:] if self.fft_overflow.shape[1] > 1 else 0

            crate, slot, chan = self.unpack_stream_id(stream_id)
            metrics.add('raw_acq_fft_rms', value=np.sqrt(np.mean(fft_rms_current)), crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_packet_length_error',
                        value=metrics_incomplete, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_scaler_overflows',
                        value=np.sum(fft_scaler_overflows), crate=crate, slot=slot, chan=chan)
            time.sleep(0.0001)
            await asyncio.sleep(0)
            self.fft_metrics_updated[cix] = False

    def stop(self):
        pass
