"""FPGA UDP control-link recovery (clear_fpga_udp_errors) and its diagnostics."""
import asyncio
import logging
import socket

from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
from pychfpga.hardware.interfaces.fpga_mmi import FPGAMmi


class FakeUDP:
    """Replies in order; once they run out, every recv() times out (an empty socket)."""
    TimeoutException = socket.timeout

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.sent = []
        self.timeout = 0.5

    def send(self, data):
        self.sent.append(bytes(data))

    def recv(self):
        if not self.replies:
            raise socket.timeout()
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def get_timeout(self):
        return self.timeout

    def set_timeout(self, timeout):
        self.timeout = timeout


def make_mmi(replies=()):
    mmi = FPGAMmi("10.10.3.7", 41000)
    mmi.udp = FakeUDP(replies)
    return mmi


def test_ignored_read_returns_without_error_and_restores_timeout():
    # retry=-1 is the "send one read and ignore the answer" used right after a UDP stack reset.
    # It raised TypeError (a str copied into the reply buffer) from 2023 (c384388d) on.
    mmi = make_mmi([socket.timeout()])

    assert mmi.read(0, type=int, length=1, retry=-1, resync=1) is None
    assert mmi.udp.timeout == 0.5


class FakeMotherboard:
    def __init__(self, status=0x0002_0003):
        self.status = status
        self.spi_writes = []
        self.sfp_resets = 0

    def get_id(self, **kwargs):  # used by repr(chFPGA)
        return (0, 7)

    async def reset_sfp(self):
        self.sfp_resets += 1

    async def fpga_core_reg_spi_write_async(self, addr, value):
        self.spi_writes.append((addr, value))

    async def fpga_core_reg_spi_read_async(self, addr):
        return self.status


def make_board(replies=(), command_count=(0x42, 0x41)):
    board = object.__new__(chFPGA)
    board.BSB_TRANSPORT = chFPGA.BSB_TRANSPORT_UDP
    board.logger = logging.getLogger('test_fpga_udp_recovery')
    board.mb = FakeMotherboard()
    board.mmi = make_mmi(replies)
    board.mmi.error_counter = 50
    board.GPIO = type('GPIO', (), {'get_command_count': staticmethod(lambda: command_count)})()
    return board


def test_recovery_resynchronizes_the_command_counters(caplog):
    # After the reset send_counter is 0. The flush finds an empty socket, the ignored read is
    # command 1 (no reply), the checked read is command 2 and gets a matching reply.
    board = make_board(replies=[socket.timeout(), socket.timeout(), bytes([2, 0x00])])

    with caplog.at_level(logging.WARNING, logger='test_fpga_udp_recovery'):
        asyncio.run(board.clear_fpga_udp_errors())

    assert board.mb.sfp_resets == 1
    assert (board.mmi.send_counter, board.mmi.recv_counter) == (0x42, 0x41)
    assert board.mmi.error_counter == 0
    assert 'Still obtaining' not in caplog.text
