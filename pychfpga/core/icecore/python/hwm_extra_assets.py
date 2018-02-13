""" Enhanced IceBoard and IceCrate objects that support local python support code and auto-discovery.
"""
import logging
import inspect
import base64
import zlib  # used to compute crc32
import functools  # Used in iceboard discovery
import tornado  # Used in iceboard discovery
import time  # Used in iceboard discovery
import socket  # used in iceboard discrovery (itoa())

from sqlalchemy import Column, String, Integer
from sqlalchemy.orm import class_mapper

from . import handler
from . import session  # YAML loader
from . import hardware_map
from . import tuber
from .async import async, async_return, async_moment
from .hwm_assets import IceBoard, IceBoardHandler, FMCMezzanine, IceCrate, IceCrateHandler


def mdns_discover(hwm=None, icecrates=None, iceboards=None, timeout=5, resolve_ip=True):
    """ Automatically detect IceBoards and IceCrates on the network using mDNS
    and update hardware map ``hwm`` accordingly.

    If no hardware map is provided, a new empty one is created. This can be
    used to query and further filter the discovered objects before adding them
    to a final hardware map.

    If ``iceboards`` is specified,  all the IceBoards with the serial number
    found in the ``iceboards`` list are selected. If None or an empty list, no
    board is added. If ``iceboards='*'``, all discovered Iceboards are added.

    If ``icecrates`` is specified,  all the IceBoards that are on crates
    having the model number and serial number listed in ``icecrates`` are
    selected. ''icecrates'' is in the format [(model1, [serial1, serial2 ...],
    (model2, [serial3, serial4, ...]), ...]

    If ``icecrates`` is None or an empty list, no crate is added.

    If ``icecrates='*'``, all discovered Iceboards from all crates are added.

    If 'resolve_ip' is True, the hostname published by mDNS (e.g.
    iceboard0007.local) is resolved into its associated IP address. This
    accelerates Tuber accesses since every Tuber call does not have to resolve
    it on every tuber call (this is especially needed on Windows).
    """
    import pybonjour  # only needed here, and not always installed


    if hwm is None:
        hwm = hardware_map.HardwareMap()

    # if isinstance(icecrates, (str, int):
    #     icecrates = [icecrates]

    # if isinstance(iceboards, str):
    #     iceboards = [iceboards]

    logger = logging.getLogger(__name__)
    fds = []

    # Normalize iceboard and icecrate target lists to the [ (model,[serial1, serial2]), ...] format
    if isinstance(iceboards, str): # include '*'
        iceboards = [('*', [iceboards])]
    iceboards = [entry if isinstance(entry, (list, tuple)) else ('*', [entry]) for entry in iceboards]
    iceboards = [(model, serials if isinstance(serials, (list, tuple)) else [serials]) for model, serials in iceboards]

    if isinstance(icecrates, str): # include '*'
        icecrates = [('*', [icecrates])]
    icecrates = [entry if isinstance(entry, (list, tuple)) else ('*', [entry]) for entry in icecrates]
    icecrates = [(model, serials if isinstance(serials, (list, tuple)) else [serials]) for model, serials in icecrates]

    def resolve_callback(sdRef, flags, iface, err, fullname,
                         host, port, txtRecord, io_loop):
        if err != pybonjour.kDNSServiceErr_NoError:
            return

        # Parse TXT records. That's where the IceBoard publishes data.
        tr = pybonjour.TXTRecord.parse(txtRecord)
        if 'motherboard-serial' not in tr or 'motherboard-part' not in tr:
            logger.warning("DNS-SD: IceBoard at %s was discovered but cannot be added to the hardware map because it does not publish a serial number" % (host))
            return
        ib_serial = tr['motherboard-serial']
        ib_part_number = tr['motherboard-part']

        existing_ib = hwm.query(IceBoardPlus).filter_by(serial=ib_serial)
        if existing_ib.count():
            logger.warning("DNS-SD: IceBoard at %s with serial %s already exists in the hardware map. No action is taken." % (host, ib_serial))
            return

        ib = IceBoardPlus(hostname=host, serial=ib_serial)

        bp_slot = tr['backplane-slot'] if 'backplane-slot' in tr else None
        bp_part_number = tr['backplane-part'] if 'backplane-part' in tr else None
        bp_serial = tr['backplane-serial'] if 'backplane-serial' in tr else None

        logger.debug("DNS-SD: Discovered IceBoard SN%s (%s) in IceCrate %s SN%s, Slot %s." % (ib_serial, host, bp_part_number, bp_serial, bp_slot))

        try:
            int_bp_serial = int(bp_serial)
        except (TypeError, ValueError):
            int_bp_serial = None

        try:
            int_ib_serial = int(ib_serial)
        except ValueError:
            int_ib_serial = None

        # If we specify no crate number, or if we have valid backplane
        # information and the backplane match that number, Then add the
        # Iceboard
        # icecrate_match = icecrates and (icecrates == '*' or any((bp_part_number in model if isinstance(model, (tuple, list)) else bp_part_number == model) and (bp_serial in serials or int_bp_serial in serials) for (model, serials) in icecrates))
        # iceboard_match = iceboards and (iceboards == '*' or ib_serial in iceboards or int_ib_serial in iceboards)


        iceboard_match = any(
            (target_model == '*' or ib_part_number == target_model) and
            ('*' in target_serials or ib_serial in target_serials or int_ib_serial in target_serials)
            for target_model, target_serials in iceboards)

        icecrate_match = any(
            (target_model == '*' or bp_part_number == target_model) and
            ('*' in target_serials or bp_serial in target_serials or int_bp_serial in target_serials)
            for target_model, target_serials in icecrates)

        if icecrate_match or iceboard_match:
            hwm.add(ib)
            hwm.flush()

            # Find is there is IceCrate-derived superclass that handles the
            # reported crate part number
            icecrate_class = None
            for mapper in class_mapper(IceCrate).self_and_descendants:
                supported_part_numbers = mapper.class_.__ipmi_part_number__
                if not isinstance(supported_part_numbers, (list,tuple)):
                    supported_part_numbers = [supported_part_numbers]
                if bp_part_number in supported_part_numbers:
                    icecrate_class = mapper.class_
            # If so, create the IceCrate if needed, and fill in the IceBoard's crate and slot fields
            if icecrate_class:  # Check if a crate with the same serial number already exists
                existing_crate = hwm.query(icecrate_class).filter_by(serial=bp_serial)
                if existing_crate.count():  # If so, assign it to this iceboard
                    new_crate = existing_crate.one()
                    logger.debug('DNS-SD: IceCrate Model %s SN%s (class %s) is already in the hardware map. Associating IceBoard SN%s with it on slot %s.' % (bp_part_number, bp_serial, new_crate.__class__.__name__, ib_serial, bp_slot))
                    ib.slot = bp_slot  # Add slot before adding crate
                    ib.crate = new_crate
                    hwm.flush()
                else:  # Otherwise create a new one and assign it
                    logger.debug('DNS-SD: Creating IceCrate Model %s SN%s using class %s and associating IceBoard SN%s with it on slot %s.' % (bp_part_number, bp_serial, icecrate_class.__name__, ib_serial, bp_slot))
                    ib.slot = bp_slot # Add slot before adding crate
                    new_crate = icecrate_class(serial=bp_serial)
                    ib.crate = new_crate
                    hwm.add(new_crate)
                    hwm.flush() # make sure the board will pop up in queries so we can know if the board already exist in the hwm
            else:  # oops, we did not find any class to handle that IceCrate part number...
                logger.warning('DNS-SD: Could not find an IceCrate-derived class to represent IceCrate Model %s. The IceBoard is added without an associated crate.' % (bp_part_number))


            if resolve_ip:
                # Now try to resolve the hostname into an IP address to accelerate Tuber accesses
                def query_record_callback(sdRef, flags, interfaceIndex, errorCode, fullname,
                                          rrtype, rrclass, rdata, ttl, ib):
                    if errorCode == pybonjour.kDNSServiceErr_NoError:
                        ib_ip_addr = socket.inet_ntoa(rdata)
                        logger.debug("DNS-SD: IceBoard SN%s hostname %s was resolved and updated to %s" % (ib.serial, ib.hostname, ib_ip_addr))
                        ib.hostname = ib_ip_addr

                query_sdRef = \
                    pybonjour.DNSServiceQueryRecord(interfaceIndex=iface,
                                                    fullname=host,
                                                    rrtype=pybonjour.kDNSServiceType_A,
                                                    callBack=functools.partial(query_record_callback, ib=ib))
                fds.append(query_sdRef)
                io_loop.add_handler(
                    query_sdRef.fileno(),
                    lambda fd, events: pybonjour.DNSServiceProcessResult(query_sdRef),
                    io_loop.READ)
        else:
            logger.debug("DNS-SD: IceBoard SN%s (crate %s SN%s slot %s) was detected but was not added because it did not match the IceBoard serial %s or crate serial %s" % (ib_serial, bp_part_number, bp_serial, bp_slot, iceboards, icecrates))

    def browse_callback(sdRef, flags, iface, err, service,
                        regtype, replyDomain, io_loop):

        if (err != pybonjour.kDNSServiceErr_NoError) or \
                not (flags & pybonjour.kDNSServiceFlagsAdd):
            return

        resolver = pybonjour.DNSServiceResolve(
            0, iface, service, regtype, replyDomain,
            callBack=functools.partial(resolve_callback, io_loop=io_loop))
        fds.append(resolver)
        io_loop.add_handler(
            resolver.fileno(),
            lambda fd, events: pybonjour.DNSServiceProcessResult(resolver),
            io_loop.READ)

    # Create a local, captive IOLoop. We use this to epoll() on
    # Bonjour file descriptors.
    io_loop = tornado.ioloop.IOLoop()
    io_loop.add_timeout(time.time()+timeout, lambda: io_loop.stop())

    browser = pybonjour.DNSServiceBrowse(
        regtype='_tuber-jsonrpc._tcp',
        callBack=functools.partial(browse_callback, io_loop=io_loop))

    fds.append(browser)

    io_loop.add_handler(
        browser.fileno(),
        lambda fd, events: pybonjour.DNSServiceProcessResult(browser),
        io_loop.READ)

    # Go!
    logger.debug("DNS-SD: Starting mDNS discovery")
    io_loop.start()
    logger.debug("DNS-SD: mDNS discovery has ended")

    # Clean up after Bonjour
    for fd in fds:
        fd.close()

    hwm.commit()

    return hwm

