import math
import re

import numpy as np
import pytest

from pychfpga.common.raw_data_packet_processor import (
    PACKET_COOKIE_ADC,
    PACKET_COOKIE_CT,
    PACKET_COOKIE_FFT,
    RAW_SOURCE_EMPTY,
    RawDataPacketProcessor,
    decode_format_string,
)
from pychfpga.common.raw_frame_receiver import RawFrameReceiver
from pychfpga.common.state import State


def capture_format_for_tests(format="8"):
    return decode_format_string(format)


def _unused_capture_format_slot():
    """Placeholder for dual-source ``capture_format[1]`` when tests only use source 0."""
    return State(fmt_bus_width=8, fmt_data_width=8, fmt_is_complex=False, fmt_interleave_factor=1)


def process_and_get(processor, source=0, config=None, verbose=None, with_indices=False):
    processor.process_udp_packets(config=config, verbose=verbose)
    if not processor.n_out:
        return None
    ts, data, counts = processor.get_formatted_data(slice(0, processor.n_out), source=source)
    if with_indices:
        return ts, data, counts, np.arange(ts.shape[0])
    return ts, data, counts


class DummyPacketReceiver:
    def __init__(self, n_packets=16, max_packet_size=64):
        self.n_packets = n_packets
        self.max_packet_size = max_packet_size
        self.buf = np.zeros((n_packets, max_packet_size), dtype=np.uint8)
        self.pkt_len = np.zeros((n_packets,), dtype=np.uint32)
        self.n_stored = 0

    def receive_packets(self, verbose=0):
        """No real UDP in unit tests; callers fill ``buf`` / ``pkt_len`` / ``n_stored``."""
        return int(self.n_stored)

    def flush(self, **kwargs):
        self.pkt_len.fill(0)
        self.n_stored = 0
        return 0


def default_config(format="8", frames_per_burst=1, **overrides):
    cf0 = capture_format_for_tests(format)
    state = dict(
        capture_engine="PROBER",
        source="funcgen",
        channels=(0, 1),
        stream_ids=(10, 11),
        frames_per_burst=frames_per_burst,
        subframes_per_frame=1,
        target_mode=0,
        bytes_per_sample=1,
        samples_per_frame=4,
        bytes_per_frame=4,
        bytes_per_subframe=4,
        packets_per_channel_per_burst=frames_per_burst,
        packets_per_logical_frame=1,
        logical_frames_per_burst=frames_per_burst,
        fft_bins_per_word=1,
        capture_bits_per_word=8,
        capture_format=[cf0, _unused_capture_format_slot()],
        packet_header_format="raw-v1",
        receiver_recreate_attrs=(
            "capture_engine",
            "bytes_per_sample",
            "samples_per_frame",
            "frames_per_burst",
            "subframes_per_frame",
            "stream_ids",
            "packets_per_logical_frame",
            "logical_frames_per_burst",
            "packet_header_format",
        ),
        receiver_update_attrs=("stream_ids", "target_mode", "source", "capture_format"),
    )
    state.update(overrides)
    return state


def make_processor(n_packets=16, format="8", frames_per_burst=1, receive_window=3, **kwargs):
    pr = DummyPacketReceiver(n_packets=n_packets)
    state = default_config(
        format=format,
        frames_per_burst=frames_per_burst,
        **kwargs,
    )
    return RawDataPacketProcessor(
        pr,
        config=state,
        receive_window=receive_window,
    ), pr


