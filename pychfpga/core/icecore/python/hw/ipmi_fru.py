'''A Generator for IPMI Field Replaceable Unit (FRU) Descriptors.

This module is intended to create EEPROM data formatted according to IPMI FRU
documentation available here:

    http://www.intel.com/content/www/us/en/servers/ipmi/information-storage-definition.html

This data is mandated, for example, in the FPGA Mezzanine Card (FMC) standard.
Since we run into similar requirements elsewhere, we re-use this structure
where it makes sense.

You can create a fully-populated (if meaningless) FRU as follows:

    x = FRU(
        Internal("internal_data"),
        Chassis(CHASSIS_OTHER,
            "part_number",
            "serial_number"
        ),
        Board(datetime.now(),
            "manufacturer",
            "product_name",
            "serial_number",
            "part_number",
            "fru_file"
        ),
        Product("manufacturer",
            "product_name",
            "part_number",
            "product_version",
            "serial_number",
            "asset_tag",
            "fru_file"
        ),
        Multi(data_string), # or MultiDict(dict)
    )

Since these binary blobs are machine-parsed and used by on-board code to
identify and bring up hardware, it's important that you provide meaningful
and exact data. Do not rely on the example above -- there should be canonical
examples for each type of board under version control (most likely with the
board's QC scripts.)
'''

import struct
import json
from datetime import datetime, timedelta
from collections import OrderedDict
# Chassis type codes
CHASSIS_OTHER = 0x01
CHASSIS_UNKNOWN = 0x02
CHASSIS_DESKTOP = 0x03
CHASSIS_DESKTOP_LOW_PROFILE = 0x04
CHASSIS_PIZZA_BOX = 0x05
CHASSIS_MINI_TOWER = 0x06
CHASSIS_TOWER = 0x07
CHASSIS_PORTABLE = 0x08
CHASSIS_LAPTOP = 0x09
CHASSIS_NOTEBOOK = 0x0A
CHASSIS_HANDHELD = 0x0B
CHASSIS_DOCKING_STATION = 0x0C
CHASSIS_ALL_IN_ONE = 0x0D
CHASSIS_SUBNOTEBOOK = 0x0E
CHASSIS_SPACE_SAVING = 0x0F
CHASSIS_LUNCH_BOX = 0x10
CHASSIS_MAIN_SERVER = 0x11
CHASSIS_EXPANSION = 0x12
CHASSIS_SUBCHASSIS = 0x13
CHASSIS_BUS_EXPANSION = 0x14
CHASSIS_PERIPHERAL = 0x15
CHASSIS_RAID = 0x16
CHASSIS_RACK_MOUNT = 0x17
CHASSIS_SEALED_CASE_PC = 0x18
CHASSIS_MULTI = 0x19
CHASSIS_COMPACT_PCI = 0x1a
CHASSIS_ADVANCED_TCA = 0x1b
CHASSIS_BLADE = 0x1c
CHASSIS_BLADE_ENCLOSURE = 0x1d

# Multi type codes
MULTI_POWER_SUPPLY_INFORMATION = 0x00
MULTI_DC_OUTPUT = 0x01
MULTI_DC_LOAD = 0x02
MULTI_MANAGEMENT_ACCESS_RECORD = 0x03
MULTI_BASE_COMPATIBILITY_RECORD = 0x04
MULTI_EXTENDED_COMPATIBILITY_RECORD = 0x05
MULTI_OEM_RECORD_FIRST = 0xC0
MULTI_OEM_RECORD_LAST = 0x0FF

#  Helper functions used to decode EEPROM data

def get_area_data(read_fn, start_addr, area_name=''):
    """
    """
    # Get the first 3 bytes of data so we can do some checks and figure out the whole block length
    data = read_fn(start_addr, 3)
    version = ord(data[0])
    if version != 0x01:
        raise ValueError('Unsupported version in the IPMI %s field' % area_name)
    length = ord(data[1]) * 8
    # Now get all the data in the block and process it
    data = read_fn(start_addr, length)
    if sum(map(ord, data)) & 0xff:
        raise ValueError('Bad ckecksum in the IPMI %s field' % area_name)
    return data


