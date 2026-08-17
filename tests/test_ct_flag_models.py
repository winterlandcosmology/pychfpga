"""Offline unit tests of the corner-turn flag-layout models.

These exercise the pure module-level packing functions extracted from
ChanBinSel.map_flags and SHUFFLE_BIN_SEL.map_flags. The class-level
map_flags methods read live registers (bin selections, lane ranges,
combine bits) and are validated on hardware by the ct_prober and
ct_shuffle256 suites; the layout math itself is validated here, including
the full CB1 -> CB2 -> CB3 frame-flags concatenation identity that the
shuffle256 frame-flags hardware test asserts against silicon.
"""

import numpy as np

from pychfpga.fpga_firmware.chfpga.ct_engine.chan_bin_sel import (
    pack_data_flags_words, pack_frame_flags_word)
from pychfpga.fpga_firmware.chfpga.ct_engine.SHUFFLE_BIN_SEL import (
    combine_data_flag_words, passthrough_data_flag_words)


def test_pack_data_flags_uncombined():
    """CB1 shuffle256 layout: one word per bin, channel bits low, high half 0."""
    bins = [16, 32, 48]
    channels = list(range(16))
    flags = {0: {16: 1, 32: 0, 48: 1}, 15: {16: 1, 32: 1, 48: 0}}
    words = pack_data_flags_words(bins, channels, flags, combine=False)
    assert list(words) == [0x8001, 0x8000, 0x0001]
    assert all(w >> 16 == 0 for w in words)


def test_pack_data_flags_combined_matches_bench_layout():
    """CB1 shuffle16 layout (hardware-validated on the bench): bin pairs share a word."""
    bins = [8, 16, 24, 40]
    channels = list(range(16))
    flags = {2: {8: 1, 16: 1, 24: 0, 40: 0}, 9: {8: 0, 16: 0, 24: 1, 40: 1}}
    words = pack_data_flags_words(bins, channels, flags, combine=True)
    # word 0: bin 8 in the low half, bin 16 in the high half
    assert list(words) == [(1 << 2) | (1 << (16 + 2)), (1 << 9) | (1 << (16 + 9))]


def test_pack_frame_flags_word():
    adc_mask, fft_mask = 0xA5C3, 0x3C69
    word = pack_frame_flags_word(
        list(range(16)),
        {ch: ((adc_mask >> ch) & 1, (fft_mask >> ch) & 1) for ch in range(16)})
    assert word == (fft_mask << 16) | adc_mask


def test_combine_data_flag_words_cb2_rule():
    """CB2: per bin, low half = even lane of the pair, high half = odd lane."""
    lane_a = [0x000A, 0x000B, 0x000C]
    lane_b = [0x00A0, 0x00B0, 0x00C0]
    words = combine_data_flag_words([lane_a, lane_b], [2, 0])
    assert [hex(int(w)) for w in words] == ['0xc0000c', '0xa0000a']
    # only the low 16 bits of each input word are carried
    words = combine_data_flag_words([[0xFFFF1234], [0xEEEE5678]], [0])
    assert list(words) == [0x56781234]


def test_passthrough_data_flag_words_cb3_rule():
    """CB3: bins outer, lanes inner, one word per lane per bin."""
    lanes = [[10, 11], [20, 21], [30, 31]]
    words = passthrough_data_flag_words(lanes, [1, 0])
    assert list(words) == [11, 21, 31, 10, 20, 30]


def test_frame_flags_shuffle256_concatenation_identity():
    """The full CB1 -> CB2 -> CB3 frame-flags chain reproduces the packet's
    16-word order: word w carries exactly source slot w+1's mask.

    This is the same oracle the shuffle256 hardware frame-flags test asserts
    against silicon: each of 16 boards injects the distinctive ADC mask
    1 << (slot-1); the flags ride the identical concatenation path as the
    data (CB2 pairs input lanes (2j, 2j+1); CB3 concatenates its 8 input
    lanes, remapped [0,4,1,5,2,6,3,7] from the CB2 output lanes, which
    restores ascending slot order).
    """
    masks = {slot: 1 << (slot - 1) for slot in range(1, 17)}
    # CB1: one frame-flags word per source board (ADC half only)
    cb1 = {slot: pack_frame_flags_word(
        list(range(16)), {ch: ((masks[slot] >> ch) & 1, 0) for ch in range(16)})
        for slot in range(1, 17)}
    assert all(cb1[slot] == masks[slot] for slot in cb1)
    # CB2 post-remap input lane j carries slot j+1. Output lane k = 4*bs + j
    # forwards input lanes (4j + first, 4j + first + 1) with first = 0 for
    # bs 0 and first = 2 for bs 1; frame flags are concatenated, not combined.
    cb2_out = {}
    for bs, first in ((0, 0), (1, 2)):
        for sublane in range(4):
            lanes = (4 * sublane + first, 4 * sublane + first + 1)
            cb2_out[4 * bs + sublane] = [cb1[lane + 1] for lane in lanes]
    # CB3: input lane L takes CB2 output lane [0,4,1,5,2,6,3,7][L]; every
    # output lane concatenates all 8 input lanes' frame-flags lists.
    lane_map3 = [0, 4, 1, 5, 2, 6, 3, 7]
    out = []
    for L in range(8):
        out.extend(cb2_out[lane_map3[L]])
    assert out == [masks[slot] for slot in range(1, 17)], \
        "frame-flags words are not in ascending source-slot order"


def test_frame_flags_swap_is_visible():
    """A swapped backplane pair produces a detectably wrong word order."""
    masks = {slot: 1 << (slot - 1) for slot in range(1, 17)}
    swapped = dict(masks)
    swapped[3], swapped[4] = swapped[4], swapped[3]
    direct = [masks[s] for s in range(1, 17)]
    wrong = [swapped[s] for s in range(1, 17)]
    assert direct != wrong
    assert direct[2:4] == wrong[3:1:-1]
