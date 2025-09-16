""" Memory Mapped interface

..  History:
    2011-08-03 JFC : Created from ANT.py

    2011-09-25 JFC: Added read_DRP and read_RAM

    2012-06-23 JFC: Added bitfield_property to introduce a new way to define
        bitfields (allows these bitfields to be more easily referred to as
        function arguments, and makes pylint happier) Fixed class name
        printing when raising exception when attempting to write to a locked
        attribute

    2012-07-23 JFC: Fixed read_ and write_bitfield to correctly handle data as
        big endian (MSB at lower address). Added 32-bit field support.

    2012-07-25 JFC: added bitfield() to facilitate access to bitfield properties and methods
"""

import numpy as np
import time
import logging

# In python, we encode the page as extra address bits 20:19. Those are stripped out downstream and
# converted into the appropriate opcode by BSB_MMI.
_CONTROL_BASE_ADDR = 0x000000
_STATUS_BASE_ADDR = 0x080000
_RAM_BASE_ADDR = 0x100000  # also used for DRP access


# Page values
CONTROL = 0  # Control bytes (read/write)
STATUS = 1  # STATUS bytes (read only)
RAM = 2  # RAM or FIFO
DRP = 3  # Dynamic Reconfiguration Port

# Match the page number with the corresponding address offset
PAGE_OFFSET = {
    CONTROL: _CONTROL_BASE_ADDR,
    STATUS: _STATUS_BASE_ADDR,
    RAM: _RAM_BASE_ADDR,
    DRP: _RAM_BASE_ADDR
    }


class BitField(object):
    """
    Holds the definition of a memory-mapped variable.

    It is implemented as a data descriptor that calls the read_bitfield() and
    write_field() properties of the parent object when accessed.
    """
    # Page values
    CONTROL = CONTROL  # Control bytes (read/write)
    STATUS = STATUS  # STATUS bytes (read only)
    RAM = RAM  # RAM or FIFO
    DRP = DRP  # Dynamic Reconfiguration Port

    def __init__(self, page, addr, bit, width=1, default=None, doc='No documentation available'):

        if page not in PAGE_OFFSET:
            raise ValueError('Unknown page %i' % page)

        self.page = page
        self._addr = addr  # relative address of the last control byte within the page
        self.bit = bit
        self.width = width
        self.default = default
        self.doc = doc

        # Pre-process some readout parameters to improve speed
        self.lsb_addr = addr - bit // 8  # rightmost byte address
        self.msb_addr = addr - (bit + width - 1) // 8  # leftmost byte address
        self.number_of_bytes = self.lsb_addr - self.msb_addr + 1
        self.name = 'Unknown'

    def __set__(self, obj, value):
        self.write(obj, value)

    def __get__(self, obj, obj_type):
        if obj is None:  # if not accessed from an instance
            return self
        else:
            return self.read(obj)

    def __set_name__(self, obj, name):
        """ Set the name of the attribute to which this descriptor is assigned to.

        This is called automatically when the owner class is created (Python 3.6)
        """
        self.name = name

    def write(self, obj, value):
        # obj.write_bitfield(self, value)
        if (value >= 2 ** self.width) or value < 0:
            raise Exception(f'Bad value {value} for memory-mapped property {self.name}. '
                            f'Valid range is between 0 and {2**self.width-1}')

        if self.page == self.DRP:
            old_data = obj.read_drp(self._addr)  # read 16-bit value
            mask = (2 ** self.width - 1) << self.bit
            new_data = old_data & ~mask
            new_data |= ((value << self.bit) & mask)
            obj.write_drp(self._addr, new_data)
        elif self.page == self.CONTROL:
            number_of_bytes = (self.bit + self.width - 1) // 8 + 1
            mask_bytes = (((1 << self.width) - 1) << self.bit).to_bytes(number_of_bytes, 'big')
            data_bytes = (value << self.bit).to_bytes(number_of_bytes, 'big')
            obj.write_control(self.msb_addr, data_bytes, mask=mask_bytes)

        elif self.page == self.STATUS:
            raise RuntimeError('Cannot write to a STATUS register')
        elif self.page == self.RAM:
            raise RuntimeError('Cannot use a bitfield to write to a RAM page')
        else:
            raise RuntimeError(f'Unknown page {self.page}')  # Should never happen, was tested in __init__

    def read(self, obj):

        if self.page == self.DRP:
            value = obj.read_drp(self._addr)  # read 16-bit value
        elif self.page == self.CONTROL:
            value = obj.read_control(self.msb_addr, type=int, length=self.number_of_bytes)
        elif self.page == self.STATUS:
            value = obj.read_status(self.msb_addr, type=int, length=self.number_of_bytes)
        elif self.page == self.RAM:
            value = obj.read_ram(self.msb_addr, type=int, length=self.number_of_bytes)
        else:
            raise RuntimeError(f'Unknown page {self.page}')  # Should never happen, was tested in __init__

        # if verbose:
        #     print('Read base address %05X, addr: %i - %i, bit %i, width=%i, value=%i' % (obj.base_address, msb_addr, lsb_addr, self.bit, self.width, data))
        # print 'Read bit at port %i, bit=%i, data: %X' % (bit_name,  bit_def.addr,bit_def.bit, data)

        # Extract the desired bits
        return (int(value) >> self.bit) & ((1 << self.width)-1)

