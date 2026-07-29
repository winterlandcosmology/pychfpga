#!/usr/bin/python

"""Direct digital gain computation for the SCALER."""

import time
import traceback

import numpy as np

from wtl import log


DEFAULT_GAIN_TARGET = 1.5 * np.sqrt(2)
SCALER_REAL_GAIN_BITS = 16
SCALER_CORR_OUTPUT_COMPONENT_BITS = 4


def derive_target_shift(
        fpga,
        scaler_gain_bits=SCALER_REAL_GAIN_BITS,
        scaler_output_component_bits=SCALER_CORR_OUTPUT_COMPONENT_BITS):
    """Derive the SCALER binary-point shift for direct gain computation."""
    fft_info = fpga.FFT_INFO[int(fpga.FFT_TYPE)]
    fft_component_bits = int(fft_info['bits_per_bin']) // 2
    return int(fft_component_bits + scaler_gain_bits - scaler_output_component_bits)


class DirectGainCalc(object):
    """One-shot gain calculator using natural-order per-bin FFT RMS values.

    The SCALER floating-gain packing is intentionally not reproduced here.
    ``set_gains_async`` / ``SCALER.set_gain_table`` receives float gains as
    ``(gain_vector, -1)`` and performs the mantissa/exponent packing.
    """

    def __init__(
            self,
            channel_ids,
            stream_ids,
            target=DEFAULT_GAIN_TARGET,
            target_shift=44,
            n_bins=None):
        self.log = log.get_logger(self)
        self.channel_ids = channel_ids
        self.stream_ids = stream_ids
        self.stream_id_map = {sid: i for i, sid in enumerate(self.stream_ids)}
        self.nchan = len(self.channel_ids)
        self.target = float(target)
        target_shift = np.asarray(target_shift, dtype=np.int32)
        if target_shift.ndim == 0:
            target_shift = np.full(self.nchan, int(target_shift), dtype=np.int32)
        if target_shift.shape != (self.nchan,):
            raise ValueError(
                f'target_shift must be a scalar or one value per channel '
                f'({self.nchan}); got shape {target_shift.shape}')
        self.target_shift = target_shift
        self.target_full = self.target * np.power(2.0, self.target_shift)
        self.n_bins = int(n_bins) if n_bins is not None else None
        self.gains = None
        self.done = np.zeros((self.nchan), dtype=bool)
        if self.n_bins is not None:
            self._alloc_gain_buffers(self.n_bins)

    def _alloc_gain_buffers(self, n_bins):
        n_bins = int(n_bins)
        if self.n_bins is not None and n_bins != self.n_bins and self.done.any():
            raise ValueError(
                f'Cannot resize DirectGainCalc from {self.n_bins} to {n_bins} bins '
                'after channels are already complete')
        self.n_bins = n_bins
        self.gains = np.zeros((self.nchan, self.n_bins), dtype=np.float64)
        self.resolution_limited = np.zeros(
            (self.nchan, self.n_bins), dtype=bool)

    def _ensure_n_bins(self, rms):
        n_bins = int(np.asarray(rms).shape[-1])
        if self.gains is None:
            self._alloc_gain_buffers(n_bins)
        elif self.n_bins is None:
            if self.gains.shape[-1] != n_bins:
                raise ValueError(
                    f'RMS spectrum has {n_bins} bins but gain buffer has '
                    f'{self.gains.shape[-1]} bins')
            self.n_bins = n_bins
        elif n_bins != self.n_bins:
            raise ValueError(
                f'RMS spectrum has {n_bins} bins but DirectGainCalc '
                f'expects {self.n_bins} bins')
        return n_bins

    def __repr__(self):
        return f'{self.__class__.__name__}()'

    def get_gains(self, ix=None):
        """Return float linear gains for ``set_gains_async`` (glog=-1)."""
        if ix is None:
            ix = np.arange(self.nchan)
        return {
            tuple(self.channel_ids[i]): (self.gains[i].astype(np.float64), -1)
            for i in ix
        }

    def get_filtered_gains(self, ix=None):
        """Return exact float gains and the resolution-limited-bin mask.

        The method name is retained for the fpga_master call site, but this no
        longer filters or smooths the direct result. Masked gains are still
        returned and applied; the mask identifies bins whose RMS was below one
        full-resolution FFT count and therefore used the one-LSB floor.
        """
        if ix is None:
            ix = np.arange(self.nchan)
        ix_done = ix[self.done[ix]]
        mask = self.resolution_limited[ix_done].copy()
        return self.get_gains(ix_done), mask

    def update_gains(self, stream_ids, rms):
        """Compute final float gains from one per-bin RMS spectrum per channel.

        Full-resolution capture is taken before the SCALER gain. An RMS below
        one FFT count is therefore below the useful capture resolution and
        cannot be recovered by increasing the downstream gain. Such values use
        one count for the division so that the first observable FFT count maps
        to the requested target without producing an excessive gain.
        """
        try:
            t1 = time.time()
            stream_ids = np.asarray(stream_ids)
            ix = np.array(
                [i for i, sid in enumerate(stream_ids) if sid in self.stream_id_map],
                dtype=np.int16)
            bix = np.array(
                [self.stream_id_map[sid] for sid in stream_ids[ix]],
                dtype=np.int16)
            ix_not_done = ~self.done[bix]
            ix = ix[ix_not_done]
            bix = bix[ix_not_done]
            if bix.size == 0:
                return {}

            rms = np.asarray(rms, dtype=np.float64)
            if rms.ndim == 1:
                rms = rms[np.newaxis, :]
            rms_selected = rms[ix]
            bad = ~np.isfinite(rms_selected) | (rms_selected < 0)
            if np.any(bad):
                bad_rows, bad_bins = np.where(bad)
                examples = [
                    (int(stream_ids[ix[row]]), int(bin_))
                    for row, bin_ in zip(bad_rows[:8], bad_bins[:8])
                ]
                raise ValueError(
                    'RMS spectra contain negative or non-finite '
                    f'values; example (stream_id, bin) entries: {examples}')

            self._ensure_n_bins(rms)
            resolution_limited = rms_selected < 1.0
            rms_for_gain = rms_selected.copy()
            rms_for_gain[resolution_limited] = 1.0
            calculated_gains = (
                self.target_full[bix, np.newaxis] / rms_for_gain)

            self.log.info(
                f'{self!r}: Received RMS data from '
                f'{rms.shape[0]} channels. Processing {bix.size} of those.')
            for row, bins in enumerate(resolution_limited):
                limited_bins = np.flatnonzero(bins)
                if limited_bins.size:
                    self.log.warning(
                        f'{self!r}: Stream ID {int(stream_ids[ix[row]])} has '
                        f'{limited_bins.size} RMS bins below one FFT count; '
                        f'using the one-LSB gain floor. Example bins: '
                        f'{limited_bins[:8].tolist()}')
            self.gains[bix] = calculated_gains
            self.resolution_limited[bix] = resolution_limited
            self.done[bix] = True
            t2 = time.time()
            self.log.info(
                f'{self!r}: Direct gain update time: {(t2 - t1) * 1000:0.3f} ms '
                f'for {bix.size} channels')
            return self.get_gains(bix)
        except Exception as e:
            self.log.error(f'{self!r}: Exception:\n{e!r}')
            traceback.print_exc()
            raise

    def is_done(self):
        return all(self.done)

    def percent_done(self):
        return float(sum(self.done)) / self.done.size * 100.0
