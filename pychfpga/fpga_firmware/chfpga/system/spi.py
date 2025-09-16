""" Interface to the chFPGA's firmware-based SPI peripheral

.. History:
    2011-07-07 : JFC : Created from test code in chFPGA.py
    2011-07-13 JFC: added read_reg() and write_reg() to make code more manageable and reader-friendly
    2013-08-16 JFC: Cleanup. Used new BitFiield style. Removed all explicit address references. removed REV0 and moved ALT_TIMING and added PORT.
"""

import numpy as np
import time
import logging

from ..mmi import MMI, BitField, CONTROL, STATUS

class SPI(MMI):
    # SPI addresses

    ADDRESS_WIDTH = 12

    TX_DATA        = BitField(CONTROL, 0x03, 0, width=32, doc='Data to be transmitted. MSB (bit 31) is transmited first.')
    ADDR           = BitField(CONTROL, 0x04, 4, width=4, doc='Address of SPI device to communicate with')
    RESET          = BitField(CONTROL, 0x04, 3, doc='Resets the SPI state machine. NOTE: Only available on the HPC connector')
    START          = BitField(CONTROL, 0x04, 2, doc='A 0 to 1 transition on this bit starts SPI read/write')
    BYTES          = BitField(CONTROL, 0x04, 0, width=2, doc='Number of bytes in the SPI communication 0=1 Byte, 1=2 bytes, 2=3 bytes, 3=4 bytes')

    DEFAULT_ADDR   = BitField(CONTROL, 0x05, 4, width=3, doc='Default address of SPI device (enabled when there is no communication or ADC_PLL1 is accessed')
    CLK_ENABLE     = BitField(CONTROL, 0x05, 3, doc='When 1, enables the SPI clock')
    ALT_TIMING     = BitField(CONTROL, 0x05, 2, doc='When 1, uses the alternate timing where the CS is deactivated later. This is to be used with the ADC PLL.')
    PORT           = BitField(CONTROL, 0x05, 0, width=1, doc='Indicates which SPI port is used.')
    RX_DATA        = BitField(STATUS, 0x03, 0, width=32, doc='Data received during the transaction. MSB (first received bit) is always on bit 31.')
    READY          = BitField(STATUS, 0x04, 0, doc='High when SPI transaction is completed')



    def __init__(self, *, router, router_port):
        # self.fpga_instance = fpga;
        super().__init__(router=router, router_port=router_port)
        self.current_port = 0
        self.logger = logging.getLogger(__name__)
        self._lock() # Prevent inadvertent changes to the class instance

    # def read_reg(self, addr, length=1, type=np.uint8):
    #     """ Reads from the SPI control register"""
    #     fpga = self.fpga_instance; # use a shorter variable name to access the FPGA instance attributes
    #     data = fpga.read(fpga.SYSTEM_PORT, fpga.SYSTEM_SPI_MODULE, addr, length=length, type=type)
    #     return data

    # def write_reg(self, addr, data, type=np.uint8):
    #     """ Writes to the SPI control register"""
    #     fpga = self.fpga_instance; # use a shorter variable name to access the FPGA instance attributes
    #     length = fpga.write(fpga.SYSTEM_PORT,fpga.SYSTEM_SPI_MODULE,addr,data);
    #     return length

    def set_port(self, port):
        """
        Sets the SPI port to be used in subsequent transactions
        """
        self.current_port = port;

    def read_write(self, device=2, data=[0x00, 0x00, 0x00, 0x00],  type=np.uint8, port = None, verbose=1):
        """ Serially writes a word (1-4 bytes long) to the specified device on the SPI bus while reading serial data put the bus at the same time
        The written word must be padded so its total length covers the whole SPI transaction (read and write bits).

        Parameters:

            device (int or tuple): if an ``int``, address of the SPI
                device to talk to (0-15). If `device` is a ``(addr, timing_mode)``
                tuple,  we talk at the device at address ``addr`` using the
                specified timing mode  (0 or 1).

            data (list, bytes or ndarray(uint8 or int8)): Data to send during the
                SPI transaction. Each element of `data` must be a byte (0-255).
                `data` can be 1 to 4 bytes long.

            type (dtype): type of the data to read back. Must be a valid numpy dtype.

            port: SPI port to use (0 or 1). If ``None``, the last port number is reused.

            verbose (bool):  If True, shows additional information on the SPI transaction.

        Returns:
            A numpy scalar of type `type`.

        """
        if port is not None:
            self.set_port(port)
        self.PORT = self.current_port

        if isinstance(device, tuple):
            self.ALT_TIMING = device[1]
            device = device[0]
        else:
            self.ALT_TIMING = 0
        word_length = self.write(self.get_addr('TX_DATA') - 3, data); # make sure first byte of array is on Byte 0
        self.ADDR = device
        self.BYTES = word_length - 1
        self.START = 0
        self.START = 1
        # self.write_reg(0x000+0x04, [0x00+(device << 4) + (word_length-1)]);
        # self.write_reg(0x000+0x04, [0x04+(device << 4) + (word_length-1)]);
        while not self.READY:
            time.sleep(0.1)
            if verbose:
                print('.', end=' ')
        data = self.read(self.get_addr('RX_DATA')-3, length=word_length, type=np.uint8)
        read_length= int(np.dtype(type).itemsize)
        data = data[-read_length:].view(type)[0]
        return data

    def init(self):
        self.DEFAULT_ADDR = 3 # Default SPI_ADDR<2:0> when not accessing the SPI devices or interfacing devices with addresses >=8 (SPI_ADDR1_TEMP_ADDR)
        self.CLK_ENABLE = 1 # enable SPI clock

    def status(self):
        # self.logger.info('--- SPI Interface---')
        # self.logger.info(' No status info')
        pass