class MMIRouter(object):
    """ Class representing the function of a BSB router.

    This class represents a BSB router.

    In case of a top router (i.e a router that does not have a parent), do not specify `router`
      and `router_port`, but instead have the top router subclass ADDRESS_WIDTH with the BSB full
      address space and specify `fpga_instance` at router instantiation.

    """

    ADDRESS_WIDTH = None  # Number of address bits used by this module. None means the address width will be determined by the parent router address width and port number width.
    ROUTER_PORT_NUMBER_WIDTH = None # Number MSB bits of this router's address space used to select the port. Must be defined in all subclasses.
    ROUTER_PORT_MAP = {} # Optional map to associate port names with port indices.

    def __init__(self, *, router=None, router_port=None, fpga_instance=None):

        self.logger = logging.getLogger(__name__)
        if not self.ROUTER_PORT_NUMBER_WIDTH:
            raise RuntimeError('ROUTER_PORT_NUMBER_WIDTH must be defined in MMIRouter subclasses')
        if router is None: # if this is the top router
            self.fpga = fpga_instance
            self.base_address = 0
            self.address_width = self.ADDRESS_WIDTH
        else:
            self.fpga = router.fpga
            self.base_address, self.address_width = router.get_port_addr(router_port)

    def get_port_addr(self, router_port):
            """ Return the base address and maximum address width of the specified port.
            """
            if isinstance(router_port, str):
                router_port = self.ROUTER_PORT_MAP[router_port]
            if router_port >= 1 << self.ROUTER_PORT_NUMBER_WIDTH:
                raise RuntimeError('Invalid port number {port_number} for router {self}. Port number width is {self.ROUTER_PORT_NUMBER_WIDTH}')
            max_address_width = self.address_width - self.ROUTER_PORT_NUMBER_WIDTH
            base_address = self.base_address + (router_port << max_address_width)
            return base_address, max_address_width