def get_area_fields(data, area_name=''):
    """ Extract all fields in an area data block.
    """
    offset = 0
    fields = []
    while True:
        type_ = ord(data[offset]) & 0xC0
        length = ord(data[offset]) & 0x3F
        offset += 1
        if type_ != 0xC0:
            raise ValueError('Unsupported field type 0x%02X' % type_)
        if length == 1:
            break
        else:
            fields.append(data[offset:offset + length])
            offset += length
    return fields


class Internal(object):
    def __init__(self, data):
        self.data = data

    def encode(self):
        """Reduce to a string containing binary IPMI data."""
        return struct.pack('B %is 0q' % len(self.data), 0x01, self.data)

    @classmethod
    def decode(cls, read_function, start, max_length):
        data = read_function(start, max_length)
        version = ord(data[0])
        if version != 0x01:
            raise ValueError('Unsupported version in the IPMI %s field' % cls.__name__)
        return cls(data[1:])

class Chassis(object):
    def __init__(self, type_code=CHASSIS_UNKNOWN, part_number='', serial_number=''):
        self.type_code = type_code
        self.part_number = part_number
        self.serial_number = serial_number

    def encode(self):
        """Reduce to a string containing binary IPMI data."""
        # If any string fields are of length 1, bump them to 2. Otherwise, we
        # run into the terminator code 0xc1.
        lprt = 2 if len(self.part_number) == 1 else len(self.part_number)
        lser = 2 if len(self.serial_number) == 1 else len(self.serial_number)

        # Work out the structure length, including padding to 8-byte alignment
        length = 7 + lprt + lser
        length += -length % 8

        # Build up the structure.
        x = struct.pack(
            'BBB B%is B%is B x 0q' % (lprt, lser),
            0x01,            # version
            length / 8,      # length
            self.type_code,  # chassis type
            0xc0 | lprt, self.part_number,
            0xc0 | lser, self.serial_number,
            0xc1
        )

        # Replace last byte with checksum
        return x[:-1] + chr((-sum(map(ord, x)) & 0xff))

    @classmethod
    def decode(cls, read_fn, start_addr):
        data = get_area_data(read_fn, start_addr, area_name=cls.__name__)
        chassis_type = data[2]
        fields = get_area_fields(data[3:], area_name=cls.__name__)
        return cls(type_code=chassis_type,
                   part_number=fields[0],
                   serial_number=fields[1])


class Board(object):
    def __init__(self,
                 mfg_date=0,
                 manufacturer='',
                 product_name='',
                 serial_number='',
                 part_number='',
                 fru_file=''):

            self.mfg_date = mfg_date
            self.manufacturer = manufacturer
            self.product_name = product_name
            self.serial_number = serial_number
            self.part_number = part_number
            self.fru_file = fru_file

    def encode(self):
        """Reduce to a string containing binary IPMI data."""

        # Convert mfg_date from a Python datetime() to an IPMI minutes count
        if self.mfg_date:
            mfg_date = self.mfg_date - datetime(1996, 1, 1)
            mfg_date = int(round(mfg_date.total_seconds() / 60))
        else:
            mfg_date = 0  # unspecified date, as per IPMI standard

        lman = 2 if len(self.manufacturer) == 1 else len(self.manufacturer)
        lprd = 2 if len(self.product_name) == 1 else len(self.product_name)
        lser = 2 if len(self.serial_number) == 1 else len(self.serial_number)
        lprt = 2 if len(self.part_number) == 1 else len(self.part_number)
        lfru = 2 if len(self.fru_file) == 1 else len(self.fru_file)

        # Work out the structure length, including padding to 8-byte alignment
        length = 13 + lman + lprd + lser + lprt + lfru
        length += -length % 8

        # Build up the structure.
        x = struct.pack(
            'BBB BBB B%is B%is B%is B%is B%is B x 0q' % (
                lman, lprd, lser, lprt, lfru),
            0x01,       # version
            length/8,   # length
            0x00,       # language code (english)
            mfg_date >> 16, (mfg_date >> 8) & 0xff, mfg_date & 0xff,
            0xc0 | lman, self.manufacturer,
            0xc0 | lprd, self.product_name,
            0xc0 | lser, self.serial_number,
            0xc0 | lprt, self.part_number,
            0xc0 | lfru, self.fru_file,
            0xc1
        )

        # Replace last byte with checksum
        return x[:-1] + chr((-sum(map(ord, x)) & 0xff))

    @classmethod
    def decode(cls, read_fn, start_addr):
        data = get_area_data(read_fn, start_addr, area_name=cls.__name__)
        language_code = ord(data[2])
        if language_code != 0x00:
            raise ValueError('Unsupported language code 0x%02x in the IPMI %s area' % (language_code, cls.__name__))
        mfg_minutes = ord(data[3]) + ord(data[4])*256 + ord(data[5])*65536
        mfg_date = datetime(1996, 1, 1) + timedelta(0, 60*mfg_minutes)
        fields = get_area_fields(data[6:], area_name=cls.__name__)
        return cls(mfg_date=mfg_date,
                   manufacturer=fields[0],
                   product_name=fields[1],
                   serial_number=fields[2],
                   part_number=fields[3],
                   fru_file=fields[4])


