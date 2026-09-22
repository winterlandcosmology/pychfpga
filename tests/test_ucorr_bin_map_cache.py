"""UCORR bin_map / prod_map construction: vectorized output and class-level cache."""

import sys
import types

import numpy as np
import pytest

matplotlib_stub = types.ModuleType("matplotlib")
matplotlib_pyplot_stub = types.ModuleType("matplotlib.pyplot")
matplotlib_stub.pyplot = matplotlib_pyplot_stub
sys.modules.setdefault("matplotlib", matplotlib_stub)
sys.modules.setdefault("matplotlib.pyplot", matplotlib_pyplot_stub)

from pychfpga.common.state import State
from pychfpga.fpga_firmware.chfpga.x_engine.UCORR import UCORR


def _legacy_compute(ct_level, NDECIM, NCHAN, NCLK, fft_bin_map):
    """Original nested-comprehension map builder, for value comparison."""
    NPROD = NCHAN * (NCHAN + 1) // 2
    NPROD_PER_BINCLK = NPROD // NCLK
    ij_to_pix_map = UCORR.get_ij_to_prod_map(NCHAN)
    cmac_out_ij = np.array(
        [
            [
                (i, (j + i) % NCHAN) if i < NCHAN - j else ((j + i) % NCHAN, i - 1)
                for i in range(NCHAN + 1)
                for j in range(NCHAN // 2 // NCLK * clk, NCHAN // 2 // NCLK * (clk + 1))
            ]
            for clk in range(NCLK)
        ]
    )
    ct_bin_map = np.asarray(fft_bin_map)
    chan_map = np.arange(NCHAN)
    if ct_level >= 2:
        ct_bin_map = np.array(
            [
                [
                    b
                    for clk, b in enumerate(lane_bins)
                    if not (clk < 256 and clk & 0b11 == lane)
                ]
                for lane, lane_bins in enumerate(ct_bin_map)
            ]
        )
    if ct_level >= 3:
        s = ct_bin_map.shape
        ct_bin_map = (
            ct_bin_map.reshape(s[0], 2, -1, order="F")
            .transpose((1, 0, 2))
            .reshape(s[0] * 2, -1, order="C")
        )
        chan_map = np.array(
            [
                i + NCHAN // 4 * clk + NCHAN // 2 * lane
                for clk in range(2)
                for lane in range(2)
                for i in range(NCHAN // 4)
            ],
            dtype=np.intp,
        )
    corr_bins = np.array([ct_bin_map[:, bs::NDECIM] for bs in range(NDECIM)])
    bin_map = np.array(
        [
            [
                [[bin_ for prod in range(NPROD_PER_BINCLK)] for bin_ in bins for clk in range(NCLK)]
                for bins in bin_set_map
            ]
            for bin_set_map in corr_bins
        ]
    )
    cmac_out_pix = ij_to_pix_map[
        chan_map[cmac_out_ij[..., 0]], chan_map[cmac_out_ij[..., 1]]
    ]
    prod_map = np.array(
        [
            [
                [cmac_out_pix[clk, clkprod] for clkprod in range(NPROD_PER_BINCLK)]
                for _ in lane_bins
                for clk in range(NCLK)
            ]
            for lane_bins in corr_bins[0]
        ]
    )
    return bin_map, prod_map


def _base_config(**kwargs):
    params = dict(
        CT_LEVEL=1,
        NDECIM=1,
        NCHAN=4,
        NCLK=1,
        fft_bin_map=np.arange(8, dtype=np.intp).reshape(2, 4),
    )
    params.update(kwargs)
    return State(**params)


@pytest.fixture(autouse=True)
def _clear_ucorr_map_cache():
    UCORR.clear_map_cache()
    yield
    UCORR.clear_map_cache()


def test_vectorized_maps_match_legacy_ct1():
    fft = np.arange(8, dtype=np.intp).reshape(2, 4)
    got = UCORR._compute_bin_prod_maps(1, 1, 4, 1, fft)
    legacy_bin, legacy_prod = _legacy_compute(1, 1, 4, 1, fft)
    assert np.array_equal(got["bin_map"], legacy_bin)
    assert np.array_equal(got["prod_map"], legacy_prod)


def test_vectorized_maps_match_legacy_ct2_small():
    # 4 lanes x 8 clks: cull drops clk where clk<256 and clk&3==lane.
    fft = np.arange(32, dtype=np.intp).reshape(4, 8)
    got = UCORR._compute_bin_prod_maps(2, 1, 4, 1, fft)
    legacy_bin, legacy_prod = _legacy_compute(2, 1, 4, 1, fft)
    assert np.array_equal(got["bin_map"], legacy_bin)
    assert np.array_equal(got["prod_map"], legacy_prod)


def test_vectorized_maps_match_legacy_ct3_small():
    # 16 clks -> 12 kept per lane after CT2; CT3 yields (8, 6), divisible by NDECIM=2.
    fft = np.arange(64, dtype=np.intp).reshape(4, 16)
    got = UCORR._compute_bin_prod_maps(3, 2, 4, 1, fft)
    legacy_bin, legacy_prod = _legacy_compute(3, 2, 4, 1, fft)
    assert np.array_equal(got["bin_map"], legacy_bin)
    assert np.array_equal(got["prod_map"], legacy_prod)


def test_class_cache_shares_arrays_across_configs():
    fft = np.arange(8, dtype=np.intp).reshape(2, 4)
    a = _base_config(fft_bin_map=fft)
    b = _base_config(fft_bin_map=fft.copy())
    UCORR._update_bin_maps(a)
    UCORR._update_bin_maps(b)
    assert a.bin_map is b.bin_map
    assert a.prod_map is b.prod_map
    assert len(UCORR._MAP_CACHE) == 1


def test_class_cache_miss_on_different_fft_map():
    a = _base_config(fft_bin_map=np.arange(8, dtype=np.intp).reshape(2, 4))
    b = _base_config(fft_bin_map=np.arange(8, dtype=np.intp)[::-1].reshape(2, 4))
    UCORR._update_bin_maps(a)
    UCORR._update_bin_maps(b)
    assert a.bin_map is not b.bin_map
    assert not np.array_equal(a.bin_map, b.bin_map)
    assert len(UCORR._MAP_CACHE) == 2


def test_same_config_second_call_does_not_rebuild():
    cfg = _base_config()
    UCORR._update_bin_maps(cfg)
    first = cfg.bin_map
    UCORR.clear_map_cache()
    UCORR._update_bin_maps(cfg)
    assert cfg.bin_map is first
    assert UCORR._MAP_CACHE == {}


def test_autocorr_only_slices_last_axis_without_touching_cache():
    """Diagonal CMACs become NCMAC; the cached full triangle stays intact."""
    NCHAN, NCLK = 8, 1
    fft = np.arange(16, dtype=np.intp).reshape(2, 8)
    maps = UCORR.compute_bin_prod_maps(1, 1, NCHAN, NCLK, fft)
    cached = next(iter(UCORR._MAP_CACHE.values()))
    full_ncmac = maps["NCMAC"]
    assert full_ncmac == NCHAN * (NCHAN + 1) // 2 // NCLK
    assert cached["bin_map"] is maps["bin_map"]

    sliced = UCORR.slice_autocorr_maps(maps, NCHAN, NCLK)
    assert sliced["NCMAC"] == NCHAN
    assert sliced["bin_map"].shape[-1] == NCHAN
    assert sliced["prod_map"].shape[-1] == NCHAN
    assert sliced["NPROD_PER_ARRAY"] == maps["NPROD_PER_ARRAY"]
    assert maps["NCMAC"] == full_ncmac
    assert maps["bin_map"].shape[-1] == full_ncmac
    assert cached["bin_map"].shape[-1] == full_ncmac
    assert cached["bin_map"] is maps["bin_map"]

    diagonal = UCORR.get_ij_to_prod_map(NCHAN)[np.arange(NCHAN), np.arange(NCHAN)]
    assert set(np.unique(sliced["prod_map"])) == set(diagonal)


def test_corr64_map_shapes():
    fft = np.arange(8192, dtype=np.intp).reshape(4, 2048)
    maps = UCORR._compute_bin_prod_maps(3, 8, 64, 16, fft)
    assert maps["bin_map"].shape == (8, 8, 1984, 130)
    assert maps["prod_map"].shape == (8, 1984, 130)
    assert maps["NDECIM"] == 8
    assert maps["NCORRS"] == 8
    assert maps["NPROD_PER_CMAC"] == 1984
    assert maps["NCMAC"] == 130
    assert maps["NPROD_PER_ARRAY"] == 8 * 1984