class MMI(object):
    """ Provides access to the memory-mapped resources of a module.

    It is intended to be inherited by classes of FPGA modules possess a Byte-Serial Bus (BSB) registers/memory/DRP access.

    Parameters:

        fpga_instance (FPGA): Instance of the FPGA object through which the BSB will be accessed

        base_address (int): Base address of the module. Default to 0. It can be modified by the following:

            - If a parent module is specified, its base address is added to base_address.
            - If router_port is specified, the base address is increased by ``router_port<<ADDRESS_WIDTH``.

        instance_number (int): arbitrary number indicating which module this is in an array.

        router (MMI): Specified a parent module containing a router from which the base_address should be derived from.

        router_port (int): Indicates the port number of the router to which the module is connected.
            Defaults to 0, which can also mean there is no router. Is simply used to offset the base address in combination with the
            router_port_lsb.

        router_port_lsb (int): Position of the LSB of the port number in the address word. Defaults to the modules ADDRESS_WIDTH, i.e. (possibly wrongly) assumes that the routing address sits right on top of the module address space.

    NOTE:

        - Subclass shall redefine the ADDRESS_WIDTH attribute to match the address space used by the
          FPGA module. This is necessary to compute the proper address increments for each port of
          the router if ``router_port_lsb`` is not specified, and allows address range verifications.

        - The defaut valaue of `router_port_lsb` makes the dubious assumption that the module
          addressing bits start on bit ADDRESS_WIDTH. It is convenient as we have one more less
          argument to pass to MMI. If the module does not use all the bits immediately below the
          port addessing bits, specify `router_port_lsb`.  The reason this is a dubious default is
          that in theory, the modules know nothing about the upstream router.

    """
    _locked = False  # when 1, prevents new attributes from being created

    ADDRESS_WIDTH = None  # Number of address bits used by this module. If None, the address space provided by the upstream router port will be used.

    CONTROL = CONTROL
    STATUS = STATUS
    DRP = DRP

    def __init__(self, *, router=None, router_port=None, instance_number=None):

        self.logger = logging.getLogger(__name__)

        self._unlock()

        self.instance_number = instance_number
        self.fpga = router.fpga
        self.base_address, max_address_width = router.get_port_addr(router_port)
        if self.ADDRESS_WIDTH and self.ADDRESS_WIDTH > max_address_width:
            raise RuntimeError('Insufficient post-routing address width for module {self!r}. The module needs {self.ADDRESS_WIDTH} bits but the router port offers {max_address_width} bits of addressing space')
        self.address_width = self.ADDRESS_WIDTH or max_address_width
        self.address_max = (1 << self.address_width) - 1

    def __repr__(self):
        """ Return a string that represents this object and its parent object.
        """
        inst = self.instance_number if self.instance_number is not None else ''
        return f"{self.fpga!r}.{self.__class__.__name__}[{inst}]"


    def __setattr__(self, name, value):
        """ Prevents creating new attributes to the class when _locked==1"""
        # Allow write only if not locked or if attribute already exists in the
        # class. We do not use hasattr(self,name) because this invokes
        # __getattr__(self,name), which will retreive bitfield values over the
        # network and slows down the program needlessly.
        if (not self._locked) or name in self.__class__.__dict__ or name in self.__dict__:

