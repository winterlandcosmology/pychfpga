import logging
import matplotlib.pyplot as plt
import numpy as np


from command_line_helper import get_command_line_arguments

from pychfpga.core.icecore.tests.test_engine import *
from pychfpga.core.icecore import IceBoard, IceCrate
from pychfpga.core.icecore_ext.chfpga_handler import chFPGAHandler
from pychfpga.core.icecore.session import load_session as load_yaml

class MGK7BP16QC(TestGroup):
    '''MGK7BP16 16-slot 10 Gbps Full-mesh IceBoard backplane Quality Control.

    This is the top-level Quality Control script for MGK7BP16.
    '''

    def test_list(self):
        yield I2CTests()
        yield self.dummy_test

    def dummy_test(self, ib):
        """ Dummy test function.

        No description
        """
        yield PASSED(True)


class I2CTests(TestGroup):
    '''I2C tests'''

    def test_list(self):
        yield self.test_eeprom_presence
        yield self.get_eeprom_serial_number
        yield self.test_image

    def test_eeprom_presence(self, ib):
        """ Check is the backplane is present.

        For this we check if the backplane EEPROM responds to a dummy I2C command.
        """
        is_present = ib.is_backplane_present()
        # yield is_present  # This is the PASS/FAIL criteria
        # It would be clearer if we could just do
        yield PASSED(is_present)

        yield SUMMARY("Backplane is %spresent" % ('' if is_present else 'NOT '))

        d= {1:1, 2:2, 3:3}
        yield DETAILS(P('This was a good test. result is %r' % d))

    def get_eeprom_serial_number(self, ib):
        """ Get the backplane EEPROM serial number.
        """
        try:
            serial = ib.bp.get_backplane_eeprom_serial_number()
        except RuntimeError:
            serial = None

        yield PASSED(bool(serial)) # This is the PASS/FAIL criteria

        yield SUMMARY("Backplane serial ]</div> is 0x%s" % (serial))
        yield DETAILS(P('This was a good test'))
        yield DETAILS(P('A very good test indeed'))
        yield DETAILS('A test without P')
        yield 'And a detail witout DETAIL'
        yield """ This is a very long
        multi-line comment. """
        yield P(""" and this is
        another one that doen't show""")
        yield 123

    def test_image(self, mezzanine):
        '''Here's something that always fails, descriptively.'''

        yield PASSED(False)

        plt.figure(1)
        plt.clf()
        x = np.arange(1000)/100.
        y = np.sin(x)
        plt.plot(x,y)

        yield PLOT('This is a sinewave that unequivoqually explains the test failure')


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


if __name__=='__main__':

    # Get command line arguments
    args = get_command_line_arguments()

    # Set-up logging
    logger = logging.getLogger('')
    logger.setLevel(args.log_level)
    logger.addHandler(args.log_handler)

    # Associate the bitstream with the target Handler
    chFPGAHandler.register_fpga_bitstream(args.fpga_bitstream)

    # *** JFC: We should use the YAML file to provide information on the test
    yaml_string = """
        !HardwareMap
        - !IceBoard {{hostname: {0}, app_handler_name: "chFPGAHandler"}}
        """.format(args.iceboards[0])

    # Load the hardware map, which defines every piece of the hardware in the
    # array. For now, we have only one IceBoard.
    hwm = load_yaml(yaml_string)

    # Get the only Iceboard of the map
    ib = hwm.query(IceBoard).one()

    # Configure the FPGA with the bitstream associated with the handler
    if args.fpga_bitstream:
        ib.set_fpga_bitstream()

    # Get the backplane test engine and execute the tests
    te = MGK7BP16QC(context={
        "Date": datetime.datetime.now(),
        "Bitstream filename": args.bitfile,
        "Bitstream CRC32": '0x%08X' % ib.get_fpga_bitstream_crc(),
        "Master Iceboard hostname": args.iceboards[0],
        "Iceboard handler name": type(ib.handler).__handler_name__,
        })
    te.run(ib)
    te.write_html('qc.html')
    te.write_xml('qc.xml')
    print '\n'.join(te.synopsis_as_strings())
# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
