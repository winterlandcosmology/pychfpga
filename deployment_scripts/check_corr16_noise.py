#!/usr/bin/env python3
"""Check a corr16 run whose 16 inputs were all fed by the FPGA function generator's noise.

Usage: check_corr16_noise.py DATA_DIR [--run RUN_NAME]

DATA_DIR is the folder mounted at /data (PYCHFPGA_DATA_DIR), e.g.
/storage/crate012/corr16_noise_docker. The run defaults to the one `current` points to.
Prints one PASS/FAIL line per check with the measured values; exits 1 if any check fails.

What the noise input predicts:
- The noise generator sends the same uniform noise to all 16 inputs, so the digital gains
  are equal across inputs and flat across frequency, and every pair of inputs is fully
  correlated with zero phase.
- The gains set the RMS of each 4-bit complex output sample to TARGET_RMS
  (DirectGainCalc in pychfpga/calculate_gains.py), so each autocorrelation, per frame,
  is about TARGET_RMS**2 = 4.5 in every bin.
- The raw_acq correlator files hold raw sums over FRAMES_PER_ROW frames; a row is complete
  when `counts` is PACKETS_PER_ROW (no packet lost). Bins 0 and 512 carry the correlator
  readout and are excluded.
"""

import argparse
import glob
import os
import sys

import h5py
import numpy as np

N_INPUTS = 16
N_BINS = 1024
BAD_BINS = (0, 512)                    # correlator readout words
TARGET_RMS = 2.1213203436              # compute_gains.target
GAIN_SHIFT = 30                        # DirectGainCalc target_shift on ICE
FIRMWARE_FRAMES = 16384                # firmware_correlator.firmware_integration_period
SOFTWARE_INTEGRATIONS = 128            # firmware_correlator.software_integration_period
FRAMES_PER_ROW = FIRMWARE_FRAMES * SOFTWARE_INTEGRATIONS
PACKETS_PER_ROW = N_BINS * SOFTWARE_INTEGRATIONS

# Acceptance limits (first estimates; the report prints the measured values)
GAIN_SPREAD_MAX = 0.10                 # median per-bin (max-min)/median across inputs
FLATNESS_MAX = 0.15                    # std/mean across bins
RMS_ATOL = 0.35                        # as firmware_tests/test_gain.py
MIN_COMPLETE_FRACTION = 0.90
MIN_COMPLETE_ROWS = 40                 # about 5 minutes at 5.37 s per row
CTIME_TOL = 0.1                        # s
MIN_COHERENCE = 0.95
MAX_PHASE_DEG = 5.0


def valid_bins():
    mask = np.ones(N_BINS, dtype=bool)
    mask[list(BAD_BINS)] = False
    return mask


