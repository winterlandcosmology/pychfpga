#!/usr/bin/python

"""Direct digital gain computation for the SCALER."""

import time
import traceback
from pathlib import Path

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
            n_bins=None,
            debug_plot_dir=None):
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
        self.debug_plot_dir = (
            Path(debug_plot_dir).expanduser() if debug_plot_dir is not None else None)
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
        """Return exact float gains and an all-False mask.

        The method name is retained for the fpga_master call site, but this no
        longer filters, smooths, masks, clips, or sanitizes the direct result.
        """
        if ix is None:
            ix = np.arange(self.nchan)
        ix_done = ix[self.done[ix]]
        mask = np.zeros((ix_done.size, self.n_bins), dtype=bool)
        return self.get_gains(ix_done), mask

    def _save_debug_plot(self, stream_ids, rms, gains):
        # TEMP DEBUG: save direct-gain inputs/results for the corr16 startup failure.
        # Remove this method and debug_plot_dir plumbing after the zero-bin issue is resolved.
        if self.debug_plot_dir is None:
            return
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt

            self.debug_plot_dir.mkdir(parents=True, exist_ok=True)
            stream_ids = np.asarray(stream_ids)
            rms = np.asarray(rms, dtype=np.float64)
            gains = np.asarray(gains, dtype=np.float64)
            bins = np.arange(rms.shape[-1])

            fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
            for row, sid in enumerate(stream_ids[:2]):
                axes[0].plot(
                    bins, np.where(rms[row] > 0, rms[row], np.nan),
                    linewidth=0.8, alpha=0.65, label=f'stream {int(sid)}')
                axes[1].plot(
                    bins, np.where(np.isfinite(gains[row]) & (gains[row] > 0), gains[row], np.nan),
                    linewidth=0.8, alpha=0.65)

            bad_rows, bad_bins = np.where(~np.isfinite(rms) | (rms <= 0))
            if bad_bins.size:
                positive_rms = rms[np.isfinite(rms) & (rms > 0)]
                finite_gains = gains[np.isfinite(gains) & (gains > 0)]
                rms_marker_y = float(np.min(positive_rms)) * 0.5 if positive_rms.size else 1.0
                gain_marker_y = float(np.max(finite_gains)) if finite_gains.size else 1.0
                axes[0].scatter(
                    bad_bins, np.full(bad_bins.size, rms_marker_y),
                    marker='x', color='red', s=28, label='zero/invalid RMS')
                axes[1].scatter(
                    bad_bins, np.full(bad_bins.size, gain_marker_y),
                    marker='x', color='red', s=28, label='infinite/invalid gain')

            axes[0].set_yscale('log')
            axes[1].set_yscale('log')
            axes[0].set_ylabel('Accumulated FFT RMS')
            axes[1].set_ylabel('Calculated full gain')
            axes[1].set_xlabel('Natural-order FFT bin')
            axes[0].grid(True, which='both', alpha=0.25)
            axes[1].grid(True, which='both', alpha=0.25)
            axes[0].legend(loc='best', fontsize='small', ncol=2)
            if bad_bins.size:
                axes[1].legend(loc='best', fontsize='small')
            fig.suptitle(
                'TEMP DEBUG: direct gain calculation input and result '
                f'({rms.shape[0]} streams, {rms.shape[1]} bins)')
            fig.tight_layout()

            sid_text = '-'.join(str(int(sid)) for sid in stream_ids)
            filename = self.debug_plot_dir / f'direct_gain_debug_streams_{sid_text}.png'
            fig.savefig(filename, dpi=150)
            plt.close(fig)
            self.log.warning(f'{self!r}: TEMP DEBUG plot saved to {filename}')
        except Exception:
            self.log.exception(f'{self!r}: Failed to save TEMP DEBUG direct-gain plot')

    def update_gains(self, stream_ids, rms):
        """Compute final float gains from one per-bin RMS spectrum per channel."""
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
            self._ensure_n_bins(rms)
            rms_selected = rms[ix]
            with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
                calculated_gains = self.target_full[bix, np.newaxis] / rms_selected
            self._save_debug_plot(stream_ids[ix], rms_selected, calculated_gains)
            bad = ~np.isfinite(rms_selected) | (rms_selected <= 0)
            if np.any(bad):
                bad_rows, bad_bins = np.where(bad)
                examples = [
                    (int(stream_ids[ix[row]]), int(bin_))
                    for row, bin_ in zip(bad_rows[:8], bad_bins[:8])
                ]
                raise ValueError(
                    'RMS spectra contain zero, negative, or non-finite '
                    f'values; example (stream_id, bin) entries: {examples}')

            self.log.info(
                f'{self!r}: Received RMS data from '
                f'{rms.shape[0]} channels. Processing {bix.size} of those.')
            self.gains[bix] = calculated_gains
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
