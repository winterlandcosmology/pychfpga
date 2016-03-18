#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
Module.py module
  Module base class definition
#
# History:
    2011-08-03 JFC : Created from ANT.py
    2011-09-25 JFC: Added read_DRP and read_RAM
    2012-06-23 JFC: Added bitfield_property to introduce a new way to define bitfields (allows these bitfields to be more easily referred to as function arguments, and makes pylint happier)
        Fixed class name printing when raising exception when attempting to write to a locked attribute
    2012-07-23 JFC: Fixed read_ and write_bitfield to correctly handle data as big endian (MSB at lower address).
        Added 32-bit field support.
    2012-07-25 JFC: added bitfield() to facilitate access to bitfield properties and methods
"""

import numpy as np
import time

_CONTROL_BASE_ADDR = 0x000000
_STATUS_BASE_ADDR = 0x080000
_RAM_BASE_ADDR = 0x100000

class BitField(object):
    """
    Holds the definition of a memory-mapped variable
    It is implemented as a data descriptor that calls the read_field() and write_field() properties of the parent object when accessed.
    """
    # Page values
    CONTROL = 0  # Control bytes (read/write)
    STATUS = 1  # STATUS bytes (read only)
    RAM = 2  # RAM or FIFO
    DRP = 3  # Dynamic Reconfiguration Port


    def __init__(self, page, addr, bit, width=1, default=None, doc='No documentation available'):
        self.page = page
        self._addr = addr
        self.bit = bit
        self.width = width
        self.default = default
        self.doc = doc

    def __set__(self, obj, value):
        obj.write_field(self, value)

    def __get__(self, obj, obj_type):
        if obj is not None:  # if accesed from an instance
            return obj.read_field(self)
        else:
            return self

    def get_addr(self):
        """
        Returns the Memory-mapped address corresponding to the bit field
        """
        if self.page == self.CONTROL:
            return _CONTROL_BASE_ADDR + (self._addr & 0x07F)
        elif self.page == self.STATUS:
            return _STATUS_BASE_ADDR + (self._addr & 0x07F)
        elif self.page == self.RAM:
            return _RAM_BASE_ADDR + (self._addr & 0x1FF)
        elif self.page == self.DRP:
            return _RAM_BASE_ADDR + ((self._addr << 1) & 0x1FF)

    addr = property(get_addr, doc='Returns the memory-mapped address of the current bitfield item')

class Module_base(object):
    """ Implements basic interfaces to a module. It is intended to be inherited by a subclass that specializes to specific modules"""
    _locked = False # when 1, prevents the object to be modified

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS
    DRP = BitField.DRP

    #BitDef=BitDef_base # make class accessible to subclass (somehow the class is not inherited directly)
    # BITS = {} # Should be overriden by the subclass

    def __init__(self, fpga_instance, base_address, instance_number=0):
        self._unlock()
        self.fpga = fpga_instance
        self.base_address = base_address
        self.instance_number = instance_number
        # self.module_number = module_number
        # for field_name, bitfield in self.BITS.items():
        #     setattr(self.__class__, field_name, bitfield)
        #     print ' OBSOLETE:  Defining property "%s"' % (field_name)

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
            print "Class '%s' is locked: cannot assign new attribute '%s'" % (self, name)
            raise AttributeError("This instance of class '%s' is locked: cannot assign new attribute '%s'" % (self.__class__.__name__, name)) # 120623 JFC

    def __getitem__(self, index):
        return self.read(index)

    def __setitem__(self, index, value):
        self.write(index, value)

    def _unlock(self):
        self.__dict__['_locked'] = False

    def _lock(self):
        self.__dict__['_locked'] = True

    def read(self, addr, *args, **kwargs):
        """ Reads bytes from the FPGA memory-mapped registers."""
        # if isinstance(addr, int):
        return self.fpga.mmi.read(self.base_address + addr, *args, **kwargs)
        # elif isinstance(addr, str):
        #     return self.fpga.read(self.base_address + self.BITS[addr].addr, *args, **kwargs)

    def read_bit(self, addr, bit):
        """ Reads a bit from a FPGA memory-mapped register."""
        return bool(self.read(addr) & (1 << bit))

    def read_drp(self, addr):
        """
        Reads a DRP (Dynamic Reconfigurable Port) from one of the FPGA
        internal devices (PLL, SYSMON, MGT etc). 'addr' is the 16-bit DRP
        register address.
        """
        return self.read(_RAM_BASE_ADDR + 2*addr, type=np.dtype('<u2'))

    def read_ram(self, addr, *args, **kwargs):
        """
        Reads a byte from the RAM space
        """
        return self.read(_RAM_BASE_ADDR + addr, *args, **kwargs)

    def read_status(self, addr, *args, **kwargs):
        """
        Reads byte(s) from the STATUS registers
        """
        return self.read(_STATUS_BASE_ADDR + addr, *args, **kwargs)

    def read_field(self, bitfield, verbose=0):
        """ Reads the field identified by the name 'bit_name' which is looked
        up in the BITS table to find the bit definition (port, bit position
        etc). Returns a boolean."""

        if bitfield.page == BitField.DRP:
            data = self.read_drp(bitfield._addr) # read 16-bit value
            return (data >> bitfield.bit) & ((1 << bitfield.width)-1)

        word_width = 8
        lsb_addr = bitfield.addr - int(bitfield.bit//word_width)
        msb_addr = bitfield.addr - int((bitfield.bit+bitfield.width-1)//word_width)
        number_of_bytes = lsb_addr - msb_addr + 1
        data_type = {1: np.dtype('>u1'),
                     2: np.dtype('>u2'),
                     4: np.dtype('>u4'),
                     8: np.dtype('>u8')}[number_of_bytes]
        data = int(self.read(msb_addr, type=data_type))
        if verbose:
            print 'Read base address %05X, addr: %i - %i, bit %i, width=%i, value=%i' % (self.base_address, msb_addr, lsb_addr, bitfield.bit, bitfield.width, data)
        #print 'Read bit at port %i, bit=%i, data: %X' % (bit_name,  bit_def.addr,bit_def.bit, data)
        return (data >> bitfield.bit) & ((1 << bitfield.width) - 1)

    def write_field(self, bitfield, data):
        """ Writes the field identified by the name 'bit_name' which is looked up in the BITS table to find the bit definition (port, bit position etc). Returns a boolean."""

        if (data >= 2**bitfield.width) or data < 0:
            raise Exception('Bad value %i for memory-mapped property %s' % (data, bitfield))

        if bitfield.page == BitField.DRP:
            old_data = self.read_drp(bitfield._addr)  # read 16-bit value
            mask = (2**bitfield.width-1) << bitfield.bit
            new_data = old_data & ~mask
            new_data |= ((data << bitfield.bit) & mask)
            self.write_drp(bitfield._addr, new_data)
            return

        # word_width = 8
        # lsb_addr = bitfield.addr - int(bitfield.bit / word_width)
        number_of_bytes = (bitfield.bit + bitfield.width-1) // 8 + 1
         # = lsb_addr - msb_addr + 1
        mask_string = np.array(((1 << bitfield.width)-1) << bitfield.bit, '>u8').tostring()
        data_string = np.array(data << bitfield.bit, '>u8').tostring()
        self.write(bitfield.addr - number_of_bytes + 1,
                   data_string[-number_of_bytes:],
                   mask=mask_string[-number_of_bytes:])

    def write(self, addr, data, *args, **kwargs):
        """ Writes bytes to the FPGA memory-mapped registers.
        Returns the number of bytes written.
        """
        return self.fpga.mmi.write(self.base_address + addr, data, *args, **kwargs)

    def write_ram(self, addr, data, *args, **kwargs):
        """
        Writes within the RAM/FIFO address space of the module. Simply calls the write() function with the appropriate address offset.
        """
        return self.write(_RAM_BASE_ADDR + addr, data, *args, **kwargs)

    def write_control(self, addr, data, *args, **kwargs):
        """
        Writes to control register(s).
        """
        return self.write(_CONTROL_BASE_ADDR + addr, data, *args, **kwargs)

    def write_drp(self, addr, data):
        """
        Writes a DRP (Dynamic Reconfigurable Port) of the FPGA internal devices (PLL, SYSMON, MGT etc).
        'addr' is the 16-bit DRP register address.
        """
        return self.write(_RAM_BASE_ADDR + 2*addr, [data & 0xFF, (data >> 8) & 0xFF])

    write_DRP = write_drp

    def write_bit(self, addr, bit):
        """ Sets a bit of the FPGA memory-mapped registers"""
        mask = (1 << bit)
        old_value = self.read(addr)
        self.write(addr, old_value & ~mask)
        self.write(addr, old_value | mask)

    def write_mask(self, addr, mask, data):
        old_value = self.read(addr)
        self.write(addr, (old_value & ~mask) | (data & mask))

    def get_bitfield(self, bitfield_name):
        """
        Returns the bitfield object with name 'bitfield_name'.
        This is used to access the attributes and methods of the bitfield objects, since this is a python data descriptor and direct access calls its fget() method instead of returning the object.
        """
        try:
            bitfield = getattr(type(self), bitfield_name)
            if not isinstance(bitfield, BitField):
                raise TypeError("'%s' is not a Bitfield" % bitfield_name)
            return bitfield
        except AttributeError:
            raise AttributeError("The BitField '%s' is not defined" % bitfield_name)

    def get_addr(self, bitfield_name):
        """
        Returns the address of the register containing the specified bitfield.
        """
        return self.get_bitfield(bitfield_name).get_addr()

    def pulse_bit(self, addr, bit=0):
        """
        Pulses the specified bit to '1' then back to '0'.
        if 'addr' is numeric, the bit 'bit' at address 'addr' is pulsed.
        If 'addr' is a string containing the name of a bit field, then this bit is pulsed.
        """

        if isinstance(addr, str):
            bitfield = self.get_bitfield(addr)
            if bitfield.width != 1:
                raise Exception('The bit field must be a single bit (width=1)')
            else:
                (addr, bit) = (bitfield.addr, bitfield.bit)

        mask = (1<<bit)
        old_value = self.read(addr)
        self.write(addr, old_value | mask) # Set bit to '1'
        self.write(addr, old_value & ~mask) # Set bit to '0'

    def wait_for_bit(self, addr, bit=0, timeout=1):
        """
        Wait for specified bit to become '1'.
        if 'addr' is numeric, the bit 'bit' at address 'addr' is pulsed.
        If 'addr' is a string containing the name of a bit field, then this bit is pulsed.
        """
        if isinstance(addr, str):
            bitfield = self.get_bitfield(addr)
            if bitfield.width != 1:
                raise Exception('The bit field must be a single bit (width=1)')
            else:
                (addr, bit) = (bitfield.addr, bitfield.bit)

        mask = (1 << bit)
        t0 = time.time()
        while 1:
            if self.read(addr) & mask:
                return
            if (time.time() - t0) > timeout:
                raise(Warning('Timeout exceeded while waiting for status bit'))

    def read_all_fields(self, format='%(name)-30s = %(page_name)7s(0x%(addr)-02X)[%(bit_range)-5s]:  %(value)5i, 0x%(hex_value)-4s, 0b%(bin_value)s', sort = ['page','name']):
        """ Returns a list of all bitfields and their values.
            Each element of the list is a dictionary describing the bitfield with the following keys:
                name (str), page (int), page_name (str), addr (int), bit (int), bit_range (str), width (int), doc (str), value (int), bin_value (str)
            'sort' indicated on which field(s) to sort the list
            If a 'format' string is specified, a list of  strings formatted using the specified format is returned instead.
        """
        def entries():  # generator to list all the bitfield values
            for (name, bitfield) in vars(type(self)).items():
                if type(bitfield) is BitField:
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
                             'hex_value': ('{0:0%iX}' % int((bitfield.width+3)/4)).format(value)
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
