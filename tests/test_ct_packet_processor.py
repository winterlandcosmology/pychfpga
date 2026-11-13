"""Corner-turn packet processor: cookie 0xA2, one packet size, nibble unpack."""

import numpy as np

from pychfpga.common.ct_packet_processor import CTPacketProcessor
from pychfpga.common.state import State

from test_raw_data_processor import (
    DummyPacketReceiver,
    config_getter_from_mapping,
    process_and_get,
)


CT_STREAM_ID = 0x015
CT_HEADER_WORDS = 4
CT_DATA_WORDS = 2
CT_WORDS_PER_PACKET = CT_HEADER_WORDS + CT_DATA_WORDS + 1


def make_ct_processor(n_packets=16, frames_per_burst=1, receive_window=3, **kwargs):
    payload_bytes = 4 * CT_WORDS_PER_PACKET
    state = State(
        stream_ids=(CT_STREAM_ID,),
        frames_per_burst=frames_per_burst,
        bytes_per_frame=payload_bytes,
        bytes_per_subframe=payload_bytes,
        subframes_per_frame=1,
        bytes_per_sample=4,
        samples_per_frame=CT_WORDS_PER_PACKET,
        fft_bins_per_word=1,
        capture_bits_per_word=32,
        target_mode=0,
        source=0,
        header_words=CT_HEADER_WORDS,
        data_words=CT_DATA_WORDS,
        four_bits=True,
        use_offset_binary=False,
        capture_format=[
            State(
                fmt_is_complex=False,
                fmt_bus_width=32,
                fmt_data_width=32,
                fmt_interleave_factor=1,
                use_offset_encoding=False,
            )
        ],
    )
    if kwargs:
        state.update(**kwargs)
    pr = DummyPacketReceiver(n_packets=n_packets, max_packet_size=64)
    processor = CTPacketProcessor(
        pr, config_getter=config_getter_from_mapping(state), receive_window=receive_window)
    processor.update_config()
    return processor, pr


def write_ct_packet(
        processor, pr, ix, ts, burst_index=0, stream_id=CT_STREAM_ID,
        data_words=(0, 0), pkt_len=None):
    words = np.zeros(CT_WORDS_PER_PACKET, dtype=np.uint32)
    words[0] = (stream_id << 16) | (1 << 12) | (CT_HEADER_WORDS << 8) | 0xCF
    words[3] = int(ts) & 0xFFFFFFFF
    words[CT_HEADER_WORDS:CT_HEADER_WORDS + CT_DATA_WORDS] = np.asarray(data_words, dtype=np.uint32)
    pr.buf[ix, 0] = CTPacketProcessor.PACKET_COOKIE
    pr.buf[ix, 1] = ((burst_index & 0xF) << 4) | ((stream_id >> 8) & 0xF)
    pr.buf[ix, 2] = stream_id & 0xFF
    pr.buf[ix, 3] = 0
    pr.buf[ix, 4:10] = np.frombuffer(int(ts).to_bytes(6, "big"), dtype=np.uint8)
    payload = words.astype(">u4").view(np.uint8)
    pr.buf[ix, 10:10 + payload.size] = payload
    pr.pkt_len[ix] = processor.PACKET_SIZE if pkt_len is None else pkt_len
    pr.n_stored = max(pr.n_stored, ix + 1)


def test_ct_cookie_accepted_and_decoded():
    processor, pr = make_ct_processor()
    assert processor.PACKET_SIZE == 10 + 4 * CT_WORDS_PER_PACKET
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x7F8801F0, 0x12345678))
    result = process_and_get(processor)
    assert result is not None
    ts, data, counts = result
    assert data.dtype == np.complex64
    assert data.shape == (1, 1, 4 * CT_DATA_WORDS)
    np.testing.assert_array_equal(ts, [100])
    np.testing.assert_array_equal(counts, [[1]])
    expected = np.asarray([
        -1 + 0j, 0 + 1j, -8 - 8j, 7 - 1j,
        7 - 8j, 5 + 6j, 3 + 4j, 1 + 2j,
    ], dtype=np.complex64)
    np.testing.assert_array_equal(data[0, 0], expected)


def test_ct_offset_binary_decode():
    processor, pr = make_ct_processor(use_offset_binary=True)
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x00F78888, 0))
    _ts, data, _counts = process_and_get(processor)
    expected_first = np.asarray([0 + 0j, 0 + 0j, 7 - 1j, -8 - 8j], dtype=np.complex64)
    np.testing.assert_array_equal(data[0, 0, :4], expected_first)


def test_ct_format32_returns_raw_words():
    processor, pr = make_ct_processor()
    write_ct_packet(processor, pr, 0, ts=100, data_words=(0x7F8801F0, 0x12345678))
    processor.process_udp_packets()
    _ts, data, _counts = processor.get_formatted_data(
        slice(0, processor.n_out), format="32")
    assert data.shape == (1, 1, CT_WORDS_PER_PACKET)
    words = data[0, 0].astype(np.int64) & 0xFFFFFFFF
    assert words[0] & 0xFF == 0xCF
    assert (words[0] >> 8) & 0xF == CT_HEADER_WORDS
    assert (words[0] >> 12) & 0xF == 1
    assert words[0] >> 16 == CT_STREAM_ID
    assert words[4] == 0x7F8801F0
    assert words[5] == 0x12345678
    assert words[6] == 0


def test_ct_burst_split_rows_and_timestamps():
    processor, pr = make_ct_processor(frames_per_burst=2)
    write_ct_packet(processor, pr, 0, ts=100, burst_index=0, data_words=(0x01010101, 0))
    write_ct_packet(processor, pr, 1, ts=100, burst_index=1, data_words=(0x02020202, 0))
    ts, data, counts = process_and_get(processor)
    np.testing.assert_array_equal(ts, [100, 101])
    assert data.shape == (2, 1, 4 * CT_DATA_WORDS)
    np.testing.assert_array_equal(data[0, 0, :4], np.full(4, 0 + 1j, dtype=np.complex64))
    np.testing.assert_array_equal(data[1, 0, :4], np.full(4, 0 + 2j, dtype=np.complex64))
    np.testing.assert_array_equal(counts, [[1], [1]])


def test_ct_wrong_length_dropped():
    processor, pr = make_ct_processor()
    write_ct_packet(processor, pr, 0, ts=100, pkt_len=processor.PACKET_SIZE - 4)
    processor.process_udp_packets()
    assert processor.n_out == 0
