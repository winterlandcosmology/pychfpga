
import matplotlib.pyplot as plt
import numpy as np

from pychfpga.core.icecore.tests import *

# from pychfpga.core.icecore import IceBoardPlus, IceBoardPlusHandler, IceCrate, HardwareMap
# from pychfpga.MGADC08 import MGADC08
# from pychfpga.core.chFPGA_controller import chFPGA_controller
from pychfpga.core.chFPGA_receiver import chFPGA_receiver
# from pychfpga.core.icecore.session import load_session as load_yaml

class ScalerTests(TestGroup):
    '''Tests the chFPGA SCALER operation.

    This is the top-level Quality Control script for the SCALER tests.
    '''

    def test_list(self):
        yield self.chfpga_init
        yield self.bypass_tests

    def chfpga_init(self, ib):
        """ Initialize chFPGA firmware.
        """
        ib.open()
        config = ib.get_config()
        ib.r = chFPGA_receiver(config)

        yield PASSED(True)

    def bypass_tests(self, ib):
        """ Scaler bypass tests.

        Check that the function generator data can be routed directly to the
        scaler output when the FFT and SCALER is bypassed.
        """
        ib.set_data_source('funcgen')
        ib.set_funcgen_function('a', a=1)
        ib.set_fft_bypass(True)
        ib.set_scaler_bypass(True)
        ib.start_data_capture(period=0.5, source='scaler')

        yield self.PASSED
        yield """ Testing Scaler bypass """
        ib.r.flush()  #
        data = ib.r.read_frames()  # Read data from all channels
        data = ib.r.read_frames()  # Read data from all channels
        for ch in range(16):
            if any(data[ch]):
                yield 'Ch%i contains non-zero data' % ch
                yield self.FAILED
            else:
                yield 'Ch%i data is all zero' % ch

# class I2CTests(TestGroup):
#     '''I2C tests'''

#     def test_list(self):
#         yield self.test_eeprom_presence
#         yield self.get_eeprom_serial_number
#         yield self.test_image

#     def test_eeprom_presence(self, ib):
#         """ Check is the backplane is present.

#         For this we check if the backplane EEPROM responds to a dummy I2C command.
#         """
#         is_present = ib.is_backplane_present()
#         # yield is_present  # This is the PASS/FAIL criteria
#         # It would be clearer if we could just do
#         yield PASSED(is_present)

#         yield SUMMARY("Backplane is %spresent" % ('' if is_present else 'NOT '))

#         d= {1:1, 2:2, 3:3}
#         yield DETAILS(P('This was a good test. result is %r' % d))

#     def get_eeprom_serial_number(self, ib):
#         """ Get the backplane EEPROM serial number.
#         """
#         try:
#             serial = ib.bp.get_backplane_eeprom_serial_number()
#         except RuntimeError:
#             serial = None

#         yield PASSED(bool(serial)) # This is the PASS/FAIL criteria

#         yield SUMMARY("Backplane serial ]</div> is 0x%s" % (serial))
#         yield DETAILS(P('This was a good test'))
#         yield DETAILS(P('A very good test indeed'))
#         yield DETAILS('A test without P')
#         yield 'And a detail witout DETAIL'
#         yield """ This is a very long
#         multi-line comment. """
#         yield P(""" and this is
#         another one that doen't show""")
#         yield 123

#     def test_image(self, mezzanine):
#         '''Here's something that always fails, descriptively.'''

#         yield PASSED(False)

#         plt.figure(1)
#         plt.clf()
#         x = np.arange(1000)/100.
#         y = np.sin(x)
#         plt.plot(x,y)

#         yield PLOT('This is a sinewave that unequivoqually explains the test failure')


# class RailTests(TestGroup):
#     '''Voltage rails'''

#     def gen(self):
#         def test_vadj(mezzanine):
#             '''VADJ rail tolerance'''
#             return self.test_rail(mezzanine, 'MEZZANINE_RAIL_VADJ', 2.5)

#         def test_3v3(mezzanine):
#             '''3V3 rail tolerance'''
#             return self.test_rail(mezzanine, 'MEZZANINE_RAIL_VCC3V3', 3.3)

#         def test_12v(mezzanine):
#             '''12V rail tolerance'''
#             return self.test_rail(mezzanine, 'MEZZANINE_RAIL_VCC12V0', 12)

#         yield test_3v3
#         yield test_vadj
#         yield test_12v

#     def test_rail(self, mezzanine, rail, nom):
#         '''Testing mezzanine rail.'''

#         lo = nom*.95
#         hi = nom*1.05
#         got = mezzanine.get_mezzanine_voltage(rail)

#         if got < lo:
#             return False, \
#                 SUMMARY("%.2fv rail: Measured %.2f, lower bound %.2f" % \
#                         (nom, got, lo))
#         if got > hi:
#             return False, \
#                 SUMMARY("%.2fv rail: Measured %.2f, upper bound %.2f" % \
#                         (nom, got, hi))
#         return True


# class BasicTests(TestGroup):
#     '''Basic tests

#     This is a pretty degenerate test case right now, but it will eventually do
#     things that test the mezzanine's communications etc.
#     '''

#     def gen(self):
#         yield self.test_serial
#         yield self.test_something_that_fails

#     def test_serial(self, mezzanine):
#         '''Ensure the IPMI serial number matches the HWM.'''
#         expected = mezzanine.serial
#         got = mezzanine._get_mezzanine_serial()
#         if expected==got:
#             return True
#         return False, SUMMARY("got %s, expected %s" % (got, expected))



