import sys
from pathlib import Path

import h5py
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deployment_scripts'))

import check_corr16_noise as cn  # noqa: E402

N_ROWS = 50
FIRST_CHAN_ID = 128          # slot 9 of the 144-input gain table
RUN_NAME = '20261119T000000Z_TEST_NOISE'


def ideal_vis(level=cn.TARGET_RMS):
    """Raw-sum visibilities of identical inputs whose per-frame power is `level`**2 in every bin."""
    per_frame = np.full((cn.N_BINS, 136), level**2, dtype=np.complex128)
    return np.repeat((per_frame * cn.FRAMES_PER_ROW)[np.newaxis], N_ROWS, axis=0)


def write_run(data_dir, vis=None, counts=None, gains=None):
    """Write a gain archive and a correlator file with the layout fpga_master/raw_acq produce."""
    vis = ideal_vis() if vis is None else vis
    counts = np.full(len(vis), cn.PACKETS_PER_ROW, dtype=np.uint32) if counts is None else counts
    if gains is None:
        gains = np.zeros((cn.N_BINS, 144), dtype=np.complex64)
        gains[:, FIRST_CHAN_ID:FIRST_CHAN_ID + cn.N_INPUTS] = 0.02 * 2**cn.GAIN_SHIFT

    gain_dir = data_dir / 'digital_gains' / f'{RUN_NAME}_digitalgain'
    gain_dir.mkdir(parents=True)
    with h5py.File(gain_dir / '00000001.h5', 'w') as f:
        f['gain_coeff'] = gains[np.newaxis]
        f['index_map/input'] = np.array([(i, b'X') for i in range(144)],
                                        dtype=[('chan_id', '<u2'), ('correlator_input', 'S32')])

    corr_dir = data_dir / RUN_NAME / 'corr' / '20261119T000100Z'
    corr_dir.mkdir(parents=True)
    t = np.zeros(len(vis), dtype=[('integ_number', '<u4'), ('fpga_count', '<u8'),
                                  ('irigb_time', '<u8'), ('ctime', '<f8')])
    t['integ_number'] = np.arange(1, len(vis) + 1)
    t['ctime'] = 1.8e9 + 5.37 * np.arange(len(vis))
    prod = np.array([(a, b) for a in range(16) for b in range(a, 16)],
                    dtype=[('input_a', '<u2'), ('input_b', '<u2')])
    with h5py.File(corr_dir / '0000', 'w') as f:
        f['vis'] = vis
        f['counts'] = counts
        f['index_map/time'] = t
        f['index_map/prod'] = prod
    (data_dir / 'current').symlink_to(RUN_NAME)
    return data_dir


def results_by_name(data_dir, capsys):
    rc = cn.main([str(data_dir)])
    out = capsys.readouterr().out
    status = {line.split(':')[0][6:]: line[:4] for line in out.splitlines()
              if line.startswith(('PASS', 'FAIL'))}
    return rc, status


def test_ideal_noise_run_passes(tmp_path, capsys):
    rc, status = results_by_name(write_run(tmp_path), capsys)
    assert rc == 0
    assert set(status.values()) == {'PASS'}
    assert len(status) == 7


def test_incomplete_rows_fail_d1(tmp_path, capsys):
    counts = np.full(N_ROWS, cn.PACKETS_PER_ROW, dtype=np.uint32)
    counts[:10] -= 1
    rc, status = results_by_name(write_run(tmp_path, counts=counts), capsys)
    assert rc == 1
    assert status['D1 rows complete and regular'] == 'FAIL'
    assert status['D2 auto level matches the gain target'] == 'PASS'


def test_decorrelated_pair_fails_d4(tmp_path, capsys):
    vis = ideal_vis()
    vis[:, :, 1] *= 0.3          # pair (0, 1)
    rc, status = results_by_name(write_run(tmp_path, vis=vis), capsys)
    assert rc == 1
    assert status['D4 all pairs fully correlated'] == 'FAIL'
    assert status['D2 auto level matches the gain target'] == 'PASS'


def test_wrong_auto_level_fails_d2(tmp_path, capsys):
    rc, status = results_by_name(write_run(tmp_path, vis=ideal_vis(level=1.4)), capsys)
    assert rc == 1
    assert status['D2 auto level matches the gain target'] == 'FAIL'


def test_unequal_gains_fail_g2(tmp_path, capsys):
    gains = np.zeros((cn.N_BINS, 144), dtype=np.complex64)
    gains[:, FIRST_CHAN_ID:FIRST_CHAN_ID + cn.N_INPUTS] = 0.02 * 2**cn.GAIN_SHIFT
    gains[:, FIRST_CHAN_ID] *= 1.5
    rc, status = results_by_name(write_run(tmp_path, gains=gains), capsys)
    assert rc == 1
    assert status['G2 gains equal across inputs'] == 'FAIL'
    assert status['G3 gains flat across frequency'] == 'PASS'


@pytest.mark.parametrize('bad_bin', cn.BAD_BINS)
def test_readout_bins_are_ignored(tmp_path, capsys, bad_bin):
    vis = ideal_vis()
    vis[:, bad_bin, :] = 0
    gains = np.zeros((cn.N_BINS, 144), dtype=np.complex64)
    gains[:, FIRST_CHAN_ID:FIRST_CHAN_ID + cn.N_INPUTS] = 0.02 * 2**cn.GAIN_SHIFT
    gains[bad_bin, FIRST_CHAN_ID:FIRST_CHAN_ID + cn.N_INPUTS] = 0
    rc, status = results_by_name(write_run(tmp_path, vis=vis, gains=gains), capsys)
    assert rc == 0, status