@session.register_yaml_object()
class IceBoardPlus(IceBoard):
    """ Hardware map object that describes the basic properties of an IceBoard
    and give access to the methods to operate its hardware and FPGA firmware.

    This object differs from the basic IceBoard in that is can support
    multiple user-defined handler that define the functionnality of various
    FPGA firmware or mezzanines.

    All methods and attributes provided by the handler (in the handler itself
    or through Tuber) are accessible as if they belonged to this ORM object.

    The IceBoard object can be associated with multiple handlers in order to
    represent boards runing different FPGA firmware. The handler are
    identified by the 'handler_name' column.

    Project-specific classes are meant to be derived from this class.
    """

    __mapper_args__ = {'polymorphic_identity': "IceBoardPlus"}
    __ipmi_part_number__ = 'MGK7MB'  # Must match part number in IPMI data

    handler_name = Column(
        String, doc="The name of the handler to use for this resource.")

    subarray = Column(
        Integer,
        doc="Arbitrary string used to group and select subsets of Iceboard")

    def __init__(self, hostname=None, handler_name='IceBoardPlusHandler',
                 **kwargs):
        """ Create a new Iceboard object from scratch. """

        # As a convenience, we can pass a Handler object as the
        # app_handler_name and we'll extract the name from it.
        if handler_name and inspect.isclass(handler_name) and \
                issubclass(handler_name, handler.Handler):
            handler_name = handler_name.get_handler_name()

        # Populate the object instrumented attributes
        super(IceBoardPlus, self).__init__(
            hostname=hostname, handler_name=handler_name, **kwargs)

        self.logger = logging.getLogger(__name__)
        self.logger.debug('%r: Created instance with args %r' % (self, kwargs))

    def set_handler(self, handler=None, bitstream=None,
                    configure_fpga=False, force=False, tag=None):
        """ Helper function used to specify the handler and bitstream
        associated with this IceBoard.

        Example:
            ib = hwm.query(Iceboards)
            ib.set_handler(ChannelizerIceBoardHandler, chan_bistream_v20, tag='2.0', configure_fpga=True)
            ib.some_channelizer_method(...)  # the iceboard runs as a channelizer ...
            ib.set_handler(CorrelatorIceBoardHandler, corr_bistream_v17, tag='1.7', configure_fpga=True)
            ib.some_correlator_method(...)  # .. and now it runs as a correlator
        """
        if handler:
            self.handler_name = handler.__handler_name__
            self.update_handler()
        if bitstream:
            self.register_fpga_bitstream(bitstream, tag=tag)
        if configure_fpga:
            self.set_fpga_bitstream(tag=tag, force=force)

    @async
    def discover_serial(self, update=True):
        """ Discover the serial number of this IceBoard from its IPMI data, and update the hardware map accordingly if `update=True`"""
        self.logger.info('%r: discovering the serial number of board at %s' % (self, self.tuber_uri))
        try:
            actual_serial = str((yield self.get_motherboard_serial.async()))
        except:  #  Deal with uninitialized boards
            self.logger.warn('%r: Error while attempring to read the board serial number' % (self))
            actual_serial = None
        self.logger.info('%r: got the serial number of board at %s to be %s' % (self, self.tuber_uri, actual_serial))
        yield async_moment
        if update:
            if not actual_serial:
                self.logger.warn('%r: Could not read the board serial number from IPMI storage or serial number is null. Serial number is not updated.' % (self))
            elif self.serial and actual_serial != self.serial:
                self.logger.warn('%r: The discovered serial number differs from the current (hardware map) one. Updating to the discovered value.' % (self))
            self.serial = actual_serial
            self.hwm.flush()
        self.logger.info('%r: finished discovering the serial number of board at %s' % (self, self.tuber_uri))
        async_return(self.serial)

    @async
    def discover_slot(self, update=True):
        """ Discover the slot number of this IceBoard, and update the hardware map accordingly if `update=True`"""
        actual_slot = yield self.get_backplane_slot.async()
        if update:
            if not actual_slot:
                self.logger.warn('%r: The board is not connected to a backplane. Slot number is not updated.' % (self))
            elif self.slot and actual_slot != self.slot:
                self.logger.warn('%r: The discovered slot (%s) number differs from the current (hardware map) one (%s). Updating to the discovered slot.' % (self, actual_slot, self.slot))
            self.slot = actual_slot
            self.hwm.flush()
        async_return(self.slot)

    @async
    def discover_mezzanines(self, update=True):
        '''Detect mezzanines attached to the Iceboard and update the hardware map accordingly if
        update=True.

        This method uses IPMI data on the mezzanine's EEPROMs to guide itself.
        New Mezzanine objects that match the IPMI product number are added in
        the hardware map if found.

        You do NOT need to use this method if the mezzanines present in the
        system are already explicitely specified in the YAML hardware maps.
        '''
        mezz_class = {}
        for m in range(1, self.NUM_MEZZANINES + 1):

            # MezzClass = MissingMezzanine # Used by Graeme
            part_number = None
            serial = None
            mezz_class[m] = None
            # if there is no mezzanine in this slot, proceed to the next one
            if not (yield self.is_mezzanine_present.async(m)):
                continue

            # If a mezzanine is present, get its EEPROM data and search for the first mezzanine class that can decode it.
            try:
                eeprom_data = yield self._mezzanine_eeprom_read.async(m)  # this does not read all eeprom for some mezxzanines...
                # eeprom_data = self.hw.read_mezzanine_eeprom(m,0,512)
            except (tuber.TuberRemoteError, AttributeError):  # If the method does not exist
                eeprom_data = None

            ipmi = None
            if eeprom_data is not None:
               for mapper in class_mapper(FMCMezzanine).self_and_descendants:
                    if hasattr(mapper.class_, 'decode_eeprom'):
                        ipmi = mapper.class_.decode_eeprom(eeprom_data)
                        if ipmi:
                            break
            if not ipmi:
                try:
                    ipmi = self._get_mezzanine_ipmi(m)  # Read IPMI from the ARM's cache
                    self.logger.debug('%r: detect_mezzanines(): read Mezzanine %i EEPROM using the ARM' % (self, m))
                except tuber.TuberRemoteError:
                    pass
            if not ipmi: # If we still did not get an IPMI block, give up and proceed to the next mezzanine
                self.logger.debug('%r: detect_mezzanines(): Could not decode the EEPROM in Mezzanine %i' % (self, m))
                continue

            # Extract the useful unformation from IPMI
            part_number = ipmi.product.part_number
            serial = ipmi.product.serial_number

            self.logger.debug(
                '%r: detect_mezzanines(): Detected Mezzanine '
                'Model: %s Serial %s in Mezzanine %i'
                % (self, part_number, serial, m))

            # Look through the Mezzanine classes to see if one matches the model number found in the EEPROM
            for mapper in class_mapper(FMCMezzanine).self_and_descendants:
                    if mapper.class_.__ipmi_part_number__ == part_number:
                        mezz_class[m] = mapper.class_
                        break

            if update:
                if not self.hwm:
                    raise SystemError(
                        '%r: detect_mezzanines(): Attempt to add new '
                        'mezzanine objects while the IceBoard is not yet '
                        'added to the  hardware map. ' % self)

                if m in self.mezzanine:
                    del(self.mezzanine[m])

            if not mezz_class[m]:
                self.logger.warning(
                    "%r: detect_mezzanines(): There is no known "
                    "class for Mezzanine object of type '%r' "
                    "in mezzanine slot %r" % (self, part_number, m))
            elif update:
                self.logger.debug(
                    '%r: detect_mezzanines(): Creating Mezzanine '
                    'Serial %s in Mezzanine %i' % (self, serial, m))
                new_mezz = mezz_class[m](
                    mezzanine=m,
                    serial=serial
                    #type=''  # 'type' cannnot be None
                    )
                self.hwm.add(new_mezz)
                self.hwm.flush()
                self.mezzanine[m] = new_mezz
        async_return(mezz_class)

    @async
    def discover_crate(self, update=True):
        """ Detect the Icecrate and slot number on which this Iceboard is
        attached by reading the backplane IPMI data, and update the hardware
        map accordingly if `update=True`. An IceCrate object is created if it
        does not already exist.

        This method does *not* use mDNS. It relies of the IPMI data stored in
        the backplane's EEPROM, which is obtaines through the Iceboard's ARM
        processor.

        You do NOT need to use this method if the backplane is already
        explicitely specified for this IceBoard in the YAML hardware maps.
        """

        icecrate_class = {}
        part_number = None
        serial = None
        if (yield self.is_backplane_present.async()):
            ipmi = yield self._get_backplane_ipmi.async()  # Tuber call
            part_number = ipmi.product.part_number
            serial = ipmi.product.serial_number
            slot_number = yield self.get_backplane_slot.async()
            self.logger.debug(
                '%.32r: discover_crate(): '
                'Detected Backplane Model: %s Serial %s'
                % (self, part_number, serial)
                )

            for mapper in class_mapper(IceCrate).self_and_descendants:
                supported_part_numbers = mapper.class_.__ipmi_part_number__
                if not isinstance(supported_part_numbers, (list, tuple)):
                    supported_part_numbers = [supported_part_numbers]
                if part_number in supported_part_numbers:
                    icecrate_class = mapper.class_

        if not icecrate_class:
            self.logger.warning(
                "%.32r: discover_crate(): "
                "There is no known backplane object with "
                "polymorphic map name '%r'"
                % (self, part_number))

        if icecrate_class and update:
            if not self.hwm:
                raise SystemError(
                    '%.32r: discover_crate(): Attempt to update new backplane '
                    'object while the IceBoard is not yet added to the '
                    'hardware map. ' % self)

            # Assign slot number to IceBoard because the IceCrate will grab
            # that info upon IceBoard assignment to a slot
            self.slot = slot_number
            self.hwm.flush()

            if self.crate:  # if there is already an icecrate
                if isinstance(self.crate, icecrate_class) \
                        and self.crate.serial != serial:
                    del(self.crate)

            if not self.crate:
                # Check is a crate with the same serial number already exists
                existing_crate = self.hwm.query(icecrate_class).filter_by(serial=serial)
                if existing_crate.count():  # If so, assign it to this iceboard
                    self.logger.debug(
                        '%.32r: discover_crate(): Reusing IceCrate %s SN%s'
                        % (self, part_number, serial))
                    self.crate = existing_crate.one()
                else:  # otherwise create a new one and assign it
                    self.logger.debug(
                        '%.32r: discover_crate(): Creating IceCrate %s SN%s'
                        % (self, part_number, serial))
                    new_crate = icecrate_class(serial=serial)
                    self.crate = new_crate
                    self.hwm.add(new_crate)
                self.hwm.flush()

            # Assign this iceboard to the proper crate slot. Note that the
            # 'slot_number' index is ignored after the flush. The
            # IceBoard.slot_number is the real index.
            # self.crate.slot[slot_number] = self
            # self.hwm.flush()
        async_return(icecrate_class)


