"""Base object for IceBoard hardware.

These objects are simple Python objects that represent the IceBoard, IceCrate,
and FMC Mezzanines. These are not instrumented as database objects (e.g with SQLAlchemy) nor are
they equipped with FPGA-firmware-specific support functions.

To specialize an IceBoard object for a particular experiment, you're
encouraged to create a subclass.
"""

import asyncio
import datetime
import base64
from . import tuber, hw

class IceCrateBase:
    """
    Basic IceCrate object.

    The object is a placeholder representation of a crate. It does not provide
    any backplane management functionalities; those are provided by the
    IceBoard.

    Parameters:

        serial : Serial number of the board. Typically in string format. Te
            serial number is typically provided by the hardware map or is
            extracted from the backplane EEPROM by the IceBoard before the
            object is created.

    This object does not manage its ``slot`` property, which maps an Iceboard object to each slot number.
    """
    serial = None  # The serial number written on the board (verbatim!)
    slot = {}  # {slot_number:IceBoard_object} dict describing the board of each slot

    def __init__(self, serial=None):
        self.serial = serial
        self.slot = {}


    def __repr__(self):
        return f"{self.__class__.__name__}({self.serial!r})"


class TuberIceBoardBase(tuber.TuberObject):
    """ Base class used to provide Tuber access to an Iceboard.

    The class imherits TuberObject and therefore exposes all functions
    provided by the board ARM processor software.

    Provides some Iceboard-specific Tuber wrappers methods in addition to the TuberObject loral and remote methods.

    In order to be operational, this class must be subclassed by a class that defines the following instance attributes:

        self.hostname
        self.serial
        self.slot
        self.crate
    """

    # Set the name of the set of software functions provided by Tuber (i.e.
    # the board's 'personnality'). This is needed by the TuberObject
    # superclass.
    tuber_objname = 'IceBoard'


    @property
    def tuber_uri(self):
        """Smarter, IceBoard-aware tuber_uri.

        The version of 'tuber_uri' in tuber.py doesn't know about calculating
        hostnames from serials, for instance.
        """

        if self.hostname:
            # We have a hostname; just use it.
            return f"http://{self.hostname}/tuber"

        if self.serial:
            # We have a serial number; compute the hostname.
            return f"http://iceboard{self.serial}.local/tuber"

        if self.slot and self.crate:
            # We have a slot and crate,
            # we can use the crate-based hostname (i.e. slot3.crate001.local).
            return f"http://slot{self.slot}.icecrate{self.crate.serial}.local/tuber"

        raise NameError(
            "Couldn't figure out a Tuber URI for this object! "
            "I need serial or crate information."
        )

    async def resolve_async(self):
        await self._tuber_get_meta_async()

    async def set_fpga_bitstream_buffer_async(self, buf):
        """
        Configures the FPGA with the specified buffer.

        The buffer is an ordinary string object or similar, and
        contains an already loaded .BIT or .BIN file.
        """
        b64_string = base64.b64encode(buf)
        await self._tuber_set_fpga_bitstream_base64_async(b64_string)

    async def _eeprom_write_ipmi_async(self, part_number, serial_number, product_version):
        """Write IPMI-formatted EEPROM for IceBoards.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting:

        >>> await m._eeprom_write_ipmi_async(
        ...     part_number="MGK7MB",
        ...     serial_number="004",
        ...     product_version="2")

        DON'T fill incorrect values unless they're visibly incorrect,
        since this data tends to be useful when debugging physical
        problems (e.g. tracing board history). Incorrect data that
        pretends to be valid can make this kind of debugging very painful.
        """

        fru = hw.ipmi_fru.FRU(
            board=hw.ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="Winterland",
                product_name="IceBoard",
                part_number=part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=hw.ipmi_fru.Product(
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
        return await self._tuber_motherboard_eeprom_write_base64_async(b64_string)

    async def _backplane_eeprom_write_ipmi_async(self, part_number, serial_number, product_version):
        """Write IPMI-formatted EEPROM for IceCrates.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting:

        >>> await m._backplane_eeprom_write_ipmi_async(
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
        """

        fru = hw.ipmi_fru.FRU(
            board=hw.ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="Winterland",
                product_name="IceCrate",
                part_number=part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=hw.ipmi_fru.Product(
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
        return await self._tuber_backplane_eeprom_write_base64_async(b64_string)



class IceBoardBase(TuberIceBoardBase):
    """Basic functional IceBoard object.


    """

    # class attributes are not needed.

    # hostname = None  # The hostname (or IP) to use for this resource
    # serial = None # Column(String, doc="The serial number written on the board (verbatim!)")
    # slot = None  # The IceCrate slot occupied by this board (first slot is slot 1)

    # backplane_serial = None  # The serial number of IceCrate occupied by this board

    # mezzanine = {}  # A {FMC_number, FMCMezzanine_object} dict describing the mezzanines. FMC number is 1 or 2.

    # Defines the set of software functionalities provided buy the ARM processor.
    # This overrides the TuberObject property so we can define a dynamic instance attribute.


    def __init__(self, hostname=None, serial=None):
        """

        The hostname, serial, crate and mezzanine are initialized only if they
        dont already exist, or if there are specified as non-None arguments.
        """
        # Make parameter initialization conditional for cases where those are initialized by classes that appear earlier in the MRO.

        self.hostname = hostname
        self.serial = serial
        self.crate = None
        self.mezzanine = {}

    def __repr__(self):
        return f"{self.crate!r}.{self.__class__.__name__}({(f'slot={self.slot}' if self.crate else f'serial={self.serial}' if self.serial else f'hostname={self.hostname}')})"



class FMCMezzanineBase():
    """Basic FMC Mezzanine object.

    This is an abstract class. To specialize it for a particular FMC
    mezzanine, create a subclass. There should be some good examples
    to borrow from; you should refer to them rather than this code.
    """

    serial = None
    mezzanine = None
    iceboard = None

    def __repr__(self):
        return f"{self.iceboard!r}.{self.__class__.__name__}({self.mezzanine!r},{self.serial!r})"

    async def eeprom_write_async(self, buf):
        """Writes a collection of bytes to the internal EEPROM.

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
        """
        b64_string = base64.b64encode(buf)
        await self.iceboard._tuber_mezzanine_eeprom_write_base64_async(self.mezzanine, b64_string, 0)


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
