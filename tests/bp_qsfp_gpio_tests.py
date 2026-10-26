"""Offline: backplane QSFP expander U11 (PCAL6524) pin map and output enable, against a fake I2C bus."""
from pychfpga.hardware.crs.crs import CRS
from pychfpga.hardware.i2c_devices.gpio import GPIO
from pychfpga.hardware.i2c_devices.pca6524 import pca6524
from pychfpga.hardware.i2c_devices.qsfp import QSFP


class FakeI2C:
    """PCAL6524 registers after power-up: outputs high, every pin an input."""

    def __init__(self):
        self.regs = {0x04: 0xFF, 0x05: 0xFF, 0x06: 0xFF, 0x0C: 0xFF, 0x0D: 0xFF, 0x0E: 0xFF}
        self.addresses = set()

    def select_bus(self, bus):
        pass

    def write_read(self, address, data, read_length=0):
        self.addresses.add(address)
        if len(data) == 2:
            self.regs[data[0]] = data[1]
            return []
        return [self.regs.get(data[0], 0)]


def test_bp_qsfp_pins():
    u11 = pca6524(FakeI2C(), address=0x22, port=0)
    t = CRS.bp_qsfp_gpio_table(u11)
    assert t['P5A_ModSelL'][1:] == (1, 4, 1) and t['P5A_ModPrsL'][1:] == (1, 7, 1) and t['P5A_LPMode'][1:] == (2, 3, 1)
    assert t['P5B_ModSelL'][1:] == (0, 0, 1) and t['P5B_LPMode'][1:] == (2, 0, 1)
    assert t['P6A_ResetL'][1:] == (0, 5, 1) and t['P6A_LPMode'][1:] == (2, 1, 1)
    assert t['P6B_IntL'][1:] == (1, 2, 1) and t['P6B_LPMode'][1:] == (2, 2, 1)
    bits = {(e[1], e[2]) for e in t.values()}
    assert len(bits) == len(t) == 20                     # no pin used twice


def test_write_makes_only_that_pin_an_output():
    bus = FakeI2C()
    u11 = pca6524(bus, address=0x22, port=0)
    q = QSFP(bus, bus_name=0, gpio_prefix='P5A_', gpio=GPIO(CRS.bp_qsfp_gpio_table(u11)), address=0x50)
    q.enable_i2c(True)                                   # ModSelL (port 1 bit 4) low
    assert bus.regs[0x05] == 0xEF and bus.regs[0x0D] == 0xEF
    assert bus.regs[0x0C] == 0xFF and bus.regs[0x0E] == 0xFF
    q.set_control_bit('LPMode', 0)                       # port 2 bit 3
    assert bus.regs[0x06] == 0xF7 and bus.regs[0x0E] == 0xF7
    assert bus.addresses == {0x22}


def test_media_rule():
    assert CRS.qsfp_media(0x23, 0xA0) == 'copper'    # 1 m DAC: no separable connector, copper cable unequalized
    assert CRS.qsfp_media(0x00, 0x80) == 'copper'    # StarTech loopback plug, as read on hardware
    assert CRS.qsfp_media(0x0C, 0x00) == 'optical'   # SR4: MPO, 850 nm VCSEL, as read on hardware
    assert CRS.qsfp_media(0x07, 0x40) == 'optical'   # LR4: LC, 1310 nm DFB


def test_check_bp_qsfp_copper():
    """Copper or empty ports pass; an optical module is reported as a warning naming its port; a backplane without its position-1 board is skipped."""
    from pychfpga.apps.ber_dashboard.sfp_ber_tests import check_bp_qsfp_copper

    class Board:
        def __init__(self, slot, media=None):
            self.slot, self._media = slot, media
        def bp_qsfp_media(self):
            return self._media

    class Arr:
        def __init__(self, *boards): self.ib = list(boards)

    msgs = []
    ok = Arr(Board(1, {'P5A': 'copper', 'P5B': None, 'P6A': 'copper', 'P6B': 'copper'}), Board(2), Board(6))   # no board in slot 5
    assert check_bp_qsfp_copper(ok, log=msgs.append) == ({1: ok.ib[0]._media}, [])
    assert any('slot 5' in m and 'not checked' in m for m in msgs) and not any('WARNING' in m for m in msgs)
    msgs.clear()
    bad = Arr(Board(9, {'P5A': 'optical', 'P5B': 'copper', 'P6A': None, 'P6B': 'copper'}))
    assert check_bp_qsfp_copper(bad, log=msgs.append)[1] == [(9, 'P5A')]
    assert any(m.startswith('WARNING') and 'slot 9 P5A' in m for m in msgs)