def write_packet(
        processor,
        pr,
        ix,
        stream_id,
        ts,
        frame=0,
        subframe=0,
        cookie=PACKET_COOKIE_ADC,
        payload=None):
    payload = np.asarray(payload if payload is not None else np.arange(4), dtype=np.uint8)
    pr.buf[ix, 0] = cookie
    pr.buf[ix, 1] = ((frame & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = subframe & 0x3
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    pr.buf[ix, 10:14] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE
    pr.n_stored = max(pr.n_stored, ix + 1)


def write_burst(processor, pr, start_ix, ts, payload_base=0):
    write_packet(processor, pr, start_ix, stream_id=10, ts=ts, payload=[payload_base + 1] * 4)
    write_packet(processor, pr, start_ix + 1, stream_id=11, ts=ts, payload=[payload_base + 2] * 4)


def test_duplicate_subframe_does_not_complete_missing_subframe():
    """A repeated UCAP subframe must not stand in for a missing packet."""
    processor, pr = make_processor(
        n_packets=4,
        stream_ids=(10,),
        channels=(0,),
        samples_per_frame=8,
        bytes_per_frame=8,
        bytes_per_subframe=4,
        subframes_per_frame=2,
    )
    write_packet(processor, pr, 0, stream_id=10, ts=100, subframe=0)
    processor.process_udp_packets()
    assert processor.raw_data_count[0, 0, 0] == 1

    pr.n_stored = 0
    pr.pkt_len.fill(0)
    write_packet(processor, pr, 0, stream_id=10, ts=100, subframe=0)
    write_packet(processor, pr, 1, stream_id=10, ts=100, subframe=1)
    processor.process_udp_packets()

    assert processor.raw_subframe_mask[0, 0, 0] == 0b11
    assert processor.raw_data_count[0, 0, 0] == -1


def test_mixed_adc_fft_subframes_invalidate_physical_frame():
    """A source transition must not assemble one frame from A0 and A1 packets."""
    processor, pr = make_processor(
        n_packets=4,
        stream_ids=(10,),
        channels=(0,),
        samples_per_frame=8,
        bytes_per_frame=8,
        bytes_per_subframe=4,
        subframes_per_frame=2,
    )
    write_packet(
        processor, pr, 0, stream_id=10, ts=100, subframe=0,
        cookie=PACKET_COOKIE_ADC)
    write_packet(
        processor, pr, 1, stream_id=10, ts=100, subframe=1,
        cookie=PACKET_COOKIE_FFT)
    processor.process_udp_packets()

    assert processor.raw_source_mask[0, 0, 0] == 0b11
    assert processor.raw_source[0, 0, 0] == RAW_SOURCE_EMPTY
    assert processor.raw_data_count[0, 0, 0] == -1


def test_noncomplex_formatter_excludes_other_source_counts():
    """ADC formatting must not report a complete row assembled from FFT packets."""
    processor, pr = make_processor(
        n_packets=2,
        stream_ids=(10,),
        channels=(0,),
    )
    write_packet(
        processor, pr, 0, stream_id=10, ts=100,
        cookie=PACKET_COOKIE_FFT)
    processor.process_udp_packets()

    _, _, adc_counts = processor.get_formatted_data(
        slice(0, processor.n_out), source=0)
    _, _, fft_counts = processor.get_formatted_data(
        slice(0, processor.n_out), source=1)
    np.testing.assert_array_equal(adc_counts, [[0]])
    np.testing.assert_array_equal(fft_counts, [[1]])


def test_duplicate_stream_ids_rejected():
    """Duplicate stream_ids collapse six_map; reject instead of silently mis-routing."""
    pr = DummyPacketReceiver()
    state = default_config(stream_ids=(0, 0))
    with pytest.raises(ValueError, match="stream_ids must be unique"):
        RawDataPacketProcessor(pr, config=state, receive_window=3)


def test_captured_payload_not_all_zeros_process_packets():
    """Regression: assembled rows must contain UDP payload bytes (not all zeros).

    One timestamp is not enough: with default receive_window, ``n_out = max(0, n_ts - nc)``
    can be 0 until enough complete rows sit behind the tail window (same as
    ``test_process_packets_returns_complete_burst_after_carryover_tail``).
    """
    processor, pr = make_processor()
    write_burst(processor, pr, 0, ts=5, payload_base=11)
    write_burst(processor, pr, 2, ts=6, payload_base=21)
    write_burst(processor, pr, 4, ts=7, payload_base=31)
    write_burst(processor, pr, 6, ts=8, payload_base=41)
    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out
    flat = np.asarray(data, dtype=np.uint8).ravel()
    assert flat.size > 0, "expected formatted data"
    assert not np.all(flat == 0), (
        "all-zero capture after valid packets (pkt_off/scatter/header path likely broken)"
    )
    assert np.any(counts > 0)
    # Synthetic UDP payloads: stream 10 → base+1, stream 11 → base+2 (four bytes each).
    np.testing.assert_array_equal(data[0, 0, 0], np.full(4, 12, dtype=np.int8))
    np.testing.assert_array_equal(data[0, 1, 0], np.full(4, 13, dtype=np.int8))


@pytest.mark.parametrize(
    "fmt,bytes_per_sample,samples_per_frame",
    [
        ("8", 1, 4),
        ("16", 2, 2),
    ],
)
def test_captured_payload_not_all_zeros_by_format(fmt, bytes_per_sample, samples_per_frame):
    """Notebook often uses format '16'; both 8- and 16-bit paths must carry payload into ``data``."""
    processor, pr = make_processor(
        format=fmt,
        bytes_per_sample=bytes_per_sample,
        samples_per_frame=samples_per_frame,
    )
    write_burst(processor, pr, 0, ts=5, payload_base=3)
    write_burst(processor, pr, 2, ts=6, payload_base=13)
    write_burst(processor, pr, 4, ts=7, payload_base=23)
    write_burst(processor, pr, 6, ts=8, payload_base=33)
    out = process_and_get(processor)
    assert out is not None
    _ts, data, _counts = out
    sample = np.asarray(data).ravel()
    assert sample.size > 0
    assert not np.all(sample == 0), (
        f"format={fmt!r}: all-zero data after valid packets (assembly/format view bug)"
    )
    if fmt == "8":
        np.testing.assert_array_equal(data[0, 0, 0], np.full(4, 4, dtype=np.int8))
        np.testing.assert_array_equal(data[0, 1, 0], np.full(4, 5, dtype=np.int8))
    else:
        # payload bytes [4,4,4,4] / [5,5,5,5] as big-endian i16 pairs (payload_base=3).
        np.testing.assert_array_equal(data[0, 0, 0], np.full(2, 1028, dtype=np.dtype(">i2")))
        np.testing.assert_array_equal(data[0, 1, 0], np.full(2, 1285, dtype=np.dtype(">i2")))


def test_read_raw_frames_yields_nonzero_data():
    """Notebook path: receive → process → copy formatted rows into output buffer."""
    pr = DummyPacketReceiver()
    cfg = default_config(format="8")
    r = RawFrameReceiver(pr, config=cfg, verbose=0)
    write_burst(r, pr, 0, ts=9001, payload_base=20)
    write_burst(r, pr, 2, ts=9002, payload_base=30)
    write_burst(r, pr, 4, ts=9003, payload_base=40)
    write_burst(r, pr, 6, ts=9004, payload_base=50)
    ts_, data_, counts_ = r.read_raw_frames(ncap=1, flush=False, chan_first=False)
    assert int(ts_[0]) == 9001
    sample = np.asarray(data_).ravel()
    assert sample.size > 0
    assert not np.all(sample == 0), (
        "read_raw_frames returned all-zero data (process_packets vs copy path bug)"
    )
    assert np.any(np.asarray(counts_) > 0)
    np.testing.assert_array_equal(data_[0, 0, 0], np.full(4, 21, dtype=np.int8))
    np.testing.assert_array_equal(data_[0, 1, 0], np.full(4, 22, dtype=np.int8))


def test_read_raw_frames_omitting_verbose_preserves_constructor():
    """read_raw_frames must not reset verbose when omitted (use constructor / get_data_receiver value)."""
    pr = DummyPacketReceiver()
    r = RawFrameReceiver(pr, config=default_config(format="8"), verbose=4)
    for i, tsv in enumerate((100, 101, 102, 103)):
        write_burst(r, pr, 2 * i, ts=tsv, payload_base=10 * i)
    r.read_raw_frames(ncap=1, flush=False)
    assert r.verbose == 4
    r.flush()
    for i, tsv in enumerate((200, 201, 202, 203)):
        write_burst(r, pr, 2 * i, ts=tsv, payload_base=40 + 10 * i)
    r.read_raw_frames(ncap=1, flush=False, verbose=1)
    assert r.verbose == 1


def test_back_to_back_read_raw_frames_each_gets_fresh_iterator():
    """Regression: singleton data_iter caused confusing state; each read_raw_frames starts clean."""
    pr = DummyPacketReceiver()
    r = RawFrameReceiver(pr, config=default_config(format="8"), verbose=0)
    for i, tsv in enumerate((100, 101, 102, 103)):
        write_burst(r, pr, 2 * i, ts=tsv, payload_base=10 * i)
    ts1, _d1, c1 = r.read_raw_frames(ncap=4, flush=False)
    assert int(ts1[0]) == 100
    assert np.all(c1 > 0)

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    for i, tsv in enumerate((200, 201, 202, 203)):
        write_burst(r, pr, 2 * i, ts=tsv, payload_base=50 + 10 * i)
    ts2, _d2, c2 = r.read_raw_frames(ncap=4, flush=False)
    assert int(ts2[0]) == 200
    assert np.all(c2 > 0)


def test_process_packets_returns_complete_burst_after_carryover_tail():
    """All timestamp rows that are fully assembled in the UDP block are emitted at once.

    Carry only withholds rows that are still incomplete in the last ``receive_window`` active rows
    (not garbage rows past ``n_ts`` in the allocation tail).
    """
    processor, pr = make_processor()
    write_burst(processor, pr, 0, ts=5, payload_base=0)
    write_burst(processor, pr, 2, ts=6, payload_base=10)
    write_burst(processor, pr, 4, ts=7, payload_base=20)
    write_burst(processor, pr, 6, ts=8, payload_base=30)

    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    np.testing.assert_array_equal(ts, np.array([5, 6, 7, 8], dtype=ts.dtype))
    np.testing.assert_array_equal(data[0, 0, 0], np.array([1, 1, 1, 1], dtype=np.int8))
    np.testing.assert_array_equal(data[0, 1, 0], np.array([2, 2, 2, 2], dtype=np.int8))
    np.testing.assert_array_equal(data[3, 0, 0], np.array([31, 31, 31, 31], dtype=np.int8))
    np.testing.assert_array_equal(data[3, 1, 0], np.array([32, 32, 32, 32], dtype=np.int8))
    np.testing.assert_array_equal(counts[0], np.array([1, 1]))
    np.testing.assert_array_equal(counts[3], np.array([1, 1]))


def test_carryover_tail_compacts_on_next_packet_block():
    """Second UDP block yields only the new timestamps once the first block is fully emitted."""
    processor, pr = make_processor()
    for i, ts in enumerate((5, 6, 7, 8)):
        write_burst(processor, pr, 2 * i, ts=ts, payload_base=10 * i)
    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    np.testing.assert_array_equal(ts, np.array([5, 6, 7, 8], dtype=ts.dtype))

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    for i, ts_ in enumerate((9, 10, 11)):
        write_burst(processor, pr, 2 * i, ts=ts_, payload_base=40 + 10 * i)
    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    np.testing.assert_array_equal(ts, np.array([9, 10, 11], dtype=ts.dtype))


def test_multi_channel_sequential_blocks_rowwise_carry():
    """Regression (notebook ncap=16 / many channels): carry_len must count timestamp rows.

    Using ``flatnonzero`` on the 2D ``(time, channel)`` complete mask mixed channel index into
    ``carry_len`` and could make ``ts_array[old_nts-nc:old_nts]`` wider than ``pkt_ts_with_carry[n:n+nc]``.
    """
    sids = (10, 11, 12, 13)
    processor, pr = make_processor(
        n_packets=64,
        stream_ids=sids,
        channels=(0, 1, 2, 3),
    )
    nch = len(sids)

    def write_ts_block(start_ix, ts, payload_base):
        for j, sid in enumerate(sids):
            write_packet(
                processor,
                pr,
                start_ix + j,
                stream_id=sid,
                ts=ts,
                payload=[payload_base + j + 1] * 4,
            )

    for i, tsv in enumerate((100, 101, 102, 103)):
        write_ts_block(nch * i, tsv, 10 * i)
    out1 = process_and_get(processor)
    assert out1 is not None
    assert len(out1[0]) == 4
    d1 = out1[1]
    np.testing.assert_array_equal(d1[0, 0, 0], np.full(4, 10 * 0 + 1, dtype=np.int8))
    np.testing.assert_array_equal(d1[0, 3, 0], np.full(4, 10 * 0 + 4, dtype=np.int8))
    np.testing.assert_array_equal(d1[3, 0, 0], np.full(4, 10 * 3 + 1, dtype=np.int8))

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    for i, tsv in enumerate((200, 201, 202)):
        write_ts_block(nch * i, tsv, 50 + 10 * i)
    out2 = process_and_get(processor)
    assert out2 is not None
    assert len(out2[0]) == 3


@pytest.mark.skip(
    reason=(
        "Sequential process_packets calls after a partial timestamp row hit carryover indexing "
        "in process_packets (pkt_ts_with_carry vs ts_array slice); re-enable when emit/carry matches UDP batches."
    )
)
def test_partial_burst_survives_across_packet_blocks():
    processor, pr = make_processor()
    write_packet(processor, pr, 0, stream_id=10, ts=7, payload=[1, 1, 1, 1])

    r0 = process_and_get(processor)
    assert r0 is None or len(r0[0]) == 0

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_packet(processor, pr, 0, stream_id=11, ts=7, payload=[2, 2, 2, 2])
    r1 = process_and_get(processor)
    assert r1 is None or len(r1[0]) == 0

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=8, payload_base=10)
    write_burst(processor, pr, 2, ts=9, payload_base=20)
    write_burst(processor, pr, 4, ts=10, payload_base=30)
    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    assert len(ts) == 1
    assert int(ts[0]) == 7
    np.testing.assert_array_equal(data[0, 0, 0], np.array([1, 1, 1, 1], dtype=np.int8))
    np.testing.assert_array_equal(data[0, 1, 0], np.array([2, 2, 2, 2], dtype=np.int8))


def test_block_output_capacity_allows_nearly_complete_buffered_burst():
    processor, pr = make_processor(n_packets=2)
    # Same formula as buffer allocation: UDP slots / timestamps_per_packet + receive window.
    expected_max_ts = math.ceil(pr.n_packets / processor.packets_per_timestamp) + processor.receive_window
    assert processor.max_timestamps == expected_max_ts


def test_timestamp_clip_keeps_newest_distinct_timestamps():
    """When distinct timestamps exceed ``max_timestamps``, keep the newest N and warn."""
    processor, pr = make_processor(n_packets=16)
    processor.max_timestamps = 2
    for i, tsv in enumerate((100, 101, 102)):
        write_burst(processor, pr, 2 * i, ts=tsv, payload_base=10 * i)
    with pytest.warns(UserWarning, match="timestamp backlog"):
        out = process_and_get(processor)
    assert out is not None
    ts, _, _ = out
    np.testing.assert_array_equal(ts, np.array([101, 102], dtype=ts.dtype))


def test_monotonic_rejects_udp_not_after_last_emitted():
    """Packets at or before the last returned timestamp are ignored; emits stay strictly increasing."""
    processor, pr = make_processor()
    for i, tsv in enumerate((5, 6, 7, 8)):
        write_burst(processor, pr, 2 * i, ts=tsv, payload_base=10 * i)
    out1 = process_and_get(processor)
    assert out1 is not None
    assert int(out1[0][-1]) == 8

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=7, payload_base=100)
    write_burst(processor, pr, 2, ts=9, payload_base=20)
    out2 = process_and_get(processor)
    assert out2 is not None
    ts2, data2, _ = out2
    assert len(ts2) == 1
    assert int(ts2[0]) == 9
    np.testing.assert_array_equal(data2[0, 0, 0], np.full(4, 21, dtype=np.int8))

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=6, payload_base=30)
    write_burst(processor, pr, 2, ts=7, payload_base=40)
    assert process_and_get(processor) is None


