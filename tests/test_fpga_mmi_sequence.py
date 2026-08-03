import socket

from pychfpga.hardware.interfaces.fpga_mmi import FPGAMmi


class FakeUDP:
    TimeoutException = socket.timeout

    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []
        self.timeout = 0.5

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def get_timeout(self):
        return self.timeout

    def set_timeout(self, timeout):
        self.timeout = timeout


def make_mmi(replies):
    mmi = FPGAMmi("10.10.3.103", 41000)
    mmi.send_counter = 0xCF
    mmi.udp = FakeUDP(replies)
    return mmi


def test_send_command_waits_for_matching_reply_after_stale_packet():
    mmi = make_mmi([
        bytes([0xCF, 0x00]),
        bytes([0xD0, 0x42]),
    ])

    result = mmi._send_command(
        b"\x41\x06\x00", expected_reply_length=1, retry=1)

    assert result == b"\x42"
    assert mmi.udp.sent == [b"\x41\x06\x00"]
    assert mmi.recv_counter == 2


def test_send_command_waits_for_matching_length():
    mmi = make_mmi([
        bytes([0xD0, 0x42, 0x43]),
        bytes([0xD0, 0x44]),
    ])

    result = mmi._send_command(
        b"\x41\x06\x00", expected_reply_length=1, retry=1)

    assert result == b"\x44"
    assert mmi.udp.sent == [b"\x41\x06\x00"]
    assert mmi.recv_counter == 2


def test_send_command_resynchronizes_only_after_timeout():
    mmi = make_mmi([
        bytes([0xCF, 0x00]),
        socket.timeout(),
        bytes([0xD0, 0x42]),
    ])

    result = mmi._send_command(
        b"\x41\x06\x00", expected_reply_length=1, retry=2)

    assert result == b"\x42"
    assert mmi.udp.sent == [b"\x41\x06\x00", b"\x41\x06\x00"]
    assert mmi.recv_counter == 2
