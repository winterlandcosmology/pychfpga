#!/usr/bin/python
# Disable pylint TAB warnings (W0312) and Line too long (=C0301)
# pylint: disable=W0312,C0301

"""
icearray.py module
Provides access to an array of ICEBoards and ICEBoxes (backplanes)

 History:
        2014-03-04 JFC: Created
"""
import argparse
import logging
import __main__  # used to store host_ip_address
import csv
import re  # used by mdns_discovery
from sqlalchemy.orm.session import Session
from sqlalchemy.orm import class_mapper

from ..icecore import hardware_map
from ..icecore.hwm_assets import IceBoard
from ..icecore.tuber import TuberObject  # used to ping boards
from ..icecore.session import load_session


class IceException(Exception):
    pass

class IceArray(object):
    """
    Provides access to the ressources of the IceArray by using the
    HardwareManager. An IceArray is essentially a SQL Session, from which you can issue
    queries to retrieve hardware items described in the HardwareManager
    database. This class also adds some helper functions to simplify operations of the array.

    This class specializes the HardwareManager by providing methods that are aware of specific hardware such as:
       - IceBoards
       - IceBoxes
       - IceBoard mezzanines

    More specifically, this class allow auto-discovery of these ressources
    and/or and loading of those from a file.

    Also, the class allows the specification of the Ethernet interface IP
    address needed for direct host-to-FPGA communications (which is not needed
    if communications are done through the ARM).
    """

    @staticmethod
    def close_all_sessions():
        """
        Close all existing sessions.
        """
        Session.close_all()

    def __init__(self, uri='sqlite:///:memory:', interface_ip_addr=None,  *args, **kwargs):
        """
        'interface_ip_addr' is the IP address of the Ethernet interface
            that will be used for direct UDP FPGA communications.

        Todo:

        2014-03-03 JFC: If interface_ip is not specified, the first call to
            discover() could scan all adapters and find on which one there are
            ICEBoards.
        """

        # super(type(self), self).__init__(uri='sqlite:///:memory:', *args, **kwargs)
        self.logger = logging.getLogger(__name__)
        self.logger.debug('Init IceArray parent')
        self.interface_ip_addr = interface_ip_addr

        # Set the interface IP address that will be used to bind UDP sockets
        # for direct FPGA MMI interfaces.
        __main__._host_interface_ip_addr = interface_ip_addr

        self._hwmap = hardware_map.HardwareMap(uri=uri, *args, **kwargs)
        # Remove the logger handlers that is created for the SQLAlchemy Engine. We want to use our own top level handler.
        # If we don't do this, the SQLAlchemy messages get displayed twice
        sa_logger =  logging.getLogger('sqlalchemy.engine.base.Engine')
        while sa_logger.handlers:
            sa_logger.removeHandler(sa_logger.handlers[0])

    def __getattr__(self, name):
        """
        Redirects all attributes access to the hardware map (Session) object.
        We do this because we can't just inherit a HardwareMap, because it
        does not return a HardwareMap object
        """
        return getattr(self._hwmap, name)

    def __dir__(self):
        # return type(self).__dict__ + self.__dict__ + dir(self._hwmap)
        return dir(self._hwmap) + self.__dict__.keys()

    def __repr__(self):
        return '%s' % self.__class__.__name__

    def discover(self, timeout=0.5):
        """
        Discover all hardware and firmware resources on the specified
        interface(s) and add them to the database.
        """
        self.discover_iceboards_using_mdns(
            timeout=timeout,
            default_app_name='chfpga',
            default_subarray=100)
        self.commit()  # commit any changes made during discovery
        #

    def get_iceboards(self, serials=[], subarray=[], *args, **kwargs):
        """
        Returns a list of all ICEBoards covered by the specified scope
        """
        new_args = list()
        if serials:
            new_args.append(IceBoard.serial_number.in_(serials))
        if subarray:
            new_args.append(IceBoard.subarray.in_(subarray))
        # kwargs['locked']=0 # force selection of non-locked boards
        # if 'present' not in kwargs:
        #     kwargs['present'] = 1
        return self.query(IceBoard).filter(*tuple(new_args + list(args)))\
            .filter_by(**kwargs)

    def discover_iceboards(self, timeout=0.1):
        """
        Update the Iceboard table with the list of available
        Iceboards actually found on the network. 'timout' indicates the time we
        wait for an answer before we decide that there is no board.

        For now, this function just checks if all IceBoards currently in the
        database are present by verifying if their ARM processors offer a
        tuber interface.

        Once we have a broadcast discovery protocol in the ARM we will be able
        to add complete new fields. In that case we will broadcast a
        identification request, and every board will reply back a packet,
        which will reveal their IP address and any other information in the
        packet. Once we have that, we can contact tuber to obtain all the
        information needed to create an IceBoard object. This includes:

            arm serial number: from arm (needed?)
            fpga serial number: from JTAG,
            board serial number: from board's EEPROM

        """
        logger = logging.getLogger(__name__)
        logger.debug('Discovering IceBoards')

        iceboards = self.query(IceBoard)  # get all the iceboards from the database

        # Check if each iceboard actually responds to tuber requests
        for ib in iceboards:
            if ib.hostname:
                ib.present = TuberObject.ping(ib.hostname)
                logger.info('Discovery: The IceBoard S/N %s ping result at URI= %s is %s' % (ib.serial_number, ib.hostname, ib.present))

    def discover_iceboards_using_mdns(self,
                                      timeout=0.5,
                                      update=False,
                                      default_app_name='',
                                      default_subarray=0,
                                      print_results=True):
        """ Discover IceBoards on the subnet using mDNS discovery.

        if 'update' is True, the in-memory hardware map is updated with the
        discovered boards. New boards are assigned with the hostname and
        serial number obtained from mDNS, but are assigned handlers and
        subarrays defined by the default_app_name and default_subarray
        parameters.

        if print_results is True, the discovered boards are display on screen.
        The discovery results are always logged.
        """

        self.logger.debug('%r: Discovering IceBoards through mDNS' % self)

        # We import pybonjour here so we don't need to have this module
        # installed if this feature is not needed
        from . import mdns_discovery

        providers = mdns_discovery.browse(
            '_ssh._tcp', browse_timeout=timeout, resolve_timeout=timeout)

        # Get all the iceboards from the database
        iceboards = self.query(IceBoard)

        # Get a dictionnary that maps the iceboard serial number to primary keys
        if iceboards:
            keymap = dict(iceboards.values(IceBoard.hostname, IceBoard._pk))
        else:
            keymap = {}

        for provider in providers:
            host = provider['host']
            port = provider['port']
            serial_number = re.findall(r'\w+?(\d+)\.local\.$', host)
            # hostname = 'http://%s:80/tuber' % host
            hostname = host
            if not serial_number:
                continue
            serial_number = int(serial_number[0])
            message = ('%r: mDNS discover: IceBoard SN %s found at %s:%s'
                       % (self, serial_number, host, port))
            self.logger.info(message)
            if print_results:
                print message
            if update:
                if hostname in keymap:
                    ib = iceboards.get(keymap[hostname])
                    ib.core_handler_name = 'IceBoard'
                    ib.app_handler_name = default_app_name
                    ib.subarray = default_subarray
                else:
                    self.logger.info(
                        '%r: IceBoard SN%s does not exist in the database. '
                        'Creating from file.' % (self, serial_number))
                    ib = IceBoard(
                        serial_number=serial_number,
                        hostname=hostname,
                        app_handler_name=default_app_name,
                        subarray=default_subarray
                        )
                    self.add(ib)
                self.flush()

    def discover_fpga_serial_numbers(self, timeout=0.3, only_new = True, print_on_screen=True):
        """
        Scans the network for FPGAs whose serial numbers. If only_new=True, only those that are not already in the database are returned.
        This works only for FPGAs offering a direct Ethernet interface. This method will become obsolete as the FPGA serials can be discovered through the ARM processor.
        The FPGAs to be discovered must be configured for them to be discovered.

         ^
        /!\ WARNING: This can disrupt operations of all FPGAs on the network as we are requesting all FPGAs to direct their Ethernet packets on the broadcast port of this machine.
        """

        import fpga_mmi # used for direct FPGA serial discovery

        logger = logging.getLogger(__name__)
        logger.debug('Discovering new FPGAs')

        iceboards = self.query(IceBoard) # get all the iceboards from the database
        database_serials = dict(iceboards.values(IceBoard.fpga_serial_number, IceBoard._pk)) # get the serial numbers of all known FPGAs

        new_serials = []
        serials = fpga_mmi.discover_fpgas(interface_ip_addr = self.interface_ip_addr, timeout=timeout)
        for ser in serials:
            if ser not in database_serials or not only_new:
                message = 'A FPGA with serial number %i (0x%08X) was detected on the network.' % (ser, ser)
                if ser not in database_serials:
                    message += ' It is not in the database.'
                logger.info(message)
                if print_on_screen: print message
                new_serials.append(ser)
        return new_serials

    # ----------------------------------------------------------------
    # Extra methods, possible candidates for deletion if no longer used
    # ----------------------------------------------------------------

    # *** JFC: Changed to have the default behavior to only return the
    #     mezzanines that were detected.
    # *** JFC: We could put the update part in a separate function, maybe
    #     outside IceBoard
    # *** JFC: Maybe we should add a check=True option to raise an error if
    #     the hardware map does not match reality

    # *** JFC: detect_icecrate would be a more consistent name

    def load_yaml_hardware_map(self, stream):
        self._hwmap.close()  # close current session
        self._hwmap = load_session(stream)

    def load_iceboards(self, filename):
        """
        Adds the Iceboard entries listed in the specified CSV file into the database.
        """
        session = self
        logger = logging.getLogger(__name__)

        iceboards = session.query(IceBoard) # get all the iceboards from the database
        keymap = dict(iceboards.values(IceBoard.serial_number, IceBoard._pk)) # get a dictionnary that maps the serial number to primary keys

        with open(filename, 'rb') as file:
            reader = csv.reader((line.split('#')[0].rstrip() for line in file if line.split('#')[0].strip())) # uses a generator to strip the comments
            for (serial_number, hostname, arm_mac_address, app_handler_name, fpga_ip_addr, fpga_serial_number, locked, subarray) in reader:
                serial_number = serial_number.strip("' ")
                hostname = hostname.strip("' ")
                app_handler_name = app_handler_name.strip("' ")
                # fpga_ip_addr = fpga_ip_addr.strip("' ")
                # fpga_serial_number = int(fpga_serial_number, 0)
                # locked = int(locked, 0)
                subarray = int(subarray, 0)

                if serial_number in keymap:
                    logger.info('IceBoard SN%s already exists in the database. Updating columns from file.' % serial_number)
                    ib = iceboards.get(keymap[serial_number])
                    ib.hostname = hostname
                    ib.core_handler_name = 'IceBoard'
                    ib.app_handler_name = app_handler_name
                    ib.subarray = subarray
                else:
                    logger.info('IceBoard SN%s does not exist in the database. Creating from file.' % serial_number)
                    ib = IceBoard(
                        serial_number=serial_number,
                        # arm = TuberHWMResource(hostname=hostname, tuber_objname = 'IceBoard'),
                        hostname = hostname ,
                        core_handler_name = 'IceBoard',
                        app_handler_name=app_handler_name,
                        subarray = subarray
                        )
                    session.add(ib)
        session.flush()
        session.commit()

    def status(self):
        print 'The array contains the following resources'
        print self.get_iceboards()

