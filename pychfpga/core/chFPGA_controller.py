#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301
# pylint: disable=C0321

"""
chFPGA.py module
 Implements interface to the CHIME chFPGA Proof of Concept board


History:
    2011-01-10 : JFC : First version
    2011-04-30 JFC : Modified UDP.py into chFPGA.py to implement higher level communication system
    2011-04 - 2011-08 JFC : Major modifications & cleanup
    2011-08-29 JFC: Moved hex to util to solve circular import reference.
    2012-03-27 JFC: Modified the read and write commands to support the new format following AXI4-Streaming implementation of the command bus
    2012-05-29 JFC: Cleanup init. Support FMC board detection. Extracted test functions.
    2012-07-xx JFC: Implemented Thread-based frame buffering. Updated frame reading and plotting functions accordingly.
    2012-07-16 KMB: Started moving plotting/saving functions out to plot_utils.py, and removing redundant programs
    2012-07-25 JFC: Splitted the init() from __init() to make sure the controller object creation does not change the state of the FPGA.
        Added LCD initialization and firmware version display on the LCD
        Implemented default channel managements
    2012-08-27 JFC : Fixed reference to common.util as pychfpga.common.util
    2012-09-18 JFC: Added set_global_trig()
    2012-10-17 JFC: Added an exception if wring function name is used in set_funcgen_function()
    2012-11-28 JM: Added function set_gain()
    2014-04-24 JM: Added functions set_adc_delays_with_check and check_ramp_errors copied from iceboard_dev branch
"""

import logging
import numpy as np
# import socket #needed for inet_aton
# import struct

#import pdb
import time

from .icecore_ext.chfpga_handler import chFPGAHandler

from pychfpga.common import util

# import Shared_variables # Note: do not reload this module or we will lose acces to the data in it
import Module
# import SocketIO

# FPGA subsystems handlers
import SPI
import I2C
import GPIO
import SYSMON
import FreqCtr
import REFCLK
import MGT

# FPGA Antenna processor handlers
import ANT
import ADCDAQ # Included only so it can be reloaded
import SRCSEL # Included only so it can be reloaded
import INJECT # Included only so it can be reloaded
import FUNCGEN # Included only so it can be reloaded
import FFT # Included only so it can be reloaded
import SCALER # Included only so it can be reloaded
import PROBER # Included only so it can be reloaded

# FPGA Correlator handlers
import CORR_BLOCK

import CROSSBAR
# import CH_DIST    # Included only so it can be reloaded
import shuffle

import ACC # Included only so it can be reloaded

import GPU

# ML605 FPGA board specific device handlers

# from pychfpga.motherboards import ML605_LCD
# from pychfpga.motherboards import ML605_PMBus
# from pychfpga.motherboards import mgk7mb # McGill ICEBoard hardware ressources wrapper

# MGADC08 FMC ADC board device handlers
#from pychfpga.MGADC08 import MGADC08

# -- Module reloader --
# Reload modules if we are debugging in case the source code has changed

# MODULE_LIST = (
#         util,
#         # SocketIO,
#         Module,
#         SPI,
#         I2C,
#         GPIO,
#         SYSMON,
#         REFCLK,
# #        MGADC08,
#         # ML605_LCD,
#         FreqCtr,
#         # ML605_PMBus,
#         ANT,
#         ADCDAQ,
#         SRCSEL,
#         INJECT,
#         FUNCGEN,
#         FFT,
#         SCALER,
#         PROBER,
#         CORR_BLOCK,
#         CROSSBAR,
#         GPU,
#         shuffle,
#         ACC,
#         MGT,
#         # mgk7mb,
# #        MGADC08,
#         # iceboard
#         )

# util.reload_modules(MODULE_LIST)


# -- chFPGA --
class chFPGA_config(object):
    def __str__(self):
        return '\n'.join(['%s = %s' % (key, repr(value)) for (key,value) in sorted(vars(self).items())])


class chFPGAException(Exception):
    _logger = logging.getLogger('chFPGAException')

    def __init__(self, message):
        super(self.__class__, self).__init__(message)
        self._logger.exception(message)


class chFPGA_controller(chFPGAHandler):
    """
    Creates an object that connects to the specified chFPGA board and provides
    the methods to configure it and control its operations.

    Arguments:
        ip_address : string indicating the IP address of the chFPGA board, e.g. "10.10.10.11"
        port: control port number
        sampling_frequency: sampling frequency of the ADC in Hz, from 150 to 2500 MHz
        reference_frequency: frequency in Hz of the reference signal provided to the chFPGA. Typically 10 MHz.
    """

    # Basic system constants
    _IMPLEMENT_CORR = False
    _ADC_CLK_SELECT = 1  # Antenna number from which the antenna processing will be clocked. This is hardwired in the firmware (need to use an ADCDAQ with a PLL)
    #SAMPLING_FREQUENCY = 800e6 # in Hz
    #REFERENCE_FREQUENCY = 10e6 # in Hz
    _SYSTEM_CLOCK_FREQUENCY = 200e6  # in Hz
    _FRAME_HEADER_LENGTH = 9
    #FRAME_PERIOD = float(FRAME_LENGTH)/SAMPLING_FREQUENCY

    # Set the basic paramaters used to compute the address of each module
    _SYSTEM_BASE_ADDR     = 0x00000  # This is always at zero so we can gather info from the FPGA before we know the number of antennas etc.
    _CHAN_BASE_ADDR       = 0x10000  # Channelizer top address
    _CROSSBAR1_BASE_ADDR  = 0x20000  # CROSSBAR top address
    _GPU_LINK_BASE_ADDR   = 0x30000  # GPU Link top address
    _CORR_BASE_ADDR       = 0x40000  # Correlator ports are determined dynamically based on the info from the firmware
    _BP_SHUFFLE_BASE_ADDR = 0x50000
    _CROSSBAR2_BASE_ADDR  = 0x60000  # CROSSBAR top address

    _CHAN_ADDR_INCREMENT           = 0x01000  # Address increment between each channelizer address spaces
    _CROSSBAR_ADDR_INCREMENT       = 0x00800
    _GPU_LINK_ADDR_INCREMENT       = 0x00800  # Address increment between each subsystem of the GPU links
    _CORR_ADDR_INCREMENT           = 0x01000  # Address increment between each correlator
    _BP_SHUFFLE_ADDR_INCREMENT     = 0x00800  # Address increment between each shuffle submodule

    _CHAN_SUBMODULE_ADDR_INCREMENT = 0x00200  # Address increment between each submodule within a channelizer (ADCDAQ, FUNCGEN, FFT, SCALER etc.)

    # Build the memory map for every module of the system ( work in progress)
    # MEMORY_MAP = {}
    # MEMORY_MAP.update( ('SYSTEM/%s' % (module_name)                , 0x00000 + i * 0x02000                           ) for (i, module_name) in enumerate(['GPIO', 'SYSMON', 'FREQ_CTR', 'SPI', 'REFCLK', 'I2C']))
    # MEMORY_MAP.update( ('CHAN%i/%s' % (channel_number, module_name), 0x20000 + channel_number * 0x02000 + module_number * 0x00400) for (module_number, module_name) in enumerate(['ADCDAQ','SRCSEL','FFT', 'SCALER', 'PROBER', 'FUNCGEN', 'INJECT']) for channel_number in range(16))


    # SYSTEM Modules addresses
    _SYSTEM_GPIO_BASE_ADDR     = _SYSTEM_BASE_ADDR + 0x00000
    _SYSTEM_SYSMON_BASE_ADDR   = _SYSTEM_BASE_ADDR + 0x01000
    _SYSTEM_FREQ_CTR_BASE_ADDR = _SYSTEM_BASE_ADDR + 0x02000
    _SYSTEM_SPI_BASE_ADDR      = _SYSTEM_BASE_ADDR + 0x03000
    _SYSTEM_REFCLK_BASE_ADDR   = _SYSTEM_BASE_ADDR + 0x04000
    # SYSTEM_I2C_BASE_ADDR      = _SYSTEM_BASE_ADDR + 0x05000

    # _GPIO_COOKIE_REG = 0x00 # Register address of the firmware cookie

    _PLATFORM_ID_ML605 = 0
    _PLATFORM_ID_KC705 = 1
    _PLATFORM_ID_MGK7MB_REV0 = 2
    _PLATFORM_ID_MGK7MB_REV2 = 3

    _PLATFORM_ID_LIST = {
        # ID: ( Board name, class to instantiate)
        _PLATFORM_ID_ML605: ('Virtex 6 (XC6V240T-1 FFG1156) on Xilinx ML605 Evaluation board', None),
        _PLATFORM_ID_KC705: ('Kintex 7 (XC7K325T-2 FFG900C) on Xilinx KC705 Evaluation board', None),
        _PLATFORM_ID_MGK7MB_REV0: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev0', None),
        _PLATFORM_ID_MGK7MB_REV2: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev2', None),
    }

    def __init__(self, **kwargs):
        """
        Creates the object providing the methods and attributes needed to
        operate the chFPGA firmware. This does not affect the state and
        operations of chFPGA.

        'motherboard' is a reference to the motherboard hardware, which must offer the following attributes/methods
            .NUMBER_OF_FMC_SLOTS
            .i2c.select_bus(bus_name)  where bus_name is 'FMCA' or 'FMCB'
            .i2c.write_read(...)
        """
        super(chFPGA_controller, self).__init__(**kwargs)
        # Initialize instance attributes
        # For now, we do not know their values unless the system is initialized.
        # We may want to fix that by reading the FPGA states and determining those values.

        self._logger = logging.getLogger(__name__)
        self._logger.info("%.32r: Creating chFPGA_controller object" % (self))

        self._sampling_frequency = None
        self._reference_frequency = None
        self._FRAME_PERIOD = None
        self._FMC_present = []  # indicates if the FMC board is present. If not, the modules will act accordingly.
        self._adc_board = []
        self._last_init_time = None

    def open(self, init=1, verbose=0, *args, **kwargs):

        super(chFPGA_controller, self).open()
        self.logger.info('%r: Instantiating chFPGA firmware handlers objects' % (self))

        self.read = self.mmi.read
        self.write = self.mmi.write

        if init < 0: # If init<0, we do not perform any communication with the FPGA, so we don't read the firmware configuration
            self._logger.info('%r: Upon user request (init < 0), communication with the FPGA are inhibited. Initialization sequence stops here. Use this for debug only.' % self)
            return
        self._logger.info('%r:    ---> Hello! This is chFPGA! <---' % self)

        try:  # catch initialization errors so we can free the socket for future instantiation

            # Create handware handling objects
            #  NOTE: Does not initialize them yet because some modules are interdependent - we need to wait until all of them are instantiated.
            #  NOTE: The instantiation does not initiate communicattion with the hardware yet. this is done in the INIT phase.

            # ---------------------------------------------------------------------
            # -- Create basic FPGA ressource handlers objects
            # ---------------------------------------------------------------------

            if verbose >= 2: self._logger.debug('%r: === Instantiating GPIO' % self)
            self.GPIO = GPIO.GPIO_base(self, self._SYSTEM_GPIO_BASE_ADDR)
            # get system constants from the FPGA

            self._logger.info('%r: === Getting board info information' % self)

            self.PLATFORM_ID = self.GPIO.PLATFORM_ID
            if self.PLATFORM_ID not in self._PLATFORM_ID_LIST:
                raise RuntimeError('%r: Platform ID 0x%02X is not recognized' % (self, self.PLATFORM_ID))
            self._NUMBER_OF_FMC_SLOTS = 2

            # Get frame size info
            self._LOG2_FRAME_LENGTH = self.GPIO.LOG2_FRAME_LENGTH
            self.FRAME_LENGTH = 2**self._LOG2_FRAME_LENGTH # 2**11 = 2048 time samples per frame
            self.NUMBER_OF_FREQUENCY_BINS = self.FRAME_LENGTH/2 # 1024 frequency bins per frame

            # Identify the number of channelizers and their properties
            self.CHANNELIZERS_CLOCK_SOURCE = self.GPIO.CHANNELIZERS_CLOCK_SOURCE
            self.NUMBER_OF_ANTENNAS = self.GPIO.NUMBER_OF_CHANNELIZERS
            self.NUMBER_OF_ANTENNAS_WITH_FFT = self.GPIO.NUMBER_OF_CHANNELIZERS_WITH_FFT
            self.LIST_OF_ANTENNAS_WITH_FFT = range(self.NUMBER_OF_ANTENNAS_WITH_FFT)

            # if self.NUMBER_OF_ANTENNAS == 0:
            #     self.NUMBER_OF_ANTENNAS = 16

            # Get crossbar configuration
            self.NUMBER_OF_CROSSBAR_INPUTS = self.GPIO.NUMBER_OF_CROSSBAR_INPUTS
            self.NUMBER_OF_CROSSBAR_OUTPUTS = self.GPIO.NUMBER_OF_CROSSBAR_OUTPUTS

            # Get GPU link configuration
            self.NUMBER_OF_GPU_LINKS = self.GPIO.NUMBER_OF_GPU_LINKS

            self.NUMBER_OF_BP_SHUFFLE_LANES = self.GPIO.NUMBER_OF_BP_SHUFFLE_LANES
            # Get correlator info and their properties
            self.NUMBER_OF_CORRELATORS_MAX = self.GPIO.NUMBER_OF_CORRELATORS
            self.NUMBER_OF_CORRELATORS = self.GPIO.NUMBER_OF_CORRELATORS
            #self.LIST_OF_IMPLEMENTED_CORRELATORS = [i for i in range(8) if bool(self.GPIO._IMPLEMENT_CORR & 2**i) and i<self.NUMBER_OF_CORRELATORS_MAX]
            self.LIST_OF_IMPLEMENTED_CORRELATORS = range(self.NUMBER_OF_CORRELATORS)
