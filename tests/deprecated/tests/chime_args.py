""" Test launcher.

   - Gets command-line arguments like iceboard hostnames,  bitfile name,
     desired logging target and level and performs basic checks
   - Clears any previous logging handlers and set the SQLAlchemy logging to a
     known level.
"""

import argparse
import logging
import __main__
# import datetime
import sys
# import importlib
import socket  # for gethostbyname()
import datetime

from tornado.netutil import Resolver
from tornado.ioloop import IOLoop
from tornado.gen import with_timeout, TimeoutError
Resolver.configure('tornado.netutil.ThreadedResolver', num_threads=20)

from pychfpga.core.icecore import IceBoardPlus, IceBoardPlusHandler, IceCrate, HardwareMap, discover_iceboards, async, async_return
from pychfpga.MGADC08 import MGADC08  # Import ti make sure this Mezzanine is registered  so it can be discovered
from pychfpga.core.chFPGA_controller import chFPGA_controller


class FpgaBitstream(object):
    """ Helper object used to load and store a FPGA bitstream. You don't have
    to use it, but it makes the code look nicer"""
    bitstream = None

    def __init__(self, filename, auto_reload=True):
        self.filename = filename
        self.auto_reload = auto_reload
        if not self.auto_reload:
            self._load()
    def __str__(self):
        """ Return the bitstream as a string. """
        if self.auto_reload:
            try:
                self._load()
            except IOError:
                if not self.bitstream:
                    raise
        return self.bitstream
    def _load(self):
        with open(self.filename, 'rb') as file_:
            self.bitstream = file_.read()

ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 = (
    ([16]*8,     [3]*8),  #CH0
    ([7]*8,      [3]*8),  #CH1
    ([22]*8,     [3]*8),  #CH2
    ([19]*8,     [3]*8),  #CH3
    ([15]*8,     [3]*8),  #CH4
    ([14, 13, 14, 14, 13, 14, 15, 14],    [3]*8),  #CH5
    ([18]*8,     [3]*8),  #CH6
    ([17]*8,     [4]*8),  #CH7
    ([15, 17, 15, 18, 17, 14, 17, 15],   [3]*8),  #CH8
    ([16]*8,     [4]*8),  #CH9
    ([20]*8,     [3]*8),  #CH10
    ([18]*8,     [3]*8),  #CH11
    ([15]*8,     [3]*8),  #CH12
    ([18]*8,     [3]*8),  #CH13
    ([18]*8,     [3]*8),  #CH14
    ([16]*8,     [3]*8)  #CH15
    )

ADC_DELAY_TABLE = ADC_DELAYS_MGK7MB_REV2_MGAC08_REV2 #ADC_DELAYS_REV2_SN0001 ## select the table corresponding to the FMC serial number


