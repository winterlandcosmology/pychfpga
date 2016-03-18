
import matplotlib.pyplot as plt
import numpy as np

from pychfpga.core.icecore.tests import *


class SFPTests(TestGroup):
    '''1000Base-T PHY Operational tests.

    This is the top-level Test group for the 1000Base-T SFP interface to the FPGA.
    '''

    def test_list(self):
        yield self.open_tests

    def boot_tests(self, iceboards=None, **kwargs):
        """ Post-config FPGA ping test.

        This test reprograms the FPGA in all the provided IceBoards and check
        that the FPGA UDP link can be established every time.
        """
        # print 'allo'
        # yield self.FAILED
        print_flush()
        for iteration in range(100):
            iceboards.set_fpga_bitstream(force=1)
            sfp_status_vector = iceboards.fpga_mmi_read(4*19)
            ping_result = iceboards.ping_fpga()
            cnt1 = iceboards.mmi.read(0x80000 + 34, length=2)
            ping_result2 = iceboards.ping_fpga()
            cnt2 = iceboards.mmi.read(0x80000 + 34, length=2)
            for i, ib in enumerate(iceboards):
                s = 'Iteration %2i IceBoard SN%s (Crate SN%s Slot %2i): ping1=%5s (Rx=%3i Tx=%3i), ping2=%5s (Rx=%3i Tx=%3i), status_vector=0x%04X (%s)' % (iteration, ib.serial, ib.crate.serial, ib.slot, bool(ping_result[i]),cnt1[i][0], cnt1[i][1], bool(ping_result2[i]), cnt2[i][0], cnt2[i][1], sfp_status_vector[i], self.get_sfp_status_string(sfp_status_vector[i]))
                print s
                print_flush()
                yield DETAILS(TT(s))
                yield PASSED(ping_result[i] and ping_result2[i])
                if not ping_result[i]:
                    return

    def get_sfp_status_string(self, status_vector):
        sv = [int(bool(status_vector & (1<<bit))) for bit in range(16)]
        return ('Lnk SYNC=%i, Rstdone Tx=%i Rx=%i all=%i; MMCM lck=%i; Rst PMA=%i MMCM=%i sys=%i; RUDI C=%i I=%i' % (
            sv[1],
            sv[7], sv[8], sv[0],
            sv[5],
            sv[4], sv[10], sv[9],
            sv[2], sv[3]))

    def open_tests(self, iceboards=None, **kwargs):
        """ 'open()' reliability test

        This test repeatedly reprograms the FPGA on the selected boards and execute the open() command, then checks if the command counts are valid.
        This checks for communication errors (corrupted or dropped packets) as well as errors caused during initialization (I2C errors etc).
        """
        print_flush()
        for iteration in range(10):
            iceboards.close()
            iceboards.set_fpga_bitstream(force=1)
            if not all (iceboards.ping_fpga()):
                raise RunTimeError('Cannot Ping the FPGA!')
            iceboards.check_command_count(reset=True)
            open_result = iceboards.open()
            cmd_result = iceboards.check_command_count()

            for i, ib in enumerate(iceboards):
                r = open_result[i] is None and cmd_result[i]
                s = 'Iteration %2i IceBoard SN%s (Crate SN%s Slot %2i): open=%5s, command_count=%5s ' % (iteration, ib.serial, ib.crate.serial, ib.slot, bool(open_result[i]), bool(cmd_result[i]))
                print s
                print_flush()
                yield DETAILS(TT(s))
                yield PASSED(r)
                if not r:
                    return

