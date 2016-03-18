"""Base object for IceBoard objects.

To specialize an IceBoard object for a particular experiment, you're
encouraged to create a subclass. There should be good examples
available.
"""

import tornado.gen
import select
import socket
import contextlib
import logging
import functools
import time

from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy import UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm.collections import attribute_mapped_collection

from . import hardware_map, tuber, async

from . import handler
from .handler import HandlerParentAttribute
from . import session
from hw import ipmi_fru
import datetime
import base64

@session.register_yaml_object()  # Todo: Add transforms={'move_index': ('slots', 'slot')}
class IceCrate(hardware_map.HWMResource, handler.HandlerObject):
    handler_name = 'IceCrateHandler'
    __tablename__ = 'icecrates'
    __table_args__ = (
        UniqueConstraint('_polymorphic_key', 'serial'),  # Crates with different models can have the same serial number
    )
    __mapper_args__ = {'polymorphic_identity': 'IceCrate',
                       'polymorphic_on': '_polymorphic_key'}
    __ipmi_part_number__ = None  # Must match part number in IPMI data

    _pk = Column(Integer, primary_key=True)
    _polymorphic_key = Column(String)  # Needed to allow multiple types of crates

    serial = Column(String,
                    doc="The serial number written on the board (e.g. '001')")

    slots = relationship(
        "IceBoard",
        lazy="dynamic",
        query_class=hardware_map.HWMQuery,
        doc='''A SQLAlchemy subquery corresponding to this IceCrate's
            IceBoards. If you want to index this array using slot index,
            you should use 'slot' instead.''')

    slot = relationship(
        "IceBoard",
        back_populates="crate",  # 'crate' is defined explicitely in IceBoard
        collection_class=attribute_mapped_collection('slot'),
        doc="The IceCrate's IceBoards, indexed as you would expect.")

    def __repr__(self):
        return "%s(SN%s)" % (self.__class__.__name__, self.serial)

    def resolve(self, timeout=5, bail_on_unexpected=True):
        '''Automatically detect boards in a crate.

        We want to support hardware maps like this:

            - !IceCrate
                serial: "001"
                slots:
                    1:  !IceBoard {}
                    3:  !IceBoard {}
                    13: !IceBoard {}

        ...this is a good compromise between underspecified and overspecified
        HWMs. We know how many boards to expect, but can swap them without
        altering the HWM.

        By itself, however, this HWM snippet does not include enough
        information to contact IceBoards. Python needs a hostname or serial
        number for each board.

        The "resolve" method discovers boards using DNS-SD (pybonjour), and
        assigns their serial numbers. It does so by watching for IceBoards to
        advertise themselves. When an IceBoard's advertised slot and backplane
        serial match, we update its serial in the HWM.
        '''

        import pybonjour  # only needed here, and not always installed

        logger = logging.getLogger(__name__)
        fds = []

        if self.slots.count() == 0:
            # Either no boards are present (silly!), or they're unspecified (we
            # don't know when discovery is complete.) Bail.
            raise ValueError("Can't resolve() iceboards in an empty crate!")

        if all(self.slots.serial):
            # We know each slot's serial number, so no need to resolve().
            return

        def resolve_callback(sdRef, flags, iface, err, fullname,
                             host, port, txtRecord, io_loop):
            if err != pybonjour.kDNSServiceErr_NoError:
                return

            # Parse TXT records. That's where the IceBoard publishes data.
            tr = pybonjour.TXTRecord.parse(txtRecord)

            # If this motherboard didn't tell us its serial, we can't proceed.
            if 'motherboard-serial' not in tr:
                return

            mb_serial = tr['motherboard-serial']

            logger.debug("DNS-SD discovered IceBoard serial %s at %s" % (
                mb_serial, host))

            if 'backplane-slot' not in tr:
                ib = IceBoard(serial=mb_serial)
                with ib.tuber_context() as ctx:
                    ctx._initialize_backplane()
                    ipmi = ctx._get_backplane_ipmi()
                    slot = ctx.get_backplane_slot()

                tr['backplane-slot'] = slot.result()
                tr['backplane-serial'] = ipmi.result().product.serial_number

                logger.debug("Patched up an IceBoard that didn't know about "
                             "its backplane IPMI data. Result: crate serial "
                             "%s, slot %s" % (tr['backplane-serial'],
                                              tr['backplane-slot']))

            # If the discovered board is in a backplane...
            if 'backplane-slot' in tr:
                bp_serial = tr['backplane-serial']
                slot = int(tr['backplane-slot'])

                logger.info("IceBoard %s is slot %s in backplane %s" % (
                    mb_serial, slot, bp_serial))

                # If this isn't our serial number, these aren't our boards.
                if bp_serial != self.serial:
                    logger.debug("Skipping IceBoard %s (Pondering crate %s, "
                                 "but this board is in crate %s.)" %
                                 (mb_serial, bp_serial, self.serial))
                    return

                # Is the discovered board in the HWM?
                if slot not in self.slots.slot:
                    if bail_on_unexpected:
                        # Eek!
                        raise NameError("Discovered unexpected IceBoard "
                                        "serial %s in IceCrate serial %s "
                                        "slot %s!" % (mb_serial, bp_serial,
                                                      slot))
                    # Otherwise, just don't continue.
                    return

                logger.info("Discovered IceBoard serial %s in slot %s of "
                            "backplane %s" % (mb_serial, slot, bp_serial))

                ib = self.slot[slot]
                ib.serial = tr['motherboard-serial']

                # Break out of loop if we know everything we need to
                if all(self.slots.serial):
                    io_loop.stop()

        def browse_callback(sdRef, flags, iface, err, service,
                            regtype, replyDomain, io_loop):

            if (err != pybonjour.kDNSServiceErr_NoError) or \
                    not (flags & pybonjour.kDNSServiceFlagsAdd):
                return

            resolver = pybonjour.DNSServiceResolve(
                0, iface, service, regtype, replyDomain,
                callBack=functools.partial(resolve_callback, io_loop=io_loop)
            )
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
            callBack=functools.partial(browse_callback, io_loop=io_loop)
        )
        fds.append(browser)
        io_loop.add_handler(
            browser.fileno(),
            lambda fd, events: pybonjour.DNSServiceProcessResult(browser),
            io_loop.READ)

        # Go!
        io_loop.start()

        # Clean up after Bonjour
        for fd in fds:
            fd.close()

        # Make sure we resolved all the boards in this crate
        if not all(self.slots.serial):
            missing = [ib.slot for ib in self.slots if not ib.serial]
            raise socket.timeout("IceBoards missing in slots %s!" %
                                 missing)