def auto_indices(n=N_INPUTS):
    return [i * n - i * (i - 1) // 2 for i in range(n)]


def check_gains(gain_coeff, chan_ids):
    """Check the last gain update. `gain_coeff` is [freq, input] over all inputs of the archive."""
    results = []
    used = np.flatnonzero(np.any(gain_coeff != 0, axis=0))
    g = np.abs(gain_coeff[:, used]).astype(np.float64)
    bins = valid_bins()
    ok = len(used) == N_INPUTS and np.all(np.isfinite(g)) and np.all(g[bins] > 0)
    results.append(('G1 gains present', ok,
                    f'{len(used)} inputs with gains (chan_id {chan_ids[used].min() if len(used) else "-"}'
                    f'..{chan_ids[used].max() if len(used) else "-"}), all finite and non-zero: {ok}'))
    if len(used) != N_INPUTS:
        return results

    gv = g[bins]
    spread = (gv.max(axis=1) - gv.min(axis=1)) / np.median(gv, axis=1)
    results.append(('G2 gains equal across inputs', np.median(spread) < GAIN_SPREAD_MAX,
                    f'median per-bin spread {np.median(spread):.1%} (limit {GAIN_SPREAD_MAX:.0%}), '
                    f'worst bin {spread.max():.1%}'))

    implied_rms = TARGET_RMS * 2**GAIN_SHIFT / np.median(gv, axis=1)
    flatness = implied_rms.std() / implied_rms.mean()
    results.append(('G3 gains flat across frequency', flatness < FLATNESS_MAX,
                    f'implied FFT RMS median {np.median(implied_rms):.1f}, std/mean {flatness:.1%} '
                    f'(limit {FLATNESS_MAX:.0%})'))
    return results


def check_corr(vis, counts, integ_number, ctime, prod):
    """Check the correlator rows. `vis` is [row, freq, prod] raw sums."""
    results = []
    expected_prod = [(a, b) for a in range(N_INPUTS) for b in range(a, N_INPUTS)]
    if [tuple(p) for p in prod] != expected_prod:
        results.append(('D0 product order', False, 'index_map/prod is not the 16-input upper triangle'))
        return results

    complete = counts == PACKETS_PER_ROW
    n_complete = int(complete.sum())
    contiguous = bool(np.all(np.diff(integ_number) == 1))
    spacing = np.diff(ctime)
    spacing_ok = bool(len(spacing) and np.all(np.abs(spacing - np.median(spacing)) < CTIME_TOL))
    ok = (n_complete >= MIN_COMPLETE_ROWS and n_complete / len(counts) >= MIN_COMPLETE_FRACTION
          and contiguous and spacing_ok)
    results.append(('D1 rows complete and regular', ok,
                    f'{n_complete}/{len(counts)} rows complete (need >= {MIN_COMPLETE_ROWS} and '
                    f'{MIN_COMPLETE_FRACTION:.0%}); integ_number contiguous: {contiguous}; spacing '
                    f'{np.median(spacing) if len(spacing) else float("nan"):.2f} s, regular: {spacing_ok}'))
    if not n_complete:
        return results

    per_frame = vis[complete].sum(axis=0) / (n_complete * FRAMES_PER_ROW)   # [freq, prod]
    bins = valid_bins()
    autos = per_frame[bins][:, auto_indices()]                             # [freq, input]

    level = np.sqrt(np.median(autos.real, axis=0))
    ok = bool(np.all(np.abs(level - TARGET_RMS) <= RMS_ATOL))
    results.append(('D2 auto level matches the gain target', ok,
                    f'RMS per input {level.min():.3f}..{level.max():.3f}, target {TARGET_RMS:.4f} '
                    f'+/- {RMS_ATOL}'))

    flat = autos.real.std(axis=0) / autos.real.mean(axis=0)
    max_imag = float(np.abs(vis[complete][:, :, auto_indices()].imag).max())
    results.append(('D3 autos flat and real', bool(flat.max() < FLATNESS_MAX and max_imag == 0),
                    f'worst std/mean {flat.max():.1%} (limit {FLATNESS_MAX:.0%}), '
                    f'max |imag| of autos {max_imag:g}'))

    a_idx = auto_indices()
    pairs, coherence, phase = [], [], []
    for k, (a, b) in enumerate(expected_prod):
        if a == b:
            continue
        v = per_frame[bins, k]
        norm = np.sqrt(per_frame[bins, a_idx[a]].real * per_frame[bins, a_idx[b]].real)
        pairs.append((a, b))
        coherence.append(np.median(np.abs(v) / norm))
        phase.append(np.degrees(np.abs(np.median(np.angle(v)))))
    i_coh, i_ph = int(np.argmin(coherence)), int(np.argmax(phase))
    ok = coherence[i_coh] > MIN_COHERENCE and phase[i_ph] < MAX_PHASE_DEG
    results.append(('D4 all pairs fully correlated', ok,
                    f'lowest median coherence {coherence[i_coh]:.4f} (pair {pairs[i_coh]}, limit '
                    f'{MIN_COHERENCE}); largest median |phase| {phase[i_ph]:.2f} deg (pair '
                    f'{pairs[i_ph]}, limit {MAX_PHASE_DEG})'))

    return results


def load_gains(gain_dir):
    files = sorted(glob.glob(os.path.join(gain_dir, '*.h5')))
    if not files:
        raise FileNotFoundError(f'No gain file in {gain_dir}')
    with h5py.File(files[-1], 'r') as f:
        return f['gain_coeff'][-1], f['index_map/input']['chan_id'][:], files[-1]


def load_corr(corr_dir):
    files = sorted(p for p in glob.glob(os.path.join(corr_dir, '*', '*')) if not p.endswith('.lock'))
    if not files:
        raise FileNotFoundError(f'No correlator file in {corr_dir}')
    locked = sorted(glob.glob(os.path.join(corr_dir, '*', '*.lock')))
    vis, counts, times, prod = [], [], [], None
    for path in files:
        with h5py.File(path, 'r') as f:
            vis.append(f['vis'][:])
            counts.append(f['counts'][:])
            times.append(f['index_map/time'][:])
            prod = f['index_map/prod'][:]
    t = np.concatenate(times)
    return (np.concatenate(vis), np.concatenate(counts), t['integ_number'], t['ctime'],
            [(int(p[0]), int(p[1])) for p in prod], files, locked)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('data_dir', help='folder mounted at /data, e.g. /storage/crate012/corr16_noise_docker')
    parser.add_argument('--run', help='run folder name (default: the one `current` points to)')
    args = parser.parse_args(argv)

    run_name = args.run or os.path.basename(os.path.realpath(os.path.join(args.data_dir, 'current')))
    run_dir = os.path.join(args.data_dir, run_name)
    gain_coeff, chan_ids, gain_file = load_gains(
        os.path.join(args.data_dir, 'digital_gains', f'{run_name}_digitalgain'))
    vis, counts, integ, ctime, prod, corr_files, locked = load_corr(os.path.join(run_dir, 'corr'))

    print(f'Run:          {run_dir}')
    print(f'Gain file:    {gain_file}')
    print(f'Corr files:   {len(corr_files)}, {len(counts)} rows')
    if locked:
        print(f'Note: {len(locked)} .lock file(s) left: the last file may not have been closed cleanly')
    results = check_gains(gain_coeff, chan_ids) + check_corr(vis, counts, integ, ctime, prod)
    print()
    for name, ok, detail in results:
        print(f'{"PASS" if ok else "FAIL"}  {name}: {detail}')
    failed = [name for name, ok, _ in results if not ok]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