def close_all_sockets():
    """
    Close all the sockets that has been opened and were registered in the main module __opened_sockets__ attribute.
    """
    if '__opened_sockets__' in vars(__main__): # i.e. if __main__ has an __opened_sockets__ attribute
        while __main__.__opened_sockets__: # close all sockets so we won't get a 'socket already opened' error because of a previous run
            __main__.__opened_sockets__.pop().close()


if __name__ == '__main__':
    logging.getLogger('iceboard.arm.FpgaBitFile').setLevel(logging.INFO)
    logging.getLogger('requests.packages').setLevel(logging.WARN)
    logging.getLogger('sqlalchemy.engine.base.Engine').setLevel(logging.INFO)

    close_all_sockets()

    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0]) # description is the first line of the docstring
    parser.add_argument('-l', '--log_level', action = 'store', type=str, choices=['info','debug'], default='debug', help='Logging level')
    parser.add_argument('-i', '--if_ip', action = 'store', type=str, default=None, help='IP address of adapter through which the connection to the FPGA will be established. If not specified, the controller will attempt to identify the proper host based on the FPGA IP address.')
    args = parser.parse_args()

    log_level = {'info': logging.INFO, 'debug': logging.DEBUG}[args.log_level]
    logging.basicConfig(level=log_level, format='%(asctime)s %(name)-32s %(levelname)-10s : %(message)s')

    logger = logging.getLogger(__name__)
    logger.info('------------------------')
    logger.info('icearray')
    logger.info('------------------------')
    logger.info('This module is called with the follwing parameters:' )
    for (key,value) in args.__dict__.items():
        logger.info('   %s = %s' % (key, repr(value)))

    ice = IceArray(args.if_ip)

    bitfile = arm.FpgaBitFile('../../../../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit')#('../../../chfpga/xilinx_projects/CHFPGA_MGK7MB_REV2/CHFPGA_MGK7MB_REV2.runs/impl_Rev2/chFPGA_MGK7MB_Rev2.bit')
    c = ice.get_iceboards([7, 14, 19]) # get one or more IceBoards