# The following defines the default handler for an Icecrate object
@tuber.TuberCategory("Backplane", lambda ic: ic.master_iceboard)
class IceCrateHandler(handler.Handler):
    """
    Provide the basic methods to operate the IceCrate.
    """
    __handler_for__ = IceCrate
    part_number = None
    NUMBER_OF_SLOTS = 0

    # Locally give access to the hardware_map attributes
    slot = property(lambda self: {slot:iceboard.handler for (slot, iceboard) in self.parent.slot.items()})
    serial = property(lambda self: self.parent.serial)

    @property
    def master_iceboard(self):
        active_iceboards = [(slot, iceboard) for (slot, iceboard) in self.slot.items() if iceboard.hostname or iceboard.serial]
        return sorted(active_iceboards)[0][1]

    def __repr__(self):
        return '%s(SN%s)' % (self.__class__.__name__, self.serial)

    def get_id(self):
        return 'No backplane'

    def init(self):
        pass


@session.register_yaml_object()  # Todo: add transforms={'move_index': ('mezzanines', 'mezzanine')}
class IceBoard(hardware_map.HWMResource, handler.HandlerObject):
    """ Provides access to the basic functions of an IceBoard.

    This object inherits from a generic Hardware Map Resource (HWMResource),
    which allows the iceboard objects to be added to the hardware
    map database.

    Project-specific classes are meant to be derived from this class.
    """
    __tablename__ = 'iceboards'
    __table_args__ = (
        UniqueConstraint('serial'),
    )
    __mapper_args__ = {
        'polymorphic_identity': "IceBoard",
        'polymorphic_on': '_cls'
    }

    handler_name = 'IceBoardHandler'  # Fixed, default handler

    _pk = Column(Integer, primary_key=True)
    _cls = Column(String, nullable=False)
    _icecrate_pk = Column(Integer, ForeignKey('icecrates._pk'), index=True)

    hostname = Column(String,
                      doc="The hostname (or IP) to use for this resource.")
    serial = Column(String,
                    doc="The serial number written on the board (verbatim!)")
    slot = Column(Integer, doc="The IceCrate slot occupied by this board")

    crate = relationship(
        "IceCrate",
        back_populates="slot",
        enable_typechecks=False,  # Allow IceCrate subclasses
        doc="The IceCrate in which this Iceboard is connected.")

    mezzanines = relationship(
        "FMCMezzanine",
        lazy="dynamic",
        query_class=hardware_map.HWMQuery,
        doc='''A SQLAlchemy subquery corresponding to this IceBoard's
            mezzanines. If you want to index this array using logical
            keys (1 or 2), you should use 'mezzanine' instead.''')

    mezzanine = relationship(
        "FMCMezzanine",
        backref=backref("iceboard"),
        collection_class=attribute_mapped_collection('mezzanine'),
        doc='''The IceBoard's mezzanines, indexed as you would expect
            (1 for mezzanine A, 2 for mezzanine B).''')

    def __repr__(self):
        """ Provides a concise string representation of this Iceboard that is
        informative enough to be used for logs.
        """
        if self.crate and self.slot:
            return "%s(C%s.S%02i)" % (self.__class__.__name__,
                                      self.crate.serial, self.slot)
        if self.serial:
            return "%s(SN%s)" % (self.__class__.__name__,  self.serial)
        if self.hostname:
            return "%s(%s)" % (self.__class__.__name__,  self.hostname)
        return "%s(?)" % (self.__class__.__name__)