class ChimeArgs(object):
    def __init__(self, argv=None, **kwargs):
        """ Return a context object that includes command line arguments,
        keyword parameters, and optionally initialized hardware map elements.

        """

        # This is the bitfile that is generated if implementing the the Vivado
        # project located in
        # icecore/rtl/projects/iceboard_top_example/iceboard_top_example.xpr
        default_bitfile = (
            '../../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/CHFPGA_MGK7MB_REV2.bit')

        # Configure the various loggers to provide adequate levels of details
        log_levels = {'info': logging.INFO, 'debug': logging.DEBUG, 'warn': logging.WARNING, 'error': logging.ERROR}

        # -------------------------------
        # Process command line arguments
        # -------------------------------

        parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
        parser.add_argument('test_names', nargs='*', type=str, help="Name of the test to run")

        parser.add_argument('-t', '--log_target', action='store', type=str, default='syslog', help="Logging target ('stream', 'syslog' or a filename)")
        parser.add_argument('-l', '--log_level', action='store', type=str, choices=log_levels, default='debug', help='Logging level')
        parser.add_argument('--sql_log_level', action='store', type=str, choices=log_levels, default='warn', help='SQLAlchemy Logging level')
        parser.add_argument('--if_ip', action='store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. This is used solely for direct UDP communications with the FPGA. If not specified, the system will use the same interface that communicates with the ARM processor.')

        parser.add_argument('-i', '--iceboards', action='store', nargs='*', type=str, help="Space-separated list of the iceboard hostnames (e.g. 10.10.10.7 or iceboard0007.local if the mDNS system is operational")
        parser.add_argument('-d', '--discover', action='store_true', help="Discover all boards and crates on the network using mDNS and add them to the hardware map")
        parser.add_argument('-c', '--crate', action='store', type=str, help="Discover and select only boards in the specified crate serial number")
        parser.add_argument('-s', '--slot', action='store', type=str, help="Select only boards in the specified slot(s)")
        parser.add_argument('--force', action='store', type=int, default=-1, help='Force FPGA programming even if the firmware is already programmed. If -1, the FPGA is not configured.')
        parser.add_argument('--bitfile', action='store', type=str, default=default_bitfile,  help='Filename of the bitfile used to to program the FPGAs')
        parser.add_argument('--ping', action='store', type=int, default=1, help="1: Check if Tuber is responding. 0: Check but ignore. ")


        # parser.add_argument('-s', '--subarray', action='store', nargs='+', type=int, help='Space-separated list of subarrays to include')
        parser.add_argument('--init', action='store', type=int, default=-1, help='Initialization level: -1: Just create sockets, 0: connect and read only. 1: initialize hardware')
        parser.add_argument('-f', '--sampling_frequency', action='store', type=float, default=800, help='Sampling frequency of the ADC in MHz')
        parser.add_argument('-w', '--data_width', action='store', type=int, choices=[4,8], default=4, help='Data width of each Re and Im component of the channelizer output')
        parser.add_argument('-g', '--frames_per_packet', action='store', type=int, default=4, help='Number of frames to group before sending to the GPU or FPGA correlator. The total size of the frame, including the header and ethernet obverhead, cannot exceed 8 kibytes.')
        parser.add_argument('--enable_gpu_link', action='store', type=int, default=0, help='Enables the GPU link transmission')
        # parser.add_argument('--sn', action='store', type=int, default=7, help='Serial number of the Iceboard')

        args = parser.parse_args(argv or sys.argv[1:])
        for k,v in kwargs.items():
            setattr(args, k, v)

        __main__._host_interface_ip_addr = args.if_ip


        # -------------------------------
        # Set-up logging
        # -------------------------------


        # Make sure SQLAlchemy does not log too much
        sql_logger = logging.getLogger('sqlalchemy.engine.base.Engine')
        sql_logger.setLevel(log_levels[args.sql_log_level])

        # Set-up chFPGA loggers
        if args.log_target == 'stream':
            log_handler = logging.StreamHandler()
        elif args.log_target == 'syslog':
            log_handler = logging.handlers.SysLogHandler()
        else:
            log_handler = logging.FileHandler(args.log_target)

        logger = logging.getLogger('')
        logger.handlers = []  # Clear all existing handlers

       # Set-up log for this test run
        logger.setLevel(log_levels[args.log_level])
        logger.addHandler(log_handler)


        # Associate the bitstream with the target Handler
        fpga_bitstream = FpgaBitstream(args.bitfile)
        # chFPGA_controller.register_fpga_bitstream(fpga_bitstream)

        # -------------------------------
        # Create a hardware map
        # -------------------------------
        hwm = HardwareMap()  # Create empty hardware map
        # print 'args are', args
        # First, Add iceboards that are explicitely listed. For now, we know only their hostname
        if args.iceboards:
            for hostname in args.iceboards:
                # ip_addr = socket.gethostbyname(hostname)  # convert hostname to IP address for faster Tuber access
                ip_addr = hostname
                i = IceBoardPlus(hostname=ip_addr)
                hwm.add(i)
                hwm.flush()
                if args.ping and not i.ping():
                    raise RuntimeError("%r could not be found at '%s'"
                                       % (i, i.tuber_uri))
                i.discover_serial()
                i.discover_crate()

        # Discover additional boards and crates on the network using mDNS
        if args.discover:
            discover_iceboards(hwm)
        elif args.crate:
            print 'Discovering IceBoards and IceCrates...'
            self.flush()
            discover_iceboards(hwm, crate=args.crate, timeout=2)

            crates = hwm.query(IceCrate)
            for cr in crates:
                logger.debug('DNS-SD: IceCrate is class %s' % (cr.__class__.__name__))

        # Query all iceboards, and apply slot numbe rfilter if applicable
        if args.slot:
            ib = hwm.query(IceBoardPlus).filter_by(slot=args.slot)
        else:
            ib = hwm.query(IceBoardPlus)
        # Query all IceCrates
            ic = hwm.query(IceCrate)


        print 'The following IceBoards were selected:'
        for i in ib:
            print 'Crate SN%s, slot %2s: Iceboard SN%s at %s (ping =%s)' % (i.crate.serial if i.crate else None, i.slot, i.serial, i.hostname, i.ping())


        if ib.count():
            ib.set_handler(chFPGA_controller, fpga_bitstream)
            # ib.set_handler(IceBoardPlusHandler, fpga_bitstream)

            # Configure the FPGA with the bitstream associated with the handler
            if args.force > -1:
                print 'Configuring FPGAs...'
                self.flush()
                ib.set_fpga_bitstream(force=args.force)

            # Auto-discover mezzanines and add them to the hardware map McGill
            # MGADC08 can only be discovered if the FPGA is programmed with the
            # chFPGA_controller firmware
            for i in ib:
                if i.is_fpga_programmed():
                    print 'Discovering Mezzanines...'
                    self.flush()
                    i.discover_mezzanines()

        print
        print 'The following boards were selected:'
        for i in ib:
            mezz = ['%s SN%s' % (m.__ipmi_part_number__, m.serial) if m else 'None' for m in [i.mezzanine.get(1,None), i.mezzanine.get(2,None)]]
            print '   Crate SN%s, slot %2i: Iceboard SN%s at %s (ping =%s), Mezz1=%s, Mezz2=%s' % (i.crate.serial if i.crate else None, i.slot, i.serial, i.hostname, i.ping(), mezz[0], mezz[1])
        self.flush()

        print
        print 'Initializing firmware (calling ib.open())'
        if ib.count() and args.init > -1:
            ib.open(adc_delay_table=ADC_DELAY_TABLE,
                    init=args.init,
                    sampling_frequency=args.sampling_frequency * 1e6,
                    reference_frequency=10e6,
                    data_width=args.data_width,
                    group_frames=args.frames_per_packet,
                    enable_gpu_link=args.enable_gpu_link)
        self.flush()


        # Augment the arg Namespace with conveniently proprocessed elements
        args.hwm = hwm
        args.ib = ib
        args.ic = ic
        args.fpga_bitstream = fpga_bitstream

        # import all command line argument values into this object
        for k,v in args._get_kwargs():
            setattr(self, k, v)
        print 'Done processing arguments'

    def flush(self):
        sys.stdout.flush()

    def dns_resolve(self, hostnames='iceboard0077.local', timeout=1):
        if isinstance(hostnames, str):
            hostnames = [hostnames]
        io_loop = IOLoop()
        resolver = Resolver()
        futures = [resolver.resolve(h, 9000) for h in hostnames]

        def stop_when_all_resolved(one_future):
            print [ff.done() for ff in futures]
            self.flush()
            return
            # if all(f.done() for f in futures):
            #     io_loop.stop()
            # print one_future.exception() or one_future.result()
        for f in futures:
            io_loop.add_future(f, stop_when_all_resolved)
        io_loop.add_timeout(io_loop.time() + timeout, lambda: io_loop.stop())
        io_loop.start()
        ip_addr = [None if not f.done() or f.exception() else dict(f.result())[socket.AF_INET][0] for f in futures]
        for f in futures:
            f.cancel()
        resolver.close()
        return (resolver, futures, ip_addr)

if __name__ == '__main__':

    args = ChimeArgs()
    hwm = args.hwm
    ib = args.ib
    ic = args.ic

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
