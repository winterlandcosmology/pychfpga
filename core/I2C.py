#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301 

"""
I2C.py module 
    Implements I2C interface of chFPGFA

History:
    2012-03-29 JFC : Created from SPI.py
    2012-04-11 JFC : generalized i2c_write_read to allow simple read and writes. Trig the state machine (START) in two lines to make sure the 0-to-1 transition is not missed. Cleanup.
    2012-08-27 JFC : Fixed reference to common.util as pychime.common.util
    2012-11-19 JFC: Fixed tabs. Changed how errors are handled. Now SystemError restuns error strings.         
"""

import numpy as np
from pychime.common.util import hex

from Module import Module_base, BitField

__reload__=True

class I2C_base(Module_base):
    # I2C addresses
    I2C_FMC_HPC_EPPROM_ADDR=0    # ADC. R/W device. 8 bit address+RW, 16 bit data.


    CONTROL=BitField.CONTROL
    STATUS=BitField.STATUS

    BITS={
        'START' :   BitField(CONTROL, 0x04, 7, doc='A 0 to 1 transition on this bit starts I2C transaction'),
        'BYTES2' :   BitField(CONTROL, 0x04, 4, width=3, doc='Number of bytes to read back from the same address after the write sequence'),
        'BYTES1' :   BitField(CONTROL, 0x04, 0, width=2, doc='Number of bytes in the I2C communication (excluding the address byte) 1=1 Byte, 1=2 bytes, 2=3 bytes'),

        'RESET' :           BitField(CONTROL, 0x05, 7, doc='1 resets the I2C subsystem'),
        'ALWAYS_CLK' :        BitField(CONTROL, 0x05, 6, doc='Force the generation if a clock even when idle (will prevent the system from detecting idle bus state unless STOP events are seen'),
        'PORT' :            BitField(CONTROL, 0x05, 4, width=2, doc='I2C port number (0=FMC HPC EEPROM/ML605 EEPROM/ML605 EEPROM, 1=SMBus'),
        'FORCE_SCK' :         BitField(CONTROL, 0x05, 3, doc='Enables forcing SCK to the state identified in FORCE_SCK_STATE'),
        'FORCE_SDA' :         BitField(CONTROL, 0x05, 2, doc='Enables forcing SDA to the state identified in FORCE_SDA_STATE'),
        'FORCE_SCK_STATE':     BitField(CONTROL, 0x05, 1, doc='State to which SCK is forces when FORCE_SCK=1'),
        'FORCE_SDA_STATE':     BitField(CONTROL, 0x05, 0, doc='State to which SDA is forces when FORCE_SDA=1'),

        'IDLE':        BitField(STATUS, 0x04, 7, doc='Indicates if the bus is idle'),
        'TIMEOUT':    BitField(STATUS, 0x04, 4, doc='Indicates if a timeout has occured during the transaction'),
        'COLLISION':BitField(STATUS, 0x04, 3, doc='Indicates if the transaction experienced a collision'),
        'SCK' :     BitField(STATUS, 0x04, 2, doc='State of the SCK line'),
        'SDA' :     BitField(STATUS, 0x04, 1, doc='State of the SDA line'),
        'DONE' :     BitField(STATUS, 0x04, 0, doc='High when I2C transaction is completed'),



        'ACK_STATUS' :     BitField(STATUS, 0x05, 0, width=8,doc='Ack bits'),

        'BYTE_CTR' :     BitField(STATUS, 0x06, 4, width=3, doc='Byte counter at end of transmission'),
        'DIR' :         BitField(STATUS, 0x06, 1, doc='Dirction at end of transmission'),
        'BIT_CTR' :     BitField(STATUS, 0x06, 0, width=3, doc='Bit counter at end of transmission'),

        'START_CTR' :     BitField(STATUS, 0x07, 4, width=4, doc='Counts the number of START events'),
        'DONE_CTR' :     BitField(STATUS, 0x07, 0, width=4, doc='Counts the number of DONE events'),

    }



    def __init__(self,fpga):
        self.fpga_instance=fpga;
        super(self.__class__,self).__init__(fpga,fpga.SYSTEM_PORT, fpga.SYSTEM_I2C_MODULE)

    I2C_DEVICE_LIST_KC705 = {
        'FMC': 0x02
        } 
    I2C_DEVICE_ADDR_KC705 = 0x74

    def set_i2c_switch(self, device_name):
        'Selects the active I2C sevice for the platforms that use a I2C switch'
        if self.fpga_instance.PLATFORM_ID == self.fpga_instance.PLATFORM_ID_KC705:
            self.i2c_write(addr=self.I2C_DEVICE_ADDR_KC705, data=[self.I2C_DEVICE_LIST_KC705[device_name]])    

    def i2c_write_read(self, port=0, addr=0, data=[0], read_length=0, verbose=False, noerror=False):
        """ 
        When data=None, reads 'read_length' (0-3) bytes from the I2C device at specified I2C address in a single I2C transaction, 
        or, if data is an non-empty array of 1-3 data bytes, writes the data to the specified I2C address, 
        send a restart condition and reads 'read_length' (0-4) bytes. 
        """
        #verbose=1
        if port<0 or port>1:
            print 'i2c_write_read: port number is out of range'
            raise ValueError()

        if addr<0 or addr>0xFF:
            print 'i2c_write_read: I2C address is out of range'
            raise ValueError()

        if read_length<0 or read_length>4:
            print 'i2c_write_read: read_length is out of range'
            raise ValueError()

        if data is None:
            write_length = 0
        else:
            write_length=len(data)
            if write_length>3:
                print 'i2c_write_read: write length is out of range'
                raise ValueError()

        if verbose:
            print 'i2c_write_read:  writing %i and reading %i bytes at port %i at address 0x%02x with the following data:' % (write_length, read_length, port,addr), hex(data)

        #error = 0
        error_msg = ''
        
        start_ctr = self.START_CTR
        done_ctr = self.DONE_CTR

        if start_ctr != done_ctr:
            error_msg += 'i2c_write_read: start_ctr is different from done_ctr\n'

        self.write(0x000 + 0x05, port << 4) # disables RESET, set port number

        if data is None: # if we do not write any date, we perform a single transaction with BYTES1=read_length and BYTES2=0 
            self.write(0x000 + 0x00, [(addr << 1) + 0x01]) # write I2C address with read flag to the transmit buffer 
            expected_ack = 2**(read_length + 1) - 1;
            self.write(0x000 + 0x04,[0x00 + read_length]) # Prepare to start transaction by clearing the START bit
            self.write(0x000 + 0x04,[0x80 + read_length]) # start transaction by creating a 0-to-1 trsnsition on the START bit. Do this as a separate transmission to make sure that the firmware registered the zero
        else: # if we write and optionnally read
            self.write(0x000 + 0x00, [(addr << 1) + 0x00] + data) # write address with write flag and data in transmit buffer (4 bytes max)
            expected_ack= 2**(read_length + write_length + 1 + (read_length != 0)) - 1
            self.write(0x000 + 0x04,[0x00+(read_length << 4) + write_length]) # Prepare to start transaction by clearing the START bit
            self.write(0x000 + 0x04,[0x80+(read_length << 4) + write_length]) # start transaction by creating a 0-to-1 trsnsition on the START bit. Do this as a separate transmission to make sure that the firmware registered the zero
        self.wait_for_bit('DONE')
        data=self.read(0x080+0x00, length=4, type=np.uint8)
        ack=self.ACK_STATUS
        if ack!=expected_ack:
            #error=1
            error_msg += 'i2c_write_read: communication error: did not receive correct ACK bits. Received 0x%02x, expected 0x%02x\n' % (ack, expected_ack)
        start_ctr=(start_ctr+1) % 16
        done_ctr=(done_ctr+1) % 16
        if self.START_CTR != start_ctr:
            error_msg +='i2c_write_read: communication error: start_ctr do not match. Read %i, expected %i\n' (self.START_CTR, start_ctr)
        if self.DONE_CTR != done_ctr:
            error_msg += 'i2c_write_read: communication error: done_ctr do not match. Read %i, expected %i\n' (self.DONE_CTR, done_ctr)

        #print 'I2C communication: ACK byte is 0x%02x' % ack
        data=data[-read_length:]
        #data.dtype=np.dtype(type)
        if error_msg:
            error_msg = 'i2c_write_read:  The following errors occured while writing %i and reading %i bytes at port %i at address 0x%02x with data %s\n %s' % (write_length, read_length, port, addr,  hex(data), error_msg)
        if verbose and error_msg:
            print error_msg
        if error_msg and not noerror:
            raise SystemError(error_msg)

        return data

    def i2c_read(self, port=0, addr=0, length=1,  type=np.uint8, **kwargs):
        """ Serially reads 0-3 bytes  bytes long) from the I2C bus at the specified I2C address 
        """
        return self.i2c_write_read(port=port, addr=addr, data=None, read_length=length, **kwargs)

    def i2c_write(self, port=0, addr=0, data=[0], **kwargs):
        """ Serially writes 1-3 bytes to the specified I2C node 
        """
        return self.i2c_write_read(port=port,addr=addr,data=data,read_length=0, **kwargs)

    def i2c_reset(self, port=0, verbose=0):
        """ 
        """
        self.write(0x000+0x05,[0x80+(port<<5)])
        self.write(0x000+0x05,[0x00+(port<<5)])

    def i2c_status(self, verbose=0):
        s=self.read(0x04,length=2)
        print 'Current selected port: %i' % (s[0]&0b01100000)>>6
        print 'Reset state: %i' % bool(s[1]&0x80)
        print 'Force line SCK: %i, SDA: %i' % (bool(s[1]&0x02),bool(s[1]&0x01))
        s=self.read(0x80,length=9)
        print 'Read bytes:', hex(s[0:4])
        print 'last state:', hex(s[4]>>4)
        print 'SCK = %i, SDA= %i' %(bool(s[4]&0x02), bool(s[4]&0x01))
        print 'ACK bits:', bin(s[5])
        
    

    def init(self):
        '''
        For KC705 Board, set the switch to FMC EEPROM by default
        '''
        print '     Platform ID:  ' + str(self.fpga.PLATFORM_ID)
        if self.fpga.PLATFORM_ID == 1:
            print '     Setting Default I2C to FMC HPC'
            self.i2c_write(addr=0x74, data=[2])
        pass