class IceBoardHandler(handler.Handler, tuber.TuberObject):
    """ Provide the basic code needed to operate the Iceboard (i.e. Tuber-
    provided code and a few Python wrappers)
    """
    # Make this class (and any subclass) register with IceBoard
    __handler_for__ = IceBoard

    # Provide access to hardware_map attributes as if they were local
    hostname = HandlerParentAttribute(lambda ib: ib.hostname)
    serial = HandlerParentAttribute(lambda ib: ib.serial)
    crate = HandlerParentAttribute(lambda ib: ib.crate.handler if ib.crate else None)
    slot = HandlerParentAttribute(lambda ib: ib.slot)
    mezzanine = HandlerParentAttribute(lambda ib: {slot: mezz.handler if mezz else None for (slot, mezz) in ib.mezzanine.items()}, {})
    tuber_objname = HandlerParentAttribute(lambda ib: ib.__class__.__name__, 'IceBoard')


    def __repr__(self):
        """ Provides a concise string representation of this Iceboard that is
        informative enough to be used for logs.
        This string is typically used in syslog tags (32 characters max,
        alphanumeric characters only).
        """

        if self.crate and self.slot:
            return "%s(C%s.S%02i)" % (self.__class__.__name__,
                                      self.crate.serial, self.slot)
        if self.serial:
            return "%s(SN%s)" % (self.__class__.__name__,  self.serial)
        if self.hostname:
            return "%s(%s)" % (self.__class__.__name__,  self.hostname)
        return "%s(?)" % (self.__class__.__name__)

    def set_fpga_bitstream(self, buf):
        '''
        Configures the FPGA with the specified buffer.

        The buffer is an ordinary string object or similar, and
        contains an already loaded .BIT or .BIN file.
        '''
        b64_string = base64.b64encode(buf)
        self._set_fpga_bitstream_base64(b64_string)

    def _eeprom_write_ipmi(self, part_number, serial_number, product_version):
        '''Write IPMI-formatted EEPROM for IceBoards.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting:

        >>> m._eeprom_write_ipmi(
        ...     part_number="MGK7MB",
        ...     serial_number="0004",
        ...     product_version="2")

        DON'T fill incorrect values unless they're visibly incorrect,
        since this data tends to be useful when debugging physical
        problems (e.g. tracing board history). Incorrect data that
        pretends to be valid can make this kind of debugging very painful.
        '''

        fru = ipmi_fru.FRU(
            board=ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="Winterland",
                product_name="IceBoard",
                part_number=part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=ipmi_fru.Product(
                manufacturer="Winterland",
                product_name="IceBoard",
                part_number=part_number,
                product_version=product_version,
                serial_number=serial_number,
                asset_tag="",
                fru_file="",
            ),
            # multi=Multi(...), when it's supported by this code
        )
        b64_string = base64.b64encode(fru.encode())
        return self._motherboard_eeprom_write_base64(b64_string)

    def _backplane_eeprom_write_ipmi(self,
                                     part_number,
                                     serial_number,
                                     product_version):
        '''Write IPMI-formatted EEPROM for IceCrates.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting:

        >>> m._backplane_eeprom_write_ipmi(
        ...     part_number="MGK7BP",
        ...     serial_number="001",
        ...     product_version="00")

        DON'T fill incorrect values unless they're visibly incorrect,
        since this data tends to be useful when debugging physical
        problems (e.g. tracing board history). Incorrect data that
        pretends to be valid can make this kind of debugging very painful.

        This method is attached to an IceBoard instead of an IceCrate since
        you can't properly address an IceCrate unless its EEPROM has already
        been programmed. Chickens and eggs.
        '''
        chassis_type = ipmi_fru.CHASSIS_SUBCHASSIS

        fru = ipmi_fru.FRU(
            chassis=ipmi_fru.Chassis(
                type_code=chassis_type,
                part_number=part_number,
                serial_number=serial_number
            ),
            board=ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="Winterland",
                product_name="IceCrate",
                part_number=part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=ipmi_fru.Product(
                manufacturer="Winterland",
                product_name="IceCrate",
                part_number=part_number,
                product_version=product_version,
                serial_number=serial_number,
                asset_tag="",
                fru_file="",
            ),
            # multi=Multi(...), when it's supported by this code
        )
        b64_string = base64.b64encode(fru.encode())
        return self._backplane_eeprom_write_base64(b64_string)

    @property
    def tuber_uri(self):
        '''Smarter, IceBoard-aware tuber_uri.

        The version of 'tuber_uri' in tuber.py doesn't know about calculating
        hostnames from serials, for instance.
        '''

        if self.hostname:
            # We have a hostname; just use it.
            return 'http://{}/tuber'.format(self.hostname)

        if self.serial:
            # We have a serial number; compute the hostname.
            return 'http://iceboard{}.local/tuber'.format(self.serial)

        if self.slot and self.crate:
            # We're in a specified slot in a crate. For now, that means we
            # need IceCrate.resolve() to be called. When I2C contention on the
            # backplane isn't an issue, this is also enough to compute the
            # hostname (i.e. slot3.crate001.local).
            raise NameError("Slot and crate supplied, but you haven't called "
                            "'resolve' on the crate yet. Until you do, I "
                            "don't know how to talk to boards.")

        raise NameError("Couldn't figure out a Tuber URI for this object! "
                        "I need serial or crate information.")

    @property
    def _sleep_python(self):
        class P(async.Parallelizable):
            @tornado.gen.coroutine
            def __call_async__(self, delay):
                yield tornado.gen.Task(
                    tornado.ioloop.IOLoop.current().add_timeout,
                    datetime.timedelta(seconds=delay))
        return P()