#            print 'setting ',name
            object.__setattr__(self, name, value)
        else:
            print("Class '%s' is locked: cannot assign new attribute '%s'" % (self, name))
            raise AttributeError("This instance of class '%s' is locked: cannot assign new attribute '%s'" % (self.__class__.__name__, name)) # 120623 JFC

    def __getitem__(self, index):
        return self.read(index)

    def __setitem__(self, index, value):
        self.write(index, value)

    def _unlock(self):
        self.__dict__['_locked'] = False

    def _lock(self):
        self.__dict__['_locked'] = True

    def read(self, addr, type, length, **kwargs):
        """ Reads bytes from the FPGA memory-mapped registers according to the specified `type` and `length`.
        """
        # if isinstance(addr, int):
        return self.fpga.mmi.read(self.base_address + addr, type=type, length=length, **kwargs)
        # elif isinstance(addr, str):
        #     return self.fpga.read(self.base_address + self.BITS[addr].addr, *args, **kwargs)

    def read_bit(self, addr, bit):
        """ Reads a bit from a FPGA memory-mapped register."""
        return bool(self.read(addr, type=int, length=1) & (1 << bit))

    def read_drp(self, addr):
        """
        Reads a DRP (Dynamic Reconfigurable Port) from one of the FPGA
        internal devices (PLL, SYSMON, MGT etc). 'addr' is the 16-bit DRP
        register address.
        """
        if not 0 <= 2 * addr <= self.address_max:
            raise RuntimeError('%r: Invalid DRP address %i' % (self, addr))
        return self.read(_RAM_BASE_ADDR + 2 * addr, type=np.dtype('<u2'), length=1)[0]

    def read_ram(self, addr, type, length, **kwargs):
        """
        Reads an array of `length` bytes from address `addr` in the RAM space
        """
        if not 0 <= addr <= self.address_max:
            raise RuntimeError('%r: Invalid RAM address %i' % (self, addr))
        return self.read(_RAM_BASE_ADDR + addr, type=type, length=length, **kwargs)

    def read_status(self, addr, type, length, **kwargs):
        """
        Reads a `length` bytes from the STATUS registers from address `addr`
        """

        if not 0 <= addr <= 0x07F:
            raise RuntimeError('%r: Invalid STATUS register address %i' % (self, addr))
        return self.read(_STATUS_BASE_ADDR + addr, type=type, length=length, **kwargs)

    def read_control(self, addr, type, length):
        """
        Reads `length` bytes from the CONTROL registers starting at address `addr`
        """

        if not 0 <= addr <= 0x07F:
            raise RuntimeError('%r: Invalid CONTROL register address %i' % (self, addr))
        return self.read(_CONTROL_BASE_ADDR + addr, type=type, length=length)

    def read_bitfield(self, bitfield, verbose=0):
        """ Reads the field identified by the name 'bit_name' which is looked
        up in the BITS table to find the bit definition (port, bit position
        etc). Returns a boolean."""

        if isinstance(bitfield, str):
            bitfield = self.get_bitfield(bitfield)

        return bitfield.read(self)

    def write_bitfield(self, bitfield, data):
        """ Writes 'data' to the bitfield.

        Parameters:

            bitfield (bitfield or str): bitfield object to write to. `bitfield` can be either a bitfield object or a string containing the
                name of the bitfield.

            data (int): Value to be written as a zero or positive integer
        """

        if isinstance(bitfield, str):
            bitfield = self.get_bitfield(bitfield)

        bitfield.write(self, data)

    # write_field = write_bitfield # for backwards compatibility

    def write(self, addr, data, *args, **kwargs):
        """
        Writes an array of bytes to the FPGA memory-mapped address space. Address ``addr`` is
        relative to the base address of the current MMI module.

        The MSBs of ``addr`` determines the page in which data is written
        (control, status, RAM etc.). use ``write_control(...)``, ``write_ram(...)`` etc. to write to specific pages.

        Returns the number of bytes written.
        """
        return self.fpga.mmi.write(self.base_address + addr, data, *args, **kwargs)

    def write_ram(self, addr, data, *args, **kwargs):
        """
        Writes within the RAM/FIFO address space of the module. Simply calls the write() function with the appropriate address offset.
        """

        if not 0 <= addr <= self.address_max:
            raise RuntimeError('%r: Invalid RAM address %i' % (self, addr))
        return self.write(_RAM_BASE_ADDR + addr, data, *args, **kwargs)

    def write_control(self, addr, data, *args, **kwargs):
        """
        Writes data bytes to control register(s).

        Parameters:

            addr (int): Address of the first control byte relative to the base address of the current module instance

            data (bytes or ndarray): data bytes to write

            args, kwargs: additional arguments passed to fpga_mmi_write, including mask

        """
        if not 0 <= addr <= 0x07F:
            raise RuntimeError('%r: Invalid CONTROL register address %i' % (self, addr))
        return self.write(_CONTROL_BASE_ADDR + addr, data, *args, **kwargs)


    def write_drp(self, addr, data):
        """
        Writes a 16-bit value `data` to a register of a DRP (Dynamic Reconfigurable Port) of the FPGA internal devices (PLL, SYSMON, MGT etc).

        Parameters:

            addr (int): Address of the 16-bit DRP register word (the address is internally multiplied by 2 to convert it to a byte address)

            data (int): 16-bit value to be written

        Notes:

            - The DRP and RAM pages use the same address space. Either one or the other is connected to the module.
            - The DRP values are stored as little endians
        """
        if not 0 <= 2 * addr <= self.address_max:
            raise RuntimeError('%r: Invalid DRP address %i' % (self, addr))
        if not 0 <= data <= 65535:
            raise AttributeError('%r: Invalid unsigned 16-bit DRP register value %i' % (self, data))
        return self.write(_RAM_BASE_ADDR + 2 * addr, bytes([data & 0xFF, (data >> 8) & 0xFF]))

    write_DRP = write_drp

    def write_bit(self, addr, bit):
        """ Sets a bit of the FPGA memory-mapped registers"""
        mask = (1 << bit)
        old_value = self.read(addr)
        self.write(addr, old_value & ~mask)
        self.write(addr, old_value | mask)

    # def write_mask(self, addr, mask, data):
    #     old_value = self.read(addr)
    #     self.write(addr, (old_value & ~mask) | (data & mask))

    def get_bitfield(self, bitfield_name):
        """
        Returns the bitfield object with name 'bitfield_name'.
        This is used to access the attributes and methods of the bitfield objects, since this is a python data descriptor and direct access calls its fget() method instead of returning the object.
        """
        if not isinstance(bitfield_name, str):
            raise TypeError('The bitfield name must be a string')

        try:
            bitfield = getattr(type(self), bitfield_name)
            if not isinstance(bitfield, BitField):
                raise TypeError("'%s' is not a Bitfield" % bitfield_name)
            return bitfield
        except AttributeError:
            raise AttributeError("The BitField '%s' is not defined" % bitfield_name)

    def get_addr(self, bitfield_name):
        """
        Returns the address of the bitfield relative to the base address,
        including the page offset. To be used directly with the read() and
        write() methods.

        Parameters:

            bitfild_name (str): name of the bitfield

        Returns:

            address (int), relative to the current  module base address. The
            address includes the page (CONTROL/STATUS/RAM/DRP) offset.
        """
        bitfield = self.get_bitfield(bitfield_name)
        return bitfield._addr + PAGE_OFFSET[bitfield.page]

    def pulse_bit(self, bitfield_name, bit=0):
        """
        Pulses the bitfield specified by the string ``bitfield_name`` to '1' then back to '0'.
        """

        bitfield = self.get_bitfield(bitfield_name)
        if bitfield.width != 1:
            raise TypeError('The bitfield must be a single bit (width=1)')
        bitfield.write(self, 1)
        bitfield.write(self, 0)

    def wait_for_bit(self, bitfield_name, timeout=1, target_value=1, no_error=False):
        """
        Wait for the bitfield specified by the string ``bitfield_name`` to return the value ``target_value``.
        ``True`` is returned when the value is found before ``timeout`` seconds, otherwise a RuntimeError exception is raised if ``no_error`` is False, or ``False`` is returned if ``no_error`` is True.
        """

        bitfield = self.get_bitfield(bitfield_name)

        # mask = (1 << bit)
        t0 = time.time()
        while True:
            if self.read_bitfield(bitfield) == target_value:
                return True
            if (time.time() - t0) > timeout:
                if no_error:
                    return False
                else:
                    raise RuntimeError("Timeout exceeded while waiting for bitfield %s==%i" % (bitfield_name, target_value))

    def read_all_fields(
            self,
            format='%(name)-30s = %(page_name)7s(0x%(addr)-02X)[%(bit_range)-5s]:  '
                   '%(value)5i, 0x%(hex_value)-4s, 0b%(bin_value)s',
            sort = ['page','name']):
        """ Returns a list of all bitfields and their values.

        Parameters:

            format (str): if None, the method returns a list of dict describing each bitfield. If `format` is a string, the method returns a list
                of strings describing the bitfields, where each string is formatted using the provided string format. The string format can refer to the keys below.

                - name (str),
                - page (int),
                - page_name (str),
                - addr (int),
                - bit (int),
                - bit_range (str),
                - width (int),
                - doc (str),
                - value (int),
                - bin_value (str)

            sort (list): list of strings indicateing on which field(s) to sort the bitfield list

        Returns:

            list: list of strings if  `format` is a string, list of dict if format is None.
        """
        def entries():  # generator to list all the bitfield values
            for (name, bitfield) in vars(type(self)).items():
                if isinstance(bitfield, BitField):
                    value = getattr(self, name)
                    entry = {'name': name,
                             'page': bitfield.page,
                             'page_name': ('CONTROL', 'STATUS', 'RAM', 'DRP')[bitfield.page],
                             'addr': bitfield._addr,
                             'bit' : bitfield.bit,
                             'bit_range' : '%i' % bitfield.bit if bitfield.width<=1 else '%i:%i' % (bitfield.bit+bitfield.width-1, bitfield.bit),
                             'width' : bitfield.width,
                             'doc' : bitfield.doc,
                             'value': value,
                             'bin_value': ('{0:0%ib}' % bitfield.width).format(value),
                            #  'hex_value': ('{0:0%iX}' % (bitfield.width + 3) // 4).format(value)
                             'hex_value': ('{0:0%iX}' % (bitfield.width + 3)).format(value)
                             }
                    yield entry
        table = list(entries())
        if not format:
            return table
        if sort:
            if not isinstance(sort, list):
                sort = [sort]
            for sort_key in sort[::-1]:
                table.sort(key=lambda x: x[sort_key])
        if format:
            return [format % entry for entry in table]
        else:
            return table

    def init(self):
        pass
