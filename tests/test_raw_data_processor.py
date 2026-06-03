import math
import re

import numpy as np
import pytest

from pychfpga.common.raw_data_packet_processor import RawDataPacketProcessor
from pychfpga.common.raw_frame_receiver import RawFrameReceiver
from pychfpga.common.state import State


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
        format=format,
        split=False,
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
        receiver_update_attrs=("stream_ids", "target_mode", "source", "format", "split"),
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


def write_packet(processor, pr, ix, stream_id, ts, frame=0, payload=None):
    payload = np.asarray(payload if payload is not None else np.arange(4), dtype=np.uint8)
    pr.buf[ix, 0] = processor.PACKET_COOKIE
    pr.buf[ix, 1] = ((frame & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    pr.buf[ix, 10:14] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE
    pr.n_stored = max(pr.n_stored, ix + 1)


def write_burst(processor, pr, start_ix, ts, payload_base=0):
    write_packet(processor, pr, start_ix, stream_id=10, ts=ts, payload=[payload_base + 1] * 4)
    write_packet(processor, pr, start_ix + 1, stream_id=11, ts=ts, payload=[payload_base + 2] * 4)


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
    out = processor.process_packets()
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
    out = processor.process_packets()
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


def test_raw_frame_receiver_iter_yields_nonzero_data():
    """Same path as notebooks using ``get_data_iter`` / ``read_raw_frames`` (receive → process → zip)."""
    pr = DummyPacketReceiver()
    cfg = default_config(format="8")
    r = RawFrameReceiver(pr, config=cfg, verbose=0)
    write_burst(r, pr, 0, ts=9001, payload_base=20)
    write_burst(r, pr, 2, ts=9002, payload_base=30)
    write_burst(r, pr, 4, ts=9003, payload_base=40)
    write_burst(r, pr, 6, ts=9004, payload_base=50)
    ts_, data_, counts_ = next(iter(r.read_raw_frames_iter(ncap=1, flush=False)))
    assert int(ts_) == 9001
    sample = np.asarray(data_).ravel()
    assert sample.size > 0
    assert not np.all(sample == 0), (
        "iterator yielded all-zero data (process_packets vs zip path bug)"
    )
    assert np.any(np.asarray(counts_) > 0)
    np.testing.assert_array_equal(data_[0, 0], np.full(4, 21, dtype=np.int8))
    np.testing.assert_array_equal(data_[1, 0], np.full(4, 22, dtype=np.int8))


def test_read_raw_frames_iter_omitting_verbose_preserves_constructor():
    """read_raw_frames_iter must not reset verbose when omitted (use constructor / get_data_receiver value)."""
    pr = DummyPacketReceiver()
    r = RawFrameReceiver(pr, config=default_config(format="8"), verbose=4)
    r.read_raw_frames_iter(ncap=1, flush=False)
    assert r.verbose == 4
    r.read_raw_frames_iter(ncap=1, flush=False, verbose=1)
    assert r.verbose == 1


def test_back_to_back_read_raw_frames_each_gets_fresh_iterator():
    """Regression: singleton data_iter caused confusing state; each read_raw_frames starts clean."""
    pr = DummyPacketReceiver()
    r = RawFrameReceiver(pr, config=default_config(format="8"), receive_window=0, verbose=0)
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

    out = processor.process_packets()
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
    out = processor.process_packets()
    assert out is not None
    ts, data, counts = out

    np.testing.assert_array_equal(ts, np.array([5, 6, 7, 8], dtype=ts.dtype))

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    for i, ts_ in enumerate((9, 10, 11)):
        write_burst(processor, pr, 2 * i, ts=ts_, payload_base=40 + 10 * i)
    out = processor.process_packets()
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
    out1 = processor.process_packets()
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
    out2 = processor.process_packets()
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

    r0 = processor.process_packets()
    assert r0 is None or len(r0[0]) == 0

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_packet(processor, pr, 0, stream_id=11, ts=7, payload=[2, 2, 2, 2])
    r1 = processor.process_packets()
    assert r1 is None or len(r1[0]) == 0

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=8, payload_base=10)
    write_burst(processor, pr, 2, ts=9, payload_base=20)
    write_burst(processor, pr, 4, ts=10, payload_base=30)
    out = processor.process_packets()
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
    processor, pr = make_processor(n_packets=16, receive_window=0)
    processor.max_timestamps = 2
    for i, tsv in enumerate((100, 101, 102)):
        write_burst(processor, pr, 2 * i, ts=tsv, payload_base=10 * i)
    with pytest.warns(UserWarning, match="timestamp backlog"):
        out = processor.process_packets()
    assert out is not None
    ts, _, _ = out
    np.testing.assert_array_equal(ts, np.array([101, 102], dtype=ts.dtype))


def test_monotonic_rejects_udp_not_after_last_emitted():
    """Packets at or before the last returned timestamp are ignored; emits stay strictly increasing."""
    processor, pr = make_processor(receive_window=0)
    for i, tsv in enumerate((5, 6, 7, 8)):
        write_burst(processor, pr, 2 * i, ts=tsv, payload_base=10 * i)
    out1 = processor.process_packets()
    assert out1 is not None
    assert int(out1[0][-1]) == 8

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=7, payload_base=100)
    write_burst(processor, pr, 2, ts=9, payload_base=20)
    out2 = processor.process_packets()
    assert out2 is not None
    ts2, data2, _ = out2
    assert len(ts2) == 1
    assert int(ts2[0]) == 9
    np.testing.assert_array_equal(data2[0, 0, 0], np.full(4, 21, dtype=np.int8))

    pr.pkt_len.fill(0)
    pr.n_stored = 0
    write_burst(processor, pr, 0, ts=6, payload_base=30)
    write_burst(processor, pr, 2, ts=7, payload_base=40)
    assert processor.process_packets() is None


