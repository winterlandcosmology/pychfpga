""" Example nosetest test suite that make use of the Xreport plugin features.

This is a standard nose test file, so you could run it normally with
``nosetests`` **if** the xreport plugin has been installed and registered::

   nosetests xreport_example --xfile test --xargs --iceboards 10.10.10.7   # Won't work unless you registered the plugin

An easier alternative is to run xreport module as a script, which will invoke nosetest but will automatically add the plugin::

   python xreport.py xreport_example --xfile test --xargs --iceboards 10.10.10.7   # Should work

This will save the test report as `test.xml`, which can be opened viewed directly by your web browser.
This will also save a reStructuredText report in 'test.rst', with 'test_image0.png' as an image referred by the document.

"""

import unittest
import argparse
import numpy as np
from matplotlib import pyplot as plt
from xreport import XReport as xr  # provides access to the Xreport extended features


def test_if_odd(i=0):
    """ test_func: Check if argument is odd

    This is a test function. ``nosetests`` will run it because its name starts
    with test...

    This function will generate an assertion exception if the argument is odd.
    The default argument is zero.
    """
    print 'I am about to check if %i is odd. This output should be captured by Xreport and included in the report.' % i
    print 'Note that I have access to the arguments passed to ``nosetests`` after ``--xargs``. Those are:', xr.xargs
    assert i & 1, 'The argument is not an odd value!'

def test_generator():
    """ test_generator: Generator that runs test_me five times to make sure

    This tests the execution of test generator functions. ``nosetests`` will
    also pick this function because its name stats with 'test...' but will see
    that this is a generator function. It will therefore run the generator,
    which will call `test_func` with various argument values.
    """
    for i in range(0, 5):
        yield test_if_odd, i


class TestClass(unittest.TestCase):
    """ TestClass: Groups a few tests with a test fixture.

    This class is also picked up by nose becaus eof its name. It runs three
    tests, each of which are run inside a fresh instance of this class. The
    setUp() method is called before each test to prepare the test environment.
    """

    def setUp(self):
        """ Prepare the test for execution.

        Here, we grab the command line arguments and parse them.
        """
        print 'Setting up tests with SetUp() using xargs=', xr.xargs
        parser = argparse.ArgumentParser(description=self.__doc__.split('\n')[0])  # description is the first line of the docstring
        parser.add_argument('--iceboards', action='store', type=str, nargs='+', help="Iceboard hostnames")
        parser.add_argument('--interactive', action='store_true', help="Enables interactive tests")
        self.args = parser.parse_args(xr.xargs)
        print 'The parsed arguments are ', self.args
        print 'Finished setting up the test. We now proceed with the test method.'
        xr.flush()  # start a new block of text to make the report look nicer

    def test_args_and_formatting(self):
        print 'This test would be running using the following iceboard(s): %s' % self.args.iceboards

        xr.rst("""
            We can include reStructuredText strings:

            .. math::
               x^2 + y^2 = z^2
            """)

        print "Back to normal text"

        xr.rst("And here is some inline math :math:`S_\phi(t) = \int_0^t\omega(x)dx` that looks nice if Sphinx is configured properly.")

    def test_plot(self):
        """ test_plot: Add a graph to the test report.

        The graph is added by calling xr.insert_plot(caption), which adds the
        current figure to the test report (as a PNG).
        """

        print 'Look at the plot below'
        x = np.arange(0, 6.28*5, .01)
        y = np.sin(x)
        plt.clf()
        plt.plot(x, y)
        xr.insert_plot('This is the figure caption')
        print 'This *was* a nice plot'


    def test_gen(self):
        """ Generator test.

        This is a generator, but unfortunately, because it is in a class,
        it will *NOT* iterate over its returned values so we can't use it to
        run a test multiple times with different parameters and have each
        iteration be treated as an independent test case. Use function
        generators (see above) to acheive this.
        """
        print 'Started to iterate over the generator... (this will not happen)'
        yield test_if_odd, 1
        yield test_if_odd, 2

    def test_interactive(self):
        """ Interactive input test.
        Although ``nosetests`` is designed for fully automated tests, we can use interactive prompts.
        """
        if not self.args.interactive:
            print ' Interactive tests is enabled if you pass the --interactive option after --xargs'
            return

        xr.output('We are now testing interactive tests methods.')
        x = xr.input("Enter a message: ")  # We can also use raw_input() here
        print 'The user entered the following interactive message:', x
        xr.output('Thank you. This information will be saved in the report for the posterity')

if __name__ == '__main__':
    """ Run the test in this file."""
    xr.run_from_module()