class Product(object):
    def __init__(self,
                 manufacturer='',
                 product_name='',
                 part_number='',
                 product_version='',
                 serial_number='',
                 asset_tag='',
                 fru_file=''):

        self.manufacturer = manufacturer
        self.product_name = product_name
        self.part_number = part_number
        self.product_version = product_version
        self.serial_number = serial_number
        self.asset_tag = asset_tag
        self.fru_file = fru_file

    def encode(self):
        """Reduce to a string containing binary IPMI data."""

        lman = 2 if len(self.manufacturer) == 1 else len(self.manufacturer)
        lprd = 2 if len(self.product_name) == 1 else len(self.product_name)
        lprt = 2 if len(self.part_number) == 1 else len(self.part_number)
        lver = 2 if len(self.product_version) == 1 else len(self.product_version)
        lser = 2 if len(self.serial_number) == 1 else len(self.serial_number)
        ltag = 2 if len(self.asset_tag) == 1 else len(self.asset_tag)
        lfru = 2 if len(self.fru_file) == 1 else len(self.fru_file)

        # Work out the structure length, including padding to 8-byte alignment
        length = 12 + lman + lprd + lprt + lver + lser + ltag + lfru
        length += -length % 8

        # Build up the structure.
        x = struct.pack(
            'BBB B%is B%is B%is B%is B%is B%is B%is B x 0q' % (
                lman, lprd, lprt, lver, lser, ltag, lfru
            ),
            0x01,       # version
            length/8,   # length
            0x00,       # language code (english)
            0xc0 | lman, self.manufacturer,
            0xc0 | lprd, self.product_name,
            0xc0 | lprt, self.part_number,
            0xc0 | lver, self.product_version,
            0xc0 | lser, self.serial_number,
            0xc0 | ltag, self.asset_tag,
            0xc0 | lfru, self.fru_file,
            0xc1
        )

        # Replace last byte with checksum
        return x[:-1] + chr((-sum(map(ord, x)) & 0xff))

    @classmethod
    def decode(cls, read_fn, start_addr):
        data = get_area_data(read_fn, start_addr, area_name=cls.__name__)
        language_code = ord(data[2])
        if language_code != 0x00:
            raise ValueError('Unsupported language code 0x%02x in the IPMI %s area' % (language_code, cls.__name__))
        fields = get_area_fields(data[3:], area_name=cls.__name__)
        return cls(manufacturer=fields[0],
                   product_name=fields[1],
                   part_number=fields[2],
                   product_version=fields[3],
                   serial_number=fields[4],
                   asset_tag=fields[5],
                   fru_file=fields[6])