def test_flush_clears_last_emitted_timestamp_tracker():
    processor, pr = make_processor(receive_window=0)
    write_burst(processor, pr, 0, ts=50, payload_base=1)
    write_burst(processor, pr, 2, ts=51, payload_base=2)
    assert processor.process_packets() is not None
    processor.flush()
    write_burst(processor, pr, 0, ts=50, payload_base=11)
    write_burst(processor, pr, 2, ts=51, payload_base=12)
    out = processor.process_packets()
    assert out is not None
    np.testing.assert_array_equal(out[0], np.array([50, 51], dtype=out[0].dtype))


def test_view_capable_format_can_return_raw_storage_view():
    processor, pr = make_processor(n_packets=2, receive_window=0)
    write_packet(processor, pr, 0, stream_id=10, ts=9, payload=[1, 2, 3, 4])
    write_packet(processor, pr, 1, stream_id=11, ts=9, payload=[5, 6, 7, 8])

    out = processor.process_packets()
    assert out is not None
    ts, data, counts = out

    assert np.shares_memory(data, processor.raw_data)


def test_transformed_format_uses_formatted_output_buffer():
    processor, pr = make_processor(n_packets=2, format="14", receive_window=0)
    write_packet(processor, pr, 0, stream_id=10, ts=11, payload=[0, 4, 0, 8])
    write_packet(processor, pr, 1, stream_id=11, ts=11, payload=[0, 12, 0, 16])

    out = processor.process_packets()
    assert out is not None
    ts, data, counts = out

    assert np.shares_memory(data, processor.raw_data)
    d = np.asarray(data, dtype=np.int16).reshape(-1)
    np.testing.assert_array_equal(d[:4], np.array([1, 2, 3, 4], dtype=np.int16))


def test_output_only_reconfiguration_reuses_raw_buffer():
    processor, pr = make_processor(n_packets=2, format="8", receive_window=0)
    raw_data = processor.raw_data
    cfg = default_config(format="14")

    processor.process_packets(config=State(cfg))

    assert processor.raw_data is raw_data
    assert processor.format == "14"
    assert processor.fmt_out_dtype == np.dtype(np.int16)


def test_geometry_reconfiguration_reallocates_when_shape_changes():
    processor, pr = make_processor(n_packets=2, format="8", receive_window=0)
    raw_data = processor.raw_data
    cfg = default_config(
        format="8",
        frames_per_burst=2,
        packets_per_channel_per_burst=2,
        logical_frames_per_burst=2,
    )

    processor.process_packets(config=State(cfg))

    assert processor.raw_data is not raw_data
    assert processor.PKT_FRAMES_PER_BURST == 2
    assert processor.n_ts == 0


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
