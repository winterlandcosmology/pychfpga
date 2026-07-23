import socket
import __main__
import logging
from types import SimpleNamespace

import pytest

from pychfpga.fpga_firmware.chfpga import chfpga as chfpga_module
from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
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


def test_send_command_waits_for_matching_reply_after_stale_packet():
    mmi = FPGAMmi("10.10.3.103", 41000)
    mmi.send_counter = 0xCF
    mmi.udp = FakeUDP([
        bytes([0xCF, 0x00]),
        bytes([0xD0, 0x42]),
    ])

    result = mmi._send_command(b"\x41\x06\x00", expected_reply_length=1, retry=1)

    assert result == b"\x42"
    assert mmi.udp.sent == [b"\x41\x06\x00"]
    assert mmi.recv_counter == 2


class FakeBoard:
    interface_ip_addr = "127.0.0.1"
    logger = logging.getLogger(__name__)
    _data_socket = None
    BSB_TRANSPORT = "udp"
    BSB_TRANSPORT_UDP = "udp"

    def __init__(self, control_port):
        self.local_control_port_number = control_port
        self.data_socket = None

    async def get_local_data_port_number_async(self):
        return self.local_control_port_number

    def set_data_socket(self, sock):
        self.data_socket = sock


class FakeSocket:
    def __init__(self, *args):
        self.address = None

    def bind(self, address):
        ip_addr, port = address
        self.address = (ip_addr, port or 50000)

    def getsockname(self):
        return self.address

    def close(self):
        pass


def test_implicit_data_socket_does_not_reuse_mmi_control_port(monkeypatch):
    control_socket = object()
    control_port = 41000
    board = FakeBoard(control_port)
    previous = getattr(__main__, "__opened_sockets__", None)
    __main__.__opened_sockets__ = {control_port: control_socket}
    monkeypatch.setattr(
        chfpga_module,
        "socket",
        SimpleNamespace(
            socket=FakeSocket,
            AF_INET=socket.AF_INET,
            SOCK_DGRAM=socket.SOCK_DGRAM,
        ),
    )
    data_socket = None
    try:
        data_socket = chFPGA.get_data_socket(board)
        assert data_socket is not control_socket
        assert data_socket.getsockname()[1] != control_port
    finally:
        if previous is None:
            del __main__.__opened_sockets__
        else:
            __main__.__opened_sockets__ = previous


def test_explicit_data_socket_rejects_mmi_control_port():
    board = FakeBoard(control_port=41000)

    with pytest.raises(ValueError, match="MMI control socket"):
        chFPGA.get_data_socket(board, port_number=41000)