class Multi(object):
    def __init__(self,
        string='',
        type_id=MULTI_OEM_RECORD_FIRST,
        end_of_list=True):

        self.string = string
        self.type_id = type_id
        self.end_of_list = end_of_list

    def encode(self):
        """
        Returns a string containing a MultiRecords that contain the specified string.
        Multirecords do not need to be aligned to 8-byte boundaries, so no padding in included.
        """
        if len(self.string) > 255:
            raise TypeError('String length exceeds 255 characters')
        # Build up the header structure.
        header = chr(self.type_id) + chr(0x80 * bool(self.end_of_list) | 0x02) + chr(len(self.string))
        header += chr((-sum(map(ord, self.string)) & 0xff)) # add record checksum
        header += chr((-sum(map(ord, header)) & 0xff)) # add header checksum
        return header + self.string

class MultiDict(object):
    """ Represents a dictionary into a number of Multi records encoded in Json format.

    Typical Usage:
    x = FRU(
        Internal(...),
        Chassis(...),
        Board(...),
        Product(...),
        MultiDict(user_dictionary)
    )

    All Multi blocks are type ID=0xC0.

    The Multi blocks are filled by alphabetical dictionary key name until a block size
    (including the block identifier) exceeds 255 characters, at which point a
    new block is created.

    Each block is a JSON dictionary containing the fields:
        MultiType: 'json_v1' # Used by the decoder to identify the data format
        Record: (integer) # Record number, starting at 1
        Data: (dict) # Subset of the dictionary items that fits in this block

    There is no attempt to reorder dictionary items in order to make blocks as
    big as possible. There is no compression, or blank space removal. Since
    Multi records are just daisy-chained at the end of the other IPMI blocks
    and are not addressed by the IPMI Common Header block, we do not have the
    2K limitation and can fill the typically bigger eeproms with Multi data.

    The function will fail if a dictionary item is too large to fit in a single Multi block.
    """

    def __init__(self, data):
        multi_string = ""
        record_number = 1
        remaining_data = dict(data) # create a copy of the dictionary, as we are going to remove data from it
        block_data = OrderedDict()
        block_dict = OrderedDict()
        # json_data = ""
        self.multi = []

        while remaining_data:
            test_key = sorted(remaining_data.keys())[0]
            test_data = OrderedDict(block_data)
            test_data[test_key] = remaining_data[test_key]
            test_dict = OrderedDict([('MultiType','json_v1'), ('Record',record_number), ('Data',test_data)])
            test_json_data = json.dumps(test_dict)
            if len(test_json_data) > 255:
                # multi_string += Multi(json_data, end_of_list = False).encode()
                block_data = OrderedDict()
                record_number += 1
                self.multi.append(block_dict)
            else:
                # json_data = test_json_data
                block_data = test_data
                block_dict = test_dict
                remaining_data.pop(test_key)
        # multi_string += Multi(json_data, end_of_list = True).encode()
        self.multi.append(block_dict)

        # return multi_string


    def encode(self):
        multi_string = ""
        for multi in self.multi[:-1]:
            multi_string += Multi(json.dumps(multi), end_of_list = False).encode()
        multi_string += Multi(json.dumps(self.multi[-1]), end_of_list = True).encode()
        return multi_string