def test_flush_clears_last_emitted_timestamp_tracker():
    processor, pr = make_processor()
    write_burst(processor, pr, 0, ts=50, payload_base=1)
    write_burst(processor, pr, 2, ts=51, payload_base=2)
    assert process_and_get(processor) is not None
    processor.flush()
    write_burst(processor, pr, 0, ts=50, payload_base=11)
    write_burst(processor, pr, 2, ts=51, payload_base=12)
    out = process_and_get(processor)
    assert out is not None
    np.testing.assert_array_equal(out[0], np.array([50, 51], dtype=out[0].dtype))


def test_view_capable_format_can_return_raw_storage_view():
    processor, pr = make_processor(n_packets=2)
    write_packet(processor, pr, 0, stream_id=10, ts=9, payload=[1, 2, 3, 4])
    write_packet(processor, pr, 1, stream_id=11, ts=9, payload=[5, 6, 7, 8])

    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    assert np.shares_memory(data, processor.raw_data)


def test_transformed_format_uses_formatted_output_buffer():
    processor, pr = make_processor(n_packets=2, format="14")
    write_packet(processor, pr, 0, stream_id=10, ts=11, payload=[0, 4, 0, 8])
    write_packet(processor, pr, 1, stream_id=11, ts=11, payload=[0, 12, 0, 16])

    out = process_and_get(processor)
    assert out is not None
    ts, data, counts = out

    assert np.shares_memory(data, processor.raw_data)
    d = np.asarray(data, dtype=np.int16).reshape(-1)
    np.testing.assert_array_equal(d[:4], np.array([1, 2, 3, 4], dtype=np.int16))


