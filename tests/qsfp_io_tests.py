"""Offline tests: QSFP I2C retry / page handling and the CGE SI array-method encodings (no hardware).

Run: pytest tests/qsfp_io_tests.py   (or python tests/qsfp_io_tests.py)
"""
import logging

from pychfpga.hardware.i2c_devices.qsfp import QSFP
from pychfpga.fpga_firmware.chfpga.ct_engine.cge_array_methods import CGEArrayMethods


class FakeEEPROM:
    """SFF-8636 memory with a page register at byte 127. `faults` is a list of exceptions raised by the next accesses."""

    def __init__(self, faults=()):
        self.page = 0
        self.mem = {p: bytearray(256) for p in (0, 3)}
        self.faults = list(faults)

    def _maybe_fail(self):
        if self.faults:
            raise self.faults.pop(0)

    def read(self, addr, length=1):
        self._maybe_fail()
        return bytes(self.mem[self.page if a >= 128 else 0][a] for a in range(addr, addr + length))

    def write(self, addr, data):
        self._maybe_fail()
        data = [data] if isinstance(data, int) else list(data)
        for i, d in enumerate(data):
            a = addr + i
            if a == 127:
                self.page = d
            else:
                self.mem[self.page if a >= 128 else 0][a] = d


def make_qsfp(faults=()):
    q = object.__new__(QSFP)
    q._qsfp_eeprom = FakeEEPROM(faults)
    q._logger = logging.getLogger('test')
    q.selected = []
    q.enable_i2c = q.selected.append
    q.is_present = lambda: True
    q.parent = 'fake'
    q.I2C_RETRY_DELAY = q.I2C_SELECT_DELAY = q.I2C_WRITE_SETTLE = 0
    return q


def test_read_retries_timeouts_and_restores_page():
    q = make_qsfp([TimeoutError('RPC 2 timed out'), OSError('Nack')])
    q._qsfp_eeprom.mem[3][234] = 0x66
    assert list(q.read(234, 1, page=3)) == [0x66]
    assert q._qsfp_eeprom.page == 0 and not q._page_dirty
    assert q.selected[-1] is False                       # ModSelL released


def test_gives_up_after_tries_and_marks_page_for_next_access():
    q = make_qsfp([OSError('Nack')] * (QSFP.I2C_TRIES * 2 + QSFP.I2C_TRIES * 5))
    try:
        q.read(234, 1, page=3)
        raise AssertionError('expected OSError')
    except OSError:
        pass
    q._qsfp_eeprom.faults.clear()
    q._qsfp_eeprom.page = 3                              # page register left on page 3 by the failed restore
    q._page_dirty = True
    q._qsfp_eeprom.mem[0][148] = ord('O')
    assert q.read(148, 1) == b'O'                        # page 0 selected again before the upper page-0 read
    assert q._qsfp_eeprom.page == 0


def test_lower_memory_and_page0_do_not_touch_page_register():
    q = make_qsfp()
    q._qsfp_eeprom.write = lambda addr, data, _w=q._qsfp_eeprom.write: (_ for _ in ()).throw(AssertionError('page write')) \
        if addr == 127 else _w(addr, data)
    q.read(2, 1)
    q.read(148, 16)                                      # flat-memory modules: no page-select write for page 0


def test_verified_nibble_write_and_adaptive_retrain():
    q = make_qsfp()
    q._qsfp_eeprom.mem[3][224] = 0xA7                    # max TX EQ 10 dB, RX emphasis 7 dB
    q.set_tx_input_eq([1, 2, 3, 4])
    assert q.get_tx_input_eq() == [1, 2, 3, 4]
    q._qsfp_eeprom.mem[3][241] = 0x5F
    assert q.retrain_tx_adaptive_eq() is True
    assert q._qsfp_eeprom.mem[3][241] == 0x5F            # enables put back, upper nibble kept
    q.set_tx_adaptive_eq(False)
    assert q._qsfp_eeprom.mem[3][241] == 0x50 and q.retrain_tx_adaptive_eq() is False