#            self.NUMBER_OF_CORRELATORS = len(self.LIST_OF_IMPLEMENTED_CORRELATORS)
            self.NUMBER_OF_ANTENNAS_TO_CORRELATE = self.GPIO.NUMBER_OF_CHANNELIZERS_TO_CORRELATE

            # ANT_BASE_PORT = 1
            # CORR_BASE_PORT = ANT_BASE_PORT + self.NUMBER_OF_ANTENNAS
            # GPU_BASE_PORT = CORR_BASE_PORT + self.NUMBER_OF_CORRELATORS

            # self.ANT_PORT =  range(ANT_BASE_PORT, ANT_BASE_PORT + self.NUMBER_OF_ANTENNAS) # Antennas are ports 0-7
            # self.CORR_PORT = range(CORR_BASE_PORT, CORR_BASE_PORT +  self.NUMBER_OF_CORRELATORS)
            # self.GPU_PORT = range(GPU_BASE_PORT, GPU_BASE_PORT +  1)
            self.default_channels = range(self.NUMBER_OF_ANTENNAS)
            #self.LIST_OF_ANTENNAS_WITH_FFT = [i for i in range(8) if bool(self.GPIO.IMPLEMENT_FFT & 2**i)]


            self._logger.info('%r: Hardware platform: %s' % (self, self._PLATFORM_ID_LIST[self.PLATFORM_ID][0]))
            self._logger.info('%r: Firmware timestamp: %s' % (self, self.get_version()))
            self._logger.info('%r: Number of channelizers: %i' %  (self, self.NUMBER_OF_ANTENNAS))
            self._logger.info('%r: Number of channelizers with FFT: %i (antennas %s)' % (self, len(self.LIST_OF_ANTENNAS_WITH_FFT), str(self.LIST_OF_ANTENNAS_WITH_FFT)))
            self._logger.info('%r: Crossbar configuration: %i inputs x %i outputs' % (self, self.NUMBER_OF_CROSSBAR_INPUTS, self.NUMBER_OF_CROSSBAR_OUTPUTS))
            self._logger.info('%r: Number of correlators: %i (correlators %s)' % (self, len(self.LIST_OF_IMPLEMENTED_CORRELATORS),str(self.LIST_OF_IMPLEMENTED_CORRELATORS)))
            self._logger.info('%r: Number of channelizers supported by the correlators: %i ' % (self, self.NUMBER_OF_ANTENNAS_TO_CORRELATE))

            self._logger.info('%r: === Instantiating FPGA ressources' % self)

            self._logger.debug('%r: === Instantiating SYSMON' % self)
            self.SYSMON = SYSMON.SYSMON_base(self, self._SYSTEM_SYSMON_BASE_ADDR)

            self._logger.debug('%r: === Instantiating SPI' % self)
            self.SPI = SPI.SPI_base(self, self._SYSTEM_SPI_BASE_ADDR)

            self._logger.debug('%r: === Instantiating FreqCtr' % self)
            self.FreqCtr = FreqCtr.FreqCtr_base(self, self._SYSTEM_FREQ_CTR_BASE_ADDR)

            self._logger.debug('%r: === Instantiating REFCLK' % self)
            self.REFCLK = REFCLK.REFCLK_base(self, self._SYSTEM_REFCLK_BASE_ADDR)

            self._logger.debug('%r: === Instantiating CHAN' % self)
            self.ANT = ANT.ANT_base(self, self._CHAN_BASE_ADDR, self._CHAN_ADDR_INCREMENT, self._CHAN_SUBMODULE_ADDR_INCREMENT) # Antenna processors (ADCDAQ, SRCSEL, FFT, SCALER) for each input
            self.ANT_FMC_NUMBER = [i//8 for i in range(self.NUMBER_OF_ANTENNAS)]

            self._logger.debug('%r: === Instantiating 1st CROSSBAR' % self)
            self.CROSSBAR = CROSSBAR.CROSSBAR_base(self, self._CROSSBAR1_BASE_ADDR, self._CROSSBAR_ADDR_INCREMENT, crossbar_level=1) # CROSSBAR block

            if self.NUMBER_OF_BP_SHUFFLE_LANES:
                self._logger.debug('%r: === Instantiating Backplane shuffle subsystem' % self)
                self.BP_SHUFFLE = shuffle.Shuffle(self, self._BP_SHUFFLE_BASE_ADDR, self._BP_SHUFFLE_ADDR_INCREMENT)

            if self.NUMBER_OF_BP_SHUFFLE_LANES and self.NUMBER_OF_GPU_LINKS:
                self._logger.debug('%r: === Instantiating 2nd CROSSBAR' % self)
                self.CROSSBAR2 = CROSSBAR.CROSSBAR_base(self, self._CROSSBAR2_BASE_ADDR, self._CROSSBAR_ADDR_INCREMENT, crossbar_level=2) # CROSSBAR block

            self._logger.debug('%r: === Instantiating CORR' % self)
            self.CORR = CORR_BLOCK.CORR_BLOCK_base(self, self._CORR_BASE_ADDR, self._CORR_ADDR_INCREMENT) # Correlator (XMUL, ACC) for each correlator

            if self.NUMBER_OF_GPU_LINKS:
                self._logger.debug('%r: === Instantiating GPU LINKS' % self)
                self.GPU = GPU.GPU_base(self, self._GPU_LINK_BASE_ADDR, self._GPU_LINK_ADDR_INCREMENT)

            self._logger.info('%r: This motherboard has %i FMC slots' % (self, self.NUMBER_OF_FMC_SLOTS))

            # ---------------------------------------------------------------------
            # -- Create ADC board hardware ressource handlers objects
            # ---------------------------------------------------------------------

            self._logger.info('%r: === Analyzing available FMC Mezzanines' % self)
            self._adc_board = [
                self.mezzanine.get(1, None),
                self.mezzanine.get(2, None)]

            self._FMC_present = [False] * self._NUMBER_OF_FMC_SLOTS
            self.ANT_FMC_IS_PRESENT = [False] * self.NUMBER_OF_ANTENNAS
            for (fmc_number, fmc) in enumerate(self._adc_board):
                if fmc:
                    self._FMC_present[fmc_number] = fmc.is_present()
                if self._FMC_present[fmc_number]:
                    self._logger.info('%r:   An MGADC08 ADC Board is present on FMC slot %i' % (self, fmc_number))
                else:
                    self._logger.warning('%r:   An MGADC08 ADC Board is *not* present of FMC slot %i' % (self, fmc_number))

            # Determine if the FMC board corresponding to each channelizer is present
            # self.ANT_FMC_IS_PRESENT = [self._adc_board[self.ANT_FMC_NUMBER[i]].is_present() for i in range(self.NUMBER_OF_ANTENNAS)]
            for (ant_number, fmc_number) in enumerate(self.ANT_FMC_NUMBER):
                if self._adc_board[fmc_number]:
                    self.ANT_FMC_IS_PRESENT[ant_number] = self._adc_board[fmc_number].is_present()

            self.hw.set_led('GP_LED1',1) # Indicate that the Iceboard is ready

        except Exception:
            self.close()
            # raise chFPGAException('An exception has occured during module instantiation. Sockets will be closed. The exception is %s' % repr(e))
            raise
            # Initialize subsystems. This has to be done only once all subsystems are created because some subsystems depend on each other.
        if init > 0:
            try:
                self.init(**kwargs)
            except Exception:
                self.close()
                raise

    def is_fmc_present_for_channel(self, channel_number):
        return self.ANT_FMC_IS_PRESENT[channel_number]

    def is_fmc_present(self, slot_number):
        return self._FMC_present[slot_number]

    def close(self):
        """
        Close chFPGA object
        """
        # Close FMC boards
        while self._adc_board:
            fmc=self._adc_board.pop()
            if hasattr(fmc, 'close'):
                fmc.close()
        super(chFPGA_controller, self).close()  # Make sure we close underlying sytems (sockets, etc)


    def init(self,
             sampling_frequency=800e6,
             reference_frequency=10e6,
             adc_delay_table=None,
             data_width=4,
             group_frames=4,
             enable_gpu_link=1,
             verbose=0,
             **kwargs):
        """ Resets the chFPGA firmware to a known state with the specified parameters.
        """


        for (key,value) in kwargs.items():
            self._logger.warning('%r: Unknown arguments %s=%s. Ignoring.' % (self, key, repr(value)))

        self._sampling_frequency = sampling_frequency
        self._reference_frequency = reference_frequency
        self._FRAME_PERIOD = float(self.FRAME_LENGTH)/self._sampling_frequency

        self._logger.info('%r: --- Initializing FPGA ressources' % self)

        self._logger.debug('%r: --- Initializing GPIO' % self)
        self.GPIO.init()  # This stops the antenna procesors from sending data. Neeeded if the FPGA is flooding the buffers which prevent subsequent reads to come through
        self.GPIO.BUCK_PHASE = 0xfedcba9876543210  # debug
        #self.fpga.flush_data_socket() # Now the the data stops coming, flush the buffers
        self.mmi.flush()
        if verbose >= 2:
            self.GPIO.status()


        # self._logger.debug('--- Initializing I2C')
        # self.fpga_I2C.init()

        # if verbose >= 2: self._logger.debug('--- Initializing ML605 LCD')
        # self.LCD.init()
        # self.LCD.write('CHIME FW Version', col=0, row=0)
        # self.LCD.write('%s' % self.GPIO.get_bitstream_date(), col=0, row=1)

        # if verbose >= 2: self._logger.debug('--- Initializing ML605 PMBus')
        # self.ML605_PMBus.init()
        # if verbose >= 2: self.ML605_PMBus.status()



         # Module depend on the FMC_present flag after this point

        self._logger.debug('%r: --- Initializing REFCLK' % self)
        self.REFCLK.init()
        self.REFCLK.status()

        #Only do for ML605, not KC705 board
        self._logger.debug('%r: --- Initializing SYSMON' % self)
        self.SYSMON.init()
        self.SYSMON.status()

        self._logger.debug('%r: --- Initializing SPI' % self)
        self.SPI.init()
        self.SPI.status()

        self._logger.info('%r: --- Initializing FMC slots' % self)

        for (fmc_number, fmc) in enumerate(self._adc_board):
            if fmc and fmc.is_present():
                self._logger.debug('%r:   Powering up FMC%i' % (self, fmc_number))
                fmc.set_power(True)
                time.sleep(0.2) # Give it some time for the power to stabilize
                # We need to initialize the ADC board befor we initialize ANT (and its data acquisition) because the delay blocks need a clock
                self._logger.debug('%r:   Initializing FMC%i' % (self, fmc_number))
                fmc.init(sampling_frequency = sampling_frequency, reference_frequency=reference_frequency)
                fmc.status()
            else:
                self._logger.debug('%r:    Skipping FMC%i initialization since no board is present in that slot' % (self, fmc_number))

        self.sync() # might be needed  to make sure that the clock is running to set delays


#        self._FMC_present = self._adc_board[0].is_present()
        self._logger.debug('%r: === Initializing Channelizers' % self )
        self.ANT.init(delay_table=adc_delay_table, fmc_present=self.ANT_FMC_IS_PRESENT)
        self.ANT.status()

        self._logger.debug('%r: === Initializing 1st Crossbar' % self )
        if self.NUMBER_OF_CROSSBAR_OUTPUTS > 0:
            self._logger.debug('%r:  - 1st CROSSBAR' % self)
            self.CROSSBAR.init()
            self.CROSSBAR.status()
        else:
            self._logger.warning("%r: There is no 1st CROSSBAR module in this firmware build (so there can't be data streamed to the correlators or GPU links!)" % self);



        if self.NUMBER_OF_BP_SHUFFLE_LANES:
            self._logger.debug('%r: === Initializing Backplane Shuffle' % self)
            self.BP_SHUFFLE.init()
            # self.BP_SHUFFLE.status()

        self._logger.debug('%r: === Initializing 2nd Crossbar' % self)
        if self.NUMBER_OF_BP_SHUFFLE_LANES and self.NUMBER_OF_GPU_LINKS:
            self._logger.debug('%r:   - 2nd CROSSBAR' % self)
            self.CROSSBAR2.init()
            self.CROSSBAR2.status()
        else:
            self._logger.warning("%r: There is no 2nd CROSSBAR module in this firmware build" % self);

        self._logger.debug('%r: === Initializing FPGA correlators' % self)
        if self.NUMBER_OF_CORRELATORS > 0:
            self._logger.debug('%r:  - CORR' % self)
            self.CORR.init()
            self.CORR.status()
        else:
            self._logger.info('%r: There are no FPGA correlators in this firmware build' % self);

        self.set_data_width(data_width)  #sets the data width of both the SCALER and CROSSBAR
        self._logger.info('%r: Data width set to (Re+Im) = (%i+%i) bits' % (self, self.get_data_width(), self.get_data_width()))

        self.CROSSBAR.set_frames_per_packet(group_frames)
        self._logger.info('%r: The 1st crossbar will pack %i frames per packet' % (self, group_frames))

        if self.GPIO.NUMBER_OF_GPU_LINKS:
            self.GPU.set_enable(enable_gpu_link)

        self.CROSSBAR.configure()
        self._logger.info('%r: GPU link is currently %s' % (self, ['Disabled','Enabled'][bool(enable_gpu_link)]))

        # MGT is disabled
        #self._logger.debug('  - MGT_PLL')
        #self.MGT_PLL.init(fref=fref)
        #self._logger.debug('  - MGT')
        #self.MGT.init() # MGT_PLL must be initialized first

        self._logger.info("%r: Done with initializations." % self)

        #self._logger.info("Setting ADCDAQ delays.")

        #if adc_delay_table:
        #    self.ANT.set_delays(adc_delay_table)

        self.set_ant_reset(0) # disable antenna reset

        # self._logger.info("Setting default data source")
        # self.set_funcgen_function('ramp')
        # self.set_data_source('funcgen')
        # self.set_ADC_mode('data') # This implies a self.sync(), which will reset the antenna processors again to ensure data alignment
        # self.set_data_source('adc')


        self._last_init_time = time.time()

    # def get_fpga_cookie(self):
    #     """
    #     Reads the FPGA and returns the cookie that identifies the firmware.
    #     This method can be called before any FPGA modules are instatiated.
    #     """
    #     return self.read(self.mmi._STATUS_BASE_ADDR + self._SYSTEM_GPIO_BASE_ADDR + self._GPIO_COOKIE_REG) & 0x7F

    def get_config(self):
        config = chFPGA_config() # Create empty config container
        # Add configuration parameters

        config.config_protocol_version = (1,0)
        config.config_capture_time = time.time()
        config.system_firmware_version = self.get_version()
        config.system_platform_id = self.PLATFORM_ID
        config.system_interface_ip_address = self.interface_ip_addr
        config.system_fpga_ip_address = self.fpga_ip_addr
        config.system_fpga_port_number = self.fpga_port_number
        config.system_local_command_port_number = self.local_port_number
        config.system_local_data_port_number = self.get_local_data_port_number()
        config.system_local_corr_port_number = self.local_port_number + self.GPIO.CORR_IP_PORT_OFFSET


        config.number_of_antennas = self.NUMBER_OF_ANTENNAS
        config.system_list_of_antennas_with_channelizers = self.LIST_OF_ANTENNAS_WITH_FFT

        config.number_of_correlators_max = self.NUMBER_OF_CORRELATORS_MAX
        config.number_of_correlators = self.NUMBER_OF_CORRELATORS
        config.number_of_antennas_to_correlate = self.NUMBER_OF_ANTENNAS_TO_CORRELATE
        config.system_list_of_implemented_correlators = self.LIST_OF_IMPLEMENTED_CORRELATORS

        config.system_frame_length = self.FRAME_LENGTH
        config.system_sampling_frequency = self._sampling_frequency
        config.system_reference_frequency = self._reference_frequency
        config.system_frame_period = self._FRAME_PERIOD

        config.adc_board_is_present = bool(self._adc_board[0])
        if self._adc_board[0]:
            config.adc_board_temperature = self._adc_board[0].AmbTemp.temperature
            config.adc_board_adc_chip_temperature = [adc.get_temperature() for adc in self._adc_board[0].ADC]
            config.adc_serial = [fmc.serial for fmc in self._adc_board] #self._adc_board[0]._board_info['Serial #']
        config.antenna_data_source = self.get_data_source()
        config.antenna_fft_bypass = self.get_FFT_bypass()
        config.antenna_fft_shift_schedule = self.get_FFT_shift()
        config.antenna_scaler_gain = self.get_gain()
        config.antenna_adc_data_acquisition_delay_tables  = self.ANT.get_delays()
        config.FPGA_board_frequency = self.FreqCtr.read_frequency('CLK200', gate_time=0.05)
        config.CTRL_clock_frequency = self.FreqCtr.read_frequency('CTRL_CLK', gate_time=0.05)
        config.ant_clock = self.FreqCtr.read_frequency('ANT_CLK', gate_time=0.05)
        if self._IMPLEMENT_CORR:
            config.correlator_clock = self.FreqCtr.read_frequency('CORR_CLK', gate_time=0.05)
            config.correlator_capture_period_in_frames = [corr.ACC.CAPTURE_PERIOD for corr in self.CORR]
            config.correlator_integration_period_in_frames = [corr.ACC.INTEGRATION_PERIOD for corr in self.CORR]
        config.fmc_ref_clock = self.FreqCtr.read_frequency('FMC_REFCLK', gate_time=0.05)
        config.mgt_ref_clock = self.FreqCtr.read_frequency('MGT_REFCLK', gate_time=0.05)
        config.mgt_word_clock = self.FreqCtr.read_frequency('MGT_USRCLK2', gate_time=0.05)
        config.adc_clocks = [self.FreqCtr.read_frequency(('ADC_CLK'+str(i)), gate_time=0.05) for i in range(8)]
        # config.adc_serial = 'Not available'
        config.motherboard_serial = self.GPIO.FPGA_SERIAL_NUMBER
        # Add FFT shift, scaler gain, corr integration/capture period etc.
        # config.freq_flags = self.freq_flags  # JFC: what is that?
        return config


    def update_config(self):
        pass

    def read_bit(self, addr, bit):
        return (self.read(addr) & (1 << bit)) != 0

    def write_bit(self, addr, bit, data):
        old_data = self.read(addr)
        mask = 1 << bit
        self.write(addr, (old_data & (~mask)) | (mask if data else 0))

    def write_mask(self, addr, mask, data):
        old_data = self.read(addr)
        self.write(addr, (old_data & (~mask)) | (mask & data))

    def pulse_bit(self, addr, bit):
        old_data = self.read(addr)
        mask = 1 << bit
        self.write(addr, (old_data | mask))
        self.write(addr, (old_data & (~mask)))

    def sync(self, local=1, verbose=0):
        if verbose:
            self._logger.info("Sync...")
        if local:
            self.REFCLK.local_sync()
        else:
            self.REFCLK.sync()

    def pulse_ant_reset(self):
        """ Resets the stats of all antenna processor modules and clear the processing pipeline.
        Memory-mapped registers are not affected.
        """
        self.GPIO.pulse_ant_reset() # resets all

    def reset(self):
        """ Resets the antenna processor modules and collelators.
        Memory-mapped registers are not affected.
        """
        self.set_ant_reset(1)
        self.set_corr_reset(1)
        self.set_corr_reset(0)
        self.set_ant_reset(0)

    def set_default_channels(self, channels):
        """
            Sets the default channels to use in other functions when not specifically specified.
        """
        if isinstance(channels,int):
            channels = [channels]
        self.default_channels = channels

    def get_default_channels(self):
        """
            Returns the default channels that are used in other functions when not specifically specified.
        """
        return self.default_channels


    def set_channelizer_config(self, data_source=None, function=None, a=1, b=0, adc_mode='data', adcdaq_mode='data', fft_bypass=None, fft_shift=None, scaler_bypass=None, gain=None, postscaler=None, channels=None):
        """
            Single command used to set multiple channelizer settings. The data processing chain is:
            ADC --> ADCDAQ --> |        |
                   FUNCGEN --> | SRCSEL | --> FFT --> SCALER
                    INJECT --> |        |
        """
        if data_source is not None:
            self.set_data_source(data_source, channels=channels)

        if function is not None:
            self.set_funcgen_function(function=function, a=a, b=b, channels=channels)

        if adcdaq_mode is not None:
            self.set_adcdaq_mode(mode=adcdaq_mode, channels=channels)

        if fft_bypass is not None:
            self.set_fft_bypass(bypass_mode=fft_bypass, channels=channels)

        if fft_shift is not None:
            self.set_fft_shift(fft_shift, channels=channels)

        if gain is not None:
            self.set_gain(gain = gain, postscaler = postscaler, channels=channels)

        if adc_mode is not None:
            self.set_adc_mode(mode=adc_mode)

    set_data_path = set_channelizer_config # for legacy compatibility

    def set_data_source(self, source=None,  channels=None):
        '''
            Sets the data source on specified channels (or default channels if the channels are not specified).
        '''
        if (source is None) or (source.lower() not in self.ANT[0].SRCSEL.DATA_SOURCE_NAMES):
            raise ValueError("Invalid data source name '%s'. Valid data sources are %s:" % (source, ', '.join(self.ANT[0].SRCSEL.DATA_SOURCE_NAMES.keys())))

        if channels is None:
            channels = self.default_channels


        self.set_ant_reset(1) # Reset is needed to resyncronize the system with the new data
        for ch in channels:
            ant = self.ANT[ch]
            ant.SRCSEL.set_data_source(source.lower())
        self.set_ant_reset(0) # Reset is needed to resyncronize the system with the new data
        #self.sync() # SYNCs the ADC, and resets (again) the antenna processor to align the data with the ADC


    def get_data_source(self):
        '''
            Returns a list of data source for all channels.
        '''
        return [ant.SRCSEL.get_data_source() for ant in self.ANT.values()]


    def set_funcgen_function(self, function=None, a=1, b=0, channels=None):
        '''
        Sets the waveform generated by the function generator on specified channels (or default channels if the channels are not specified).
        This may cause one frame to partially contain the new waveform.
        '''
        if (function is None) or (function.lower() not in self.ANT[0].FUNCGEN.FUNCTION_NAMES):
            raise ValueError("Invalid function generator function '%s'. Valid functions are %s:" % (function, ', '.join(self.ANT[0].FUNCGEN.FUNCTION_NAMES.keys())))

        if channels is None:
            channels = self.default_channels

        for ch in channels:
            ant = self.ANT[ch]
            ant.FUNCGEN.set_function(function.lower(), a=a, b=b)

    def get_adc_board(self, channel):
        """
        Returns the ADC board object that is associated with the specified antenna channel.
        If channel is a list, returns a list of unique board objects associated with the specified channels.
        """

        if isinstance(channel, int):
            #channel = [channel]
            board_number = channel // 8
            return self._adc_board[board_number]
        else:
            board_list=[]
            for ch in channel:
                board_number = ch // 8
                board_list.append(self._adc_board[board_number])
            return list(set(board_list))

    ADC_MODE_NAMES = {
        # name, mode number, period (in 4-bytes words)
        'data': (0, 64),  # ADC sends analog data
        'ramp': (1, 64),  # ADC sends ramp from 0 to 255
        'pulse': (2, 11),  # ADC sends ten 0x00 followed by one 0xff
        }

    # ADC_MODE_NAMES_REVERSED = util.reverse_dict(ADC_MODE_NAMES)

    def set_adc_mode(self, mode='data', channels=None):
        """
        Sets the operating mode of the all the ADCs, sets the proper CAPTURE
        period, and sends a SYNC to actuate the change.

        By default, all ADCs on any board handling the specified channels are set to the desired mode.
        If no channels are specified, the default channel list is used.
        Again: both ADCs on every target board are set, even if we specify channels handled by only one adc chip.

        mode:
            'data': Normal mode (ADC output contains analog samples)
            'ramp': Ramp mode (ADC output contains repeating 0-255 pattern. Note that ADCDAQ inverts bit 7 during acquisition to convert offset binary to 2's complement binary)
            'pulse': Strobe mode (ADC output contains one 0xFF followed by ten 0x00. It repeats with a pariod of 11. Same comment as above)
        """
        if mode.lower() not in self.ADC_MODE_NAMES:
            raise ValueError("Invalid ADC mode '%s'. Valid modes are %s" % (mode, ', '.join(self.ADC_MODE_NAMES.keys())))
        (mode_value, capture_period) = self.ADC_MODE_NAMES[mode.lower()]

        if channels is None:
            channels = self.default_channels

        # Set the mode on all affected ADC boards
        adc_boards = self.get_adc_board(channels)
        for adc_board in adc_boards:
            if not adc_board.is_present():
                self._logger.warning('%r: ADC Board of FMC slot #%i (%s) is not present. Ignoring set_ADC_mode() command for this board' % (self, adc_board.fmc_number, adc_board.fmc_name))
            else:
                adc_board.ADC.set_test_mode(test_mode=mode_value)

        # Set the capture period for all specified channels
        for ch in channels:
            ant = self.ANT[ch]
            ant.ADCDAQ.CAPTURE2_PERIOD = capture_period # set the period so we are ready to capture data correctly after the SYNC resets the CAPTURE logic

        # self.current_ADC_mode = mode_value
        self.sync()  # make sure the ADC mode is set and that capture  restarts properly with the right period

    set_ADC_mode = set_adc_mode  # For legacy code compatibility

    def get_adc_mode(self):
        """
        Gets the current operating mode of all the ADCs as a string. This
        assumes all the ADCs are operating in the same mode. If not, an error
        message will be returned.
        """

        mode_value = []
        # get the ADC mode number for every ADC board
        for adc_board in self._adc_board:
            if adc_board.is_present():
                mode_value.append(adc_board.ADC.get_test_mode())
        mode_value = list(set(mode_value)) # eliminate all duplicates. We should be left with only one mode number.
        if len(mode_value) != 1:
            raise RuntimeError('The ADC chips on the ADC boards are not ALL in the same mode')
        mode_string = [key for (key, info) in self.ADC_MODE_NAMES.items() if info[0]==mode_value[0]][0] # get the name of the first ADC mode that matches the provided number
        return mode_string

    def set_adcdaq_mode(self, mode='data', channels=None):
        """
        Sets the source of the data acquisition module.

        test_mode:
            'data': the ADCDAAQ module sends data from the ADC
            'ramp': The ADCDAQ sens an internally generated ramp
        """

        if channels is None:
            channels = self.default_channels

        for ch in channels:
            ant = self.ANT[ch]
            ant.ADCDAQ.set_ADCDAQ_mode(mode) # set the period so we are ready to capture data correctly after the SYNC resets the CAPTURE logic

    set_ADCDAQ_mode = set_adcdaq_mode


    def set_ant_reset(self, state):
        self.GPIO.ANT_RESET = state

    def set_corr_reset(self, state):
        self.GPIO.CORR_RESET = state

    def set_trig(self, state):
        self.GPIO.GLOBAL_TRIG = state

    def stop_data_capture(self):
        """
        Stops the transmission of data.
        """
        self.GPIO.GLOBAL_TRIG = 0 # disable data transmission if continuous mode is currentlly selected
        for ant in self.ANT.values():
            ant.PROBER.RESET = 1

    def start_data_capture(self, period=None, frames_per_burst=1,  number_of_bursts=0,  channels=None, source='scaler', sync=1, verbose=1, burst_period_in_seconds=None, burst_period_in_frames=None):
        """
        Triggers the capture and transmission of ADC (pre-FFT) or SCALER (post
        FFT) data frames the Ethernet port. This function does not receive the
        frames from the ethernet port. This has to be done separately.

        Data is sent as N bursts ('number_of_bursts') of M frames
        ('frames_per_burst') . If 'number_of_bursts' is zero or not specified,
        burst transmission is continuous.

        Burst repetition rate is set either as a period specified in seconds
        ('period' or 'burst_period_in_seconds') or as a number of frames
        ('burst_period_in-frames').

        'source' selects the data source and is either 'adc' or 'scaler'.
        Default is 'scaler'.
        """
        if channels is None:
            channels = self.default_channels

        if burst_period_in_seconds is not None:
            period = burst_period_in_seconds

        if ( (burst_period_in_frames is None) and (period is None)) or ((burst_period_in_frames is not None) and (period is not None)) :
            raise ValueError("You must specify either 'period' or 'burst_period_in_frames' ")

        if period is not None:
            burst_period_in_frames = max(float(period)/self._FRAME_PERIOD, 1)


        burst_period_in_frames = int(burst_period_in_frames)
        # print "%s" % channels.__repr__()
        # print "%i" frames_per_burst
        # print burst_period_in_frames
        # print burst_period_in_frames*self._FRAME_PERIOD*1000
        # print ('continuously when TRIG=1' if not number_of_bursts else ('for a total of %i bursts' % number_of_bursts) )
        if verbose:
            self._logger.info("%r: Configuring antennas %s to transmit %i-frame burst every %i frames (i.e .every %.3f ms) %s." % (
               self,
               channels.__repr__(),
               frames_per_burst,
               burst_period_in_frames,
               burst_period_in_frames*self._FRAME_PERIOD*1000,
               ('continuously when TRIG=1' if not number_of_bursts else ('for a total of %i bursts' % number_of_bursts))))
            frames_per_second = len(channels)*frames_per_burst*1.0/self._FRAME_PERIOD/burst_period_in_frames
            bits_per_second = frames_per_second * 8 * self.FRAME_LENGTH
            self._logger.info('%r: Data rates are: %f kFrames/s, %f Mbits/s' % (self, frames_per_second/1e3, bits_per_second/1e6))

        self.set_trig(0) # disable data transmission if continuous mode is currentlly selected
#        self.set_ant_reset(1) # resets all
#        if clear_buffer:
#            self.flush_frame_buffer()

        for ant in self.ANT.values():
            ant.PROBER.set_data_source(source)
            ant.PROBER.RESET = 1
            ant.PROBER.PROBE_ID = 0xA0 + ant.ant_number
            ant.PROBER.config_capture(frames_per_burst=frames_per_burst, burst_period=burst_period_in_frames, number_of_bursts=number_of_bursts)
            if ant.ant_number in channels:
                self._logger.info('%r: Enabling Capture for Antenna %i' % (self, ant.ant_number))
                ant.PROBER.RESET = 0

        self.set_trig(1)  # enables data transmission if continuous mode is selected
#       self.set_ant_reset(0) # disable reset all

    def set_fft_bypass(self, bypass_mode, channels=None):
        """
        Sets the BYPASS flag on both the FFT modules.
        If the list of channels is specified, only these channels will be set.
        All antenna processors are reset to force the FFT to resynchronize to the frame boundaries.

        History:
            2012-08-31 JFC: Added this function
            2012-10-02 JFC: Added antenna reset after bypass change to ensure the FFT is synced.
            2013-12-05 JFC: Changed behavior so only the specified channels are changed.
            2014-02-09 JFC: Removed scaler bypass setting
        """

        if channels is None:
            channels = self.default_channels

        configured_channels = set()
        for ch in channels:
            if ch not in self.ANT:
                self._logger.warning('%r: FFT bypass mode on antena channel %i are not set because that channel is not available' % (self, ch))
            elif ch not in self.LIST_OF_ANTENNAS_WITH_FFT:
                self._logger.warning('%r: FFT bypass mode on antena channel %i are not set because that channel does not have an FFT module' % (self, ch))
            else:
                self.ANT[ch].FFT.BYPASS = bypass_mode
                configured_channels.add(ch)
        self._logger.info('%r: Setting FFT bypass mode to %s for Antenna %s' % (self, str(bool(bypass_mode)), ', '.join([str(i) for i in configured_channels])))
        self.reset();
        #self.sync()

    set_FFT_bypass = set_fft_bypass # for legacy code compatibility

    def get_fft_bypass(self):
        """
        Returns a list indicating if the FFT is bypassed or not for each antenna.
        """
        return [bool(ant.FFT.BYPASS) for ant in self.ANT.values()]

    get_FFT_bypass = get_fft_bypass # for legacy code compatiblity

    def set_scaler_bypass(self, bypass_mode, channels=None):
        """
        Sets the BYPASS flag on the SCALER modules.
        If the list of channels is specified, only these channels will be set.

        History:
            2013-12-05 JFC: Added this function
        """

        if channels is None:
            channels = self.default_channels

        self._logger.info('%r: Setting SCALER bypass mode for Antenna %s' % (self, ', '.join([str(i) for i in channels])))
        for ant in self.ANT.values():
            if ant.ant_number in channels:
                ant.SCALER.BYPASS = bypass_mode
            # else:
            #     self._logger.warning('Attemnpting to set SCALER bypass mode for antenna channel %i which is not present on this card' % ch)


    def get_scaler_bypass(self):
        """
        Returns a list indicating if the SCALER is bypassed or not for each antenna.
        """
        return [bool(ant.SCALER.BYPASS) for ant in self.ANT.values()]


    def set_global_trigger(self, trigger_state):
        """
        Sets the global trigger to the specified value.

        In injection mode, the injection buffers are read only when trigger=True. This allows the buffers from all the antennas to be read simultaneously. In this case, the CAPTURE flag if the injected frames is always set.
        In other modes, the trigger status is passed to the CAPTURE flag of the data frames on a frame-by-frame basis (the CAPTURE flag is set at the begining of the frame ans syats constant until the end of the frame so no partial frames will be captured downstream.)

        History:
            120918 JFC: Added this function
        """
        self.GPIO.set_global_trig(trigger_state)

    def inject_frame(self,  data=None, length=None, channels=None):
        """ Inject a frame of data in the specified antenna processing pipeline"""
        if channels is None:
            channels = self.default_channels
        if isinstance(channels, int):
            channels = [channels]
        for ch in channels:
            if isinstance(data, dict):
                self.ANT[ch].INJECT.inject_frame(data[ch])
            else:
                self.ANT[ch].INJECT.inject_frame(data)

    def start_corr_capture(self,  integration_period=1.0, capture_period=None, corr_to_use=None, verbose=1):
        """
        Instructs chFPGA to starts integrating and capturing the correlator outputs at the specified period. The captures data is sent over the Ethernet interface.
        The capture period can be optionnaly specified independently from the integration period. If not specified, it is equal to the integration period.
        This function does not receive the frames from the ethernet port. This has to be done separately.

        corr_to_use -> if not None, is a list specifying which to correlators to use
        History:
            2012-10-02 JFC: Created
            2013-03-25 KMB
        """

        if not self._last_init_time:
            self._logger.warning('%r: The system is not initialized. This might not work.' % self)

        if capture_period is None:
            capture_period = integration_period

        capture_period_in_frames = int(capture_period*1.0/self._FRAME_PERIOD)
        integration_period_in_frames = int(integration_period*1.0/self._FRAME_PERIOD)

        self.set_ant_reset(1)
        self.set_corr_reset(1)
        if corr_to_use is None:
            corrs = self.LIST_OF_IMPLEMENTED_CORRELATORS
            corrs_not_used = []
        else:
            corrs = corr_to_use
            corrs_not_used = list(set(self.LIST_OF_IMPLEMENTED_CORRELATORS).difference(corr_to_use))
        for corr_num in corrs:
            corr = self.CORR[corr_num]
            self._logger.info(
                '%r: Configuring correlator %i to integrate '
                'over %f seconds (%i frames) '
                'and transmit data every %f seconds (%i frames)' % (
                    self,
                    corr.instance_number,
                    integration_period,
                    integration_period_in_frames,
                    capture_period,
                    capture_period_in_frames))
            corr.ACC.RESET = 0
            corr.ACC.config(integration_period=integration_period_in_frames, capture_period=capture_period_in_frames)
        for corr_num in corrs_not_used:
            self._logger.info('%r: Disabling correlator %i' % (self, corr.instance_number))
            corr = self.CORR[corr_num]
            corr.ACC.RESET = 1
        self.set_corr_reset(0)
        self.set_ant_reset(0)
        #self.sync()

    def get_version(self):
        """
        Returns the firmware revion currenting running on the FPGA (which si the date and time of bitstream generation)
        """
        return self.GPIO.get_bitstream_date()

    def get_adc_delays(self):
            return self.ANT.get_delays();

    def set_adc_delays(self, delay_table):
            return self.ANT.set_delays(delay_table);

    def set_adc_delays_with_check(self, delay_table):
            """
            Sets adc delay table, check if get ramp errors,
            and retrys to set delays till no errors or tried 10 times.
            On 10 tries will continue but just report error.
            """
            ntries = 0
            while ntries < 15:
                delay_return = self.ANT.set_delays(delay_table)
                err = self.check_ramp_errors()
                if err == 0:
                    break
                else:
                    self._logger.info( "{0} errors after setring delays, retrying...".format(err) )
                ntries += 1
            if ntries == 15:
                self._logger.info("After setting delays still had ramp errors after 15 tries.")
            return delay_return

    def check_ramp_errors(self):
        """
        Uses internal ramp error checker, returns 0 if no errors,
        otherwise returns total number of word errors.
        """
        old_adc_mode = self.get_adc_mode()
        self.set_adc_mode('ramp')
        self.sync()
        # Clear the word and bit error counters
        for ant in self.ANT.values():
            ant.ADCDAQ.RAMP_ERR_CLEAR = 0
            ant.ADCDAQ.RAMP_ERR_CLEAR = 1
        t0 = time.time()
        word_error = np.zeros(len(self.ANT))
        while time.time() - t0 <= 0.1:
                for (i, ant) in self.ANT.items():
                    word_error[i] += ant.ADCDAQ.RAMP_ERR_CTR
                    ant.ADCDAQ.RAMP_ERR_CLEAR = 0
                    ant.ADCDAQ.RAMP_ERR_CLEAR = 1
        word_errors = sum(word_error)
        self.set_adc_mode(old_adc_mode)
        self.sync()
        return word_errors

    def read_eye_diagram(self, channels=[0], offset=5, noffsets=3):
        """
        Measures the eye diagram of the ADC digital data lines using the ADCDAQ capture feature.
        By default takes data at 3 offset locations (0,1,2), but can measure more
        """
        old_delays = self.get_adc_delays()
        old_adc_mode = self.get_adc_mode()
        self.set_adc_delays([[ [0]*8, [0]*8]] * 16); # Set all sampling delays and offsets to zero
        self.set_adc_mode('pulse') # generate pulse pattern

        data={}
        for ch in channels:
            d = np.zeros((32, noffsets), dtype=np.uint8)
            self._logger.info('Reading channel %i.' % (ch))
            adcdaq = self.ANT[ch].ADCDAQ

            for dly in range(32):
                adcdaq.set_delay((dly, None))
                d[dly, :] = self.ANT[ch].ADCDAQ.get_pattern(period=11)[offset[ch]:offset[ch] + noffsets];
            data[ch] = d
        self.set_adc_delays(old_delays) # restore original delays before the function was called
        self.set_adc_mode(old_adc_mode)
        return data

    def compute_adc_delays(self, channels=[0], offset=[2, 3, 3, 3, 3, 3, 3, 3, 4, 3, 3, 3, 3, 3, 3, 3], print_results=True):
        """
        Measures the eye diagram of the ADC digital data lines and computes the optimum delays to ensure reliable data acquisition.
        """

        current_delay = self.get_adc_delays()
        data = self.read_eye_diagram(channels, offset=offset)
        n = np.zeros((16, 3), dtype=np.uint8)
        delays={}

        for ch in channels:
            for bit in range(8):
                mask = 1 << bit
                n[bit, :] = np.sum((data[ch] & mask) / mask, axis=0)
            #self._logger.info('n is ', n)

            n_min = np.min(n, axis=0) # minimum number of delay values that allowed the pulse in each slot
            N = np.argmax(n_min) # slot with the maximum number of possible delays for all bits
            N = 1
            self._logger.info('%r: Aligning bits on sample #%i' % (self, N))

            self._logger.info('%r: CHANNEL %i' % (self, ch))
            if print_results:
                    print('CHANNEL %i (delay = %i)' % (ch, offset[ch]))
            computed_delay = np.zeros(8, dtype=np.uint8)
            for bit_number in range(8):
                mask = 1 << bit_number
                d = (data[ch][:, 0] & mask) / mask
                computed_delay[bit_number] = np.sum(d*range(32)) / np.sum(d)
                bit_string = ''
                for delay in range(len(d)):
                    if delay == current_delay[bit_number]:
                        bit_string += 'X'
                    elif delay == computed_delay[bit_number]:
                        bit_string += '!O'[d[delay]]
                    else:
                        bit_string += '.#'[d[delay]]
                s = 'Bit %i: %s Delay = %2i' % (bit_number, bit_string, computed_delay[bit_number])
                if print_results:
                        print s
                self._logger.info(s)

            delays[ch]=computed_delay
        return delays

    compute_delays = compute_adc_delays # For legacy software compatibility

    def status(self):
        self._logger.info('%r: ----------- chFPGA status ---------------' % self)
        self._logger.info('%r:  Controller IP address: %s, port: %i ' % (self, self.ip_addr, self.fpga.port_number))
        self._logger.info('%r:  Firmware version: %s' % (self, self.get_fpga_firmware_version()))
        self._logger.info('%r:  Number of antenna inputs: %i' % (self, self.NUMBER_OF_ANTENNAS))
        self._logger.info('%r:  Number of antennas with channelizers: %i (antennas %s)' % (self, len(self.LIST_OF_ANTENNAS_WITH_FFT), str(self.LIST_OF_ANTENNAS_WITH_FFT)))
        self._logger.info('%r:  Number of correlators: %i (correlators %s)' % (self, len(self.LIST_OF_IMPLEMENTED_CORRELATORS),str(self.LIST_OF_IMPLEMENTED_CORRELATORS)))

        self.FreqCtr.status()

    def set_data_width(self, width):
        """
        Set the number of bits used to represent the values computed by the channelizers and used by the GPU link and FPGA correlators.
        All channelizers, crossbars and correlators are set to the new setting.
        width=4: data is 4 bits Real + 4 bits Imaginary
        width=8: data is 8 bits Real + 8 bits Imaginary
        """

        if width not in (4,8):
            raise ValueError('Number of bits %i is invalid. Only 4 or 8 is allowed' % width)

        # Set the channelizer data width
        self.ANT.set_data_width(width)

        # Set the crossbar data width
        self.CROSSBAR.set_data_width(width)

    def get_data_width(self):
        """
        Returns number of bits used to represent the values computed by the channelizers and used by the GPU link and FPGA correlators.
        If all the hardware modules are not set in the same mode, an error is raised.
        """
        # get the channelizer and crossbar data width
        chan_data_width = self.ANT.get_data_width()
        xbar_data_width = self.CROSSBAR.get_data_width()

        if xbar_data_width and xbar_data_width != chan_data_width:
            raise RuntimeError("The channelizers and crossbar are not set to the same data width (chan=%i bits, xbar=%i bits). The data stream won't make much sense" % (chan_data_width, xbar_data_width))
        return chan_data_width

    def configure_crossbar(self, *args, **kwargs):
        self.CROSSBAR.configure(*args, **kwargs)

    def set_offset_binary_encoding(self, offset=True, channels=None, sync=True):
        """
        Set the output to be encoded in offset binary instead of 2's compliment
        if sync is true, perform a sync afterward.  Necessary for data to continue flowing
        """
        if channels == None:
            channels = self.default_channels

        if not isinstance(channels, list):
            raise ValueError("Channels must be a list")
        else:
            # Set the scaler to use offset binary
            for channel in channels:
                self.ANT[channel].SCALER.USE_OFFSET_BINARY=offset
            if sync:
                self.sync()

    def set_send_flags(self, send_flags=True, crossbar_outputs=None, sync=True):
        """
        Configures the gpu output to send flags in the packets.
        """
        if crossbar_outputs == None:
            crossbar_outputs = range(self.NUMBER_OF_CROSSBAR_OUTPUTS)

        if not isinstance(crossbar_outputs, list):
            raise ValueError("'crossbar_outputs' must be a list")
        else:
            # Set the scaler to use offset binary
            for output in crossbar_outputs:
                self.CROSSBAR[output].CH_DIST.SEND_FLAGS=send_flags
            if sync:
                self.sync()

    def set_gain(self, gain=None, postscaler=None, channels=None, use_fixed_gain=False):
        """
        Sets the gain between the (18+18) bits input of the scaler module (from the FFT) to its 4- or 8- bit scaler output.
        The gain can be set individually for every frequency bins and every ADC channel.

        A gain consist of a tuple G=(Glin, Glog):
            1) Glin is a linear complex gain which can be unique to each bin.
               It can be a scalar applied to every bin, or a 1024-point vector to specify a gain for evey bin.
               The real and imaginary part of the linear gain are integer values ranging from -32768 to 32767.

            2) Glog is a binary scaling factor, which is an integer between 0 and 31 representing a power of two that multiplies the linear gain.
               This is a scalar common to every bin.

        The actual gain between the scaler input and output for bin 'b' is:
           4-bit mode: out/in = Glin(b) * 2**(Glog-31)
           8-bit output: out/in = Glin(b) * 2**(Glog-27)

        G can be specified in the following manner:
           G = Glin              : Sets only the linear gain. Same as (Glin, None)
           G = (Glin, None)      : Same as above
           G = (None, Glog)      : Sets only the postscaler
           G = (Glin, Glog)      : Sets both the linear gain and the postscaler

        The 'gain' parameters can be specified as:
            gain = G: the specified gain is applied only to the ADC channels specified in the list 'channels'.
            gain = {ch1: G1, ch2: G2 ...} : The gain is applied to specified channels, but only if they are included in 'channels'
            gain = [ (ch1, G1),  (ch2, G2), ...]: Same thing, but in a list format
            gain = [ (ch_list , G1), (ch_list2, G2), ...]: Same thing, but we can apply the gains to lists of channels

        If 'channels' is None, it is applied to the default (active) channels (see set_default_channels()).

        If 'postscaler' is specified, it will be used as default value when Glog = None.

        'use_fixed_gain': if True, enables the use of fixed gain mode of the scaler module. In this case, 'gain' can only be a scalar. Is False by default. This is normally used

        Notes:
            1) The PFB/FFT has an intrisic gain of 512 (a constant FFT input of '1' will yield the value 512 in bin 0 at the input of the scaler.
            2) If the FFT is bypassed, the 8-bit values from the ADC or the function generator are applied directly to the scaler input.
            3) In 4-bit mode, the output value is taken from bits 31 to 34 of the postscaled-value. In 8-bit mode, bits 27 to 31 are used.
            4) A smaller postscaler value allows a larger gain to be used to acheive the same overall gain while providing more gain resolution.
            A gain of (1, 31) allows the function generator values to appear on the scaler output with an overall gain of 1 in 4-bit mode. This is equivalent to (2, 30), (4,29) ... (16384, 8), except that the latter offers more gain resolution.
            A gain of (1, 27) dies the same in 8-bit mode.
(16384, 8), except that the latter offers more gain resolution.

        Examples:
            set_gain(1) # Sets all gains to 1, leaves the poscslaler unchanged fro all antennas.
            set_gain((1, None)) # Same thing
            set_gain(postscaler = 26) # Sets postscaler on all antennas
            set_gain((1,31)) # For all antennas, sets all gains to 1 and postscaler to 31
            set_gain(16384,8) # In 4-bit, FFT enabled mode, outputs a value of '1' on bin 0 when the input of the FFT is a constant '1'.
            set_gain(np.arange(1024), channels=[1,2,3])
            set_gain({1: 16384, 4: 1300+15000*j, 5: np.arange(1024)}) # sets ADC channels 1-3 to a real gain of 16384, channel 4 to complex gain of (1300+15000j), and channels 5-7 with a gain ramp from 0 to 1023.
        History:
            2012-11-28 JM: Added this function
            2014-02-08 JFC: Rewrote and documented this function for the new scaler supporting complex gain tables.
        """

        # if postscaler is not None:
        #     if postscaler<0 or postscaler>31:
        #         raise chFPGAException('Invalid postscaler value');

        #         if postscaler is not None

        if channels is None:
            channels = self.default_channels

        # Convert to a list of channel-gain tuples
        if isinstance(gain, list):
            pass
        elif isinstance(gain, dict):
            gain = gain.items()
        else:  # if anything else including None, a scalar, a gain tuple etc.
            gain = [ (channels, gain) ]

        configured_channels = set()

        for (channel_list, gain_value) in gain:
            # Make sure channel_list is a list (in case we provide a single channel number)
            if isinstance(channel_list, int):
                channel_list = [channel_list]
            # Extratc Glin and Glog from the specified gain value
            if gain_value is None:
                Glin = None
                Glog = None
            elif isinstance(gain_value, (tuple, list)):
                Glin = gain_value[0]
                Glog = gain_value[1]
            else: # if a scalar or a vector
                Glin = gain_value
                Glog = None
            # Replace default postscaler value if one is provided
            if (Glog is None) and (postscaler is not None):
                Glog = postscaler

            for ch in channel_list: # process each channel
                if ch not in channels:
                    continue
                if ch not in self.ANT.keys():
                    self._logger.warning('%r: Gains on antenna channel %i are not set because that channel is not available' % (self, ch))
                    continue
                # Set the postscaler value
                if Glog is not None:
                    self.ANT[ch].SCALER.SHIFT_LEFT = Glog

                if use_fixed_gain:
                    if not np.isscalar(Glin):
                        raise TypeError('%r: Only scalar gains are allowed when using set_fixed_gain=True.' % self)
                    self.ANT[ch].SCALER.USE_GAIN_TABLE = 0
                    self.ANT[ch].SCALER.set_fixed_gain(Glin)
                else:
                    self.ANT[ch].SCALER.USE_GAIN_TABLE = 1
                    self.ANT[ch].SCALER.set_gain_table(Glin)
                configured_channels.add(ch)
        self._logger.info('%r: Setting scaler gains for Antenna %s' % (self, ', '.join([str(i) for i in configured_channels])))

    def get_gain(self):
        """
        Returns the log2 SCALER gain each antenna, and the linear gain table used for each antenna or the fixed gain.
        """
        gain_list = []
        for ant in self.ANT.values():
            glog = ant.SCALER.SHIFT_LEFT
            glin = ant.SCALER.get_gain_table()
            gain_list.append([ant.ant_number, [glin,glog]])
        return gain_list


    def set_fft_shift(self, fft_shift=0b11111111111, channels=None):
        """
        Sets the FFT shift schedule for the FFT.  Each bit represents a divide by 2 for that stage of the FFT.  Default is to shift every stage.  11 stage FFT, so default is 2**11-1.
        expects a number  in the range 0b11111111111 (2047) and 0b00000000000 (0).

        History:
            2013-02-19 KMB: Added this function
        """

        if channels is None:
            channels = self.default_channels

        for ant in self.ANT.values():
            if ant.ant_number in channels:
                self._logger.info('%r: Setting FFT shift of antenna %i' % (self, ant.ant_number))
                ant.FFT.FFT_SHIFT = fft_shift

    set_FFT_shift = set_fft_shift  # For legacy code compatibility

    def get_fft_shift(self):
        """
        Returns the FFT shift schedule for each antenna.
        """
        return [ant.FFT.FFT_SHIFT for ant in self.ANT.values()]

    get_FFT_shift = get_fft_shift # for legacy compatibility

    def set_user_output_source(self, source):
        """ Selects the signal to be sent to the SMA-A connector of this IceBoard. 'source' is the source name (as a string). """
        self.GPIO.set_user_output_source(source)

    def get_user_output_source(self):
        """ Return the name of the source currently routed to SMA-A"""
        return self.GPIO.get_user_output_source()

    def set_frame_pwm(self, offset, high_time, period, reset=False):
        """ Sets the frame-based PWM generator. All times are stated as the numbe rof frames. A SYNC is needed after changes."""
        self.GPIO.set_pwm(offset, high_time, period, reset=False)
        if reset:
            self.sync()

    def check_adc_data_acquisition(self, test_duration=1):
        """
        Sets the ADC in ramp mode and compare the incoming ramp in real time with an internally generated ramp to combute the total number of words in error (and an error count for each bit)
        """
        old_adc_mode = self.get_adc_mode()
        self.set_adc_mode('ramp')
        self.sync() # sync the board to make sure that data acquisition starts on the right ramp sample

        # Clear the word and bit error counters
        for ant in self.ANT.values():
            print 'Clearing antenna', ant.ant_number
            ant.ADCDAQ.RAMP_ERR_CLEAR=0
            ant.ADCDAQ.RAMP_ERR_CLEAR=1
        self._logger.info('%r: Measuring the data acquisition error rate over %0.1f seconds...' % (self, test_duration))
        t0 = time.time();
        word_error = np.zeros(len(self.ANT))
        bit_error = np.zeros((len(self.ANT), 8))
        try:
            while time.time() - t0 <= test_duration:
                for (i, ant) in self.ANT.items():
                    print  self.ANT[i].ADCDAQ.RAMP_ERR_CTR,
                    word_error[i] += ant.ADCDAQ.RAMP_ERR_CTR
                    for bit_number in range(8):
                        bit_error[i, bit_number] += ((ant.ADCDAQ.BIT_ERR_CTR >> (bit_number*4)) & 0x0F)
                    ant.ADCDAQ.RAMP_ERR_CLEAR = 0
                    ant.ADCDAQ.RAMP_ERR_CLEAR = 1
                    # self._logger.info('CH%i: %3i (%08X)' % (ant.ant_number, ant.ADCDAQ.RAMP_ERR_CTR, ant.ADCDAQ.BIT_ERR_CTR))
        except KeyboardInterrupt:
            pass

        for (i, ant) in enumerate(self.ANT):
            self._logger.info('%r: CH%i: %5i word errors, bit errors (7:0) = (%s)' % (self, ant.ant_number, word_error[i], ','.join('%3i' % e for e in bit_error[i,::-1])))
        total_word_errors = np.sum(word_error)
        self._logger.info('%r: There were %i word errors in total' % (self, total_word_errors))
#        self.set_adc_mode(old_adc_mode)
        return total_word_errors

    def test_speed(self, n=1000, timeout=0.1):
        old_timeout = self.fpga.get_timeout()
        self.fpga.set_timeout(timeout)
        t0 = time.time()
        errors = 0
        trials = 0
        for i in xrange(n):
            try:
                trials += 1
                self.get_fpga_cookie()
            except IOError:
                errors += 1
                print 'error on transaction #%i' % i
            except KeyboardInterrupt:
                break
        t1 = time.time()
        self.fpga.set_timeout(old_timeout)
        print '%i read operations performed in %.2f s (%.0f read/s) with %i errors (%0.3f%% errors)' % (trials, t1 - t0, float(n)/(t1 - t0), errors, float(errors)/float(trials)*100)

    def get_temperatures(self):
        res = {}
        res['FPGA_core']=self.SYSMON.temperature()
        for (fmc_number, board) in enumerate(self._adc_board):
            if board:
                for (adc_number, adc) in enumerate(board.ADC):
                    res['FMC%i ADC%i'%(fmc_number, adc_number)] = adc.get_temperature()
        return res

    def init_crossbars(self, dsmap=range(16), frames_per_packet=3, cb1_lanes=16, cb1_bins=64, cb2_lanes=8, cb2_bins=1, cb2_bypass=False, bp_bypass=1, remap=True):
        """ Initializes the 1st and 2nd crossbar to reorder and package the channelizer data send to the GPU correlators in the desired format.

        `ib` is the IceBoard to be configured.
        """
        if not self.slot:
            raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards')

        self.set_ant_reset(1)
        self.set_corr_reset(1)
        cb1 = self.CROSSBAR
        cb2 = self.CROSSBAR2

        if frames_per_packet < 1 or frames_per_packet > 4:
            raise ValueError('Number of frames per packet must be between 1 and 4')
        if cb1_lanes not in [4,8,12,16]:
            raise ValueError('Crossbar 1 number of input lanes must be 4,8,12 or 16')
        if cb2_lanes % 2:
            raise ValueError('Crossbar 2 number of input lanes must be a multiple of 2')

        self._logger.info('%r: Configuring crossbars 1 & 2 with frames_per_packet=%i, cb1_lanes=%i, cb1_bins=64, cb2_lanes=%i, cb2_bins=%i, cb2_bypass=%s, bp_bypass=%s' % (self, cb1_lanes, cb1_bins, cb2_lanes, cb2_bins, bool(cb2_bypass), bool(bp_bypass)))

        words_per_bin = cb1_lanes / 4
        cb1_minimum_bin_spacing = 16
        cb2_minimum_bin_spacing = 8

        self.BP_SHUFFLE.BYPASS = bp_bypass

        # for gtx in gpu_links.CHANNEL:
        #     gtx.LOOPBACK = bp_bypass

        # Select the bins so slot 0 receives bins 0-63, slot 1 has 64-127 ... slot 15 hs 960-1023
        for (i, bs) in enumerate(cb1):
            bs.GROUP_FRAMES = frames_per_packet
            bs.NUMBER_OF_LANES = cb1_lanes
            if remap and not bp_bypass:
                tx = (self.slot, i)  # unique transmitter id (slot, lane)
                destination_slot = self.crate.get_matching_rx(tx)[0]
                bs.select_bins(np.arange(cb1_bins) * cb1_minimum_bin_spacing + (dsmap[destination_slot-1]))
            else:
                bs.select_bins(np.arange(cb1_bins) * cb1_minimum_bin_spacing)
            # bs.select_bins(np.arange(800))
        #cb1.configure(cb1_bins)

        for (i, bs) in enumerate(cb2):
            bs.BYPASS = bool(cb2_bypass)
            bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
            bs.NUMBER_OF_LANES = cb2_lanes
            bs.NUMBER_OF_BINS_PER_FRAME = cb1_bins
            bs.NUMBER_OF_WORDS_PER_BIN = cb1_lanes/4
            bs.select_bins(np.arange(cb2_bins) * cb2_minimum_bin_spacing + i)
        #cb2.configure(cb2_bins)

        header_size = 16
        packet_flags_size = 4
        eth_overhead = 42
        bp_overhead = 8
        eth_data_rate = 156.25e6 * 66 * 32/33
        bp_data_rate = 156.25e6* 50 * 32/33
        packet_rate = 800e6/2048/frames_per_packet
        cb1_payload_size = header_size + packet_flags_size + frames_per_packet * (words_per_bin * cb1_bins + cb1_bins + 1) * 4
        cb1_eth_packet_size = (cb1_payload_size+eth_overhead+7)//8*8
        cb1_eth_data_rate = cb1_eth_packet_size * packet_rate * 8


        cb1_bp_packet_size = (cb1_payload_size+bp_overhead+7)//8*8
        cb1_bp_data_rate = cb1_bp_packet_size * packet_rate * 8

        self._logger.info('%.32r: CROSSBAR1 output: payload = %i bytes' % (self, cb1_payload_size))
        self._logger.info('%.32r: Backplane links: Packet size = %i bytes, data rate = %0.2f Gbps / %0.2f Gbps (%0.2f%%)' % (self, cb1_bp_packet_size, cb1_bp_data_rate/1e9, bp_data_rate / 1e9, cb1_bp_data_rate/bp_data_rate*100))

        cb2_payload_size = header_size + packet_flags_size + frames_per_packet * (words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4
        cb2_eth_packet_size = (cb2_payload_size + eth_overhead + 7) // 8 * 8
        cb2_eth_data_rate = cb2_eth_packet_size * packet_rate * 8
        cb2_fifo_load = cb2_bins * words_per_bin * frames_per_packet - ( cb2_bins * words_per_bin* cb2_minimum_bin_spacing* frames_per_packet / 16)
        self._logger.info('%.32r: CROSSBAR2 output: payload = %i bytes' % (self, cb2_payload_size))
        self._logger.info('%.32r: CROSSBAR2 peak FIFO load per frame: %i (Max. 16), Words per frame: %i (max %i)' % (self, cb2_fifo_load,cb2_payload_size/frames_per_packet, 512*bp_data_rate/32/200e6))

        if cb2_bypass:
            self._logger.info('%.32r: GPU link (CROSSBAR1 data): UDP Payload = %i bytes, Ethernet packets = %i bytes, data rate = %0.2f Gbps (%0.2f%%)' % (self, cb1_payload_size, cb1_eth_packet_size, cb1_eth_data_rate/1e9, cb1_eth_data_rate/eth_data_rate*100))
        else:
            self._logger.info('%.32r: GPU Link (CROSSBAR2 data): UDP Payload = %i bytes, Ethernet packets = %i bytes, data rate = %0.2f Gbps (%0.2f%%)' % (self, cb2_payload_size, cb2_eth_packet_size, cb2_eth_data_rate/1e9, cb2_eth_data_rate/eth_data_rate*100))

        self.set_corr_reset(0)
        self.set_ant_reset(0)

    def compute_adc_delay_offsets(self, channels=range(16)):
        """
        Measures the eye diagram of the ADC digital data lines and computes
        the permisable offset to ensure reliable data acquisition.

        Returns a delay/offset table (delaytable), flags any stuck bits
        (stuckbits), provides the logic level at the chosen eye sampling point
        (bitposgood)  and in that order. Note that stuck bits should all be
        false, bitposgood should be all 1s
        """
        delaytable = []
        stuckbits = []
        bitposgood = []

        for chan in channels:
            t=self.read_eye_diagram(channels=[chan], offset=[0]*16, noffsets=11)  # Creating an offset / delay table 11 columns 32 rows
            if (t[chan] == 0).sum() and (t[chan] == 255).sum():  # Have found both 0 and 255 in the table - Means no stuck bits
                stuckbits.append(False)
            else:
                stuckbits.append(True)  # Stuck bits detected


            offset = np.where(t[chan].sum(axis=0) == t[chan].sum(axis=0).max())[0][0]  # Choosing the offset by looking at the offset/delay table and picking the column with the highest sum (i.e most 255s)

            bitdelay = []
            changood = []
            for adcbits in range(0,8):
                pulsedata = t[chan][:,offset]
                mask = 1 << adcbits #looking at one adc bit at a time
                chosendelay = int(((mask & pulsedata)*np.arange(32)).sum()/(mask & pulsedata).sum())  #performing a center of mass claculation to pick eye location
                changood.append( (((t[chan][:, offset])[chosendelay]) & mask) >> adcbits)  # Checking what the bit level at the eye center is
                bitdelay.append(chosendelay)
                #self._logger.info( 'Warning: Center of eye diagram on bit %i of channel %i has glitch ' % (adcbits, chan))

            offset = offset - 3  #The difference in offset between a pulse waveform and a ramp
            if offset < 0:  # An untested wrap around conddition (Adam 12/12/2014)
                offset = offset + 11

            delaytable.append([bitdelay, [offset]*8])  # Building the delay table
            bitposgood.append([changood])  # Building the eye diagram good table

        return delaytable, stuckbits, bitposgood