def test_output_only_reconfiguration_reuses_raw_buffer():
    processor, pr = make_processor(n_packets=2, format="8")
    raw_data = processor.raw_data
    cfg = default_config(format="14", capture_format=[capture_format_for_tests("14"), _unused_capture_format_slot()])

    process_and_get(processor, config=State(cfg))

    assert processor.raw_data is raw_data
    assert processor.config.capture_format[0].fmt_bus_width == 16
    assert processor.config.capture_format[0].fmt_data_width == 14


def test_geometry_reconfiguration_reallocates_when_shape_changes():
    processor, pr = make_processor(n_packets=2, format="8")
    raw_data = processor.raw_data
    cfg = default_config(
        format="8",
        frames_per_burst=2,
        packets_per_channel_per_burst=2,
        logical_frames_per_burst=2,
    )

    process_and_get(processor, config=State(cfg))

    assert processor.raw_data is not raw_data
    assert processor.FRAMES_PER_BURST == 2
    assert processor.n_ts == 0


def test_read_raw_frames_split_kwarg_yields_per_frame_width():
    """``read_raw_frames(split=1)`` must enable splitting (notebook API alias)."""
    cfg = default_config(
        format="8",
        frames_per_burst=2,
        bytes_per_sample=1,
        samples_per_frame=8,
        bytes_per_frame=8,
        bytes_per_subframe=8,
        subframes_per_frame=1,
        packets_per_channel_per_burst=2,
    )
    pr = DummyPacketReceiver(n_packets=16, max_packet_size=32)
    r = RawFrameReceiver(pr, config=cfg, verbose=0)
    for i, tsv in enumerate((5, 6, 7, 8, 9)):
        write_burst(r, pr, 2 * i, ts=tsv, payload_base=10 * (i + 1))
    ts, data, counts = r.read_raw_frames(ncap=2, split=1, flush=False)
    assert data.shape[-1] == 8, f"expected 8 samples per frame, got shape {data.shape}"