@session.register_yaml_object()
class IceBoardPlusHandler(IceBoardHandler):
    """ Basic Python handler for the IceBoard.

    It provides:
       - Access to the methods and attributes provided by the ARM-based
             software through Tuber
       - Access to the memory-mapped registers in the FPGA's firmware using the ARM-FPGA SPI link
             through the `fpga_mmi_read()` and `fpga_mmi_write()` methods.

    `IceBoardPlusHandler` can be created as a standard Python object initialized with a number of
    parameters which set corresponding attributes (see below). If a `parent_getter` function is
    provided, the value of these attributes will instead be fetched
    dynamically from the parent object. Note that any explicitely specified parameter overrides a
    parent parameter.

    Parameters:
        parent_getter (func): Function that returns the dynamically return the parent object from which the following parameters will be fetched. Is `None` if there is no parent.
        hostname (str): hostname or IP address of the ICEBoard ARM processor (mandatory)
        serial (str): Serial number of the board. Can be provided by the ARM.
        part_number (str): Part number of the IceBoard. Can be obtained from the ARM.
        crate (IceCrateHandler): = object that handle the backplane on which the board is connected. `None` if the board is not connected to a backplane.
        slot (int): Slot number in which the board is installed ona backplane. None if there is no backplane.
        mezzanine (dict): Map {mezzanine_number: Mezzanine Handler, ...} describing the installed mezzanines. Can be obtained from the ARM.
        tuber_objname (str): name of the set of software functions that will be provided by the ARM processor through the Tuber interface.



    .. Any object provided by this this handler can be accessed at any
    .. hierarchical level. However, objects that are probided by Tuber have these
    .. restrictions:
    ..
    ..    - attributes and methods whise name begin with '_' are not accessible
    ..    - modification to the object attributes must be done by a setter
    ..      function provided by the object.
    ..    - methods or attribute access can only return string or numeric values,
    ..      or lists or dictionnary thereof


    Notes:
        - The SPI MMI interface is currently provided by peek/poke methods accessed through Tuber.
          However, if one day the ARM supports it, a faster access could potentially be provided by
          overriding the fpga_mmi_read/write methods to send the commands to a dedicated ARM port
          and thus bypass Tuber's HTTP/JSON overhead. Since the SPI link is relatively slow anyway, this might not be useful until a faster ARM-FPGA link is in place (such as the unused PCIe link).

        - This handler does *not* define a MMI interface that uses the FPGA's
          ethernet port directly through the SFP+ connector. Such functionnality is to be provided by a
          subclass of this class if the firmware supports it.
    """

    __handler_for__ = IceBoardPlus # register this class (and any subclass)
    tuber_objname = 'IceBoard'

    # Core FPGA firmware registers (delete when moved to the ARM)
    FPGA_CORE_FIRMWARE_COOKIE_ADDR        = 4 * 0
    FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR = 4 * 1
    FPGA_APPLICATION_FLAGS_ADDR           = 4 * 2
    FPGA_FIRMWARE_CRC32_ADDR              = 4 * 3
    FPGA_FIRMWARE_TIMESTAMP_ADDR          = 4 * 4
    FPGA_SERIAL_NUMBER_LSW_ADDR           = 4 * 5
    FPGA_SERIAL_NUMBER_MSW_ADDR           = 4 * 6

    NUMBER_OF_FMC_SLOTS = 2

    # _bitstream_register contains a list of bitstreams that are associated
    # with this object. Format: tag: bitstream_object
    _bitstream_register = {}

    _backplane_initialized = False  # Indicate if we have initialized the backplane access yet
    _cached_repr = None

    # ----------------------------
    # Python Bitstream management
    # ----------------------------
    @classmethod
    def register_fpga_bitstream(cls, bitstream, tag=None):
        """ Register a bitstream that is associated with this application
        handler.

        'bitstream' can be any object whose str() property returns a valid
              bitstream (e.g. a string, a buffer or a FpgaBitstream object).

        This is to provide to host-based Python application a functionnality
        equivalent to that of the ARM-based applications handlers which can
        also access the relevant bitstream.
        """
        cls._bitstream_register[tag] = bitstream

    def __repr__(self):
        """ Provides a concise string representation of this handler.

        Since this is used very frequetly (especially in logging), a cached
        version can be used if one was created by set_cache() method to avoid
        accesses to the ORM object.
        """
        if self._cached_repr:
            return self._cached_repr
        else:
            return super(IceBoardPlusHandler, self).__repr__()

    def set_cache(self):
        """ Caches key ORM-dependent values to prevent access to the ORM
        object and accelerate the code.

        Call this only when you know that the ORM won't change.
        """
        self._cached_repr = super(IceBoardPlusHandler, self).__repr__() + '*'

    @async
    def set_fpga_bitstream(self, buf=None, tag=None, force=False):
        '''
        Configures the FPGA with the specified bitstream.

        The bitstream associated with the current handler with the specifiec
        'tag' will be loaded. However, if a buffer 'buf' is explicitely
        provided, that bitstream will be used instead.,

        The 'buf' can be any an object where str(buf) returns the content of a
        .BIT or .BIN file (which includes a buffer, a string, or other objects
        defining __str__()).

        By default, the FPGA will not be reconfigured it already has a
        bitstream with the same CRC signature. That behavior can be changed by
        specifying the 'force' argument:

            force = True: FPGA will always be configured
            force = False: FPGA will be configured if it is not configured or
                    if bitstream CRC differ
            force = None: FPGA will be configured only if it is not configured
        '''

        t0 = time.time()
        self.logger.info('%.32r: called set_fpga_bitstream' % self)

        if hasattr(self, 'close'):
            self.close()

        # If the bitstream is not explicitely provided, ask the handler to
        # provide it. The str() of the returned object must yield the valid
        # bitstream buffer in a string.
        if buf is None:
            buf = self.get_fpga_bitstream(tag)
        #else:
        #    buf = str(buf)

        if hasattr(buf, 'crc32'):
		crc32 = buf.crc32
	else:
		crc32 =  zlib.crc32(str(buf))  # compute CRC32 of the data if it not already precomputed in the 'buf' object
	crc32 &=  0xFFFFFFFF
        buf = str(buf)

        self.logger.info('%.32r: getting is_programmed' % self)
        is_fpga_programmed = yield self.is_fpga_programmed.async()
	t1 = time.time()
        self.logger.info('%.32r: getting FPGA crc' % self)
        fpga_bitstream_crc = yield self.get_fpga_bitstream_crc.async()
        self.logger.debug('%.32r: fpga_programmed=%s, force=%s, fpga_crc=%08X, bitstream_crc=%08X' % (self, is_fpga_programmed, force, fpga_bitstream_crc or 0, crc32 or 0))
	t2 = time.time()
        if not is_fpga_programmed or force \
           or (force is not None and (fpga_bitstream_crc != crc32)):
            self.logger.info('%.32r: Configuring FPGA' % self)
            if hasattr(buf, 'base64'):
		b64_string = buf.base64
	    else:
		b64_string =  base64.b64encode(str(buf))
            # self._set_fpga_bitstream_base64(b64_string)
            t3 = time.time()
            yield self._set_fpga_bitstream_base64.async(b64_string)
            self.set_fpga_bitstream_crc(crc32)
            t4 = time.time()
            self.logger.info('%r: Done configuring FPGA. It took %.3fs (%.3fs, %.3fs, %.3fs, %.3fs)' % (self, t4-t0, t1-t0, t2-t1, t3-t2, t4-t3))
        else:
            t4 = time.time()
            self.logger.info(
                '%.32r: FPGA is already configured. Skipping configuration. Took %.3fs' % (self, t4 - t0)
                )

    def get_fpga_bitstream(self, tag=None):
        """ Return the bitstream associated with this handler for the supplied
        tag as a string.

        If no bitstream is registered for this Handler or if no bitstream match
        the tag, the method attempts to obtain the bitstream through Tuber.
        """
        if tag in self._bitstream_register:
            return self._bitstream_register[tag]
        else:
            return tuber.TuberObject.get_fpga_bitstream(self, tag)

    # ------------------------------------
    # Python FPGA Memory-mapped interface
    # ------------------------------------

    # *** JFC: Those methods can be updated one day to use the direct (non-
    #     Tuber) links to the FPGA (on separate socket, forwarded to the FPGA
    #     through SPI or PCIe). Otherwise we fallback to the slower tuber MMI
    #     interface.
    @async
    def fpga_mmi_read(self, addr):
        """ Read a single 32-bit word from the FPGA at the specified byte
        address. This uses the fastest interface available (currently the ARM-
        FPGA SPI link)

        Value is returned as an unsigned integer.
        """
        word = yield self._fpga_spi_peek.async(addr)
        async_return(word & 0xFFFFFFFF)

    @async
    def fpga_mmi_write(self, addr, value):
        """ Write a single 32-bit word to the FPGA at specified byte address.
        This uses the fastest interface available (currently the ARM-FPGA SPI
        link)
        """
        yield self._fpga_spi_poke.async(addr, value)

    # # *** JFC: Proposed new names for the Tuber MMI access
    # @async
    # def fpga_tuber_spi_mmi_read(self, addr):
    #     """ Read a single 32-bit word at specified byte address through the
    #     SPI interface.
    #     """
    #     word = yield self._fpga_spi_peek.async(addr)
    #     async_return(word & 0xFFFFFFFF)

    # def fpga_tuber_spi_mmi_write(self, addr, value):
    #     """ Write a single 32-bit word at specified byte address through the
    #     SPI interface.
    #     """
    #     self._fpga_spi_poke(addr, value)

    # --------------------------------------------------------------------------
    # ARM Core methods (Should be implemented by the ARM and removed from here)
    # --------------------------------------------------------------------------
    # *** JFC:  Suggested method renaming
    def _write_motherboard_spi_eeprom_ipmi(self, *args, **kwargs):
        return self._eeprom_write_ipmi(*args, **kwargs)
    # *** JFC: Proposed renaming
    def _write_backplane_eeprom_ipmi(self, *args, **kwargs):
        return self._backplane_eeprom_write_ipmi(*args, **kwargs)


    # Mezzanine management

    # Backplane management
    @async
    def _mezzanine_eeprom_read(self, mezzanine):
        """ Returns the contents of the specified mezzanine's EEPROM.
        """
        data = yield self._mezzanine_eeprom_read_base64.async(mezzanine)
        async_return(base64.decodestring(data))

    # *** JFC: method rename
    @async
    def _write_motherboard_spi_eeprom_base64(self, *args, **kwargs):
        result = yield self._motherboard_eeprom_write_base64.async(*args, **kwargs)
        async_return(result)

    # def get_motherboard_serial(self):
    #     """ Read the motherboard serial number from the IPMI data. """
    #     ipmi = self._get_motherboard_ipmi()
    #     return str(ipmi.board.serial_number)

    # *** JFC: We now have the equivalent ARM method. Will delete this when we
    #     confirm it behaves the same.
    @async
    def get_slot_number(self):
        """ Reads the GPIO to determine in which slot number this IceBoard is
        connected.

        """
        if (yield self.is_backplane_present.async()):  # Is this test necessary?
            async_return( (yield self.get_backplane_slot.async()))
        else:
            async_return(None)

    # Bitstream management

    @async
    def get_fpga_bitstream_crc(self):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        is_fpga_programmed = yield self.is_fpga_programmed.async()
        if not is_fpga_programmed:
            async_return(None)
        crc = yield self.fpga_mmi_read.async(self.FPGA_FIRMWARE_CRC32_ADDR)
        async_return(crc)
        # return self._bitstream_crc

    @async
    def set_fpga_bitstream_crc(self, crc32):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        if (yield self.is_fpga_programmed.async()):
            # self._bitstream_crc = crc32
            yield self.fpga_mmi_write.async(self.FPGA_FIRMWARE_CRC32_ADDR, crc32)
        else:
            # self._bitstream_crc = None
            yield self.fpga_mmi_write.async(self.FPGA_FIRMWARE_CRC32_ADDR, 0)

    # def clear_fpga_bitstream(self):
    #     """ Stop the operation of the FPGA.

    #     Could be used if we detect that we don't have the right kind of
    #     mezzanines.
    #     """
    #     raise NotImplementedError()

    # Core firmware functions

    @async
    def get_fpga_core_cookie(self):
        """ Return the core FPGA firmware cookie. Should always be 0xBEEFFACE.
        """
        if not (yield self.is_fpga_programmed.async()):
            async_return(None)
        cookie = yield self.fpga_mmi_read.async(self.FPGA_CORE_FIRMWARE_COOKIE_ADDR)
        async_return(cookie)

    @async
    def get_fpga_application_cookie(self):
        """ Return the application-specific FPGA firmware cookie. """
        if not (yield self.is_fpga_programmed.async()):
            async_return(None)
        cookie = yield self.fpga_mmi_read.async(self.FPGA_APPLICATION_FIRMWARE_COOKIE_ADDR)
        async_return(cookie)

    @async
    def get_fpga_serial_number(self):
        """ Return the FPGA serial number, as read from the FPGA's core
        firmware throught the MMI interface. """
        fpga_serial_number = (
            (yield self.fpga_mmi_read.async(self.FPGA_SERIAL_NUMBER_LSW_ADDR)) |
            (yield (self.fpga_mmi_read(self.FPGA_SERIAL_NUMBER_MSW_ADDR) << 32))
            )

        async_return(fpga_serial_number)

    @async
    def get_fpga_firmware_timestamp(self):
        """ Returns a string containing the date-time of the currrent firmware
        bitstream.
        """
        timestamp = yield self.fpga_mmi_read.async(self.FPGA_FIRMWARE_TIMESTAMP_ADDR)
        seconds = (timestamp >> 0) & 0x3F
        minutes = (timestamp >> 6) & 0x3F
        hour = (timestamp >> 12) & 0x1F
        year = (timestamp >> 17) & 0x3F
        month = (timestamp >> 23) & 0x0F
        day = (timestamp >> 27) & 0x1F
        timestamp_string = '%04i-%02i-%02i %02i:%02i:%02i' % (
            year + 2000, month, day, hour, minutes, seconds)
        async_return(timestamp_string)

    @async
    def check_tuber_version(self):
        """ Check if the ARM processor provides the methods required to run this code. """

        required_tuber_methods = [
            'is_fpga_programmed']#, '_mezzanine_eeprom_read_base64']

        (meta, props, tuber_methods) = yield self._tuber_get_meta.async()  # get the meta info
        if not tuber_methods:
            raise RuntimeError("%r: The ARM does not publish any methods under the object name '%s'. Was the right Tuber object name used for this ARM firmware?" % (self, self.tuber_objname))

        for method in required_tuber_methods:
            if method not in tuber_methods:
                raise RuntimeError("%r: The current version of the ARM firmware does not provide the method '%s' that is needed for this application" % (self, method))

        async_return(True)

    def print_tuber_methods(self):
        """ Print all the methods and properties provided by the Iceboard's
        ARM processor through the Tuber protocol.
        """
        (meta, props, methods) = self._tuber_get_meta()  # get the meta info
        print "Methods for tuber object '%s':" % self.tuber_objname
        print '-----------------------------------------'
        for method_name, method_properties in sorted(methods.items()):
            print '%-30s: %s' % (method_name, method_properties.summary)
        print
        print "Properties for tuber object '%s':" % self.tuber_objname
        print '-----------------------------------------'
        for prop_name, prop_properties in sorted(props.items()):
            try:
                values = ', '.join('.%s' % p for p in prop_properties)
            except TypeError:
                values = '= %s' % prop_properties
            print '%-30s: %s' % (prop_name, values)

# @session.register_yaml_object
# @tuber.TuberCategory("Mezzanine", lambda m: m.iceboard,
#                      mezzanine=lambda m: m.mezzanine)
# # class CoreMezzanineHandler(_FMCMezzaninePythonSupport, handler.Handler):
#     """
#     Provides the methods needed to operate a mezzanine.
#     """
#     __handler_for__ = FMCMezzanine

#     def hwm_update(self, parent):
#         """ Import from the HWM object the main attributes needed to
#         operate the mezzanine.
#         """
#         self.iceboard = parent.iceboard.handler
#         self.mezzanine = parent.mezzanine

#     def __repr__(self):
#         # return '%s(%s)' % (self.__class__.__name__, self.serial)
#         return "%r.%s(%r,%r)" % (
#             self.iceboard,
#             self.__class__.__name__,
#             self.mezzanine,
#             self.serial)

#     def is_present(self):
#         """ returns a boolean indicating whether the ADC board is present"""
#         return self.iceboard.is_mezzanine_present(self.mezzanine)