def test_retrain_restores_enables_after_failure():
    q = make_qsfp()
    q._qsfp_eeprom.mem[3][241] = 0x0F
    real = QSFP.write
    calls = []
    def flaky_write(self, addr, data, page=0, enable=True):
        calls.append(list(data))
        if len(calls) == 1:
            raise OSError('Nack on adaptive off')
        return real(self, addr, data, page=page, enable=enable)
    q.write = flaky_write.__get__(q)
    try:
        q.retrain_tx_adaptive_eq()
    except OSError:
        pass
    assert q._qsfp_eeprom.mem[3][241] == 0x0F


def test_read_optical_keeps_capability_and_enables_apart():
    q = make_qsfp()
    m0, m3 = q._qsfp_eeprom.mem[0], q._qsfp_eeprom.mem[3]
    m0[193], m3[224], m3[225], m3[241] = 0x0F, 0xA7, 0x03, 0x00      # adaptive-capable module, adaptive EQ off
    ib = type('IB', (), dict(serial='0001', GPU=object(), i2c1_qsfp=q))()
    arr = type('Arr', (), dict(ib=[ib]))()
    s = CGEArrayMethods(arr).read_optical(verbose=False)['0001']
    assert s['tx_adaptive_eq_capable'] is True and s['tx_adaptive_eq'] == [False] * 4


def test_read_optical_skips_empty_cage():
    q = make_qsfp([OSError('must not touch the EEPROM')] * 99)
    q.is_present = lambda: False
    ib = type('IB', (), dict(serial='0002', GPU=object(), i2c1_qsfp=q))()
    arr = type('Arr', (), dict(ib=[ib]))()
    assert CGEArrayMethods(arr).read_optical(verbose=False) == {'0002': {'present': False}}
    assert len(q._qsfp_eeprom.faults) == 99


def test_nearest_tx_codes():
    n = CGEArrayMethods.nearest_tx_codes
    assert n(950, 0.01, 0) == (24, 0, 0)
    assert n(720, 0.01, 1.3) == (13, 0, 6)
    assert n(390, 0.01, 6.69) == (0, 0, 21)
    assert n(1040, 0.81, 7.84) == (31, 4, 24)


def test_si_registers_by_name():
    q = make_qsfp()
    m0, m3 = q._qsfp_eeprom.mem[0], q._qsfp_eeprom.mem[3]
    m0[193] = 0x0F                                       # all SI controls; byte 2 bit 2 = 0: paged
    m3[224], m3[225], m3[241] = 0x77, 0x0F, 0xA0         # max 7 dB / 7 dB, amplitude codes 0-3
    assert q.get_si_capabilities()['max_tx_eq_db'] == 7
    q.set_tx_input_eq([1, 2, 3, 4])
    q.set_rx_output_emphasis(5)
    q.set_rx_output_amplitude(2)
    q.set_tx_adaptive_eq([True, False, False, True])
    assert list(m3[234:240]) == [0x12, 0x34, 0x55, 0x55, 0x22, 0x22]
    assert m3[241] == 0xA9 and q.get_tx_adaptive_eq() == [True, False, False, True]
    m0[3:6] = bytes([0x21, 0x00, 0x30])
    assert q.get_signal_flags() == dict(tx_los=2, rx_los=1, tx_adaptive_eq_fault=0, tx_fault=0, tx_cdr_lol=3, rx_cdr_lol=0)
    assert q._qsfp_eeprom.page == 0


def media_qsfp(ext=0, eth=0, tech=0, wavelength_nm=0, power_class_ext=0):
    """QSFP whose EEPROM holds the given media bytes (byte 192, byte 131, byte 147[7:4], bytes 186-187, byte 129)."""
    q = make_qsfp()
    m0 = q._qsfp_eeprom.mem[0]
    m0[129], m0[131], m0[147], m0[192] = power_class_ext, eth, tech << 4, ext
    m0[186:188] = (wavelength_nm * 20).to_bytes(2, 'big')
    return q