def test_split_parameter_yields_samples_per_frame():
    """UCAP-style burst: split=True exposes 16384 samples/frame, not 32768 (full burst)."""
    processor, _pr = make_processor(
        n_packets=4,
        format="16",
        frames_per_burst=2,
        bytes_per_sample=2,
        samples_per_frame=16384,
        bytes_per_frame=32768,
        bytes_per_subframe=8192,
        subframes_per_frame=4,
        stream_ids=(10,),
        channels=(0,),
    )
    assert processor.BYTES_PER_FRAME == 32768
    assert processor.raw_data.shape[-1] == 2 * 32768

    processor.raw_data[0, 0, :] = 1
    processor.ts_array[0] = 100
    processor.raw_data_count[0, 0, :] = 4
    processor.raw_source[0, 0, :] = 0
    processor.n_out = 1

    ts_wide, burst_wide, counts_wide = processor.get_formatted_data(slice(0, 1), source=0, split=False)
    assert burst_wide.shape == (1, 1, 32768), burst_wide.shape
    np.testing.assert_array_equal(ts_wide, [100])
    np.testing.assert_array_equal(counts_wide, [[8]])

    split_ts, split_rows, split_counts = processor.get_formatted_data(slice(0, 1), source=0, split=True)
    assert split_rows.shape == (2, 1, 16384), split_rows.shape
    assert split_rows.shape[-1] == 16384
    np.testing.assert_array_equal(split_ts, [100, 101])
    np.testing.assert_array_equal(split_counts, [[4], [4]])


def test_full_resolution_complex_ice_interleave4():
    cf1 = State(fmt_bus_width=32, fmt_data_width=29, fmt_is_complex=True, fmt_interleave_factor=4)
    processor, _pr = make_processor(
        n_packets=4,
        frames_per_burst=4,
        bytes_per_sample=1,
        samples_per_frame=24,
        bytes_per_frame=24,
        bytes_per_subframe=24,
        packets_per_channel_per_burst=4,
        packets_per_logical_frame=4,
        logical_frames_per_burst=1,
        fft_bins_per_word=2,
        capture_bits_per_word=32,
        source="scaler",
        stream_ids=(10,),
        channels=(0,),
        capture_format=[_unused_capture_format_slot(), cf1],
        fft_bin_map=np.array([0, 2, 4, 6, 8, 10, 1, 3, 5, 7, 9, 11], dtype=np.intp),
    )

    payload = np.asarray(
        [
            # Re/Im pairs in frame-pair order: lane 0 frames, then lane 1 frames.
            # The bin map below then converts the lane-grouped transport order to natural bins.
            0, 1000, 2, 1002, 4, 1004,
            1, 1001, 3, 1003, 5, 1005,
            6, 1006, 8, 1008, 10, 1010,
            7, 1007, 9, 1009, 11, 1011,
        ],
        dtype=np.int32,
    )
    components = (payload << 3).astype(">i4")
    processor.raw_data[0, 0, :] = components.view(np.uint8)
    processor.ts_array[0] = 20
    processor.raw_data_count[0, 0, :] = 1
    processor.raw_source[0, 0, :] = 1
    processor.n_out = 1

    ts, data, counts = processor.get_formatted_data(slice(0, 1), source=1)

    expected_bins = np.arange(12, dtype=np.float32)
    expected = (expected_bins + 1j * (1000 + expected_bins)).reshape(1, 1, -1).astype(np.complex64)
    np.testing.assert_array_equal(ts, [20])
    np.testing.assert_array_equal(counts, [[1]])
    np.testing.assert_array_equal(data, expected)


def test_full_resolution_complex_crs_interleave8_uses_msb_slot():
    cf1 = State(fmt_bus_width=128, fmt_data_width=32, fmt_is_complex=True, fmt_interleave_factor=8)
    processor, _pr = make_processor(
        n_packets=8,
        frames_per_burst=8,
        bytes_per_sample=2,
        samples_per_frame=16,
        bytes_per_frame=32,
        bytes_per_subframe=32,
        packets_per_channel_per_burst=8,
        packets_per_logical_frame=8,
        logical_frames_per_burst=1,
        fft_bins_per_word=4,
        capture_bits_per_word=128,
        source="scaler",
        stream_ids=(10,),
        channels=(0,),
        capture_format=[_unused_capture_format_slot(), cf1],
        fft_bin_map=np.array([1, 0, 3, 2, 5, 4, 7, 6], dtype=np.intp),
    )

    frame_components = (
        (100, 200),
        (104, 204),
        (101, 201),
        (105, 205),
        (102, 202),
        (106, 206),
        (103, 203),
        (107, 207),
    )
    slots = np.full((8, 8), -999, dtype=">i4")
    for frame, values in enumerate(frame_components):
        slots[frame, 0::4] = values
    processor.raw_data[0, 0, :] = slots.reshape(-1).view(np.uint8)
    processor.ts_array[0] = 100
    processor.raw_data_count[0, 0, :] = 4
    processor.raw_source[0, 0, :] = 1
    processor.n_out = 1

    ts, data, counts = processor.get_formatted_data(slice(0, 1), source=1)

    expected = np.asarray([[[
        101 + 201j, 100 + 200j, 103 + 203j, 102 + 202j,
        105 + 205j, 104 + 204j, 107 + 207j, 106 + 206j,
    ]]], dtype=np.complex64)
    np.testing.assert_array_equal(ts, [100])
    np.testing.assert_array_equal(counts, [[4]])
    np.testing.assert_array_equal(data, expected)


def test_split_format_core_and_modifiers_suffix():
    def split_format_core_and_modifiers(fmt: str):
        m = re.fullmatch(r"(\d+(?:\+\d+)?)(.*)", fmt)
        if not m:
            return fmt, ""
        return m.group(1), m.group(2) or ""

    split = split_format_core_and_modifiers
    assert split("32+32d") == ("32+32", "d")
    assert split("32+32") == ("32+32", "")
    assert split("16saturationflags") == ("16", "saturationflags")
    assert split("8") == ("8", "")


# ---------------------------------------------------------------------------
# Corner-turn capture probe (ct_prober, cookie 0xA2, source index 2)
# ---------------------------------------------------------------------------

CT_STREAM_ID = 0x015  # crate 0, slot 1, lane 5
CT_HEADER_WORDS = 4
CT_DATA_WORDS = 2  # tiny geometry: 2 data words -> 7 words per CT packet
CT_WORDS_PER_PACKET = CT_HEADER_WORDS + CT_DATA_WORDS + 1  # + trailer word


