#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 

"""
IOExpander.py module 
 Implements IOExpander interface of chFPGFA

 History:
 2011-07-08 JFC : Created from test code in chFPGA.py
 2011-07-17 JFC: Fixed the getter and setter : needed to use a closure to fix the bit name at function creation. 
            Created bit definition class to access elements by name instead of index. Updated code appropriately.
 2011-07-18 JFC: Added comments
 2011-09-08 JFC: Removed printed message when defining the properties   
"""
import time
import warnings
import numpy as np
import logging

class IOExpander_base(object):

    _locked = False # when 1, prevents the object from being modified

    """ Class Providing interfaces to the IOExpander on the ADC FMC board.""" 
    # Register addresses (assumes BANK=0, which is the default after power-up)
    REG_IODIRA   = 0x00 # GPIO pin is 1=input, 0 = output
    REG_IODIRB   = 0x01
    REG_IOPOLA   = 0x02 # 0= non-inverted, 1 = inverted
    REG_IOPOLB   = 0x03
    REG_GPINTENA = 0x04 # 1 = enable interrupt
    REG_GPINTENB = 0x05
    REG_DEFVALA  = 0x06 # Comparison value for interrupts
    REG_DEFVALB  = 0x07
    REG_INTCONA  = 0x08 # Interrupt control
    REG_INTCONB  = 0x09
    REG_IOCON    = 0x0A # IO COntrol: Bit 7 = Bank, Bit 6 = MIRROR, Bit 7 = SEQOP, Bit 4: DISSLW (SDA slew), Bit 3: HAEN (Hardware addr enable (S17 only)), bit 2 = ODR (oupen drain INT line), Bit 1: INTPOL (Interrupt line polarity), Bit 0: Unused
    REG_GPPUA    = 0x0C # Pullup resistor control: 1=Pull-up enabled
    REG_GPPUB    = 0x0D
    REG_INTFA    = 0x0E # INTFA: Interrupt flags (read only)
    REG_INTFB    = 0x0F
    REG_INTCAPA  = 0x10 # INTCAP: Interrupt capture
    REG_INTCAPB  = 0x11
    REG_GPIOA    = 0x12 # Port register: Reading returns the pin value, Writing changes the output latch value
    REG_GPIOB    = 0x13
    REG_OLATA    = 0x14 # Output latch: Reading returns the output latch value, Writing changes the output latch value 
    REG_OLATB    = 0x15

    # --- Define GPIO bits for the IO Expander
    # Directions
    RD = 1
    WR = 0
    # Ports
    PORT_A = 0
    PORT_B = 1
    # Bit definition class
    class BitDef:
        """ Holds the definition of a GPIO bit on the IO expander. Contains Port number (0=A, 1=B), Bit number (0..7), Direction (0=WR, 1=RD) and default value (0 or 1)"""
        def __init__(self, port, bit, dir, default): 
            self.port = port 
            self.bit = bit
            self.dir = dir 
            self.default = default
    # Bit configuration table:   name: (port, bit, dir (0=wr,1=rd), default_value)
    BITS = {
        'ADC_RESET'        : BitDef(PORT_A, 0, WR, 1),
        'PLL1_CE'          : BitDef(PORT_A, 1, RD, 0),
        'PLL1_LOCK'        : BitDef(PORT_A, 2, RD, 0),
        'PLL1_MUTE'        : BitDef(PORT_A, 3, RD, 0),
        'PLL1_MUXOUT'      : BitDef(PORT_A, 4, RD, 0),
        'PLL2_LOCK'        : BitDef(PORT_A, 5, RD, 0),
        'LED0'             : BitDef(PORT_A, 6, WR, 1),  #changed to 0
        'LED1_PLL2_RESET'  : BitDef(PORT_A, 7, WR, 0), # default=0 (read mde) to enable MGT_PLL (there is a pull-up on the line) and make sure the user does not inadvertantly resets the PLL while thinking he accesses the LED
        'LED2'             : BitDef(PORT_B, 0, WR, 1),
        'LED3'             : BitDef(PORT_B, 1, WR, 1),
        'REFCLK_INPUT_SEL' : BitDef(PORT_B, 3, WR, 0), ## kmb added for new fmc board clock set -> 1 is SMA, 0 is fmc
        'SYNC_INPUT_SEL'   : BitDef(PORT_B, 6, WR, 0), #SYNC source: 0=FPGA, 1= SMA
        'SYNC_FF_BYPASS'   : BitDef(PORT_B, 2, WR, 0), #SYNC FlipFlop Bypass: 0=Bypass, 1= Use FF (Note: It is not enough to set this bit for FF bypass. Resistors must also be set to route the buffered SYNC to the FF or the FF bypass input)
        }

    def __init__(self, adc_board, verbose=0):
        self.adc_board = adc_board
        self.verbose = verbose
        self.logger = logging.getLogger(__name__)

        # Automatically generate properties for each of the IOExpander bits
        for bit_name in self.BITS.keys():
            #print '  Defining property "%s" with port=, bit=' % (bit_name)
            # Use function closures to create the callback function with arguments that won't be rebinded
            fget = lambda s, _bit_name = bit_name : s.read_gpio_bit(_bit_name) # Pass bit_name as a default argument to 'close' that variable (i.e. bind it now). Otherwise the function will use the value at call time (which is the last value assigned to that variable) 
            fset = lambda s, value, _bit_name = bit_name : s.write_gpio_bit(_bit_name, value)
            if self.BITS[bit_name].dir == self.RD:
                setattr(self.__class__, bit_name, property(fget))
            else:
                setattr(self.__class__, bit_name, property(fget, fset))
        self._lock() # Prevent further changes to the instance

    def __setattr__(self, name, value):
        """ Prevents creating new attributes to the class when _locked==1"""
        if (not self._locked) or (hasattr(self, name)): # allow write if not locked or if attribute already exists
