#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301

"""
I2C.py module
    Implements I2C interface of chFPGFA

History:
    2012-03-29 JFC : Created from SPI.py
    2012-04-11 JFC : generalized write_read to allow simple read and writes. Trig the state machine (START) in two lines to make sure the 0-to-1 transition is not missed. Cleanup.
    2012-08-27 JFC : Fixed reference to common.util as pychfpga.common.util
    2012-11-19 JFC: Fixed tabs. Changed how errors are handled. Now SystemError restuns error strings.
    2014-09-29 JFC: changed code to use read_status() and write_control() so we don't have to assume anything on how to access the various address spaces.
"""

import logging

import numpy as np
from pychfpga.common.util import hex
from Module import Module_base, BitField

# __reload__ = True

class I2C_base(Module_base):
    # I2C addresses
    # I2C_FMC_HPC_EPPROM_ADDR = 0    # ADC. R/W device. 8 bit address+RW, 16 bit data.

    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    START           = BitField(CONTROL, 0x04, 7, doc='A 0 to 1 transition on this bit starts I2C transaction')
    BYTES2          = BitField(CONTROL, 0x04, 4, width=3, doc='Number of bytes to read back from the same address after the write sequence')
    BYTES1          = BitField(CONTROL, 0x04, 0, width=2, doc='Number of bytes in the I2C communication (excluding the address byte) 1=1 Byte, 1=2 bytes, 2=3 bytes')
    RESET           = BitField(CONTROL, 0x05, 7, doc='1 resets the I2C subsystem')
    ALWAYS_CLK      = BitField(CONTROL, 0x05, 6, doc='Force the generation if a clock even when idle (will prevent the system from detecting idle bus state unless STOP events are seen')
    PORT            = BitField(CONTROL, 0x05, 4, width=2, doc='I2C port number (0=FMC HPC EEPROM/ML605 EEPROM/ML605 EEPROM, 1=SMBus')
    FORCE_SCK       = BitField(CONTROL, 0x05, 3, doc='Enables forcing SCK to the state identified in FORCE_SCK_STATE')
    FORCE_SDA       = BitField(CONTROL, 0x05, 2, doc='Enables forcing SDA to the state identified in FORCE_SDA_STATE')
    FORCE_SCK_STATE = BitField(CONTROL, 0x05, 1, doc='State to which SCK is forces when FORCE_SCK=1')
    FORCE_SDA_STATE = BitField(CONTROL, 0x05, 0, doc='State to which SDA is forces when FORCE_SDA=1')
    IDLE            = BitField(STATUS, 0x04, 7, doc='Indicates if the bus is idle')
    TIMEOUT         = BitField(STATUS, 0x04, 4, doc='Indicates if a timeout has occured during the transaction')
    COLLISION       = BitField(STATUS, 0x04, 3, doc='Indicates if the transaction experienced a collision')
    SCK             = BitField(STATUS, 0x04, 2, doc='State of the SCK line')
    SDA             = BitField(STATUS, 0x04, 1, doc='State of the SDA line')
    DONE            = BitField(STATUS, 0x04, 0, doc='High when I2C transaction is completed')
    ACK_STATUS      = BitField(STATUS, 0x05, 0, width=8,doc='Ack bits')
    BYTE_CTR        = BitField(STATUS, 0x06, 4, width=3, doc='Byte counter at end of transmission')
    DIR             = BitField(STATUS, 0x06, 1, doc='Dirction at end of transmission')
    BIT_CTR         = BitField(STATUS, 0x06, 0, width=3, doc='Bit counter at end of transmission')
    START_CTR       = BitField(STATUS, 0x07, 4, width=4, doc='Counts the number of START events')
    DONE_CTR        = BitField(STATUS, 0x07, 0, width=4, doc='Counts the number of DONE events')



    def __init__(self, fpga, base_address):
        self.fpga_instance = fpga;
        super(I2C_base, self).__init__(fpga, base_address)
        self.current_port = None
        self.logger = logging.getLogger(__name__)

    def set_port(self, port_number):
        """
        Selects on which FPGA I2C port the subsequents I2C transactions will be made.
        """
        if port_number<0 or port_number>1:
            self.logger.error('write_read: port number is out of range')
            raise ValueError()
        self.current_port = port_number
        # self.logger.debug("Setting FPGA I2C port to %i" % port_number)

    def write_read(self, addr=0, data=[0], read_length=0, verbose=1, noerror=False, retry=1):
        """
        When data=None, reads 'read_length' (0-3) bytes from the I2C device at specified I2C address in a single I2C transaction,
        or, if data is an non-empty array of 1-3 data bytes, writes the data to the specified I2C address,
        send a restart condition and reads 'read_length' (0-4) bytes.
        """
        #verbose=1

        if addr<0 or addr>0xFF:
            self.logger.error('write_read: I2C address is out of range')
            raise ValueError()

        if read_length<0 or read_length>4:
            self.logger.error('write_read: read_length is out of range')
            raise ValueError()

        if data is None:
            write_length = 0
        else:
            write_length=len(data)
            if write_length>3:
                self.logger.error('write_read: write length is out of range')
                raise ValueError()

        #if verbose:
        # self.logger.debug('write_read:  writing %i byte(s) and reading %i byte(s) at FPGA port %i at address 0x%02x with the following data: %s' % (write_length, read_length, self.current_port, addr, hex(data)))

        #error = 0
        error_msg = ''

        start_ctr = self.START_CTR
        done_ctr = self.DONE_CTR

        if start_ctr != done_ctr:
            error_msg += 'write_read: start_ctr is different from done_ctr\n'

        trial = 0
        while True:
            self.write_control(0x05, self.current_port << 4) # disables RESET, set port number

            idle = self.IDLE
            if data is None: # if we do not write any date, we perform a single transaction with BYTES1=read_length and BYTES2=0
                self.write_control(0x00, [(addr << 1) + 0x01]) # write I2C address with read flag to the transmit buffer
                expected_ack = 2**(read_length + 1) - 1;
                self.write_control(0x04,[0x00 + read_length]) # Prepare to start transaction by clearing the START bit
                self.write_control(0x04,[0x80 + read_length]) # start transaction by creating a 0-to-1 trsnsition on the START bit. Do this as a separate transmission to make sure that the firmware registered the zero
            else: # if we write and optionnally read
                self.write_control(0x00, [(addr << 1) + 0x00] + data) # write address with write flag and data in transmit buffer (4 bytes max)
                expected_ack= 2**(read_length + write_length + 1 + (read_length != 0)) - 1
                self.write_control(0x04,[0x00+(read_length << 4) + write_length]) # Prepare to start transaction by clearing the START bit
                self.write_control(0x04,[0x80+(read_length << 4) + write_length]) # start transaction by creating a 0-to-1 trsnsition on the START bit. Do this as a separate transmission to make sure that the firmware registered the zero
            self.wait_for_bit('DONE')

            # Increment the transaction counters to track how many start and done events we *should* have
            start_ctr=(start_ctr+1) % 16
            done_ctr=(done_ctr+1) % 16

            if self.DONE_CTR != done_ctr:
                self.logger.warn('write_read: Transaction is not completed yet!')

            # Get the data that was read back
            read_data=self.read_status(0x00, length=4, type=np.uint8)

            # Check the ACK flags
            ack=self.ACK_STATUS
            if ack == expected_ack:
                break
            trial += 1
            if trial > retry:
                error_msg += 'write_read: communication error: did not receive correct ACK bits. Received 0x%02x, expected 0x%02x\n. ' % (ack, expected_ack)
                break
            else:
                self.logger.warn('write_read: communication error: did not receive correct ACK bits. Received 0x%02x, expected 0x%02x\n. Start ctr: %i => %i, Done ctr: %i => %i, Idle: %i => %i, Collisiotn=%i, timeout=%i. Retrying...' % (ack, expected_ack, start_ctr, self.START_CTR, done_ctr, self.DONE_CTR, idle, self.IDLE, self.COLLISION, self.TIMEOUT))

        # Che
        if self.START_CTR != start_ctr:
            error_msg +='write_read: communication error: start_ctr do not match. Read %i, expected %i\n' % (self.START_CTR, start_ctr)
        if self.DONE_CTR != done_ctr:
            error_msg += 'write_read: communication error: done_ctr do not match. Read %i, expected %i\n' % (self.DONE_CTR, done_ctr)

        #print 'I2C communication: ACK byte is 0x%02x' % ack
        read_data=read_data[-read_length:]
        #data.dtype=np.dtype(type)
        if error_msg:
            error_msg = 'write_read:  The following errors occured while writing %i bytes and reading %i bytes on FPGA I2C port %i at address 0x%02x with data %s\n %s' % (write_length, read_length, self.current_port, addr,  hex(read_data), error_msg)
            if noerror:
                self.logger.info(error_msg)
            else:
                self.logger.info(error_msg)
                raise IOError(error_msg)

        return read_data

    def i2c_read(self, addr=0, length=1,  type=np.uint8, **kwargs):
        """ Serially reads 0-3 bytes  bytes long) from the I2C bus at the specified I2C address
        """
        return self.write_read(addr=addr, data=None, read_length=length, **kwargs)

    def i2c_write(self, addr=0, data=[0], **kwargs):
        """ Serially writes 1-3 bytes to the specified I2C node
        """
        return self.write_read(addr=addr,data=data,read_length=0, **kwargs)

    # def reset(self, port=0, verbose=0):
    #     """
    #     """
    #     self.write(0x000+0x05,[0x80+(port<<5)])
    #     self.write(0x000+0x05,[0x00+(port<<5)])

    def status(self, verbose=0):
        # s=self.read(0x04,length=2)
        self.logger.info('Current selected port: %i' % self.PORT)
        self.logger.info('Reset state: %i' % self.RESET)
        self.logger.info('Force line SCK: %i, SDA: %i' % (self.FORCE_SCK, self.FORCE_SDA))
        # s=self.read(0x80,length=9)
        # print 'Read bytes:', hex(s[0:4])
        # print 'last state:', hex(s[4]>>4)
        # print 'SCK = %i, SDA= %i' %(bool(s[4]&0x02), bool(s[4]&0x01))
        # print 'ACK bits:', bin(s[5])



    def init(self, verbose = 0):
        '''
        For KC705 Board, set the switch to FMC EEPROM by default
        '''
        # if verbose >= 2: print '     Platform ID:  ' + str(self.fpga.PLATFORM_ID)
        # if self.fpga.PLATFORM_ID == 1:
        #     if verbose >= 2: print '     Setting Default I2C to FMC HPC'
        #     self.write(addr=0x74, data=[2])
        pass
