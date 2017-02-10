""" read/write IceBoard PLL using the FTDI cable.

Requirements:

    Cable: FTDI 3.3V USB-MPSSE (Multi-Protocol Synchronous Serial Engine) cable model C232HM-DDHSL-0
    Python modules:
        pyftdi (pip install git+https://github.com/eblot/pyftdi.git  to make sure we get the latest version)
        pyusb (installed when instaklling pyftdi)
    Driver:
        Linux: libusb (http://www.libusb.org/)
        Windows: libusb-win32 (http://www.libusb.org/wiki/libusb-win32)
    For Ubuntu:
        create /etc/udev/rules.d/99-FTDI-pllprog.dules file containing
        "SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", MODE="666""

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
#loop through devices, printing vendor and product ids in decimal and hex
#for cfg in dev:
#   sys.stdout.write('Decimal VendorID=' + str(cfg.idVendor) + ' & ProductID=' + str(cfg.idProduct) + '\n')
#   sys.stdout.write('Hexadecimal VendorID=' + hex(cfg.idVendor) + ' & ProductID=' + hex(cfg.idProduct) + '\n\n')

s=spi.SpiController(cs_count=4, silent_clock=False)
s.configure(0x403, 0x6014, 0)
pll1port=s.get_port(0)
pll1port.set_frequency(1000)
pll2port=s.get_port(1)
pll2port.set_frequency(1000)


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

def program_pll(port, regs, write_eeprom=False):
    for reg, v in enumerate(regs):
        write_pll_reg(port, reg, v)
    if write_eeprom:
        write_pll(port, 0x0000001F)  # Write to EEPROM, but do not permanently lock it

def program_pll1(regs, write_eeprom=False):
    program_pll(port=pll1port, regs=regs, write_eeprom=write_eeprom)

def program_pll2(regs, write_eeprom=False):
    program_pll(port=pll2port, regs=regs, write_eeprom=write_eeprom)

# def program_pll1(port=pll1port, regs, write_eeprom=False):
#     regs = [ 0x01260320,
#              0xEB060301,
#              0x011E0302,
#              0xEB040303,
#              0xEB860314,
#              0x101C1E75,
#              0x849F4FE6,
#              0xBDB23BE7,
#              0x20009CF8 ]

#     for reg, v in enumerate(regs):
#         write_pll_reg(port, reg, v)
#     if write_eeprom:
#         write_pll(port, 0x0000001F)  # Write to EEPROM, but do not permanently lock it

# def program_pll2(port=pll2port, write_eeprom=False):
#     regs = [ 0xEB840320,
#              0xEB840301,
#              0xEB840302,
#              0xEB860303,
#              0xEB400014,
#              0x101C1E75,
#              0x84BF49A6,
#              0xBDB23BE7,
#              0x20009DD8 ]

#     for reg, v in enumerate(regs):
#         write_pll_reg(port, reg, v)
#     if write_eeprom:
#         write_pll(port, 0x0000001F)  # Write to EEPROM, but do not permanently lock it

def read_pll(port):
    regstore=[]
    for i in range(9):
        regstore.append(read_pll_reg(port,i))
    return regstore

def read_pll1():
    regstore = read_pll(port=pll1port)
    return regstore

def read_pll2():
    regstore = read_pll(port=pll2port)
    return regstore

def print_reg(reg):
    for i in range(8):
         print 'Config Register %i: 0x%08X' % (i, reg[i])

    if(len(reg)==9):
        print 'Status Register %i: 0x%08X' % (9, reg[8])


def comp_reg(desreg, measreg ):
    passed = 1
    for i in range(8):
        if(desreg[i] == measreg[i]):
            print 'Register %i: should be 0x%08X and we measure 0x%08X: SAME' % (i, desreg[i], measreg[i])
        else:
            print 'Register %i: should be 0x%08X and we measure 0x%08X: DIFFERENT!' % (i, desreg[i], measreg[i])
            passed = 0;
    if(passed == 0):
        print "A difference was detected between the desired configuration and measured configuration"

    return passed