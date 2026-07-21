import numpy as np

from pychfpga.common.ucorr_packet_processor import UCorrPacketProcessor


def test_store_packets_accumulates_repeated_destinations():
    processor = UCorrPacketProcessor.__new__(UCorrPacketProcessor)
    processor.overrun = 0
    processor.buf_flags = np.zeros(2, dtype=np.uint8)
    processor.pkt_bin_set = np.zeros(2, dtype=np.uint8)
    processor.pkt_corr = np.zeros(2, dtype=np.uint16)
    processor.pkt_bin = np.zeros(2, dtype=np.uint16)
    processor.temp32 = np.zeros((2, 2), dtype=np.int32)
    processor.buf_data_h = np.array(
        [[1 << 10, 2 << 10], [3 << 10, 4 << 10]], dtype=np.int32
    )
    processor.buf_data_l = np.array([[5, 6], [7, 8]], dtype=np.int32)
    processor.buf_data_sat = np.array(
        [[0x20, 0x10], [0x30, 0x00]], dtype=np.uint8
    )
    processor.acc_re = np.zeros((1, 1, 1, 1, 2), dtype=np.int64)
    processor.acc_im = np.zeros_like(processor.acc_re)
    processor.sat = np.zeros((1, 1, 1, 1, 2, 2), dtype=np.int32)
    processor.raw_data_count = np.zeros(1, dtype=np.int64)

    processor.store_packets(np.array([0, 1]), np.array([0, 0]))

    np.testing.assert_array_equal(processor.acc_re[0, 0, 0, 0], [4, 6])
    np.testing.assert_array_equal(processor.acc_im[0, 0, 0, 0], [12, 14])
    np.testing.assert_array_equal(processor.sat[0, 0, 0, 0, :, 0], [0x40, 0x00])
    np.testing.assert_array_equal(processor.sat[0, 0, 0, 0, :, 1], [0x10, 0x10])
    assert processor.raw_data_count[0] == 2


class _FlushCountingReceiver:
    def __init__(self):
        self.calls = []

    def flush(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 0


def test_flush_clears_ucorr_state_and_flushes_receiver_once():
    processor = UCorrPacketProcessor.__new__(UCorrPacketProcessor)
    processor.pr = _FlushCountingReceiver()
    processor.bin_map = np.array([0])
    processor.firmware_integ_period = 1
    processor.acc_re = np.ones((2, 1, 1, 1, 1), dtype=np.int64)
    processor.acc_im = np.ones_like(processor.acc_re)
    processor.sat = np.ones((2, 1, 1, 1, 1, 2), dtype=np.int32)
    processor.raw_data_count = np.ones(2, dtype=np.int64)
    processor.ts_array = np.ones(2, dtype=np.uint64)
    processor.n_ts = 2
    processor.carry_len = 1
    processor.n_out = 1
    processor._last_emitted_ts = np.uint64(42)
    processor.overrun = 1

    processor.flush(flush_timeout=0.25)

    assert processor.pr.calls == [((0.25,), {})]
    assert not processor.acc_re.any()
    assert not processor.acc_im.any()
    assert not processor.sat.any()
    assert not processor.raw_data_count.any()
    assert not processor.ts_array.any()
    assert processor.n_ts == 0
    assert processor.carry_len == 0
    assert processor.n_out == 0
    assert processor._last_emitted_ts == 0
    assert processor.overrun == 0
