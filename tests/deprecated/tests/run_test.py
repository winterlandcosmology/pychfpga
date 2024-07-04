""" Test launcher.

   - Gets command-line arguments like iceboard hostnames,  bitfile name,
     desired logging target and level and performs basic checks
   - Clears any previous logging handlers and set the SQLAlchemy logging to a
     known level.
"""

import logging
import datetime
import importlib

from chime_args import ChimeArgs

if __name__=='__main__':

    # -------------------------------------------
    # Run the tests on selected boards and crates
    # -------------------------------------------

    import sys
    print sys.argv
    args = ChimeArgs()

    for test_name in args.test_names:
        test_name_split = test_name.split('.')
        module_name = '.'.join(test_name_split[0:-1])
        test_class_name = test_name_split[-1]

        module = importlib.import_module(module_name)
        reload(module)  # Make sure the module is up to date
        test_class = getattr(module, test_class_name)

        # if not issubclass(test_class, TestGroup):
        #     raise TypeError('The test class must be derived from TestGroup')

        # for i in ib:
        test_filename = 'results/%s' % (test_name)
        te = test_class(context={
                "Date": datetime.datetime.now(),
                "Bitstream filename": args.bitfile,
                "Bitstream CRC32": '0x%08X' % i.get_fpga_bitstream_crc(),
                "Master Iceboard hostname": i.hostname,
                "Iceboard handler name": type(i.handler).__handler_name__,
                })
        try:
            te.run(iceboards=ib, crates=ic)
        except Exception:
            raise
        finally:
            print
            print '---------------- TEST COMPLETED --------------------'
            te.write_xml(test_filename + '.xml')
            # te.write_html(test_filename + '.html')
            print 'TEST SYNOPSIS:'
            print '\n'.join(te.synopsis_as_strings())
# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