#            print 'setting ',name
            object.__setattr__(self, name, value)
        else:
            print "Class '%s' is locked: cannot assign new attribute '%s'" % (self, name)
            raise AttributeError("This instance of class '%s' is locked: cannot assign new attribute '%s'" % (self.__class__.__name__, name)) # 120623 JFC

    def _unlock(self):
        self.__dict__['_locked'] = False
        
    def _lock(self):
        self.__dict__['_locked'] = True

    def read(self, addr):
        """ Reads a IOExpander 8-bit register""" 
        brd = self.adc_board
        data = brd.spi_read_write(brd.SPI_IO_EXP_ADDR, [0x41, addr, 0x00], type=np.uint8)
        return data

    def write(self, addr, data):
        """ Writes a IOExpander 8-bit register"""
        brd = self.adc_board
        data = brd.spi_read_write(brd.SPI_IO_EXP_ADDR, [0x40, addr, data])

    def read_gpio_bit(self, bit_name):
        """ Reads the IOExpander GPIO bit identified by the name 'bit_name' which is looked up in the BITS table to find the bit definition (port, bit position etc). Returns a boolean."""  
        bit_def = self.BITS[bit_name]
        data = self.read(self.REG_GPIOA + bit_def.port)
        #print 'Read ,bit "%s" at port %i, bit=%i, data: %X' % (bit_name,  bit_def.port,bit_def.bit, data)
        return bool(data & (1 << bit_def.bit))


    def write_gpio_bit(self, bit_name, data):
        """ Writes the IOExpander GPIO bit identified by the name 'bit_name' which is looked up in the BITS table to find the bit definition (port, bit position etc). The input data in converted in Boolean before being written."""  
        bit_def = self.BITS[bit_name]
        old_data = self.read(self.REG_OLATA + bit_def.port)
        mask = 1 << bit_def.bit
        #print 'Writing data: old_data=%X, port %i, addr=%X, bit=%x' % (old_data, bit_def.port,self.REG_GPIOA+bit_def.port,bit_def.bit)
        new_data = (old_data & ~mask) | (bool(data) * mask)
        data = self.write(self.REG_GPIOA + bit_def.port, new_data)

    def wait_for_bit(self, bit_name, timeout=1): 
        """
        Wait for specified bit to become '1'. 
        """
        if bit_name not in self.BITS:
            raise Exception('The bit name does not exist')

        t0 = time.time()
        while True:
            if self.read_gpio_bit(bit_name): 
                return
            if (time.time() - t0) > timeout:
                raise RuntimeError('Timeout exceeded while waiting for bit %s' % bit_name)


    def init(self, verbose=0):
        """ 
        Initializes the register of the IOExpander. Sets the GPIO bits direction and default values based on the 'BITS' table
        Call only after the SPI subsystem is initialized.
        """
        # Do nothing if the FMC is not present
        if not self.adc_board.is_present():
            return

        bypass = 0

        direction = [0xFF, 0xFF] # direction are 'read' by default
        val = [0x00, 0x00] # values are '0' by default
        if not bypass:
            for (bit_name, bit_def) in self.BITS.iteritems():
                port = bit_def.port
                bit = bit_def.bit
                direction[port] &= ~(1 << bit) # clear bit
                direction[port] |= (bit_def.dir << bit) # set with new value
                val[port] &= ~(1 << bit) # clear bit
                val[port] |= (bit_def.default << bit) # set with new value
        if verbose or self.verbose:
            self.logger.debug('IOExpander config: IODIRA=%02X , IODIRB=%02X, GPIOA=%02X, GPIOB=%02X' % (direction[0], direction[1], val[0], val[1]))
        self.write(self.REG_IODIRA, direction[self.PORT_A]) # 
        self.write(self.REG_IODIRB, direction[self.PORT_B]) # 
        self.write(self.REG_GPIOA, val[self.PORT_A]) # 
        self.write(self.REG_GPIOB, val[self.PORT_B]) # 

    def status(self):
        """ Displays the status of the IOExpander. """
        self.logger.info('--- FMC %i IO Expander' % self.adc_board.fmc_number)
        if not self.adc_board.is_present():
            self.logger.info('FMC board not present')
        self.logger.info('No status info')
