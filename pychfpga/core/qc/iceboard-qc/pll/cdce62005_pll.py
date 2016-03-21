""" read/write IceBoard PLL using the FTDI cable.

Requirements:

    Cable: FTDI 3.3V USB-MPSSE (Multi-Protocol Synchronous Serial Engine) cable model C232HM-DDHSL-0
    Python modules:
        pyftdi (pip install git+https://github.com/eblot/pyftdi.git  to make sure we get the latest version)
        pyusb (installed when instaklling pyftdi)
    Driver:
        Linux: libusb (http://www.libusb.org/)
        Windows: libusb-win32 (http://www.libusb.org/wiki/libusb-win32)


NOTE: The ARM must not be initialized: It drived the clock line to zero.

Todo:
    - Convert code indo CDCE62005 module
    - Write config from file
    - get config
    - maybe get rid of SPI module altogether? (just use FTDI module?) Or subclass it.




"""

import sys
import usb.core
import struct
from pyftdi import ftdi, spi
from pyftdi.ftdi import Ftdi
from array import array as Array
# find USB devices
dev = usb.core.find(find_all=True)
# loop through devices, printing vendor and product ids in decimal and hex
for cfg in dev:
  sys.stdout.write('Decimal VendorID=' + str(cfg.idVendor) + ' & ProductID=' + str(cfg.idProduct) + '\n')
  sys.stdout.write('Hexadecimal VendorID=' + hex(cfg.idVendor) + ' & ProductID=' + hex(cfg.idProduct) + '\n\n')


def read(port, readlen):
        """Perform a half-duplex transaction with the SPI slave"""
        ctrl = port._controller
        cs_cmd = port._cs_cmd
        cs_high = Array('B', [Ftdi.SET_BITS_LOW, ctrl._cs_bits, ctrl._direction])

        read_cmd = struct.pack('<BH', Ftdi.READ_BYTES_PVE_MSB, readlen-1)
        cmd = Array('B', cs_cmd)
        cmd.fromstring(read_cmd)
        cmd.extend(ctrl._immediate)
        cmd.extend(cs_high)
        ctrl._ftdi.write_data(cmd)
        # USB read cycle may occur before the FTDI device has actually
        # sent the data, so try to read more than once if no data is
        # actually received
        data = ctrl._ftdi.read_data_bytes(readlen, 4)
        return data

def write(port, out):
        """Perform a half-duplex transaction with the SPI slave"""
        ctrl = port._controller
        cs_cmd = port._cs_cmd
        cs_high = Array('B', [Ftdi.SET_BITS_LOW, ctrl._cs_bits, ctrl._direction])

        write_cmd = struct.pack('<BH', Ftdi.WRITE_BYTES_NVE_MSB, len(out)-1)
        cmd = Array('B', cs_cmd)
        cmd.fromstring(write_cmd)
        cmd.extend(out)
        cmd.extend(cs_high)
        ctrl._ftdi.write_data(cmd)

def array_to_word(a):
    w = 0
    for bit in range(8*len(a)):
        w |= (1<<bit) * bool(a[bit//8] & (1<<(7-(bit%8))))
    return w

def word_to_array(w):
    a = [0,0,0,0]
    for bit in range(32):
        a[bit//8] |= (1<<(7-(bit%8))) * bool(w & (1<<bit))
    return a


def write_pll(port, word):
    a = word_to_array(word)
    write(port, a)

def read_pll(port):
    a = read(port, 4)
    word = array_to_word(a)
    #print '%08X' % word
    return word

def read_pll_reg(port, reg):
    if reg<0 or reg>8:
        raise ValueError('Register number must be between 0 and 8')
    write_pll(port, 0x0E | (reg << 4))
    a = read(port, 4)
    word = array_to_word(a)
    #print '%08X' % word
    return word

def write_pll_reg(port, reg, value):
    if reg<0 or reg>8:
        raise ValueError('Register number must be between 0 and 8')
    write_pll(port, reg | (value & 0xFFFFFFF0))

def program_pll2(port, write_eeprom=False):
    regs= [0xEB840320,
        0xEB840301,
        0xEB840302,
        0xEB860303,
        0xEB400014,
        0x101C1E75,
        0x84BF49A6,
        0xBDB23BE7,
        #0x20009D98
        ]
    for reg, v in enumerate(regs):
        write_pll_reg(port, reg, v)
    if write_eeprom:
        write_pll(port, 0x0000001F)  # Write to EEPROM, but do not permanently lock it

s=spi.SpiController(cs_count=4, silent_clock=False)
s.configure(0x403, 0x6014, 0)
p=s.get_port(0)
p.set_frequency(1000)
p1=s.get_port(1)
p1.set_frequency(1000)

print 'PLL1 contents is:'
for i in range(9):
    print 'Register %i: %08X' % (i, read_pll_reg(p,i))

print 'PLL2 contents is:'
for i in range(9):
    print 'Register %i: %08X' % (i, read_pll_reg(p1,i))



#Decimal VendorID=1027 & ProductID=24596
#Hexadecimal VendorID=0x403 & ProductID=0x6014

"""
Iceboard0130 PLL1 contents is:
Register 0: 01260320
Register 1: EB060301
Register 2: 011E0302
Register 3: EB040303
Register 4: EB860314
Register 5: 101C1E75
Register 6: 849F4FE6
Register 7: BDB23BE7
Register 8: 20009D18

PLL2
['EB840320',
 'EB840301',
 'EB840302',
 'EB860303',
 'EB400014',
 '101C1E75',
 '84BF49A6',
 'BDB23BE7',
 '20009DD8']


DRAO PLL1 contents is:
Register 0: 01260320
Register 1: EB060301
Register 2: 011E0302
Register 3: EB040303
Register 4: EB860314
Register 5: 101C1E75
Register 6: 849F4FE6
Register 7: BDB23BE7
Register 8: 20009D18


DRAO PLL2 config
['EB840320',
 'EB840301',
 'EB840302',
 'EB860303',
 'EB400014',
 '101C1E75',
 '84BF49A6',
 'BDB23BE7',
 '20009DF8']
"""
