import asyncio
import __main__
import logging
import socket
from types import SimpleNamespace

import numpy as np
import pytest

# vb/icebox predates NumPy 2 and imports the removed ``numpy.NaN`` alias in
# unrelated channelizer code while loading chFPGA.
if not hasattr(np, "NaN"):
    np.NaN = np.nan

from pychfpga.fpga_firmware.chfpga import chfpga as chfpga_module
from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA


class FakeBoard:
    interface_ip_addr = "127.0.0.1"
    logger = logging.getLogger(__name__)
    mb = SimpleNamespace(part_number="MGK7MB")

    def __init__(self, control_port):
        self.local_control_port_number = control_port
        self._data_socket = None
        self.programmed_data_port = control_port

    async def get_local_data_port_number_async(self):
        return self.programmed_data_port

    async def set_local_data_port_number_async(self, port):
        self.programmed_data_port = port


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
    try:
        data_socket = chFPGA.get_data_socket(board)
        assert data_socket is not control_socket
        assert data_socket.getsockname()[1] != control_port
        assert board.programmed_data_port == data_socket.getsockname()[1]
    finally:
        if previous is None:
            del __main__.__opened_sockets__
        else:
            __main__.__opened_sockets__ = previous


def test_explicit_data_socket_rejects_mmi_control_port():
    board = FakeBoard(control_port=41000)

    with pytest.raises(ValueError, match="MMI control socket"):
        chFPGA.get_data_socket(board, port_number=41000)


def test_clearing_stale_data_port_preserves_mac_bits():
    register = chFPGA._FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR
    control_port = 41000
    original_value = 0xA5B60000 | control_port
    writes = []

    async def read_reg(address):
        assert address == register
        return original_value

    async def write_reg(address, value):
        writes.append((address, value))

    board = SimpleNamespace(
        mb=SimpleNamespace(
            fpga_core_reg_spi_read_async=read_reg,
            fpga_core_reg_spi_write_async=write_reg,
        ),
        local_control_port_number=control_port,
        logger=logging.getLogger(__name__),
        _FPGA_DATA_DEST_MAC_ADDR_MSW_IP_PORT_ADDR=register,
    )

    asyncio.run(chFPGA._clear_stale_data_port_collision_async(board))

    assert writes == [(register, 0xA5B60000)]