def test_fiber_type_sr4_is_multi_mode():
    q = media_qsfp(ext=0x02, tech=0x0, wavelength_nm=850)
    q._qsfp_eeprom.mem[0][130] = 0x0C  # MPO
    assert q.get_fiber_type() == 'multi-mode'


def test_fiber_type_lr4_is_single_mode():
    assert media_qsfp(ext=0x03, tech=0x4, wavelength_nm=1310).get_fiber_type() == 'single-mode'


def test_fiber_type_ignores_power_class():
    q = media_qsfp(ext=0x03, tech=0x4, wavelength_nm=1310, power_class_ext=0xC0)
    assert q.get_power_class() == 4 and q.get_fiber_type() == 'single-mode'
    q = media_qsfp(ext=0x02, wavelength_nm=850, power_class_ext=0xC0)
    assert q.get_power_class() == 4 and q.get_fiber_type() == 'multi-mode'


def test_fiber_type_falls_back_to_ethernet_code():
    assert media_qsfp(eth=1 << 1).get_fiber_type() == 'single-mode'  # 40G LR4
    assert media_qsfp(eth=1 << 2).get_fiber_type() == 'multi-mode'   # 40G SR4


def test_fiber_type_falls_back_to_laser_type():
    assert media_qsfp(tech=0x4).get_fiber_type() == 'single-mode'    # 1310 nm DFB, compliance bytes zero


def test_fiber_type_falls_back_to_wavelength():
    assert media_qsfp(wavelength_nm=850).get_fiber_type() == 'multi-mode'
    assert media_qsfp(wavelength_nm=1310).get_fiber_type() == 'single-mode'


def test_fiber_type_copper():
    assert media_qsfp(tech=0xA).get_fiber_type() == 'copper'         # DAC, as read on hardware (0xA0)


def test_fiber_type_blank_eeprom_is_unknown():
    assert media_qsfp().get_fiber_type() == 'unknown'


def test_fiber_type_unlisted_code_gives_no_answer():
    assert media_qsfp(ext=0x01).get_fiber_type() == 'unknown'                     # AOC: no fiber type in the code
    assert media_qsfp(ext=0xFE, wavelength_nm=1310).get_fiber_type() == 'single-mode'
    assert media_qsfp(ext=0x21, wavelength_nm=850).get_fiber_type() == 'multi-mode'  # BiDi: left to the wavelength


def test_fiber_type_ext_code_sets_do_not_overlap():
    assert not QSFP.SINGLE_MODE_EXT_CODES & QSFP.MULTI_MODE_EXT_CODES


def test_fiber_type_warns_when_bytes_disagree():
    msgs = []
    q = media_qsfp(ext=0x02, tech=0x4, wavelength_nm=1310)
    q._logger = type('L', (), dict(info=lambda *a: None, warning=lambda self, m, *a: msgs.append(m % a)))()
    assert q.get_fiber_type() == 'multi-mode' and 'disagree' in msgs[0]


def test_fiber_type_empty_cage():
    q = make_qsfp([OSError('must not touch the EEPROM')])
    q.is_present = lambda: False
    assert q.get_fiber_type() is None and len(q._qsfp_eeprom.faults) == 1


def test_expects_host_fec():
    assert media_qsfp(ext=0x06).expects_host_fec() and media_qsfp(ext=0x07).expects_host_fec()
    assert not media_qsfp(ext=0x03).expects_host_fec()


def test_power_class_is_none_when_eeprom_never_answers():
    q = make_qsfp([OSError('Nack')] * 99)
    assert q.get_power_class(retries=2) is None


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_'):
            fn()
            print('PASS', name)
