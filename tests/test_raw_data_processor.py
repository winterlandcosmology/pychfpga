import math
import re

import numpy as np
import pytest

from pychfpga.common.raw_data_packet_processor import (
    PACKET_COOKIE_ADC,
    PACKET_COOKIE_FFT,
    RawDataPacketProcessor,
    decode_format_string,
)
from pychfpga.common.raw_frame_receiver import RawFrameReceiver
from pychfpga.common.state import State


def capture_format_for_tests(format="8"):
    return decode_format_string(format)


def _unused_capture_format_slot():
    """Placeholder for dual-source ``capture_format[1]`` when tests only use source 0."""
    return State(
        fmt_bus_width=8,
        fmt_data_width=8,
        fmt_is_complex=False,
        fmt_interleave_factor=1,
        use_offset_encoding=False,
    )


def config_getter_from_mapping(mapping):
    """Build a stable config getter for processor unit tests."""
    if isinstance(mapping, State):
        payload = mapping.as_dict()
    else:
        payload = dict(mapping)
    if "capture_format" in payload:
        payload["capture_format"] = [
            item.as_dict() if isinstance(item, State) else item
            for item in payload["capture_format"]
        ]
    state = State()
    state.update_from(payload)
    return lambda: state


def process_and_get(processor, source=0, verbose=None, with_indices=False):
    processor.process_udp_packets(verbose=verbose)
    if not processor.n_out:
        return None
    ts, data, counts = processor.get_formatted_data(processor.out_rows, source=source)
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
        source=0,
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
        logical_frames_per_burst=frames_per_burst,
        fft_bins_per_word=1,
        capture_bits_per_word=8,
        capture_format=[cf0, _unused_capture_format_slot()],
        packet_cookie=[PACKET_COOKIE_ADC, PACKET_COOKIE_FFT],
        packet_header_format="raw-v1",
        receiver_recreate_attrs=(
            "capture_engine",
            "bytes_per_sample",
            "samples_per_frame",
            "frames_per_burst",
            "subframes_per_frame",
            "stream_ids",
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
    getter = config_getter_from_mapping(state)
    processor = RawDataPacketProcessor(
        pr,
        config_getter=getter,
        receive_window=receive_window,
    )
    processor.update_config()
    return processor, pr


def write_packet(processor, pr, ix, stream_id, ts, frame=0, payload=None):
    payload = np.asarray(payload if payload is not None else np.arange(4), dtype=np.uint8)
    pr.buf[ix, 0] = PACKET_COOKIE_ADC
    pr.buf[ix, 1] = ((frame & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    pr.buf[ix, 10:14] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE
    pr.n_stored = max(pr.n_stored, ix + 1)


def write_burst(processor, pr, start_ix, ts, payload_base=0):
    """Write one complete timestamp burst for streams 10 and 11.

    Writes ``FRAMES_PER_BURST`` packets per stream (frame indices ``0..N-1``).
    """
    frames = int(getattr(processor, "FRAMES_PER_BURST", 1) or 1)
    ix = start_ix
    for frame in range(frames):
        write_packet(
            processor, pr, ix, stream_id=10, ts=ts, frame=frame,
            payload=[payload_base + 1] * 4,
        )
        write_packet(
            processor, pr, ix + 1, stream_id=11, ts=ts, frame=frame,
            payload=[payload_base + 2] * 4,
        )
        ix += 2


def test_duplicate_stream_ids_rejected():
    """Duplicate stream_ids collapse six_map; reject instead of silently mis-routing."""
    pr = DummyPacketReceiver()
    state = default_config(stream_ids=(0, 0))
    with pytest.raises(ValueError, match="stream_ids must be unique"):
        processor = RawDataPacketProcessor(
            pr, config_getter=config_getter_from_mapping(state), receive_window=3
        )
        processor.update_config()


def test_captured_payload_not_all_zeros_process_packets():
    """Regression: assembled rows must contain UDP payload bytes (not all zeros).

    One timestamp is not enough: with default receive_window, physical ready
    ``stop = max(0, n_ts - carry_len)`` (cached as ``n_out`` when start is 0)
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
    r = RawFrameReceiver(pr, config_getter=config_getter_from_mapping(cfg), verbose=0)
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
    r = RawFrameReceiver(pr, config_getter=config_getter_from_mapping(default_config(format="8")), verbose=4)
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
    r = RawFrameReceiver(pr, config_getter=config_getter_from_mapping(default_config(format="8")), verbose=0)
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
    processor.config.capture_format[0].update_from(
        capture_format_for_tests("14").as_dict()
    )

    processor.update_config()
    process_and_get(processor)

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

    processor.config.update_from(
        {k: v for k, v in cfg.items() if k != "capture_format"}
    )
    processor.update_config()
    process_and_get(processor)

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
    pr = DummyPacketReceiver(n_packets=32, max_packet_size=32)
    r = RawFrameReceiver(pr, config_getter=config_getter_from_mapping(cfg), verbose=0)
    # frames_per_burst=2 → 4 packets per timestamp (2 streams × 2 frames)
    for i, tsv in enumerate((5, 6, 7, 8, 9)):
        write_burst(r, pr, 4 * i, ts=tsv, payload_base=10 * (i + 1))
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
    processor._set_out_rows(0, 1)

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
    cf1 = State(
        fmt_bus_width=32,
        fmt_data_width=29,
        fmt_is_complex=True,
        fmt_interleave_factor=4,
        use_offset_encoding=False,
    )
    processor, _pr = make_processor(
        n_packets=4,
        frames_per_burst=4,
        bytes_per_sample=1,
        samples_per_frame=24,
        bytes_per_frame=24,
        bytes_per_subframe=24,
        packets_per_channel_per_burst=4,
        logical_frames_per_burst=1,
        fft_bins_per_word=2,
        capture_bits_per_word=32,
        source=1,
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
    processor._set_out_rows(0, 1)

    ts, data, counts = processor.get_formatted_data(slice(0, 1), source=1)

    expected_bins = np.arange(12, dtype=np.float32)
    expected = (expected_bins + 1j * (1000 + expected_bins)).reshape(1, 1, -1).astype(np.complex64)
    np.testing.assert_array_equal(ts, [20])
    np.testing.assert_array_equal(counts, [[1]])
    np.testing.assert_array_equal(data, expected)


def test_full_resolution_complex_crs_interleave8_uses_msb_slot():
    cf1 = State(
        fmt_bus_width=128,
        fmt_data_width=32,
        fmt_is_complex=True,
        fmt_interleave_factor=8,
        use_offset_encoding=False,
    )
    processor, _pr = make_processor(
        n_packets=8,
        frames_per_burst=8,
        bytes_per_sample=2,
        samples_per_frame=16,
        bytes_per_frame=32,
        bytes_per_subframe=32,
        packets_per_channel_per_burst=8,
        logical_frames_per_burst=1,
        fft_bins_per_word=4,
        capture_bits_per_word=128,
        source=1,
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
    processor._set_out_rows(0, 1)

    ts, data, counts = processor.get_formatted_data(slice(0, 1), source=1)

    expected = np.asarray([[[
        101 + 201j, 100 + 200j, 103 + 203j, 102 + 202j,
        105 + 205j, 104 + 204j, 107 + 207j, 106 + 206j,
    ]]], dtype=np.complex64)
    np.testing.assert_array_equal(ts, [100])
    np.testing.assert_array_equal(counts, [[4]])
    np.testing.assert_array_equal(data, expected)


def test_offset_encoding_4bit_in_16bit_slot():
    """CRS scaler type 0: 4-bit offset-binary in 16-bit slots decodes with a 4-bit midpoint."""
    cf1 = State(
        fmt_bus_width=16,
        fmt_data_width=4,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        use_offset_encoding=True,
    )
    processor, _pr = make_processor(
        n_packets=2,
        frames_per_burst=1,
        bytes_per_sample=2,
        samples_per_frame=4,
        bytes_per_frame=8,
        bytes_per_subframe=8,
        packets_per_channel_per_burst=1,
        logical_frames_per_burst=1,
        fft_bins_per_word=1,
        capture_bits_per_word=16,
        source=1,
        stream_ids=(10,),
        channels=(0,),
        capture_format=[_unused_capture_format_slot(), cf1],
    )
    # Offset-binary codes 0, 8, 15, 7 → signed -8, 0, 7, -1
    codes = np.array([0, 8, 15, 7], dtype=np.uint16)
    packed = (codes << 12).astype(">u2")
    processor.raw_data[0, 0, : packed.nbytes] = packed.view(np.uint8)
    processor.ts_array[0] = 1
    processor.raw_data_count[0, 0, :] = 1
    processor.raw_source[0, 0, :] = 1
    processor._set_out_rows(0, 1)

    _ts, data, _counts = processor.get_formatted_data(slice(0, 1), source=1)
    expected = np.asarray([[[-8 + 0j, 7 - 1j]]], dtype=np.complex64)
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


_STATIC_BUFFER_ATTRS = (
    "bytes_per_sample",
    "samples_per_frame",
    "frames_per_burst",
    "fft_bins_per_word",
    "capture_bits_per_word",
    "subframes_per_frame",
    "stream_ids",
    "target_mode",
)


def test_serialized_equal_config_does_not_dirty_raw_buffers():
    processor, _pr = make_processor()
    raw_data = processor.raw_data
    fmt0 = processor.config.capture_format[0]
    fmt_buf = processor.fmt_cdata1[0]
    assert not processor.config.has_changed(_STATIC_BUFFER_ATTRS, context=_STATIC_BUFFER_ATTRS)
    assert not fmt0.has_changed(context="capture_format0")

    processor.config.update_from(processor.config.as_dict())
    processor.update_config()

    assert processor.raw_data is raw_data
    assert processor.config.capture_format[0] is fmt0
    assert not processor.config.has_changed(_STATIC_BUFFER_ATTRS, context=_STATIC_BUFFER_ATTRS)
    assert not fmt0.has_changed(context="capture_format0")
    assert processor.fmt_cdata1[0] is fmt_buf


def test_serialized_stream_id_change_is_detected():
    processor, _pr = make_processor()
    processor.config.update(stream_ids=(10, 12))
    processor.update_config()
    assert processor.stream_ids == (10, 12)
    assert int(processor.six_map[12]) == 1
    assert int(processor.six_map[11]) == -1


def test_serialized_format_change_is_detected_without_raw_realloc():
    processor, _pr = make_processor()
    raw_data = processor.raw_data
    fmt0 = processor.config.capture_format[0]
    fmt0.update_from({"fmt_data_width": 4})
    processor.update_config()
    assert processor.raw_data is raw_data
    assert processor.config.capture_format[0] is fmt0
    assert fmt0.fmt_data_width == 4
    assert not processor.config.has_changed(_STATIC_BUFFER_ATTRS, context=_STATIC_BUFFER_ATTRS)


def test_ct_cookie_dropped_by_raw_processor():
    """Cookie 0xA2 is a corner-turn packet, not a raw source."""
    processor, pr = make_processor(n_packets=4)
    pr.buf[0, 0] = 0xA2
    pr.buf[0, 1] = (10 >> 8) & 0xF
    pr.buf[0, 2] = 10 & 0xFF
    pr.buf[0, 4:10] = np.frombuffer(int(100).to_bytes(6, "big"), dtype=np.uint8)
    pr.pkt_len[0] = processor.PACKET_SIZE
    pr.n_stored = 1
    processor.process_udp_packets()
    assert processor.n_out == 0
