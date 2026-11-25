"""capture_frame_time_async: trigger without read-modify-write, and re-arm on a missed trigger."""
import asyncio

import pytest

from pychfpga.fpga_firmware.chfpga import chfpga as chfpga_module
from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA

SAMPLE2 = chFPGA._IRIGB_SAMPLE2_ADDR
TARGET1 = chFPGA._IRIGB_TARGET1_ADDR
DONE = 1 << 30


class FakeIrigTime:
    def astype(self, format):
        return 'captured'


class FakeBoard(chFPGA):
    """chFPGA with fake core registers. `done_after` lists, per trigger, how many polls of the
    done bit read 0 before it reads 1 (None: never)."""

    def __init__(self, register12=0b10 << 30, done_after=(0,)):
        self.registers = {SAMPLE2: register12}
        self.done_after = list(done_after)
        self.reads = []
        self.writes = []
        self.triggers = 0
        self.polls = 0

    async def fpga_core_reg_read_async(self, addr):
        self.reads.append(addr)
        if addr == TARGET1:
            wait = self.done_after[min(self.triggers, len(self.done_after)) - 1]
            self.polls += 1
            return DONE if wait is not None and self.polls > wait else 0
        return self.registers.get(addr, 0)

    async def fpga_core_reg_write_async(self, addr, value):
        self.writes.append((addr, value))
        if addr == SAMPLE2 and value & (1 << 28) and not self.registers[SAMPLE2] & (1 << 28):
            self.triggers += 1
            self.polls = 0
        self.registers[addr] = value

    async def _get_irigb_time_async(self, trig=0):
        return FakeIrigTime()


def test_trigger_uses_the_cached_irigb_source_without_reading_register_12():
    board = FakeBoard(register12=0x0BAD_BAD0)  # garbage: must not be written back
    board._irigb_source_bits = 0b01 << 30

    assert asyncio.run(board.capture_frame_time_async(format='raw')) == (0, 'captured')

    assert SAMPLE2 not in board.reads
    assert board.writes[:2] == [(SAMPLE2, 0b01 << 30), (SAMPLE2, (0b01 << 30) | (1 << 28))]


def test_without_a_cached_source_only_the_writable_bits_are_kept():
    board = FakeBoard(register12=(0b10 << 30) | 0x0BAD_BAD0)

    asyncio.run(board.capture_frame_time_async(format='raw'))

    assert board.writes[:2] == [(SAMPLE2, 0b10 << 30), (SAMPLE2, (0b10 << 30) | (1 << 28))]


def test_missed_trigger_is_rearmed_once(monkeypatch):
    monkeypatch.setattr(chfpga_module.asyncio, 'sleep', _no_sleep)
    board = FakeBoard(done_after=(None, 0))  # first trigger missed, second one completes
    board._irigb_source_bits = 0b10 << 30

    assert asyncio.run(board.capture_frame_time_async(format='raw', timeout=0.05)) == (0, 'captured')
    assert board.triggers == 2


def test_two_missed_triggers_raise(monkeypatch):
    monkeypatch.setattr(chfpga_module.asyncio, 'sleep', _no_sleep)
    board = FakeBoard(done_after=(None, None))
    board._irigb_source_bits = 0b10 << 30

    with pytest.raises(RuntimeError, match='re-armed once'):
        asyncio.run(board.capture_frame_time_async(format='raw', timeout=0.05))
    assert board.triggers == 2


async def _no_sleep(seconds):
    await _yield()


async def _yield():
    return None