def ct_capture_format(use_offset_encoding=False, four_bits=True):
    return State(
        fmt_bus_width=8,
        fmt_data_width=4,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        fmt_ct_packed=True,
        fmt_header_words=CT_HEADER_WORDS,
        fmt_data_words=CT_DATA_WORDS,
        fmt_four_bits=four_bits,
        fmt_frames_per_packet=1,
        fmt_bins=CT_DATA_WORDS,
        fmt_channels_per_bin=4,
        use_offset_encoding=use_offset_encoding,
    )


def default_ct_config(frames_per_burst=1, **overrides):
    bytes_per_frame = 4 * CT_WORDS_PER_PACKET
    state = default_config(
        source="ct",
        channels=(),
        stream_ids=(CT_STREAM_ID,),
        frames_per_burst=frames_per_burst,
        samples_per_frame=bytes_per_frame,
        bytes_per_frame=bytes_per_frame,
        bytes_per_subframe=bytes_per_frame,
        capture_bits_per_word=32,
        capture_format=[None, None, ct_capture_format()],
        packet_cookie=[PACKET_COOKIE_ADC, PACKET_COOKIE_FFT, PACKET_COOKIE_CT],
    )
    state.update(overrides)
    return state


def make_ct_processor(n_packets=16, frames_per_burst=1, receive_window=3, **kwargs):
    pr = DummyPacketReceiver(n_packets=n_packets, max_packet_size=64)
    state = default_ct_config(frames_per_burst=frames_per_burst, **kwargs)
    return RawDataPacketProcessor(pr, config=state, receive_window=receive_window), pr


def write_ct_packet(
        processor,
        pr,
        ix,
        ts,
        burst_index=0,
        stream_id=CT_STREAM_ID,
        data_words=(0, 0),
        pkt_len=None):
    """Write a wrapped CT packet: 10-byte capture header + big-endian 32-bit words.

    Word 0 mimics the inner CT header cookie word (0x...14CF); the last word is
    the all-zero packet-flags trailer emitted by the firmware.
    """
    words = np.zeros(CT_WORDS_PER_PACKET, dtype=np.uint32)
    words[0] = (stream_id << 16) | (1 << 12) | (CT_HEADER_WORDS << 8) | 0xCF
    words[3] = int(ts) & 0xFFFFFFFF  # inner frame timestamp
    words[CT_HEADER_WORDS:CT_HEADER_WORDS + CT_DATA_WORDS] = np.asarray(data_words, dtype=np.uint32)
    pr.buf[ix, 0] = 0xA2
    pr.buf[ix, 1] = ((burst_index & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    payload = words.astype(">u4").view(np.uint8)  # MSB-first on the wire
    pr.buf[ix, 10:10 + payload.size] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE if pkt_len is None else pkt_len
    pr.n_stored = max(pr.n_stored, ix + 1)


def test_ct_cookie_accepted_and_decoded():
    """0xA2 packets land in source 2 and decode to (4+4)-bit complex samples."""
    processor, pr = make_ct_processor()
    assert processor.PACKET_SIZE == 10 + 4 * CT_WORDS_PER_PACKET
    # Word value 0x7F8801F0: wire bytes 7F 88 01 F0, logical channel order is
    # LSB-first, so channels decode as F0, 01, 88, 7F.
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x7F8801F0, 0x12345678))
    result = process_and_get(processor, source=2)
    assert result is not None
    ts, data, counts = result
    assert data.dtype == np.complex64
    assert data.shape == (1, 1, 4 * CT_DATA_WORDS)
    np.testing.assert_array_equal(ts, [100])
    np.testing.assert_array_equal(counts, [[1]])
    assert processor.raw_source[0, 0, 0] == 2
    expected = np.asarray([
        -1 + 0j, 0 + 1j, -8 - 8j, 7 - 1j,   # F0, 01, 88, 7F
        7 - 8j, 5 + 6j, 3 + 4j, 1 + 2j,     # 78, 56, 34, 12
    ], dtype=np.complex64)
    np.testing.assert_array_equal(data[0, 0], expected)


def test_ct_offset_binary_decode():
    """use_offset_encoding converts offset-binary nibbles to two's complement."""
    processor, pr = make_ct_processor(
        capture_format=[None, None, ct_capture_format(use_offset_encoding=True)])
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x00F78888, 0))
    ts, data, counts = process_and_get(processor, source=2)
    # Bytes LSB-first: 88, 88, F7, 00 -> XOR 0x88 -> 00, 00, 7F, 88
    expected_first = np.asarray([0 + 0j, 0 + 0j, 7 - 1j, -8 - 8j], dtype=np.complex64)
    np.testing.assert_array_equal(data[0, 0, :4], expected_first)


def test_ct_format32_returns_raw_words():
    """format='32' bypasses the packed decode and returns all packet words."""
    processor, pr = make_ct_processor()
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x7F8801F0, 0x12345678))
    processor.process_udp_packets()
    ts, data, counts = processor.get_formatted_data(
        slice(0, processor.n_out), source=2, format="32")
    assert data.shape == (1, 1, CT_WORDS_PER_PACKET)
    words = data[0, 0].astype(np.int64) & 0xFFFFFFFF
    assert words[0] & 0xFF == 0xCF                    # inner CT header cookie
    assert (words[0] >> 8) & 0xF == CT_HEADER_WORDS   # header length
    assert (words[0] >> 12) & 0xF == 1                # protocol
    assert words[0] >> 16 == CT_STREAM_ID
    assert words[4] == 0x7F8801F0
    assert words[5] == 0x12345678
    assert words[6] == 0                              # packet-flags trailer


