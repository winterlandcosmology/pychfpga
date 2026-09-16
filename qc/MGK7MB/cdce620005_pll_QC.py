""" read/write IceBoard PLL using the FTDI cable.

Requirements:

    Cable: FTDI 3.3V USB-MPSSE (Multi-Protocol Synchronous Serial Engine) cable model C232HM-DDHSL-0
    Python modules:
        pip2 install pyftdi   # (this was tested with ==0.53.3. Previous version used with Py2.7 was ==0.13.4)

    Driver:
        Linux: libusb (http://www.libusb.org/)
        Windows: libusb-win32 (http://www.libusb.org/wiki/libusb-win32)

    For Ubuntu:
        You have to create a udef rule and add the user to the correct group to give access to the FTDI device. Instructions are here:

            https://eblot.github.io/pyftdi/installation.html

        Essentially:

        create file /etc/udev/rules.d/11-ftdi.rules containing:            

            SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6014", GROUP="plugdev", MODE="0664"

        Update the rules. Either plug-unplug the devide or:

            sudo udevadm control --reload-rules
            sudo udevadm trigger

        Add current user to group::

          sudo adduser $USER plugdev

NOTE: 
    - The ARM must NOT be initialized: It drives the clock line to zero.

Troubleshooting:
    - no permission to access USB device: The udev rule listed above must be created. 
Todo:
    - Write config from file
    - get config

Note:

    Default PLL config for the IceBoard
    #pll1_regs = [ 0x01260320,
    #              0xEB060301,
    #              0x011E0302,
    #              0xEB040303,
    #              0xEB860314,
    #              0x101C1E75,
    #              0x849F4FE6,
    #              0xBDB23BE7,
    #              0x20009CF8 ]


    #pll2_regs = [ 0xEB840320,
    #              0xEB840301,
    #              0xEB840302,
    #              0xEB860303,
    #              0xEB400014,
    #              0x101C1E75,
    #              0x84BF49A6,
    #              0xBDB23BE7,
    #              0x20009DD8 ]



"""
# Standard library packages
import sys
import time

# Pypi packages

from pyftdi import spi


class CDCE620005:

    opened_controllers = {}  # {url:SpiController instance, ...}

    def __init__(self, ftdi_url='ftdi://ftdi:232h/1', port=0, freq=100e3, mode=0):
        """

        Parameters:

            port: configured FTDI SPI port object obtained by calling get_port(...) on a SPiController object.
        """
        self.port = port
        self.dev = ftdi_url

        if ftdi_url in self.opened_controllers:
            self.spi = self.opened_controllers[ftdi_url]
        else:
            self.spi = self.opened_controllers[ftdi_url] = spi.SpiController(cs_count=4)
            self.spi.configure(ftdi_url)
        self.port = self.spi.get_port(cs=port, freq=freq, mode=mode)

    def write_read_word(self, word):

        def reverse_bits(w):
            return int('{:032b}'.format(w)[::-1], 2)

        bytes_out = reverse_bits(word).to_bytes(4, 'big')
        bytes_in = self.port.exchange(bytes_out, duplex=True)
        return reverse_bits(int.from_bytes(bytes_in, 'big'))


    def read_pll_reg(self, reg):
        if reg < 0 or reg > 8:
            raise ValueError('Register number must be between 0 and 8')
        self.write_read_word(0x0E | (reg << 4))
        time.sleep(0.1)
        word = self.write_read_word(0)
        return word

    def write_pll_reg(self, reg, value):
        if reg < 0 or reg > 8:
            raise ValueError('Register number must be between 0 and 8')
        self.write_read_word(reg | (value & 0xFFFFFFF0))

    def read_pll(self):
        regstore=[]
        for i in range(9):
            regstore.append(self.read_pll_reg(i))
        return regstore


    def program_pll(self, regs, write_eeprom=False):
        for reg, v in enumerate(regs):
            self.write_pll_reg(reg, v)
        if write_eeprom:
            self.write_read_word(0x0000001F)  # Write to EEPROM, but do not permanently lock it


def print_reg(reg):
    """ Prints the PLL register values provided in `reg`"""
    for i in range(8):
         print('Config Register %i: 0x%08X' % (i, reg[i]))

    if(len(reg)==9):
        print('Status Register %i: 0x%08X' % (8, reg[8]))


def comp_reg(desreg, measreg ):
    """ Check if the desired register values `desreg` match the measured register values `measreg` """
    passed = True
    for i in range(8):
        if(desreg[i] == measreg[i]):
            print('Register %i: should be 0x%08X and we measure 0x%08X: SAME' % (i, desreg[i], measreg[i]))
        else:
            print('Register %i: should be 0x%08X and we measure 0x%08X: DIFFERENT!' % (i, desreg[i], measreg[i]))
            passed = False
    if not passed:
        print("A difference was detected between the desired configuration and measured configuration")

    return passed