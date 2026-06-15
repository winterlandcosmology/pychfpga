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
        """Packets per physical frame row after ``get_formatted_data`` split."""
        return self.raw.NUMBER_OF_SUBFRAMES

    @property
    def physical_frames_per_logical(self):
        """Physical UCAP frame rows that form one logical FFT spectrum (e.g. 2 for fft_2x)."""
        caps = self.raw.config.get('capture_format') or []
        cf = caps[SOURCE_INDEX] if len(caps) > SOURCE_INDEX else None
        if cf is not None and not isinstance(cf, State):
            cf = State(cf)
        if cf is not None and cf.fmt_is_complex and int(cf.fmt_interleave_factor or 1) > 1:
            # ``get_formatted_data`` already deinterleaves physical frame pairs.
            return 1
        pplf = self.config.get('packets_per_logical_frame')
        if pplf is None:
            pplf = getattr(self.raw.config, 'packets_per_logical_frame', 1)
        return max(1, int(pplf or 1))

    def update_config(self, config=None):
        if config is not None:
            self.config.update(config)
        nchan_before = getattr(self, "NCHAN", 0)
        n_bins_before = getattr(self, "_fft_n_bins", 1)
        self._sync_stream_id_helpers()
        if not hasattr(self, "fft_power_sum"):
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

    def _capture_format_summary(self):
        """Short decoder summary for logs and REST diagnostics."""
        caps = self.raw.config.get('capture_format') or []
        cf = caps[SOURCE_INDEX] if len(caps) > SOURCE_INDEX else None
        if cf is not None and not isinstance(cf, State):
            cf = State(cf)
        if cf is None:
            return dict(fmt=None)
        return dict(
            fmt_base_width=int(cf.fmt_base_width),
            fmt_is_complex=bool(cf.fmt_is_complex),
            fmt_interleave_factor=int(cf.fmt_interleave_factor or 1),
        )

    def get_acquisition_diagnostics(self):
        """Snapshot counters useful while FFT RMS acquisition is running."""
        if not hasattr(self, 'fft_rms_started'):
            return dict(active=False)
        with self.fft_lock:
            active = bool(np.any(self.fft_rms_started & ~self.fft_rms_done))
            return dict(
                active=active,
                expected_packets=int(self.expected_packets),
                packets_per_frame=int(getattr(self.raw, 'packets_per_frame', self.expected_packets)),
                frames_per_burst=int(getattr(self.raw, 'FRAMES_PER_BURST', 1)),
                subframes_per_frame=int(getattr(self.raw, 'NUMBER_OF_SUBFRAMES', 1)),
                rows_emitted=int(self._diag_rows_emitted),
                rows_complete=int(self._diag_rows_complete),
                rows_incomplete=int(self._diag_rows_incomplete),
                max_count_seen=int(self._diag_max_count),
                incomplete_total=self.metrics_incomplete_frames.tolist(),
                non_complex_total=self.metrics_non_complex_frames.tolist(),
                capture_format=self._capture_format_summary(),
            )

    def _maybe_log_acquisition_progress(self):
        """Periodic INFO log while FFT RMS acquisition is in progress."""
        if not np.any(self.fft_rms_started & ~self.fft_rms_done):
            return
        now = time.time()
        if now - self._diag_last_log_time < 5.0:
            return
        self._diag_last_log_time = now
        with self.fft_lock:
            channels = [
                dict(
                    stream_id=int(self.stream_id[cix]),
                    n_frames=int(self.fft_n_frames[cix]),
                    target=int(self.fft_rms_average[cix]),
                    done=bool(self.fft_rms_done[cix]),
                )
                for cix in range(self.NCHAN)
                if self.fft_rms_started[cix] and not self.fft_rms_done[cix]
            ]
        diag = self.get_acquisition_diagnostics()
        self.log.info(
            f'{self!r}: FFT RMS progress rows_complete={diag["rows_complete"]} '
            f'rows_incomplete={diag["rows_incomplete"]} max_count={diag["max_count_seen"]} '
            f'expected={diag["expected_packets"]} decoder={diag["capture_format"]} '
            f'channels={channels}')
        self._diag_rows_emitted = 0
        self._diag_rows_complete = 0
        self._diag_rows_incomplete = 0
        self._diag_max_count = 0

    def _init_fft_state(self, nchan=None):
        """FFT / scaler RMS acquisition state fed from assembled frames."""
        if nchan is None:
            nchan = self.NCHAN
        self.fft_processed_packets = 0
        self.fft_max_processing_time = 0
        self.fft_current_processing_time = 0
        self.fft_current_processed_packets = 0
        self.metrics_incomplete_frames = np.zeros(nchan, dtype=np.int32)
        self.metrics_non_complex_frames = np.zeros(nchan, dtype=np.int32)
        self.fft_lock = threading.RLock()
        self._fft_n_bins = 1
        self._logical_frame_parts = [[] for _ in range(nchan)]
        self._diag_last_log_time = 0.0
        self._diag_rows_emitted = 0
        self._diag_rows_complete = 0
        self._diag_rows_incomplete = 0
        self._diag_max_count = 0
        self._alloc_fft_rms_buffers(nchan, self._fft_n_bins)

    def _alloc_fft_rms_buffers(self, nchan, n_bins, preserve_acquisition=False):
        saved = None
        if preserve_acquisition and hasattr(self, 'fft_rms_started'):
            saved = dict(
                started=self.fft_rms_started.copy(),
                done=self.fft_rms_done.copy(),
                target_bank=self.fft_target_bank.copy(),
                n_frames=self.fft_n_frames.copy(),
                average=self.fft_rms_average.copy(),
                power_sum=self.fft_power_sum.copy(),
            )
        self._fft_n_bins = int(n_bins)
        self.fft_rms_started = np.zeros(nchan, dtype=bool)
        self.fft_rms_done = np.zeros(nchan, dtype=np.int8)
        self.fft_target_bank = np.zeros(nchan, dtype=np.int8)
        # Running sum of re^2+im^2 per bin (float32 for bandwidth / CPU efficiency)
        self.fft_power_sum = np.zeros((nchan, self._fft_n_bins), dtype=np.float32)
        self.fft_power_current = np.zeros((nchan, self._fft_n_bins), dtype=np.float32)
        self.fft_n_frames = np.zeros(nchan, dtype=np.int32)
        self.fft_rms_average = np.zeros(nchan, dtype=np.int32) + 100
        self.fft_rms = np.zeros((nchan, self._fft_n_bins), dtype=np.float32)
        self.fft_overflow = np.zeros((nchan, self._fft_n_bins), dtype=np.int32)
        self.fft_metrics_updated = np.zeros(nchan, dtype=np.int8)
        if saved is not None:
            n = min(nchan, saved['started'].shape[0])
            use_bins = min(self._fft_n_bins, saved['power_sum'].shape[1])
            self.fft_rms_started[:n] = saved['started'][:n]
            self.fft_rms_done[:n] = saved['done'][:n]
            self.fft_target_bank[:n] = saved['target_bank'][:n]
            self.fft_n_frames[:n] = saved['n_frames'][:n]
            self.fft_rms_average[:n] = saved['average'][:n]
            self.fft_power_sum[:n, :use_bins] = saved['power_sum'][:n, :use_bins]

    @staticmethod
    def _bin_power(row) -> np.ndarray:
        """Per-bin power vector (re^2+im^2) from one natural-order complex spectrum row."""
        row = np.asarray(row).reshape(-1)
        if not np.iscomplexobj(row):
            raise TypeError('FFT RMS acquisition expects complex spectra from the data formatter')
        re = row.real
        im = row.imag
        return re * re + im * im

    def _feed_logical_spectrum(self, cix, row):
        """Buffer split physical frames until a full logical spectrum is ready."""
        row = np.asarray(row).reshape(-1)
        if not np.iscomplexobj(row):
            self.metrics_non_complex_frames[cix] += 1
            return
        pfl = self.physical_frames_per_logical
        if pfl <= 1:
            self._accumulate_spectrum_power(cix, row)
            return
        parts = self._logical_frame_parts[cix]
        parts.append(row)
        if len(parts) < pfl:
            return
        combined = np.concatenate(parts, axis=-1)
        self._logical_frame_parts[cix] = []
        self._accumulate_spectrum_power(cix, combined)

    def _accumulate_spectrum_power(self, cix, row):
        """Add one logical spectrum to the running RMS acquisition for a channel."""
        power = self._bin_power(row)
        n_bins = power.size
        if n_bins != self._fft_n_bins:
            self._alloc_fft_rms_buffers(self.NCHAN, n_bins, preserve_acquisition=True)

        np.add(self.fft_power_sum[cix], power, out=self.fft_power_sum[cix])
        self.fft_power_current[cix] = power
        self.fft_metrics_updated[cix] = True

        if not self.fft_rms_started[cix] or self.fft_rms_done[cix]:
            return
        self.fft_n_frames[cix] += 1
        n = int(self.fft_n_frames[cix])
        target = int(self.fft_rms_average[cix])
        if n == 1 or n == target or (target > 10 and n % max(1, target // 10) == 0):
            sid = int(self.stream_id[cix])
            self.log.info(
                f'{self!r}: FFT RMS stream_id={sid} ch={cix}: '
                f'{n}/{target} logical spectra accumulated ({n_bins} bins)')
        if n != target:
            return
        self.fft_rms_done[cix] = True
        sid = int(self.stream_id[cix])
        self.log.info(
            f'{self!r}: FFT RMS stream_id={sid} ch={cix}: '
            f'completed {target} averages ({n_bins} bins)')
        np.sqrt(
            self.fft_power_sum[cix] / self.fft_n_frames[cix],
            out=self.fft_rms[cix],
        )
        self.fft_power_sum[cix] = 0

    def _process_fft_emitted(self, ts, data, counts, indices):
        """Update FFT RMS acquisition and metrics from one ``get_formatted_data`` emission."""
        n_out = ts.shape[0]
        packet_inc = int(counts[:n_out].sum())
        self.processed_packets += packet_inc
        self.fft_processed_packets += packet_inc
        self.fft_current_processed_packets += packet_inc

        exp = self.expected_packets
        if n_out:
            self._diag_rows_emitted += n_out * self.NCHAN
            if counts.size:
                self._diag_max_count = max(self._diag_max_count, int(counts[:n_out].max()))

        for tix in range(n_out):
            for cix in range(self.NCHAN):
                n_pkt = int(counts[tix, cix])
                if n_pkt < exp:
                    self.metrics_incomplete_frames[cix] += 1
                    self._diag_rows_incomplete += 1
                    continue
                self._diag_rows_complete += 1
                row = np.asarray(data[tix, cix]).reshape(-1)
                with self.fft_lock:
                    self._feed_logical_spectrum(cix, row)

        self._maybe_log_acquisition_progress()

    def _validate_fft_decoder_for_rms(self):
        """FFT RMS needs the raw assembly decoder (capture_format[1]) to be complex."""
        summary = self._capture_format_summary()
        if not summary.get('fmt_is_complex'):
            raise RuntimeError(
                f'{self!r}: FFT RMS requires complex capture_format[1] on the raw '
                f'assembly config (got {summary}). Configure the scaler capture mode '
                'on the FPGA (e.g. fft_2x) and push firmware config to raw_acq via '
                'fpga_master update-config before starting FFT RMS acquisition.'
            )
        return summary

    async def start_fft_rms_async(self, stream_ids, target_gain_bank, number_of_frames=100):
        """Start averaged RMS acquisition for the given stream IDs and scaler bank."""
        decoder = self._validate_fft_decoder_for_rms()
        ix = [self.sid_map[int(sid)] for sid in stream_ids if int(sid) in self.sid_map]
        if not ix:
            self.log.warning(
                f'{self!r}: start_fft_rms ignored stream_ids {stream_ids!r} '
                f'(known={list(self.sid_map.keys())})')
            return
        with self.fft_lock:
            self.fft_target_bank[ix] = target_gain_bank
            self.fft_rms_average[ix] = number_of_frames
            self.fft_n_frames[ix] = 0
            self.fft_power_sum[ix] = 0
            self.fft_overflow[ix] = 0
            self.fft_rms_done[ix] = False
            self.fft_rms_started[ix] = True
            for cix in ix:
                self._logical_frame_parts[cix] = []
        self.log.info(
            f'{self!r}: FFT RMS acquisition started for stream_ids={stream_ids!r} '
            f'number_of_frames={number_of_frames} target_gain_bank={target_gain_bank} '
            f'decoder={decoder} expected_packets={self.expected_packets}')
        self._diag_last_log_time = 0.0
        self._diag_rows_emitted = 0
        self._diag_rows_complete = 0
        self._diag_rows_incomplete = 0
        self._diag_max_count = 0

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
                fft_power_current = self.fft_power_current[ix]
                metrics_incomplete = self.metrics_incomplete_frames[ix]
                fft_scaler_overflows = self.fft_overflow[ix, 1:] if self.fft_overflow.shape[1] > 1 else 0

            crate, slot, chan = self.unpack_stream_id(stream_id)
            metrics.add(
                'raw_acq_fft_rms',
                value=float(np.sqrt(np.mean(fft_power_current))) if fft_power_current.size else 0.0,
                crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_packet_length_error',
                        value=metrics_incomplete, crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_non_complex_frames',
                        value=int(self.metrics_non_complex_frames[ix]),
                        crate=crate, slot=slot, chan=chan)
            metrics.add('raw_acq_fft_scaler_overflows',
                        value=np.sum(fft_scaler_overflows), crate=crate, slot=slot, chan=chan)
            time.sleep(0.0001)
            await asyncio.sleep(0)
            self.fft_metrics_updated[cix] = False

    def stop(self):
        pass