@session.register_yaml_object()
class FMCMezzanine(hardware_map.HWMResource, handler.HandlerObject):
    """FMC Mezzanine schema object.

    This is an abstract class. To specialize it for a particular FMC
    mezzanine, create a subclass. There should be some good examples
    to borrow from; you should refer to them rather than this code.
    """
    handler_name = 'FMCMezzanineHandler'
    __tablename__ = 'fmc_mezzanines'
    __mapper_args__ = {
        'polymorphic_identity': 'fmc_mezzanine',
        'polymorphic_on': '_cls'
    }
    __table_args__ = (
        CheckConstraint(
            'mezzanine >= 1 and mezzanine <= 2',
            name='check_mezz_number'
        ),
    )
    __ipmi_part_number__ = 'Generic'  # Must match part number in IPMI data

    _pk = Column(Integer, primary_key=True)
    _cls = Column(String, nullable=False)
    _iceboard_pk = Column(Integer, ForeignKey('iceboards._pk'), index=True)

    serial = Column(String)
    mezzanine = Column(Integer)

    def __repr__(self):
        return "%r.%s(%r,%r)" % (
            self.iceboard,
            self.__class__.__name__,
            self.mezzanine,
            self.serial
        )

@tuber.TuberCategory("Mezzanine", lambda m: m.iceboard,
                     mezzanine=lambda m: m.mezzanine)
class FMCMezzanineHandler(handler.Handler):
    """
    Provides the basic methods needed to operate a mezzanine.
    """
    __handler_for__ = FMCMezzanine

    iceboard = HandlerParentAttribute(lambda ib: ib.iceboard)
    serial = HandlerParentAttribute(lambda ib: ib.serial)
    mezzanine = HandlerParentAttribute(lambda ib: ib.mezzanine)

    def __repr__(self):
        # return '%s(%s)' % (self.__class__.__name__, self.serial)
        return "%r.%s(%r,%r)" % (
            self.iceboard,
            self.__class__.__name__,
            self.mezzanine,
            self.serial)
    def eeprom_write(self, buf):
        '''Writes a collection of bytes to the internal EEPROM.

        Don't do this unless you're at McGill, and you're commissioning
        and testing a new mezzanine! This method makes it trivial to
        delete non-volatile data. Figuring out how to re-write the original
        data is a tougher nut to crack.

        According to FMC specs, the EEPROM is required to contain an
        IPMI FRU descriptor. If you want to insert this kind of data,
        you should use the 'ipmi_fru' module included in this Python
        repository.

        Since EEPROM contents are parsed by machine and used during
        board bring-up, it's important that the data you write is valid.
        You should refer to reference code (likely in the QC suite) rather
        than trying to guess what structures belong in here.
        '''
        b64_string = base64.b64encode(buf)
        self.iceboard._mezzanine_eeprom_write_base64(
            self.mezzanine,
            b64_string,
            0
        )

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
