import pytest

from pychfpga.gps import SpectrumInstrumentsTM4D


def make_clock(sent):
    clk = SpectrumInstrumentsTM4D('tm4.invalid', verbose=0)
    clk.sock = object()  # pretend the socket is open so no connection is attempted
    clk.send = sent.append
    return clk


@pytest.mark.parametrize('mode', [0, 1, 2, 3])
def test_set_outputs_on_powerup_sends_command_30(mode):
    sent = []
    make_clock(sent).set_outputs_on_powerup(mode)
    assert sent == [f'#30,{mode}\r\n']


def test_irigb_with_year_is_time_code_format_2():
    # IRIG-B007 carries the BCD year that chFPGA decodes; the TM-4 reads it back as #71,2
    sent = []
    clk = make_clock(sent)
    clk.set_serial_time_code_format(2)
    assert sent == ['#16,2\r\n']
    assert clk.get_serial_time_code_format(reply='#71,2\r\n') == 2