class FRU(object):
    def __init__(self,
                 internal=None,
                 chassis=None,
                 board=None,
                 product=None,
                 multi=None):

        self.internal = internal
        self.chassis = chassis
        self.board = board
        self.product = product
        self.multi = multi

    def encode(self):
        """Reduce to a string containing binary IPMI data."""

        # Zero offsets indicate "block not present"
        internal_offset = 0x00
        chassis_offset = 0x00
        board_offset = 0x00
        product_offset = 0x00
        multi_offset = 0x00

        # Pack structures
        offset = 8

        internal_offset = offset if self.internal else 0x00
        internal_str = self.internal.encode() if self.internal else ""
        offset += len(internal_str)

        chassis_offset = offset if self.chassis else 0x00
        chassis_str = self.chassis.encode() if self.chassis else ""
        offset += len(chassis_str)

        board_offset = offset if self.board else 0x00
        board_str = self.board.encode() if self.board else ""
        offset += len(board_str)

        product_offset = offset if self.product else 0x00
        product_str = self.product.encode() if self.product else ""
        offset += len(product_str)

        multi_offset = offset if self.multi else 0x00
        multi_str = self.multi.encode() if self.multi else ""
        offset += len(multi_str)

        header = struct.pack(
            "B BBBBB x B",
            0x01,   # version
            internal_offset / 8,
            chassis_offset / 8,
            board_offset / 8,
            product_offset / 8,
            multi_offset / 8,
            0x00,   # checksum placeholder
        )

        # Replace last header byte with checksum
        header = header[:-1] + chr((-sum(map(ord, header)) & 0xff))

        # Append data
        return header + \
            internal_str + \
            chassis_str + \
            board_str + \
            product_str + \
            multi_str

    def as_dict(self):
        """ Return the FRU information as a dictionary.
        """
        return {
            'internal': self.internal.__dict__ if self.internal else None,
            'chassis': self.chassis.__dict__ if self.chassis else None,
            'board': self.board.__dict__ if self.board else None,
            'product': self.product.__dict__ if self.product else None,
            'multi': self.multi.__dict__ if self.multi else None
            }

    def __str__(self):
        """  Provide a nicely formatted version of the IPMI data
        """
        result = 'IPMI data block:\n'
        for block_name in [name for name in vars(self) if not name.startswith('_')]:
            block = getattr(self, block_name)
            if block:
                result += '   %s:\n' % block_name
                for field_name in [name for name in vars(block) if not name.startswith('_')]:
                    result += '      %s: %r\n' % (field_name, getattr(block, field_name))
            else:
                result += '   %s: Empty\n' % block_name
        return result

    @classmethod
    def decode(cls, str):
        """ Convert IPMI binary data into a FRU object.

        ``str`` is the string to decode.

        For slow access devices, ``str`` can be function str(addr, length)
        that is to obtain the IPMI binary data piece by piece as needed so
        that all the memory device does not have to be read at once.  If
        length = -1, all data from addr to the end of the IPMI storage should
        be returned.

        """

        if callable(str):
            read_function = str
        else:
            read_function = lambda addr, length, _str=str: _str[addr: addr + length] if length >= 0 else _str[addr:]

        buf = read_function(0, 8) # read common header (8 bytes)

        if sum(map(ord, buf)) & 0xff:
            raise ValueError('Bad ckecksum in the IPMI Common Header. Is the EEPROM initialized?')
        (
            version,
            internal_offset,
            chassis_offset,
            board_offset,
            product_offset,
            multi_offset,
            pad
         ) = struct.unpack("B BBBBB B x", buf)

        if version != 0x01:
            raise ValueError('Bad version 0x%02X in the IPMI Common Header. Is the EEPROM initialized?' % version)
        if pad:
            raise ValueError('Bad pad 0x%02X in the IPMI Common Header. Is the EEPROM initialized?' % pad)

        # Convert offsets to actual addresses
        internal_offset *= 8
        chassis_offset *= 8
        board_offset *= 8
        product_offset *= 8
        multi_offset *= 8


        # Internal block
        #
        # This block has no intrinsic length. We limit its content to the
        # beginning of the following block. If there is no following block, we
        # use the rest of the storage.
        if internal_offset:
            next_block_offset = min(
                [offset for offset in (chassis_offset, board_offset, product_offset, multi_offset) if offset > internal_offset],
                0)
            max_length = next_block_offset - internal_offset if next_block_offset else -1 # max_length = -1 means read until end of storage
            internal = Internal.decode(internal_offset, read_function, max_length)
        else:
            internal = None

        chassis = Chassis.decode(read_function, chassis_offset) if chassis_offset else None
        board   = Board.decode(read_function, board_offset)     if board_offset else None
        product = Product.decode(read_function, product_offset) if product_offset else None

        if multi_offset:
            raise ValueError('Sorry, Multi fields are not supported yet.')
        else:
            multi = None # Not supported yet

        return FRU(
                 internal=internal,
                 chassis=chassis,
                 board=board,
                 product=product,
                 multi=multi
                 )


# vim: sts=4 ts=4 sw=4 tw=80 smarttab expandtab
