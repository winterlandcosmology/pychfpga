""" ``nosetests`` test suite for testing various aspects of the ADC synchronization using ADC SYNC line.

This suite uses the XReport plugin to generate nicely formatted reports (with plots)

Run the desired testcase with::

   python xreport.py test_sync:TestSyncDelay --xfile results/test_sync --xargs --iceboards 10.10.10.7

"""
import matplotlib
matplotlib.use('Agg')
import unittest
# from matplotlib import pyplot as plt
from pychfpga.core.icecore import XReport as xr  # provides access to the Xreport extended features
import chime_args

class TestSyncDelay(unittest.TestCase):
    """ Report and tests the ADC SYNC delay adjustemt.
    """

    def setUp(self):
        """ Prepare the test for execution: get command line args and create the hardware map.
        """
        print 'Setting up tests with SetUp() using xargs=', xr.xargs
        self.args = chime_args.ChimeArgs(xr.xargs, force=0, init=1)
        print 'The parsed arguments are ', self.args
        print 'Finished setting up the test. We now proceed with the test method.'
        xr.flush()  # start a new block of text to make the report look nicer

    def test_delays(self):
        """ This will measure and report the optimal sync delay for every mezzanine of every IceBoard  in the hardware map """

        for ib in self.args.ib:
            try:
                ib.REFCLK.compute_sync_delay(plot_adc=0, channels=range(16))
                xr.insert_plot('ADC SYNC delays for %r' % ib)
            except:
                print "Failed sync test on iceboard %r" %ib

if __name__ == '__main__':
    xr.run_from_module()  # Run nosetests on this module with the Xreport plugin