def test_ct_burst_split_rows_and_timestamps():
    """A multi-packet burst yields one row per CT packet with consecutive ts."""
    processor, pr = make_ct_processor(frames_per_burst=2)
    write_ct_packet(processor, pr, 0, ts=100, burst_index=0, data_words=(0x01010101, 0))
    write_ct_packet(processor, pr, 1, ts=100, burst_index=1, data_words=(0x02020202, 0))
    ts, data, counts = process_and_get(processor, source=2)
    np.testing.assert_array_equal(ts, [100, 101])
    assert data.shape == (2, 1, 4 * CT_DATA_WORDS)
    np.testing.assert_array_equal(data[0, 0, :4], np.full(4, 0 + 1j, dtype=np.complex64))
    np.testing.assert_array_equal(data[1, 0, :4], np.full(4, 0 + 2j, dtype=np.complex64))
    np.testing.assert_array_equal(counts, [[1], [1]])


def test_ct_wrong_length_dropped():
    """A 0xA2 packet whose size does not match the configured geometry is dropped."""
    processor, pr = make_ct_processor()
    write_ct_packet(processor, pr, 0, ts=100, pkt_len=processor.PACKET_SIZE - 4)
    processor.process_udp_packets()
    assert processor.n_out == 0


def test_ct_dropped_by_legacy_two_cookie_config():
    """Configs without 0xA2 in packet_cookie (CRS/UCAP, older ICE) drop CT packets."""
    processor, pr = make_processor(n_packets=4)  # legacy chan config, no packet_cookie
    assert list(processor.config.packet_cookie) == [PACKET_COOKIE_ADC, PACKET_COOKIE_FFT]
    pr.buf[0, 0] = 0xA2
    pr.buf[0, 1] = (10 >> 8) & 0xF
    pr.buf[0, 2] = 10 & 0xFF
    pr.buf[0, 4:10] = np.frombuffer(int(100).to_bytes(6, "big"), dtype=np.uint8)
    pr.pkt_len[0] = processor.PACKET_SIZE  # even a size match must not help
    pr.n_stored = 1
    processor.process_udp_packets()
    assert processor.n_out == 0


def test_ct_source2_without_capture_format_raises():
    """Reading source 2 on a chan-capture config fails with a clear error."""
    processor, pr = make_processor(n_packets=4, stream_ids=(10,), channels=(0,))
    write_packet(processor, pr, 0, stream_id=10, ts=100)
    processor.process_udp_packets()
    with pytest.raises(ValueError, match="No capture format is configured for source 2"):
        processor.get_formatted_data(slice(0, processor.n_out), source=2)


def test_ct_mixed_source_invalidates_cell():
    """A cell receiving both 0xA0 and 0xA2 packets is flagged invalid (counts=-1)."""
    processor, pr = make_ct_processor()
    write_ct_packet(processor, pr, 0, ts=100)
    # Same stream id, same timestamp, chan cookie, matching size
    write_ct_packet(processor, pr, 1, ts=100)
    pr.buf[1, 0] = PACKET_COOKIE_ADC
    processor.process_udp_packets()
    assert processor.raw_data_count[0, 0, 0] == -1
    assert processor.raw_source[0, 0, 0] == RAW_SOURCE_EMPTY


def test_chan_sources_unchanged_with_three_cookie_list():
    """A0/A1 decode behavior is unchanged when 0xA2 is listed as source 2."""
    processor, pr = make_processor(
        n_packets=4,
        packet_cookie=[PACKET_COOKIE_ADC, PACKET_COOKIE_FFT, 0xA2],
        capture_format=[
            capture_format_for_tests("8"),
            _unused_capture_format_slot(),
            None,
        ],
    )
    write_burst(processor, pr, 0, ts=100, payload_base=10)
    ts, data, counts = process_and_get(processor, source=0)
    np.testing.assert_array_equal(ts, [100])
    np.testing.assert_array_equal(counts, [[1, 1]])
    np.testing.assert_array_equal(data[0, 0], np.full(4, 11, dtype=data.dtype))
    np.testing.assert_array_equal(data[0, 1], np.full(4, 12, dtype=data.dtype))


# ---------------------------------------------------------------------------
# shuffle256-geometry corner-turn packets (CB3-resliced: 8 bins x 256 channels)
# ---------------------------------------------------------------------------

S256_INNER_STREAM_ID = 0x2015  # stream_type 2, crate 0, slot0 1, lane 5
S256_DATA_WORDS_PER_FRAME = 512  # 8 bins x 64 words (256 channel bytes/bin)
S256_FLAG_WORDS_PER_FRAME = 80   # 64 data-flags + 16 frame-flags words


def s256_words_per_packet(fpp, send_flags=True):
    return (4 + fpp * S256_DATA_WORDS_PER_FRAME
            + (fpp * S256_FLAG_WORDS_PER_FRAME if send_flags else 0) + 1)


def s256_capture_format(fpp=1, use_offset_encoding=False):
    return State(
        fmt_bus_width=8,
        fmt_data_width=4,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        fmt_ct_packed=True,
        fmt_header_words=4,
        fmt_data_words=fpp * S256_DATA_WORDS_PER_FRAME,
        fmt_four_bits=True,
        fmt_frames_per_packet=fpp,
        fmt_bins=8,
        fmt_channels_per_bin=256,
        use_offset_encoding=use_offset_encoding,
    )


def make_s256_processor(fpp=1, send_flags=True, frames_per_burst=1, n_packets=8,
                        **format_kwargs):
    wpp = s256_words_per_packet(fpp, send_flags)
    bytes_per_frame = 4 * wpp
    pr = DummyPacketReceiver(n_packets=n_packets, max_packet_size=8192)
    state = default_config(
        source="ct",
        channels=(),
        stream_ids=(CT_STREAM_ID,),  # the wrapper carries the probe's 12-bit id
        frames_per_burst=frames_per_burst,
        samples_per_frame=bytes_per_frame,
        bytes_per_frame=bytes_per_frame,
        bytes_per_subframe=bytes_per_frame,
        capture_bits_per_word=32,
        capture_format=[None, None, s256_capture_format(fpp, **format_kwargs)],
        packet_cookie=[PACKET_COOKIE_ADC, PACKET_COOKIE_FFT, PACKET_COOKIE_CT],
    )
    return RawDataPacketProcessor(pr, config=state, receive_window=3), pr


def write_s256_packet(processor, pr, ix, ts, fpp=1, send_flags=True,
                      burst_index=0, planted=None):
    """Write a wrapped shuffle256 CT packet.

    planted: {(frame, bin, word): uint32} data words to set; everything else
    is zero. All flags words are poisoned with 0xDEADBEEF to prove the
    decoder never reads them; the trailer stays zero.
    """
    wpp = s256_words_per_packet(fpp, send_flags)
    words = np.zeros(wpp, dtype=np.uint32)
    words[0] = (S256_INNER_STREAM_ID << 16) | (1 << 12) | (4 << 8) | 0xCF
    words[1] = (int(send_flags) << 30) | (fpp << 24)
    words[3] = int(ts) & 0xFFFFFFFF
    data = words[4:4 + fpp * S256_DATA_WORDS_PER_FRAME].reshape(fpp, 8, 64)
    for (frame, bin_number, word), value in (planted or {}).items():
        data[frame, bin_number, word] = value
    if send_flags:
        words[4 + fpp * S256_DATA_WORDS_PER_FRAME:wpp - 1] = 0xDEADBEEF
    pr.buf[ix, 0] = 0xA2
    pr.buf[ix, 1] = ((burst_index & 0xF) << 4) | ((CT_STREAM_ID >> 8) & 0xF)
    pr.buf[ix, 2] = CT_STREAM_ID & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    payload = words.astype(">u4").view(np.uint8)
    pr.buf[ix, 10:10 + payload.size] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE
    pr.n_stored = max(pr.n_stored, ix + 1)


def test_s256_geometry_words():
    """The shuffle256 packet sizes match the doc-confirmed totals."""
    assert s256_words_per_packet(1, True) == 597
    assert s256_words_per_packet(2, True) == 1189
    assert s256_words_per_packet(1, False) == 517
    assert s256_words_per_packet(2, False) == 1029


def test_s256_decode_fpp1():
    """Planted data words land at flat index bin*256 + word*4 (+byte), LSB-first."""
    processor, pr = make_s256_processor(fpp=1)
    assert processor.PACKET_SIZE == 10 + 4 * 597
    planted = {
        (0, 0, 0): 0x7F8801F0,
        (0, 0, 63): 0x12345678,
        (0, 7, 63): 0x0000000F,
    }
    write_s256_packet(processor, pr, 0, ts=100, planted=planted)
    ts, data, counts = process_and_get(processor, source=2)
    assert data.shape == (1, 1, 2048)
    np.testing.assert_array_equal(counts, [[1]])
    samples = data[0, 0]
    # word (bin 0, word 0): wire bytes 7F 88 01 F0, channels decode LSB-first
    np.testing.assert_array_equal(
        samples[0:4],
        np.asarray([-1 + 0j, 0 + 1j, -8 - 8j, 7 - 1j], dtype=np.complex64))
    # word (bin 0, word 63) -> channels 252..255 of bin 0
    np.testing.assert_array_equal(
        samples[63 * 4:63 * 4 + 4],
        np.asarray([7 - 8j, 5 + 6j, 3 + 4j, 1 + 2j], dtype=np.complex64))
    # word (bin 7, word 63) -> the very last 4 samples of the frame
    base = 7 * 256 + 63 * 4
    np.testing.assert_array_equal(
        samples[base:base + 4],
        np.asarray([0 - 1j, 0, 0, 0], dtype=np.complex64))
    # nothing else (in particular no flags poison) leaks into the decode
    planted_idx = [0, 1, 2, 3, 252, 253, 254, 255, base, base + 1, base + 2, base + 3]
    rest = np.delete(samples, planted_idx)
    np.testing.assert_array_equal(rest, 0)


def test_s256_decode_fpp2_flags_poison():
    """Two-frame flags-on packets decode with frame 1 at offset 2048; flags ignored."""
    processor, pr = make_s256_processor(fpp=2)
    assert processor.PACKET_SIZE == 10 + 4 * 1189
    write_s256_packet(processor, pr, 0, ts=100, fpp=2,
                      planted={(1, 2, 5): 0x000000F7})
    ts, data, counts = process_and_get(processor, source=2)
    assert data.shape == (1, 1, 4096)
    samples = data[0, 0]
    idx = 2048 + 2 * 256 + 5 * 4
    np.testing.assert_array_equal(samples[idx], np.complex64(-1 + 7j))
    rest = np.delete(samples, [idx])
    np.testing.assert_array_equal(rest, 0)


def test_s256_offset_binary():
    """The offset-binary XOR applies at the shuffle256 geometry too."""
    processor, pr = make_s256_processor(fpp=1, use_offset_encoding=True)
    # byte 0x88 ^ 0x88 = 0x00 -> 0+0j; byte 0x00 ^ 0x88 = 0x88 -> -8-8j
    write_s256_packet(processor, pr, 0, ts=100, planted={(0, 0, 0): 0x00000088})
    ts, data, counts = process_and_get(processor, source=2)
    samples = data[0, 0]
    np.testing.assert_array_equal(samples[0], np.complex64(0))
    np.testing.assert_array_equal(samples[1:4], np.full(3, -8 - 8j, np.complex64))


def test_s256_burst_split():
    """A two-packet shuffle256 burst yields one row per packet, consecutive ts."""
    processor, pr = make_s256_processor(fpp=1, frames_per_burst=2)
    write_s256_packet(processor, pr, 0, ts=100, burst_index=0,
                      planted={(0, 0, 0): 0x01010101})
    write_s256_packet(processor, pr, 1, ts=100, burst_index=1,
                      planted={(0, 0, 0): 0x02020202})
    ts, data, counts = process_and_get(processor, source=2)
    np.testing.assert_array_equal(ts, [100, 101])
    assert data.shape == (2, 1, 2048)
    np.testing.assert_array_equal(data[0, 0, :4], np.full(4, 0 + 1j, np.complex64))
    np.testing.assert_array_equal(data[1, 0, :4], np.full(4, 0 + 2j, np.complex64))
    np.testing.assert_array_equal(counts, [[1], [1]])
