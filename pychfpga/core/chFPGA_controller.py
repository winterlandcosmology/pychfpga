#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: disable=C0301
# pylint: disable=C0321

"""
This module defines the `chFPGA_controller` class, which provides a Python interface to operate an
IceBoard and its chFPGA firmware.

.. Notes:
..     Created 2011-01-10. See GIT for commit history.
"""

from __future__ import absolute_import


# Python STandard Library packages
import logging
import time
import os
import pickle
from datetime import datetime
from collections import OrderedDict
from functools import wraps
import subprocess
import shlex
import bz2

# PyPi packages
import numpy as np
import yaml
import tornado.gen

# Local imoprts

from .icecore import async, async_return, async_sleep, async_moment
from .icecore.session import load_session as load_yaml

from .icecore_ext.iceboard_ext import IceBoardExtHandler
from .chFPGA_receiver import chFPGA_receiver
from wtl.metrics import Metrics

from pychfpga.common import util

# FPGA subsystems handlers
from . import SPI
from . import I2C
from . import GPIO
from . import SYSMON
from . import FreqCtr
from . import REFCLK
# import MGT

# FPGA Channelizer
from . import ANT

# FPGA Correlator, corner-turn and GPU link objects
from . import CORR # 16-channel correlator (if implemented)
from . import chan_crossbar  # Channelizer Crossbar
from . import shuffle_crossbar  # Shuffle Crossbar
from . import shuffle
from . import GPU


# -- chFPGA --
class chFPGA_config(object):
    """
    Simple namespace that holds chFPGA configuration information stored within its attributes. Is returned
    by `chFPGA_controller.get_config()`.
    """
    def __str__(self):
        return '\n'.join(['%s = %s' % (key, repr(value)) for (key,value) in sorted(vars(self).items())])


# class chFPGAException(Exception):
#     _logger = logging.getLogger('chFPGAException')

#     def __init__(self, message):
#         super(self.__class__, self).__init__(message)
#         self._logger.exception(message)

# def copy_docstring(fn, source_fn):
#     """ Function decorator to use the doctrings from an other funciton """
#     fn.__doc__ = source_fn.__doc__
#     return fn

class chFPGA_controller(IceBoardExtHandler):
    """
    Creates an object that connects to an IceBoard motherboard and its chFPGA firmware and provides
    the methods to configure it and control its operations.

    .. .. inheritance-diagram:: chFPGA_controller
    ..    :parts: 2

    `chFPGA_controller` inherits from the following classes:

    .. image:: ../images/chfpga_controller_class_inheritance_diagram.svg
       :width: 80%

    - `IceBoardExtHandler`  provides the basic Ethernet/UDP-based Memory-mapped Interface (MMI) to
      the FPGA firmware, and provides objects to access the IceBoard and IceCrate hardware (sensors,
      EEPROM etc) directly through the FPGA.
    - `IceBoardPlusHandler` provides SPI-based Memory-mapped Interface to the FPGA using the ARM-FPGA SPI link, which is used
      to configure a basic set of control registers and access some generic non-chFPGA-specfic peripherals such as IRIG-B.
    - `IceBoardHandler` provides acces and allow to execute ARM method wuning on the IceBoard's ARM
      processor as if they were local mathods. This is done over the `Tuber` interface which provide
      access to the ARM processor and the API provided by it to control and monitor the board's
      hardware. `IceBoardHandler` also inherits from `Handler`, which allows an `chFPGA_controller` instance to attach
      itself to a (volatile) hardware map object and draw some of its parameters from it.

    """

    ################################################################################################
    # Memory map for the Ethernet-accessed registers
    ################################################################################################

    #: Note: SPI-accessed registers are separate and use a different address space defined in `IceBoardExtHandler`
    _TOP_BASE_ADDR      = 0x00000  #: Base address of the whome memory map, which is always zero.
    _TOP_SUBSYSTEM_INCREMENT = 0x10000  #: Address increments between top-level systems (address bits 18:16)

    # Top systems
    _SYSTEM_BASE_ADDR      = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 0  #: 0x00000: System peripherals base address.
    _CHAN_BASE_ADDR        = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 1  #: 0x10000: Channelizer base address. The ADCDAQ subsystem is located in the CHAN address space.
    _CROSSBAR1_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 2  #: 0x20000: 1st CROSSBAR (channelizer crossbar) base address
    _GPU_LINK_BASE_ADDR    = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 3  #: 0x30000: GPU Link base address
    _CROSSBAR3_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 4  #: 0x40000: 3rd crossbar (shard with correlator)
    _CORR_BASE_ADDR        = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 4  #: 0x40000: Correlator (shared with 3rd crossbar)
    _BP_SHUFFLE_BASE_ADDR  = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 5  #: 0x50000: Backplane PCB and Backplane QSFP 10Gbps packet transmitter/receivers
    _CROSSBAR2_BASE_ADDR   = _TOP_BASE_ADDR + _TOP_SUBSYSTEM_INCREMENT * 6  #: 0x60000: 2nd CROSSBAR base address

    _SYSTEM_ADDR_INCREMENT         = 0x01000  #: Address increment between each system peripheral (addressed by bits 15:12 -> 16 possible submodules)
    _CHAN_ADDR_INCREMENT           = 0x01000  #: Address increment between each channelizer (addressed by bits 15:12 -> 16 possible submodules)
    _CROSSBAR_ADDR_INCREMENT       = 0x00800  #: Address increment between subsystems in 1st, 2nd and 3rd crossbars (addressed by bits 15:11 -> 32 possible submodules)
    _GPU_LINK_ADDR_INCREMENT       = 0x00800  #: Address increment between each subsystem of the GPU links (addressed by bits 15:11 -> 32 possible submodules)
    _CORR_ADDR_INCREMENT           = 0x01000  #: Address increment between each correlator
    _BP_SHUFFLE_ADDR_INCREMENT     = 0x00800  #: Address increment between each shuffle submodule (addressed by bits 15:11 -> 32 possible submodules)

    _CHAN_SUBMODULE_ADDR_INCREMENT = 0x00200  #: Address increment between each submodule within a channelizer (ADCDAQ, FUNCGEN, FFT, SCALER etc.)


    # SYSTEM Peripherals Submodules addresses
    _SYSTEM_GPIO_BASE_ADDR     = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 0  #: 0x00000: Address of the SYSTEM.GPIO submodule
    _SYSTEM_SYSMON_BASE_ADDR   = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 1  #: 0x01000: Address of the SYSTEM.SYSMON submodule
    _SYSTEM_FREQ_CTR_BASE_ADDR = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 2  #: 0x02000: Address of the SYSTEM.FREQ_CTR submodule
    _SYSTEM_SPI_BASE_ADDR      = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 3  #: 0x03000: Address of the SYSTEM.SPI submodule
    _SYSTEM_REFCLK_BASE_ADDR   = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 4  #: 0x04000: Address of the SYSTEM.REFCLK submodule
    # SYSTEM_I2C_BASE_ADDR      = _TOP_BASE_ADDR + _SYSTEM_ADDR_INCREMENT * 5 #: 0x05000: Address of the SYSTEM.I2C submodule

    # _GPIO_COOKIE_REG = 0x00 # Register address of the firmware cookie


    ################################################################################################
    # Supported platform information
    ################################################################################################

    _PLATFORM_ID_ML605 = 0  #: ID number for the Virtex-6-based Xilinx ML606 Evaluation board
    _PLATFORM_ID_KC705 = 1  #: ID number for the Kintex-7-based Xilinx KC705 Evaluation board
    _PLATFORM_ID_MGK7MB_REV0 = 2  #: ID number for the McGill MGK7MB Rev 0 motherboard (a.k.a Iceboard Rev 0, pre-production prototype)
    _PLATFORM_ID_MGK7MB_REV2 = 3  #: ID number for the McGill MGK7MB Rev 2 motherboard (a.k.a Iceboard Rev 2). Works for All subsequent revs.

    #: Map of all supported platform indexed by the `PLATFORM_ID` returned by the FPGA
    _PLATFORM_ID_LIST = {
        # ID: ( Board name, class to instantiate)
        _PLATFORM_ID_ML605: ('Virtex 6 (XC6V240T-1 FFG1156) on Xilinx ML605 Evaluation board', None),
        _PLATFORM_ID_KC705: ('Kintex 7 (XC7K325T-2 FFG900C) on Xilinx KC705 Evaluation board', None),
        _PLATFORM_ID_MGK7MB_REV0: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev0', None),
        _PLATFORM_ID_MGK7MB_REV2: ('Kintex 7 (XC7K420T-2 FFG901) on McGill MGK7MB / ICEBoard Rev2', None),
    }

    def __init__(self,
        parent_getter=None,
        hostname=None,
        serial=None,
        part_number=None,
        crate=None,
        slot=None,
        mezzanine={},
        tuber_objname='IceBoard'):
        """
        Creates an empty IceBoard/chFPGA handler object, but do not interact with the board yet.

        Parameters:
            parent_getter (func): Function that returns the dynamically return the parent object from which the following parameters will be fetched. Is ``None`` if there is no parent.
            hostname (str): hostname or IP address of the ICEBoard ARM processor (mandatory)
            serial (str): Serial number of the board. Can be provided by the ARM.
            part_number (str): Part number of the IceBoard. Can be obtained from the ARM.
            crate (IceCrateHandler): = object that handle the backplane on which the board is connected. ``None`` if the board is not connected to a backplane.
            slot (int): Slot number in which the board is installed ona backplane. None if there is no backplane.
            mezzanine (dict): Map {mezzanine_number: Mezzanine Handler, ...} describing the installed mezzanines. Can be obtained from the ARM.
            tuber_objname (str): name of the set of software functions that will be provided by the ARM processor through the Tuber interface.

        The `__init__` function stores the parameters as instance attributes
        of the same name. However, if a `parent_getter` function is provided
        and returns a parent object, the value of these attributes will
        instead be fetched dynamically from the parent object instead of using
        local values (see :class:`Handler`). This allows chfpga_controller to
        keep a dynamic connection with a volatile database object (in this
        case, a database-based hardware map entry)  derive its properties from
        it.


        Note: `__init__` *only* create an empty `chFPGA_controller` object and hold basic
            configuration information but does not attempt to interact with the FPGA. Interaction
            with the FPGA starts with `open`. This means that `chFPGA_controller` objects can be
            created for board that do not exist are are not powered up yet. This is useful when
            arrays of boards are loaded from an unfiltered hardware map.
        """
        super(chFPGA_controller, self).__init__(
            parent_getter=parent_getter,
            hostname=hostname,
            serial=serial,
            part_number=part_number,
            crate=crate,
            slot=slot,
            mezzanine=mezzanine,
            tuber_objname=tuber_objname)

        # Initialize basic instance attributes, but don;t do anything that involve talking to the IceBoard.

        self._logger = logging.getLogger(__name__)
        self._logger.debug("%r: Creating chFPGA_controller object" % (self))

        self._sampling_frequency = None  # Set in init()
        self._reference_frequency = None # set in init()
        self.FRAME_PERIOD = None
        self._FMC_present = []  # indicates if the FMC board is present. If not, the modules will act accordingly.
        # self._adc_board = []
        self._last_init_time = None
        self.recv = None

    @async
    def open(self, init=1, verbose=0, udp_retries=10, **kwargs):
        """
        Opens communication with the FPGA, retreives the firmware configuration information and
        create the Python objects needed to operate the firmware. If `init` =1, the :meth:`init`
        method will be called to initialize the FPGA. Otherwise, this is a read-only operation, i.e.
        the state of the FPGA is unchanged.

        Parameters:

            init (int): initialization level: 1: read config and initialize the FPGA with the `init()` method; 0: only read
                        the FPGA config; -1: Don<t read the FPGA and do not create the Python
                        objects.
            verbose (int): verbosity level, which is passed to the `init()` method.
            udp_retries: Number of retries that are made while sendinc commands to the FPGA before raising an exception.
            kwargs: All remaining parameters are passed to `init()` method if the `init` parameter is 1.
        """

        yield super(chFPGA_controller, self).open.async(udp_retries=udp_retries)  # Open UDP communication link
        self.logger.debug('%r: Instantiating chFPGA firmware handlers objects' % (self))

        # self.read = self.mmi.read
        # self.write = self.mmi.write

        if init < 0: # If init<0, we do not perform any communication with the FPGA, so we don't read the firmware configuration
            self._logger.warn('%r: Upon user request (init < 0), communication with the FPGA are inhibited. Initialization sequence stops here. Use this for debug only.' % self)
            return
        self._logger.info('%r:    ---> Hello! This is chFPGA! <---' % self)

        try:  # catch initialization errors so we can free the socket for future instantiation

            # Create handware handling objects
            #  NOTE: Does not initialize them yet because some modules are interdependent - we need to wait until all of them are instantiated.
            #  NOTE: The instantiation does not initiate communicattion with the hardware yet. this is done in the INIT phase.

            # ---------------------------------------------------------------------
            # -- Create basic FPGA ressource handlers objects
            # ---------------------------------------------------------------------

            self._logger.debug('%r: === Instantiating GPIO' % self)
            yield async_moment
            self.GPIO = GPIO.GPIO_base(self, self._SYSTEM_GPIO_BASE_ADDR)
            # get system constants from the FPGA

            self._logger.debug('%r: === Getting board info information' % self)
            yield async_moment
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

            # Get corner-turn engine configuration info
            self.NUMBER_OF_CROSSBAR_INPUTS = self.GPIO.NUMBER_OF_CROSSBAR_INPUTS
            self.NUMBER_OF_CROSSBAR_OUTPUTS = self.GPIO.NUMBER_OF_CROSSBAR_OUTPUTS
            self.NUMBER_OF_BP_SHUFFLE_LANES = self.GPIO.NUMBER_OF_BP_SHUFFLE_LANES

            # Get GPU link configuration info
            self.NUMBER_OF_GPU_LINKS = self.GPIO.NUMBER_OF_GPU_LINKS



            # Get (optional) embedded firmware correlator configuration info and their properties
            self.NUMBER_OF_CORRELATORS_MAX = self.GPIO.NUMBER_OF_CORRELATORS
            self.NUMBER_OF_CORRELATORS = self.GPIO.NUMBER_OF_CORRELATORS
            self.LIST_OF_IMPLEMENTED_CORRELATORS = range(self.NUMBER_OF_CORRELATORS)
            self.NUMBER_OF_ANTENNAS_TO_CORRELATE = self.GPIO.NUMBER_OF_CHANNELIZERS_TO_CORRELATE

            # ANT_BASE_PORT = 1
            # CORR_BASE_PORT = ANT_BASE_PORT + self.NUMBER_OF_ANTENNAS
            # GPU_BASE_PORT = CORR_BASE_PORT + self.NUMBER_OF_CORRELATORS

            # self.ANT_PORT =  range(ANT_BASE_PORT, ANT_BASE_PORT + self.NUMBER_OF_ANTENNAS) # Antennas are ports 0-7
            # self.CORR_PORT = range(CORR_BASE_PORT, CORR_BASE_PORT +  self.NUMBER_OF_CORRELATORS)
            # self.GPU_PORT = range(GPU_BASE_PORT, GPU_BASE_PORT +  1)
            self.default_channels = range(self.NUMBER_OF_ANTENNAS)
            #self.LIST_OF_ANTENNAS_WITH_FFT = [i for i in range(8) if bool(self.GPIO.IMPLEMENT_FFT & 2**i)]


            self._logger.debug('%r: Hardware platform: %s' % (self, self._PLATFORM_ID_LIST[self.PLATFORM_ID][0]))
            self._logger.debug('%r: Firmware timestamp: %s' % (self, self.get_version()))
            self._logger.debug('%r: Number of channelizers: %i' %  (self, self.NUMBER_OF_ANTENNAS))
            self._logger.debug('%r: Number of channelizers with FFT: %i (antennas %s)' % (self, len(self.LIST_OF_ANTENNAS_WITH_FFT), str(self.LIST_OF_ANTENNAS_WITH_FFT)))
            self._logger.debug('%r: Crossbar configuration: %i inputs x %i outputs' % (self, self.NUMBER_OF_CROSSBAR_INPUTS, self.NUMBER_OF_CROSSBAR_OUTPUTS))
            self._logger.debug('%r: Number of correlators: %i (correlators %s)' % (self, len(self.LIST_OF_IMPLEMENTED_CORRELATORS),str(self.LIST_OF_IMPLEMENTED_CORRELATORS)))
            self._logger.debug('%r: Number of channelizers supported by the correlators: %i ' % (self, self.NUMBER_OF_ANTENNAS_TO_CORRELATE))

            yield async_moment
            self._logger.debug('%r: === Instantiating FPGA ressources' % self)

            self._logger.debug('%r: === Instantiating SYSMON' % self)
            self.SYSMON = SYSMON.SYSMON_base(self, self._SYSTEM_SYSMON_BASE_ADDR)

            self._logger.debug('%r: === Instantiating SPI' % self)
            self.SPI = SPI.SPI_base(self, self._SYSTEM_SPI_BASE_ADDR)

            self._logger.debug('%r: === Instantiating FreqCtr' % self)
            self.FreqCtr = FreqCtr.FreqCtr_base(self, self._SYSTEM_FREQ_CTR_BASE_ADDR)

            self._logger.debug('%r: === Instantiating REFCLK' % self)
            self.REFCLK = REFCLK.REFCLK_base(self, self._SYSTEM_REFCLK_BASE_ADDR)

            self._logger.debug('%r: === Instantiating CHAN' % self)
            self.ANT = ANT.ANT_base(self, self._CHAN_BASE_ADDR, self._CHAN_ADDR_INCREMENT, self._CHAN_SUBMODULE_ADDR_INCREMENT) # Antenna processors (ADCDAQ, FUNCGEN,  FFT, SCALER) for each input
            self.ANT_FMC_NUMBER = [i//8 for i in range(self.NUMBER_OF_ANTENNAS)]

            yield async_moment
            self._logger.debug('%r: === Instantiating 1st CROSSBAR' % self)
            self.CROSSBAR = chan_crossbar.ChanCrossbar(self, self._CROSSBAR1_BASE_ADDR, self._CROSSBAR_ADDR_INCREMENT) # CROSSBAR block

            if self.NUMBER_OF_BP_SHUFFLE_LANES:
                self._logger.debug('%r: === Instantiating Backplane shuffle subsystem' % self)
                self.BP_SHUFFLE = shuffle.Shuffle(self, self._BP_SHUFFLE_BASE_ADDR, self._BP_SHUFFLE_ADDR_INCREMENT)
            else:
                self.BP_SHUFFLE = None

            if self.NUMBER_OF_BP_SHUFFLE_LANES and self.NUMBER_OF_GPU_LINKS:
                self._logger.debug('%r: === Instantiating 2nd CROSSBAR' % self)
                self.CROSSBAR2 = shuffle_crossbar.ShuffleCrossbar(self, self._CROSSBAR2_BASE_ADDR, self._CROSSBAR_ADDR_INCREMENT, crossbar_level=2, number_of_bin_sel=2) # CROSSBAR block

                self._logger.debug('%r: === Instantiating 3rd CROSSBAR' % self)
                self.CROSSBAR3 = shuffle_crossbar.ShuffleCrossbar(self, self._CROSSBAR3_BASE_ADDR, self._CROSSBAR_ADDR_INCREMENT, crossbar_level=3, number_of_bin_sel=8) # CROSSBAR block
            else:
                self.CROSSBAR2= None
                self.CROSSBAR3 = None

            if self.NUMBER_OF_CORRELATORS:
                self._logger.debug('%r: === Instantiating CORR' % self)
                self.CORR = CORR.CORR(self, self._CORR_BASE_ADDR, self._CORR_ADDR_INCREMENT) # Correlator (XMUL, ACC) for each correlator
            else:
                self.CORR = None

            if self.NUMBER_OF_GPU_LINKS:
                self._logger.debug('%r: === Instantiating GPU LINKS' % self)
                self.GPU = GPU.GPU_base(self, self._GPU_LINK_BASE_ADDR, self._GPU_LINK_ADDR_INCREMENT)
            else:
                self.GPU = None

            self._logger.debug('%r: This motherboard has %i FMC slots' % (self, self.NUMBER_OF_FMC_SLOTS))

            # ---------------------------------------------------------------------
            # -- Create ADC board hardware ressource handlers objects
            # ---------------------------------------------------------------------

            yield async_moment
            self._logger.debug('%r: === Analyzing available FMC Mezzanines' % self)
            # self._adc_board = [
            #     self.mezzanine.get(1, None),
            #     self.mezzanine.get(2, None)]

            self._FMC_present = [False] * self._NUMBER_OF_FMC_SLOTS
            for fmc_number in range(self._NUMBER_OF_FMC_SLOTS):
                if fmc_number+1 in self.mezzanine.keys():
                    self._FMC_present[fmc_number] = True
                    self._logger.debug('%r:   An MGADC08 ADC Board is present on FMC slot %i' % (self, fmc_number))
                else:
                    self._logger.warning('%r:   An MGADC08 ADC Board is *not* present on FMC slot %i' % (self, fmc_number))

            # Determine if the FMC board corresponding to each channelizer is present
            # self.ANT_FMC_IS_PRESENT = [self._adc_board[self.ANT_FMC_NUMBER[i]].is_present() for i in range(self.NUMBER_OF_ANTENNAS)]
            self.ANT_FMC_IS_PRESENT = [False] * self.NUMBER_OF_ANTENNAS
            for (ant_number, fmc_number) in enumerate(self.ANT_FMC_NUMBER):
                if fmc_number + 1 in self.mezzanine.keys():
                    self.ANT_FMC_IS_PRESENT[ant_number] = True

            self.hw.set_led('GP_LED1', 1) # Indicate that the Iceboard is ready
            self._data_socket = None

        except Exception as e:
            self.logger.error('****Exception during open!****** =  %r' % e)

            self.close()
            # raise chFPGAException('An exception has occured during module instantiation. Sockets will be closed. The exception is %s' % repr(e))

            raise
            # Initialize subsystems. This has to be done only once all subsystems are created because some subsystems depend on each other.
        if init > 0:
            try:
                yield self.init.async(**kwargs)
            except Exception as e:
                self.logger.error('****Exception during init!****** =  %r' % e)
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
        for mezz in self.mezzanine.values():
            if hasattr(mezz, 'close'):
                mezz.close()
        super(chFPGA_controller, self).close()  # Make sure we close underlying sytems (sockets, etc)


    @async
    def init(self,
             sampling_frequency=800e6,
             reference_frequency=10e6,
             adc_delay_table=None,
             data_width=4,
             group_frames=4,
             enable_gpu_link=1,
             create_receiver= False,
             verbose=0,
             **kwargs):
        """
        Initialize the FPGA firmware AND the Python objects to a known state.

        Parameters:

             sampling_frequency (float): Sampling frequency in Hz to set on the ADC Mezzanine boards
                 (default 800 MHz)

             reference_frequency (float): Frequency in Hz of the Iceboard's reference clock (default
                 is 10 MHz)

             adc_delay_table (dict): initial setting of the ADC delays. see `set_adc_delays`

             data_width (int): 4 or 8. Indicate of the channelizer output is in (4+4)bit or (8+8
                 bit) mode

             group_frames (int): Number of frames per packets used by the corner-turn engine

             enable_gpu_link (bool): 1

             create_receiver (bool): False, obsolete

             verbose (int): verbose level

        Returns:
            None

        Note:

            Not all of the FPGA registers are rewritten durint `init()`, so it might be required to
            reprogramthe fpga to come back to a known state if manual changes were made.
        """

        self._sampling_frequency = sampling_frequency
        self._reference_frequency = reference_frequency
        self.FRAME_PERIOD = float(self.FRAME_LENGTH)/self._sampling_frequency
        self.FRAME_RATE = 1 / self.FRAME_PERIOD

        self._logger.info('%r: --- Initializing FPGA subsystems' % self)

        self._logger.debug('%r: --- Initializing GPIO' % self)
        self.GPIO.init()  # This stops the antenna procesors from sending data. Neeeded if the FPGA is flooding the buffers which prevent subsequent reads to come through
        self.GPIO.BUCK_PHASE = 0xfedcba9876543210  # debug
        #self.fpga.flush_data_socket() # Now the the data stops coming, flush the buffers
        self.mmi.flush()
        if verbose >= 2:
            self.GPIO.status()


         # Module depend on the FMC_present flag after this point

        self._logger.debug('%r: --- Initializing REFCLK' % self)
        yield async_moment
        self.REFCLK.init()
        # self.REFCLK.status()

        #Only do for ML605, not KC705 board
        self._logger.debug('%r: --- Initializing SYSMON' % self)
        yield async_moment
        self.SYSMON.init()
        # self.SYSMON.status()

        self._logger.debug('%r: --- Initializing SPI' % self)
        yield async_moment
        self.SPI.init()
        # self.SPI.status()

        self._logger.debug('%r: --- Initializing FMC slots' % self)

        # Reduce the power load before we turn on the mezzanines
        yield async_moment
        self.set_adc_mask(0) # null the ADC data before it gets to the channelizers to reduce power consumption
        self.set_ant_reset(1)
        self.set_corr_reset(1)
        for mezz in self.mezzanine.values():
            mezz.set_power(False)
        yield async_sleep(0.2)  # *** make async

        for mezz_number in (1, 2):
            if mezz_number in self.mezzanine:
                mezz = self.mezzanine[mezz_number]
                self._logger.debug('%r:   Powering down FMC%i' % (self, mezz_number - 1))
                yield self.hw.set_mezzanine_power.async(mezz_number-1, False)
                # mezz.set_power(False)  # For some reason, prevents the board from rebooting (!)
                yield async_sleep(0.2)  # *** make async
                self._logger.debug('%r:   Powering up FMC%i' % (self, mezz_number - 1))
                yield self.hw.set_mezzanine_power.async(mezz_number-1, True)
                # mezz.set_power(True)
                yield async_sleep(0.2) # Give it some time for the power to stabilize
                # We need to initialize the ADC board befor we initialize ANT (and its data acquisition) because the delay blocks need a clock
                self._logger.debug('%r:   Initializing FMC%i' % (self, mezz_number - 1))
                mezz.init(sampling_frequency=sampling_frequency, reference_frequency=reference_frequency)
                # mezz.status()
            else:
                self._logger.debug('%r:    Skipping FMC%i initialization since no board is present in that slot' % (self, mezz_number - 1))


        self._logger.debug('%r:   Taking channelizers out of reset after FMC enabling' % (self))
        self.set_ant_reset(1)

        self._logger.debug('%r:   Sending sync()' % (self))
        yield async_moment
        self.sync() # might be needed  to make sure that the clock is running to set delays


#        self._FMC_present = self._adc_board[0].is_present()
        # self._logger.info('%r: --- Initializing FPGA subsystems' % self)
        self._logger.info('%r: === Initializing Channelizers' % self )
        self.ANT.init(delay_table=adc_delay_table, fmc_present=self.ANT_FMC_IS_PRESENT)
        # self.ANT.status()

        self._logger.info('%r: === Initializing Corner-Turn engine' % self)
        self._logger.debug('%r: === Initializing 1st Crossbar' % self )
        yield async_moment
        if self.NUMBER_OF_CROSSBAR_OUTPUTS > 0:
            self._logger.debug('%r:  - 1st CROSSBAR' % self)
            self.CROSSBAR.init()
            # self.CROSSBAR.status()
        else:
            self._logger.warning("%r: There is no 1st CROSSBAR module in this firmware build (so there can't be data streamed to the correlators or GPU links!)" % self);

        if self.BP_SHUFFLE:
            yield async_moment
            self._logger.debug('%r: === Initializing Backplane Shuffle' % self)
            self.BP_SHUFFLE.init()

        self._logger.debug('%r: === Initializing 2nd Crossbar' % self)
        if self.CROSSBAR2:
            yield async_moment
            self.CROSSBAR2.init()
        else:
            self._logger.warning("%r: There is no 2nd CROSSBAR module in this firmware build" % self);

        self._logger.debug('%r: === Initializing 3rd Crossbar' % self)
        if self.CROSSBAR3:  # *** Fixme
            yield async_moment
            self.CROSSBAR3.init()
        else:
            self._logger.warning("%r: There is no 3rd CROSSBAR module in this firmware build" % self);


        if self.CORR:
            self._logger.info('%r: === Initializing FPGA-based correlator (X-Engine)' % self)
            yield async_moment
            self._logger.debug('%r:  - CORR' % self)
            self.CORR.init()
        else:
            self._logger.debug('%r: There are no FPGA correlators in this firmware build' % self);

        self.set_data_width(data_width)  #sets the data width of both the SCALER and CROSSBAR
        self._logger.debug('%r: Data width set to (Re+Im) = (%i+%i) bits' % (self, self.get_data_width(), self.get_data_width()))

        self.CROSSBAR.set_frames_per_packet(group_frames)
        self._logger.debug('%r: The 1st crossbar will pack %i frames per packet' % (self, group_frames))

        yield async_moment

        if self.GPU:
            self._logger.info('%r: === Initializing GPU links' % self)
            self.GPU.init()
            self.GPU.set_enable(enable_gpu_link)
            self._logger.debug('%r: GPU link is currently %s' % (self, ['Disabled','Enabled'][bool(enable_gpu_link)]))

        # MGT is disabled
        #self._logger.debug('  - MGT_PLL')
        #self.MGT_PLL.init(fref=fref)
        #self._logger.debug('  - MGT')
        #self.MGT.init() # MGT_PLL must be initialized first

        self._logger.debug("%r: Done with initializations." % self)


        yield async_moment
        self.set_ant_reset(0) # disable antenna reset

        self._last_init_time = time.time()

        # Create a data receiver
        if create_receiver:
            self.get_data_receiver()

    @async
    def get_config(self, basic=False):
        """
        Return configuration for this FPGA.

        TODO:
            - use yield on slow statements to make this really parallel
        """
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
        config.system_frame_period = self.FRAME_PERIOD
        config.motherboard_serial = self.GPIO.FPGA_SERIAL_NUMBER


        if not basic:
            mezz1 = self.mezzanine.get(1, None)
            config.adc_board_is_present = bool(mezz1)

            if mezz1:
                config.adc_board_temperature = mezz1.AmbTemp.temperature
                config.adc_board_adc_chip_temperature = [adc.get_temperature() for adc in mezz1.ADC]
            config.adc_serial = [self.mezzanine[mezz_number].serial if mezz_number in self.mezzanine else None for mezz_number in (1, 2)] #mezz1._board_info['Serial #']

            config.antenna_data_source = self.get_data_source()
            config.antenna_fft_bypass = self.get_FFT_bypass()
            config.antenna_fft_shift_schedule = self.get_FFT_shift()
            config.antenna_scaler_gain = self.get_gains()
            config.antenna_adc_data_acquisition_delay_tables  = self.ANT.get_adc_delays()
            config.FPGA_board_frequency = self.FreqCtr.read_frequency('CLK200', gate_time=0.05)
            config.CTRL_clock_frequency = self.FreqCtr.read_frequency('CTRL_CLK', gate_time=0.05)
            config.ant_clock = self.FreqCtr.read_frequency('ANT_CLK', gate_time=0.05)
            if self.CORR:
                config.correlator_clock = self.FreqCtr.read_frequency('CORR_CLK', gate_time=0.05)
                # config.correlator_capture_period_in_frames = [corr.ACC.CAPTURE_PERIOD for corr in self.CORR]
                # config.correlator_integration_period_in_frames = [corr.ACC.INTEGRATION_PERIOD for corr in self.CORR]
            config.fmc_ref_clock = self.FreqCtr.read_frequency('FMCA_REFCLK', gate_time=0.05)
            config.mgt_ref_clock = self.FreqCtr.read_frequency('GPU_REFCLK', gate_time=0.05)
            config.mgt_word_clock = self.FreqCtr.read_frequency('GPU_TXCLK', gate_time=0.05)
            config.adc_clocks = [self.FreqCtr.read_frequency(('ADC_CLK'+str(i)), gate_time=0.05) for i in range(8)]  # todo: fix ADC range
        	# config.motherboard_serial = self.GPIO.FPGA_SERIAL_NUMBER
            # Add FFT shift, scaler gain, corr integration/capture period etc.
            # config.freq_flags = self.freq_flags  # JFC: what is that?
        async_return(config)


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
            self._logger.debug("%r: Syncing board" % self)
        self.set_adc_mask(0) # null the ADC data before it gets to the channelizers to reduce power consumption
        if local:
            self.REFCLK.local_sync()
        else:
            self.REFCLK.remote_sync()
        self.set_adc_mask(0xff) # restore full ADC data

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
        if isinstance(channels, int):
            channels = [channels]
        self.default_channels = channels

    def get_default_channels(self):
        """
            Returns the default channels that are used in other functions when not specifically specified.
        """
        return self.default_channels


    def set_channelizer(self,
                        adc_mode=None, adcdaq_mode=None,
                        data_source=None, function=None, a=1, b=0, freq_test_bins=None,
                        fft_bypass=None, fft_shift=None,
                        scaler_bypass=None, gain=None, postscaler=None, offset_binary_encoding=None,
                        local_sync=True,
                        channels=None):
        """
            Single command used to set all channelizer settings. The data processing chain is:
                  ADC --> ADCDAQ --> FUNCGEN --> --> FFT --> SCALER
        """
        # Set the ADC chip operational mode (data, ramp, pulse)
        if adc_mode is not None:
            self.set_adc_mode(mode=adc_mode, sync=False)

        # Set the FPGA's ADC data acquisition module operational mode
        if adcdaq_mode is not None:
            self.set_adcdaq_mode(mode=adcdaq_mode, channels=channels)

        # Set the date source and the function generator that feed the FFT
        if data_source is not None:
            self.set_data_source(data_source, channels=channels)  # does a channelizer reset

        if function is not None:
            if function == 'freq_test': #Configure funcgen so the visibility data has a unique real number for 108 freq bins. The other freq bins are zeros
                N = min(len(freq_test_bins), 108) # N has to be less that 108 for this to work
                if N==0: # Send same number (1+0j) for all frequencies
                    v = (9*np.ones(2048, dtype=np.uint8))<<4 # with the offset encoding, 9s here result in 1s in the complex visibility data
                    v[1::2] = (8*np.ones(1024, dtype=np.uint8))<<4 # with the offset encoding, 8s here result in 0s in the complex visibility data
                else:
                    freq_pattern_real = np.array([ 1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  1,  2,  2,
                                                    2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  2,  3,  3,  3,  3,  3,
                                                    3,  3,  3,  3,  3,  3,  3,  3,  4,  4,  4,  4,  4,  4,  4,  4,  4,
                                                    4,  4,  5,  5,  5,  5,  5,  5,  5,  5,  5,  6,  6,  6,  6,  6,  6,
                                                    6,  6,  7,  7,  7,  7,  7,  7,  7,  8,  8,  8,  8,  8,  8,  9,  9,
                                                    9,  9,  9,  9, 10, 10, 10, 10, 11, 11, 11, 11, 11, 12, 12, 12, 12,
                                                   13, 13, 13, 14, 14, 15], dtype=np.uint8)<<4
                    freq_pattern_imag = np.array([ 1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,  2,  3,
                                                    4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15,  3,  4,  5,  6,  7,
                                                    8,  9, 10, 11, 12, 13, 14, 15,  4,  5,  6,  8,  9, 10, 11, 12, 13,
                                                   14, 15,  6,  7,  8,  9, 11, 12, 13, 14, 15,  6,  8,  9, 10, 11, 12,
                                                   14, 15,  7,  8, 10, 12, 13, 14, 15,  8, 10, 12, 13, 14, 15,  9, 10,
                                                   11, 12, 14, 15, 12, 13, 14, 15, 11, 12, 13, 14, 15, 12, 13, 14, 15,
                                                   13, 14, 15, 14, 15, 15], dtype=np.uint8)<<4
                    v = np.zeros(2048, dtype=np.uint8)
                    v_real = np.zeros(1024, dtype=np.uint8)
                    v_imag = np.zeros(1024, dtype=np.uint8)
                    v_real[freq_test_bins] = freq_pattern_real[:N]
                    v_imag[freq_test_bins] = freq_pattern_imag[:N]
                    v[::2] = v_real
                    v[1::2] = v_imag
                self.set_funcgen_function('arb', channels=channels, data=v) # If FFT and scaler are bypassed, then the visibility data has a unique real number for the 108 freq bins in freq_test_bins. The rest are zeros.
            else:
                self.set_funcgen_function(function=function, channels=channels)#self.set_funcgen_function(function=function, a=a, b=b, channels=channels)

        # Set FFT bypass and shift schedule
        if fft_bypass is not None:
            self.set_fft_bypass(bypass_mode=fft_bypass, channels=channels)

        if fft_shift is not None:
            self.set_fft_shift(fft_shift, channels=channels)

        # Set Scaler parameters
        if scaler_bypass is not None:
            self.set_scaler_bypass(bypass_mode=scaler_bypass, channels=channels)

        if gain is not None:
            self.set_gain(gain=gain, postscaler=postscaler, channels=channels)

        if offset_binary_encoding is not None:
            self.set_offset_binary_encoding(offset=offset_binary_encoding, channels=channels, sync=False)

        if local_sync:
            self.sync()

    def set_channelizer_outputs(self, data):
        """
        Set the data outputted by the channelizers. FFT and SCALER and bypassed.
        data(chan, bin) = complex value (4+4) bits
        """

        d = np.zeros((16,2048), np.int8)
        d[:, 0::2] = data.real
        d[:, 1::2] = data.imag
        d <<= 4

        self.set_channelizer(data_source='funcgen', function='AB', a=0, b=0, fft_bypass=1, scaler_bypass=1, offset_binary_encoding=0)
        for ch in range(16):
            self.set_funcgen_function('arb', channels=[ch], data=d[ch])
        return d

    # set_data_path = set_channelizer # for legacy compatibility

    def set_data_source(self, source=None,  channels=None):
        """
            Sets the data source on specified channels (or default channels if the channels are not specified).
        """
        data_sources = self.ANT[0].FUNCGEN.DATA_SOURCE_NAMES
        if (source is None) or (source.lower() not in data_sources):
            raise ValueError("Invalid data source name '%s'. Valid data sources are %s:" % (source, ', '.join(data_sources.keys())))

        if channels is None:
            channels = self.default_channels


        self.set_ant_reset(1) # Reset is needed to resyncronize the system with the new data
        for ch in channels:
            ant = self.ANT[ch]
            ant.FUNCGEN.set_data_source(source.lower())
        self.set_ant_reset(0) # Reset is needed to resyncronize the system with the new data
        #self.sync() # SYNCs the ADC, and resets (again) the antenna processor to align the data with the ADC


    def get_data_source(self):
        """
            Returns a list of data source for all channels.
        """
        return [ant.FUNCGEN.get_data_source() for ant in self.ANT.values()]


    def set_funcgen_function(self, function=None, channels=None, **kwargs):
        """
        Sets the waveform generated by the function generator on specified channels (or default channels if the channels are not specified).
        This may cause one frame to partially contain the new waveform.
        """
        if (function is None) or (function.lower() not in self.ANT[0].FUNCGEN.FUNCTION_NAMES):
            raise ValueError("Invalid function generator function '%s'. Valid functions are %s:" % (function, ', '.join(self.ANT[0].FUNCGEN.FUNCTION_NAMES.keys())))

        if channels is None:
            channels = self.default_channels

        for ch in channels:
            ant = self.ANT[ch]
            ant.FUNCGEN.set_function(function.lower(), **kwargs)

    def get_adc_board(self, channel):
        """
        Returns the ADC board object that is associated with the specified antenna channel.
        If channel is a list, returns a list of unique board objects associated with the specified channels.
        """

        if isinstance(channel, int):
            #channel = [channel]
            mezz_number = (channel // 8) + 1
            return self.mezzanine.get(mezz_number, None)
        else:
            board_list=set()
            for ch in channel:
                mezz_number = (ch // 8) + 1
                if mezz_number in self.mezzanine.keys():
                    board_list.add(self.mezzanine[mezz_number])
                else:
                    self._logger.warning('%r: ADC Mezzanine board for channel %i is not present. Ignoring this board' % (self, ch))
            return list(board_list)

    ADC_MODE_NAMES = {
        # name, mode number, period (in 4-bytes words)
        'data': (0, 64),  # ADC sends analog data
        'ramp': (1, 64),  # ADC sends ramp from 0 to 255
        'pulse': (2, 11),  # ADC sends ten 0x00 followed by one 0xff
        }

    # ADC_MODE_NAMES_REVERSED = util.reverse_dict(ADC_MODE_NAMES)

    def set_adc_mode(self, mode='data', channels=None, sync=True):
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
        if isinstance(mode, list):
            if channels:
                raise RuntimeError('channels cannot be specified when multiple modes are provided')
            for fmc_number, m in enumerate(mode):
                self.mezzanine[fmc_number+1].ADC.set_test_mode(test_mode=self.ADC_MODE_NAMES[m.lower()][0])
            return

        if mode.lower() not in self.ADC_MODE_NAMES:
            raise ValueError("Invalid ADC mode '%s'. Valid modes are %s" % (mode, ', '.join(self.ADC_MODE_NAMES.keys())))
        (mode_value, capture_period) = self.ADC_MODE_NAMES[mode.lower()]

        if channels is None:
            channels = self.default_channels

        # Set the mode on all affected ADC boards
        adc_boards = self.get_adc_board(channels)
        for adc_board in adc_boards:
                adc_board.ADC.set_test_mode(test_mode=mode_value)

        # Set the capture period for all specified channels
        for ch in channels:
            ant = self.ANT[ch]
            ant.ADCDAQ.CAPTURE2_PERIOD = capture_period # set the period so we are ready to capture data correctly after the SYNC resets the CAPTURE logic

        # self.current_ADC_mode = mode_value
        if sync:
            self.sync()  # make sure the ADC mode is set and that capture  restarts properly with the right period

    # set_ADC_mode = set_adc_mode  # For legacy code compatibility

    def get_adc_mode(self, channels=None):
        """
        Gets the current operating mode of all the ADCs as a string.

        If all ADCs operate in the same mode, a single mode string is returned. Otherwise a list of mode strings is returned.
        """

        if channels is None:
            channels = self.default_channels

        mode_names = []

        # get the ADC mode number for every ADC board
        adc_boards = self.get_adc_board(channels)
        for mezz in adc_boards:
            mode_value = mezz.ADC.get_test_mode()
            mode_name = [name for (name, value) in self.ADC_MODE_NAMES.items() if value[0] == mode_value][0]
            mode_names.append(mode_name)

        if len(set(mode_names)) == 1: # eliminate all duplicates. We should be left with only one mode number.
            return mode_names[0]
        else:
            return mode_names
        # if len(mode_value) != 1:
        #     raise RuntimeError('The ADC chips on the ADC boards are not ALL in the same mode')

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

    def get_data_receiver(self, verbose=1):
        if self.recv:
            return self.recv
        chFPGA_config = self.get_config(basic=True)  # get only the info needed to start the receiver
        self.recv = chFPGA_receiver(chFPGA_config, verbose=verbose)
        self.logger.debug('Started data receiver threads on %s:%i' % (self.recv.host_ip, self.recv.port_number))
        self.set_local_data_port_number(self.recv.port_number)
        return self.recv

    def get_data_socket(self, port_number=0):
        """
        Return a socket tha is bound to the port that receives the raw/correlator data.


        """
        # Make sure there is a list of opened sockets
        import __main__
        import socket

        if not hasattr(__main__, '__opened_sockets__'):
            opened_sockets = __main__.__opened_sockets__ = {}
        else:
            opened_sockets = __main__.__opened_sockets__

        if not self._data_socket:

            # If we want to use a specific local port that was previously reserved, use its socket.
            if port_number and port_number in opened_sockets:
                return opened_sockets[port_number]

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((self.interface_ip_addr, port_number))
            # store the socket in the main module so it will live persistently until the Python session is closed.
            (actual_ip_addr, actual_port_number) = sock.getsockname()
            opened_sockets[actual_port_number] = sock
            self._data_socket = sock
            self.set_local_data_port_number(actual_port_number)

        return self._data_socket


    def start_data_capture(self, period=None, frames_per_burst=1,  number_of_bursts=0,
                           channels=None, source='scaler', sync=1, verbose=1,
                           burst_period_in_seconds=None, burst_period_in_frames=None, offset=0, send_delay=0):
        """
        Starts the transmission of ADC (post-function generator / pre-FFT) or SCALER (post
        scaler) data frames to the Ethernet port at the specified rate.

        Parameters:

            period (float): Time (in seconds) between captured bursts

            burst_period_in_seconds (float): Same as `period` or as a number of frames

            burst_period_in-frames (int): Number of frames between captured bursts.

            number_of_bursts (int): Number of bursts to send, after which the FPGA stops sending
                data. If `number_of_bursts`=0, the transmission continues indefinitely, until
                stopped by `stop_data_capture()`.

            frames_per_burst (int): Number of frames to send in a single burst. Default is 1.
                Limited by buffer space in the FPGA.

            source (str): selects the data source. 'adc':  the data is taken after the function
                generator (sorry, non intuitive). `scaler`: the data is taken after the scaler.
                Default is 'scaler'.

            channels (list): list of channels for which the data capture will be enabled. others are
            left untouched.

            sync (bool):

            verbose (int):

            offset (int): Number that translates to how many frames are skipped before the capture
                counters starts after a sync(). This is used to stagger capture frame transmission
                between boards in a crate to prevent UDP packets from being dropped by a switch.

            send_delay (int): Number that sets the amount of time to wait
                before sending a group of packets that are captured in the local
                buffer. This is a 16-bit number, where each unit corresponds to
                524.288 us.

        Data is sent as N bursts ('number_of_bursts') of M frames ('frames_per_burst') . If
        'number_of_bursts' is zero or not specified, burst transmission is continuous.

        Burst repetition rate is set either as a period specified in seconds ('period' or
        'burst_period_in_seconds') or as a number of frames ('burst_period_in-frames').
        """
        if channels is None:
            channels = self.default_channels

        if burst_period_in_seconds is not None:
            period = burst_period_in_seconds

        if ( (burst_period_in_frames is None) and (period is None)) or ((burst_period_in_frames is not None) and (period is not None)) :
            raise ValueError("You must specify either 'period' or 'burst_period_in_frames' ")

        if period is not None:
            burst_period_in_frames = max(float(period)/self.FRAME_PERIOD, 1)


        burst_period_in_frames = int(burst_period_in_frames)
        # print "%s" % channels.__repr__()
        # print "%i" frames_per_burst
        # print burst_period_in_frames
        # print burst_period_in_frames*self.FRAME_PERIOD*1000
        # print ('continuously when TRIG=1' if not number_of_bursts else ('for a total of %i bursts' % number_of_bursts) )
        if verbose:
            self._logger.info("%r: Configuring channelizer %s to transmit %i-frame burst every %i frames (i.e .every %.3f ms) %s." % (
               self,
               channels.__repr__(),
               frames_per_burst,
               burst_period_in_frames,
               burst_period_in_frames*self.FRAME_PERIOD*1000,
               ('continuously when TRIG=1' if not number_of_bursts else ('for a total of %i bursts' % number_of_bursts))))
            frames_per_second = len(channels)*frames_per_burst*1.0/self.FRAME_PERIOD/burst_period_in_frames
            bits_per_second = frames_per_second * 8 * self.FRAME_LENGTH
            self._logger.debug('%r: Data rates are: %f kFrames/s, %f Mbits/s' % (self, frames_per_second/1e3, bits_per_second/1e6))

        self.set_trig(0) # disable data transmission if continuous mode is currentlly selected
#        self.set_ant_reset(1) # resets all
#        if clear_buffer:
#            self.flush_frame_buffer()

        for ant in self.ANT.values():
            ant.PROBER.set_data_source(source)
            ant.PROBER.RESET = 1
            ant.PROBER.PROBE_ID = 0xA0 + ant.ant_number
            ant.PROBER.config_capture(frames_per_burst=frames_per_burst, burst_period=burst_period_in_frames, number_of_bursts=number_of_bursts, offset=offset, send_delay=send_delay)
            if ant.ant_number in channels:
                self._logger.debug('%r: Enabling Capture for Antenna %i' % (self, ant.ant_number))
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
            elif ch not in self.LIST_OF_ANTENNAS_WITH_FFT and not bypass_mode:
                self._logger.warning('%r: FFT bypass mode was disabled on channel %i which has no FFT module. The command will have no effect.' % (self, ch))
            else:
                self.ANT[ch].FFT.BYPASS = bypass_mode
                configured_channels.add(ch)
        self._logger.debug('%r: Setting FFT bypass mode to %s for Antenna %s' % (self, str(bool(bypass_mode)), ', '.join([str(i) for i in configured_channels])))
        # self.reset()
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

        self._logger.debug('%r: Setting SCALER bypass mode for Antenna %s' % (self, ', '.join([str(i) for i in channels])))
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

    # def inject_frame(self,  data=None, length=None, channels=None):
    #     """ Inject a frame of data in the specified antenna processing pipeline"""
    #     if channels is None:
    #         channels = self.default_channels
    #     if isinstance(channels, int):
    #         channels = [channels]
    #     for ch in channels:
    #         if isinstance(data, dict):
    #             self.ANT[ch].INJECT.inject_frame(data[ch])
    #         else:
    #             self.ANT[ch].INJECT.inject_frame(data)


    def get_version(self):
        """
        Returns the firmware revion currenting running on the FPGA (which si the date and time of bitstream generation)
        """
        return self.GPIO.get_bitstream_date()

    def get_adc_delays(self):
        delay_table = self.ANT.get_adc_delays()
        delay_table['sync_delays'] = self.REFCLK.get_sync_delays()
        return delay_table;

    def set_adc_delays(self, source='default', compute_delays=1, save_delays=True, check_sync_delays=False, check_adc_delays=20, verbose=1, retry=5):
        """
        Set all the hardare delays (sync delays, ADC tap delays, sample_delay, clock_delay) required to acheive proper
        data acquisition from the ADCs.


        If `source` is None, does not contain or does not point to an existing delay table entry (including a missing delay file or missing tag),
        new delays will be computed if `compute_delays > 0`. If recomputing is not allowed, an exception will be raised.

        If valid delays exist but `compute_delays==2`, new delays will be computed anyways.

        The delays obtained at this point will be checked according to the `check_sync_delays` or `check_adc_delays`
        parameters. If the check fails, new delays delays will be computed if allowed, otherwise an exception will be
        raised. Computation and test of delays will tried up to `retry` times.

        If new delays were computed successfully and `save_delays` is True, the new delays will be saved in the delay
        table under the tag specified in `source`, or under the 'default' tag if `source` is None or empty.


        Scenarios:

            - No delay table provided: compute delay, test succeed, save delays, return
            - No delay table provided: compute delays, test failed, retry compute delays, test succeed, save delays, return
            - No delay table provided: compute delays, test failed, retry compute delays, test fail, exception
            - load delays, test succeed, return
            - load delays, test fail, compute, test succeed, save, return
            - load delays, test fail, compute, test fail, retry compute, test succeed, save, return
            - load delays, test fail, compute, test fail, retry compute, test fail, exception

        Parameters:

            source: Depending on the type of `source`:

               - *None*: no source is specified. `compute_delays` must be > 0 so new delays will be computed.
               - *str*: fetch the latest delays from the delay file with the tag specified by `source`. Defaults to
                 the tag named 'default'
               - *dict*: use the delay tables provided by `dict`

            compute_delays (int): Determines when the delays are checked and when new ones should be computed

                - 0: Never compute delays. In this case, `source` must contain or point to valid delay tables.
                - 1: Recompute delays if `source` is not specified or is invalid, or if errors are detected during checks
                - 2: Always recompute delays, ignoring `source`.

            save_delays (bool): If True, newly computed delay will be saved in the delay table file

            check_sync_delays (bool): If True, loaded sync delays will be checked by pulsing the ADC ``sync`` line and
                verifying that the phase of the ADC clock stays constant relative to the system clock. If the test fails and if
                `compute_delays` allows it, a new delay for the sync pulse will be computed.

            check_adc_delays (int): Number of times the ADC is sync'ed and ramp data is read to check the integrity of the data acquisition. If the test fails and if
                `compute_delays` allows it, new data line delays will be computed.


            verbose (bool): If True, print the progress and results of the delay calculation and tests

        Returns:
            None

        The delays are saved in the folder 'adc_delay_files/MGK7MB_SNxxx.yaml', where xxx is the serial number of the
        motherboard. New delays are appended to the file. Each delay table is associated with a timestamp and a tag. The
        latest timestamp for a given tag is used.

        The delay file is a list in the format:

            ``[ {__tag__: , __date__:, __mezzanines__:, delay_table:}, ...]`` where:

                - __tag__ (str): arbitrary string identifying the set of delays. Multiple delays can be saved on the same tag.
                - __date__ (str): date in ISO format where the delays were saved. used to find the most recent set of delays.
                - __mezzanines__ ((str, str) tuple): tuple representing the model and serials of both mezzanines. A delay table entry will be ignored unless both mezzanine IDs match the current ones.
                - delay_table: dict containing the delay information to be applied for the mezzanines.

        A delay table is a dict in the following format::

            valid: bool
            0:
               tap_delays: [bit0_tap_delay, but1_tap_delay,...]
               sample_delay: int
               clock_delay: int
            1: ...
            ...
            15: ...
        """
        delay_table = None
        delay_table_updated = False
        if compute_delays < 2: # don't bother getting delays from the specified source if we are going to recompute the delay table anyways
            if isinstance(source, str):
                delay_table = self._load_adc_delays(source)
            elif isinstance(source, dict):
                delay_table = source
            else:
                raise TypeError('Source must be either a tag from the delay file or a dict')
            if delay_table:
                self._set_adc_delays(delay_table)
            if delay_table and check_sync_delays and self.REFCLK.check_sync_delays(trials=check_sync_delays, verbose=verbose):
                delay_table = None  # invalidate the delay table if we asked to check it and found errors
            if delay_table and check_adc_delays and self.check_ramp_errors(trials=check_adc_delays, verbose=verbose):
                delay_table = None  # invalidate the delay table if we asked to check it and found errors
            if delay_table is None:
                self.logger.warning('%r: Provided delay table failed checks' % self)
        if compute_delays >= 2 or (compute_delays >= 1 and not delay_table):
            for trial in xrange(retry):
                delay_table = self.compute_adc_delays(channels=range(16), verbose=verbose, adc_sampling_freq=800e6, compute_sync_delays=True, check_sync_delays=check_sync_delays, check_adc_delays=check_adc_delays, set_delays=False)
                delay_table_updated = True
                if delay_table and delay_table.get('valid', True):
                    break
                self.logger.warning('%r: Computed delay table failed checks. Retrying...' % self)

        if delay_table and delay_table.get('valid', True):
            self._set_adc_delays(delay_table)
            if delay_table_updated and save_delays:
                self._save_adc_delays(delay_table, tag=source or 'default')
        else:
            raise RuntimeError('Did not obtain a valid delay table.')


    def _load_adc_delays(self, tag='default'):
        filename = '%s.yaml' % self.get_string_id()
        fullpath = os.path.join(os.path.dirname(__file__), '..', 'adc_delay_tables', filename)

        # print 'Loading YAML file %s' % filename
        try:
            with open(fullpath, 'rb') as yamlfile:
                file_data = load_yaml(yamlfile)
        except IOError:
                print '%s not found' % fullpath
                return None
        if file_data is None:
            return None
        if not isinstance(file_data, list):
            raise RuntimeError('Delay table file should be a list')

        latest_delay_table = None
        latest_date = None
        for entry in file_data:
            if any(key not in entry for key in ('__tag__' , '__mezzanines__', '__date__', 'delay_table')):
                continue
            mezzanines = {i: m.get_id() for i, m in self.mezzanine.items()}
            if entry['__tag__'] == tag and entry['__mezzanines__'] == mezzanines:
                date = datetime.strptime(entry['__date__'], "%Y-%m-%dT%H:%M:%S.%f")
                if latest_date is None or date >= latest_date:
                    latest_date = date
                    latest_delay_table = entry['delay_table']
        return latest_delay_table

    def _save_adc_delays(self, delay_table, tag='default'):
        if not delay_table:
            raise ValueError('Please specify a valid delay table')
        filename = '%s.yaml' % self.get_string_id()
        fullpath = os.path.join(os.path.dirname(__file__), '..', 'adc_delay_tables', filename)
        print 'Loading YAML file %s' % filename
        try:
            with open(fullpath, 'rb') as yamlfile:
                file_data = load_yaml(yamlfile)
        except IOError:
                print '%s not found' % fullpath
                file_data = []

        if file_data is None:
            file_data = []

        if not isinstance(file_data, list):
            raise RuntimeError('Delay table file should be a list')
        mezzanines = {i: m.get_id() for i, m in self.mezzanine.items()}
        date = datetime.utcnow().isoformat()

        new_entry = dict(__date__=date, __tag__=tag, __mezzanines__=mezzanines, delay_table=delay_table)
        print 'new entry: ', new_entry
        file_data.append(new_entry)
        s = yaml.safe_dump(file_data, default_flow_style=None) # make sure we raise en exception here before we start writing the file, otherwise we will lose the whole file.
        with open(fullpath, 'wb') as yamlfile:
            yamlfile.write(s)

    def _set_adc_delays(self, delay_table):
            sync_delays = delay_table.get('sync_delays', None)
            self.REFCLK.set_sync_delays(sync_delays)
            self.ANT.set_adc_delays(delay_table);


    def check_ramp_errors(self, delay=0.1, trials=10, verbose=1):
        """
        Puts all ADCs in ramp mode and use the firmware ramp checker to check if the data acquired from them is valid.

        When the test is done, the ADC is then put in its original mode.

        Parameters:

            delay (float): Period of time during which the ADC data is checked.

            trials (int): Number of times the  ADC is sync'ed and the data is checked.

            verbose (bool): If True, prints the check progress and results.

        Returns:
            A dictionary listing the total number of mismatched words words were detected for all channels and all
            trials combined.

        """
        old_adc_mode = self.get_adc_mode()
        self.set_adc_mode('ramp')
        word_errors=[]
        if verbose:
            print 'ADC Delay checks for %r' % (self)
        for trial in xrange(trials):
            if verbose:
                print 'Trial #%2i' % (trial + 1),
            self.sync()  # This automatically clears the error counter
            time.sleep(delay)
            for (i, ant) in self.ANT.items():
                e = ant.ADCDAQ.RAMP_ERR_CTR
                if e == 1: e = 0  # we still sometimes get one (and only one) spurious error count just after sync. There is probably still a firmware problem. We'll ignore it by software.
                be = ant.ADCDAQ.BIT_ERR_CTR  # bit error counters
                word_errors.append(e)
                # ant.ADCDAQ.RAMP_ERR_CLEAR = 0
                # ant.ADCDAQ.RAMP_ERR_CLEAR = 1
                if verbose:
                    print '%2i (%08X) ' % (e, be),
            print
        self.set_adc_mode(old_adc_mode)
        return sum(word_errors)

    def capture_adc_eye_diagram(self, channels=range(16)):
        """
        Measures the eye diagram of the ADC digital data lines using the ADCDAQ capture feature.

        Parameters:
            channels (list of int): List of channels to which the command is applied

        Returns:
            ``N_channels`` x 32 x 11 byte array, where ``N_channels`` is the numbe of channels specified in :paramref:`channels`.

        Note:
            Parameter `channels` works

            Parameter :paramref:`channels` does not works

            Parameter :paramref:`CHANNELS <capture_adc_eye_diagram.channels>` work

            Parameter :paramref:`capture_adc_eye_diagram.channels` work

            Parameter :paramref:`~capture_adc_eye_diagram.channels` work

            Parameter `CHANNELS <capture_adc_eye_diagram.channels>` work

            Parameter `capture_adc_eye_diagram.channels` work

            Parameter `~capture_adc_eye_diagram.channels` does not work

            Parameter  `chFPGA_controller.init` does not work

        """
        old_delays = self.get_adc_delays()
        old_adc_mode = self.get_adc_mode(channels=channels) # make sure we don't access boards not on the channel list: they may be powered off
        for ch in channels:
                self.ANT[ch].ADCDAQ.set_delays((None, 0, 0))  # set all sample delays to zero before sync
        self.set_adc_mode('pulse', channels=channels, sync=True) # generate pulse pattern and sync
        period = 11  # The pulse waveform repeats every 11 samples
        for i in range(1):
            self.sync()
        data = np.zeros((len(channels), 32, period), np.uint8)

        for i, ch in enumerate(channels):
            # d = np.zeros((32, 11), dtype=np.uint8) # 32 delays x 11 offsets
            # self._logger.info('%.32s: Reading channel %i.' % (self, ch))
            adcdaq = self.ANT[ch].ADCDAQ
            for dly in range(32):
                adcdaq.set_delays(([dly]*8, None, None))  # Set delay, don't change sample delay. No need to sync because sample delay not changed.
                data[i, dly, :] = adcdaq.capture_pattern(period=11);
        self._set_adc_delays(old_delays) # restore original delays before the function was called
        self.set_adc_mode(old_adc_mode, channels=channels)
        return data

    def compute_adc_delays(self, channels=range(16), verbose=True, adc_sampling_freq=800e6, compute_sync_delays=True, check_sync_delays=True, check_adc_delays=True, set_delays=True):
        """
        Measures the eye diagram of the ADC digital data lines and computes the optimum delays to ensure reliable data acquisition.

        This will work only if the sync delays are set properly.
        """

        old_delays = self.get_adc_delays()
        # n = np.zeros((16, 11), dtype=np.uint8)
        new_delays = {}

        tap_delay = 1 / 200e6 / 32 / 2
        pulse_period = int((1 / adc_sampling_freq) / tap_delay) # 800 MHz period in tap delays (16 taps)

        if compute_sync_delays:
            sync_delays = self.REFCLK.compute_sync_delays(adc_clock_freq=adc_sampling_freq/2, set_sync_delays=True, verbose=verbose)
        else:
            sync_delays = self.REFCLK.get_sync_delays()
        new_delays['sync_delays'] = sync_delays

        if check_sync_delays:
            sync_invalid = self.REFCLK.check_sync_delays(trials=10, adc_clock_freq=adc_sampling_freq/2,  verbose=verbose)
        else:
            sync_invalid = None

        data = self.capture_adc_eye_diagram(channels) #  N_chan x 32 x 11 array

        for i, ch in enumerate(channels):

            # first, find the offset for which the smallest number of '1' bits for every bit is as high as possible
            q = np.array([ ((data[i] & (1 << bit)) !=0).sum(axis=0) for bit in range(8)]).min(axis = 0)  # smallest number of '1' for each possuble bit, for each offset
            offset = q.argmax() # offset that has the largest number of '1's
            print 'CH%02i: offset=%2i : %s' % (ch, offset, q)

            n = data[i, :, offset]  # extract the samples for the current channel and selected offset, byt keep all 32 delays

            # Now find the optimal delay for each bit
            computed_delay = np.zeros(8, dtype=np.uint8)
            for bit in range(8):
                mask = 1 << bit
                d = (n & mask) >> bit
                s = (d.astype(np.int8) + ord('0')).tostring() # Convert to a string of "1" and "0"s so we can use the 'find' method. before doing that, make sure this is an int8 array otherwise we'll get more than one char per value...
                re = s.find('0111')
                fe = s.find('1110')
                # print s,re,fe
                if re >= 0 and fe >= 0 and fe > re: # if we have both a rising edge
                    delay = (fe + 2 + re + 1) / 2
                elif re >= 0: # if we have a rising edge only
                    delay = min(re + 1 + pulse_period / 2 - 1, 31)  # 4 samples after the rising edge, but stop at max delay. -1 to be closer to the known good edge.
                elif fe >= 0:
                    delay = max(fe + 3 - pulse_period / 2 - 1, 0)
                else:
                    delay = -1  # invalid delay
                # computed_delay[bit] = np.sum(d*range(32)) / np.sum(d)
                computed_delay[bit] = delay


                bit_string = ''
                for delay in range(len(d)):
                    if delay == computed_delay[bit]:
                        bit_string += '!O'[d[delay]]
                    else:
                        bit_string += '.#'[d[delay]]
                s = 'Bit %i: %s Delay = %2i   (rise @ %2i, fall @ %2i)' % (bit, bit_string, computed_delay[bit], re+1, fe+2)
                if verbose:
                        print s
                # self._logger.info(s)

            new_delays[ch] = {'tap_delays': computed_delay.tolist(), 'sample_delay': int((offset + 3) % 11), 'clock_delay': 0}

        self._set_adc_delays(new_delays)

        if check_adc_delays:
            data_invalid = self.check_ramp_errors(trials=check_adc_delays, verbose=verbose)
        else:
            data_invalid = None
        new_delays['valid'] = not (sync_invalid or data_invalid)

        if not set_delays:
            self._set_adc_delays(old_delays)

        return new_delays


    def compute_adc_delay_offsets(self, channels=range(16)):
        """
        Measures the eye diagram of the ADC digital data lines and computes
        the permisable offset to ensure reliable data acquisition.

        Returns a delay/offset table (delaytable), flags any stuck bits
        (stuckbits), provides the logic level at the chosen eye sampling point
        (bitposgood)  and in that order. Note that stuck bits should all be
        false, bitposgood should be all 1s
        """
        delaytable = {}
        stuckbits = {}
        bitposgood = {}
        problem = 0

        for chan in channels:
            t = self.read_eye_diagram(channels=[chan], offset=[0]*16, noffsets=11)  # Creating an offset / delay table 11 columns 32 rows

            # Finding both 0 and 255 in the table Means we have no stuck bits
            stuckbits[chan] = not (np.any(t[chan] == 0) and np.any(t[chan] == 255))
            if(stuckbits[chan] == True):
                problem = 1 #Stuck bit detected

            offset = t[chan].sum(axis=0).argmax()  # Choosing the offset by looking at the offset/delay table and picking the column with the highest sum (i.e most 255s)

            bitdelay = []
            changood = []
            for bit in range(8):
                mask = 1 << bit #looking at one adc bit at a time
                sample = (t[chan][:,offset]) & mask
                if any(sample): # If sample has nonzero values
                    chosendelay = int((sample * np.arange(32)).sum() / sample.sum())  # performing a center of mass claculation to pick eye location
                    changood.append( (((t[chan][:, offset])[chosendelay]) & mask) >> bit)  # Checking what the bit level at the eye center is
                else: # Sample is all zeros
                    chosendelay = np.NaN
                    changood.append(np.NaN)
                    problem = 1 #Can't find a good spot so indicate a problem is present
                bitdelay.append(chosendelay)
                #self._logger.info( 'Warning: Center of eye diagram on bit %i of channel %i has glitch ' % (bit, chan))

            offset = (offset + 3) % 11  # The difference in offset between a pulse waveform 'high' sample and and the first sample of a  ramp
            # offset = offset - 3  #The difference in offset between a pulse waveform and a ramp
            # if offset < 0:  # An untested wrap around conddition (Adam 12/12/2014)
            #     offset = offset + 11

            delaytable[chan]= (bitdelay, [offset]*8)  # Building the delay table
            bitposgood[chan] = changood  # Building the eye diagram good table

            if(0 in changood):
                problem = 1 #Inverted bit detected

        return delaytable, stuckbits, bitposgood, problem

    def tune_adc_delays(self, loadfromdict = None, channels=range(16), retries=20):

        try:
            mezz1_serial = self.mezzanine[1].serial
        except:
            mezz1_serial = None

        try:
            mezz2_serial = self.mezzanine[2].serial
        except:
            mezz2_serial = None

        if (loadfromdict == None):

            for ch in channels:
                if not self.ANT_FMC_IS_PRESENT[ch]:
                    raise ValueError('Some of the requested channels are not present')

            opt_sync_delay = self.REFCLK.compute_sync_delay(channels=channels)

            #I have seen compute_sync_delay pick a solution in the middle of one of its groups
            #that results in bad eye diagrams so this bit of code tries to address that
            trycounter = 0
            goodsolution = 0
            ofset_sync_delay = opt_sync_delay
            while (trycounter < retries and not goodsolution):

                self.REFCLK.set_sync_delays(ofset_sync_delay)
                check = self.read_eye_diagram(channels=channels, offset=[0]*16, noffsets=11)

                goodsolution = 1
                for ch in channels:
                    if 255 not in check[ch]:
                        goodsolution = 0
                        break
                if goodsolution == 0:
                    increment = ([0,1][ch <8], [0,1][ch>7])
                    if ofset_sync_delay[0] != -1:
                        ofset_sync_delay[0] = (ofset_sync_delay[0] + increment[0]) % 32
                    if ofset_sync_delay[1] != -1:
                        ofset_sync_delay[1] = (ofset_sync_delay[1] + increment[1]) % 32
                    print 'Initial offset calculation resulted in bad eye diagrams - adjusting offset too {0}'.format(self.REFCLK.get_refclk_delay())
                trycounter += 1


            d1,d2,d3,problem = self.compute_adc_delay_offsets(channels=channels)
            #d1 contains the delay table with offsets
            #d2 indicates if bits are stuck
            #d3 is the value measured at the center of the adc pulse waveform for each bit - should be 1

            if (problem == 1): #NaN present in delay table, or stuck bit, or inverted bit
                raise ValueError('Delay table has problems - check for NaN, stuck bits or inverted bits')

            self.set_adc_delays(d1) #The delay table found was all good, so setting it

            tunedloc=dict()
            tunedloc['delaytable'] = d1
            tunedloc['syncdelay'] = opt_sync_delay
            tunedloc['boards'] = {'Mezz': [mezz1_serial,mezz2_serial], "MB" : self.serial }

            return tunedloc
        else: #We have chosen to load the delay table from a dictionary

            try:
                d1 = loadfromdict['delaytable']
                opt_sync_delay = loadfromdict['syncdelay']
                dict_mb_serial = loadfromdict['boards']['MB']
                dict_mezz1_serial = loadfromdict['boards']['Mezz'][0]
                dict_mezz2_serial = loadfromdict['boards']['Mezz'][1]
            except:
                raise ValueError('Missing objects in adc delay dictionary')

            if (self.serial != dict_mb_serial) \
                or (mezz1_serial!=None and mezz1_serial!=dict_mezz1_serial) \
                or (mezz2_serial!=None and mezz2_serial!=dict_mezz2_serial):
                raise ValueError('Cannot use this adc table - hardware is not the same')

            self.REFCLK.set_sync_delays(opt_sync_delay)
            self.set_adc_delays(d1)

            measuredloc=dict()
            measuredloc['delaytable'] = self.get_adc_delays()
            measuredloc['syncdelay'] = self.REFCLK.get_refclk_delay()
            measuredloc['boards'] = {'Mezz': [mezz1_serial,mezz2_serial], "MB" : self.serial }
            return measuredloc


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

        if width not in (4, 8):
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

        Parameters:
            channels (list of int): List of channels to which the command is applied

        """
        if channels is None:
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
        Sets the Corner-Turn engine to add and send Scaler and Frame and flags in its output
        packets.

        Parameters:

            send_flags (bool): If True, the flags will be sent.

            crossbar_outputs (list of int): list of indices of the 1st CROSSBAR outputs that should
                be configured.

            sync (bool): If True (default), a local sync() will be performed.
        """
        if crossbar_outputs is None:
            crossbar_outputs = range(self.NUMBER_OF_CROSSBAR_OUTPUTS)

        if not isinstance(crossbar_outputs, list):
            raise ValueError("'crossbar_outputs' must be a list")
        else:
            # Set the scaler to use offset binary
            for output in crossbar_outputs:
                self.CROSSBAR[output].CH_DIST.SEND_FLAGS = send_flags
            if sync:
                self.sync()

    def load_gains(self, folder='.'):
        """ Loads the gain file associated with this board and return the gains.

        The gain file is a pickled dictionary in the format {channel_number:gains,..}.

        Parameters:

            folder (str): Folder in which the gain files are to be found. Default is the current directory.

        Returns:

            gains that have been loaded. `None` if the gains are not found.

        """
        slot_0based = self.slot - 1
        crate = self.crate.crate_number
        try:
            gain_filename = os.path.join(folder, 'gains_FCC%02i%02i.pkl' % (crate, slot_0based))
            gains = pickle.load(open(gain_filename, 'rb'))
            # self.logger.info('Setting gains on IceBoard SN%s, crate %s, slot %i' % (ib.serial, crate, slot))
            # ib.set_gain(g_array, bank=bank)  # *** should this be bank=all_bank
        except IOError:
            self.logger.warn('Gain file not found for for crate %02i slot %02i (FCC%02i%02i)' % (crate, slot_0based, crate, slot_0based))
            gains = None
        # # Fill any missing channel info with None
        # for ch in range(self.NUMBER_OF_CHANNELIZERS):
        #     if ch not in gains:
        #         gains[ch] = None
        return gains

    def set_gains(self, gain=None, postscaler=None, channels=None, use_fixed_gain=False, bank=0, when=None):
        """
        Sets the gain between the (18+18) bits input of the scaler module (from the FFT) to its 4- or 8- bit scaler output.
        The gain can be set individually for every frequency bins and every ADC channel.

        Parameters:

            gain (complex or tuple): Linear gain and optional poscaler gain to apply to the specified channels. The real and imaginary part of the linear gain are integer
                  values ranging from -32768 to 32767.

                    - If `gain` is a scalar, its value is applied to all bins, and the postscaler value in `postscaler` is used.
                    - If `gain` is a (*Glin*, *Glog*) tuple, the linear gain *Glin* is provided along with the postscaler factor.

                        - If *Glin* is a complex scalar, the same complex gain is applied to every bin.
                        - If *Glin* is a 1024-eleemnt complex vector, each bin has the individual gain specified in the vector.
                        - Glog (posctscaler value) is a binary scaling factor, which is an integer between 0 and 31 representing a power of two that multiplies the linear gain.
                             This is a scalar common to every bin.

            postscaler (int): Postscaler factor to apply if not specified as the parameter *Glog* in `gain`.

            channels (list of int): channels to which the gain is applied

            use_fixed_gain (bool): put the scaler in fixed gain mode where the gain bank RAM is
                completeley bypassed and a single complex gain is applied to every bin. Is functionnally
                equivaleent to set the gain ov every bin to the same value. Mostly useful during the
                debuggging phase.

            bank (int): The memory bank to which the gains should be applied (0 or 1)

            when (int): Specifies when the specified gains shall become active. If `when` is 'now', the
                gains are written immediately on the target bank and the bank is made active on the
                next frame. If `when` is an  *int*, the gains are written immediately to the bank  bank,
                but than bank will become active only on frame numer (timestamp) specified by when.

        The actual gain between the scaler input and output for bin 'b' is:
           4-bit mode: out/in = :math:`Glin(b) * 2**(Glog-31)`
           8-bit output: out/in = :math:`Glin(b) * 2**(Glog-27)`

        G can be specified in the following manner:
           G = *Glin*              : Sets only the linear gain. Same as (*Glin*, None)
           G = (*Glin*, None)      : Same as above
           G = (None, Glog)      : Sets only the postscaler
           G = (*Glin*, Glog)      : Sets both the linear gain and the postscaler

        The 'gain' parameters can be specified as:
            gain = G: the specified gain is applied only to the ADC channels specified in the list 'channels'.
            gain = {ch1: G1, ch2: G2 ...} : The gain is applied to specified channels, but only if they are included in 'channels'
            gain = [ (ch1, G1),  (ch2, G2), ...]: Same thing, but in a list format
            gain = [ (ch_list , G1), (ch_list2, G2), ...]: Same thing, but we can apply the gains to lists of channels

        If 'channels' is None, it is applied to the default (active) channels (see set_default_channels()).

        If 'postscaler' is specified, it will be used as default value when Glog = None.

        'use_fixed_gain': if True, enables the use of fixed gain mode of the scaler module. In this case, 'gain' can only be a scalar. Is False by default.

        ``bank`` is the coefficient bank number (0 or 1) to which the
        coefficient should be written. Once written, the bank is made active. If ``bank`` is None, the currently inactive bank is used.


        Notes:
            #) The PFB/FFT has an intrisic gain of 512 (a constant FFT input of '1' will yield the value 512 in bin 0 at the input of the scaler.
            #) If the FFT is bypassed, the 8-bit values from the ADC or the function generator are applied directly to the scaler input.
            #) In 4-bit mode, the output value is taken from bits 31 to 34 of the postscaled-value. In 8-bit mode, bits 27 to 31 are used.
            #) A smaller postscaler value allows a larger gain to be used to acheive the same overall gain while providing more gain resolution.
            #) A gain of (1, 31) allows the function generator values to appear on the scaler output with an overall gain of 1 in 4-bit mode. This is equivalent to (2, 30), (4,29) ... (16384, 8), except that the latter offers more gain resolution.
            #) A gain of (1, 27) dies the same in 8-bit mode.
            #) (16384, 8), except that the latter offers more gain resolution.

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
                    self.ANT[ch].SCALER.set_gain_table(Glin, bank=bank)
                configured_channels.add(ch)
        self._logger.debug('%r: Setting scaler gains for Antenna %s' % (self, ', '.join([str(i) for i in configured_channels])))

        if when is not None:
            self.switch_gains(bank=bank, when=when)

    def get_next_gain_bank(self):
        """ Return the gain bank that will be used on the next automatic bank switch."""
        return [ant.SCALER.READ_COEFF_BANK ^ 1 for ant in self.ANT.values()]

    def get_gains(self, bank=0):
        """
        Returns the log2 SCALER gain and the linear gain table used for each channelizer.

        Parameters:

            bank (int): gain bank from which to get the gains.

        Returns:

            list of (log_gain, linear_gain_table) for each channelizer. ``linear_gain_table`` is an
            array of 2014 complex values.
        """
        gain_list = []
        for ant in self.ANT.values():
            glog = ant.SCALER.SHIFT_LEFT
            glin = ant.SCALER.get_gain_table(bank=bank)
            gain_list.append([ant.ant_number, [glin,glog]])
        return gain_list

    def switch_gains(self, bank=None, when='now'):
        """
        Switch the gains to the specified `bank` at the moment specified by `when`.

        Parameters:

            bank (int): target gain bank in which the gains will be written. If `bank` is -1 or
                None, the unused bank is switched in.

            when ('now', None or int):
                if `when` is 'now' (default), the switch is done immediately.
                If `when` is None, no switch is done.
                if 'when' is an integer, the switch will be done at the frame numbers specified by `when`.
        """

        if when is None:
            return

        for ant in self.ANT.values():
            if bank is None or bank < 0:
                next_bank = ant.SCALER.READ_COEFF_BANK ^ 1
            else:
                next_bank = bank

            if when == 'now':
                ant.SCALER.SYNCHRONIZE_GAIN_BANK = False
                ant.SCALER.READ_COEFF_BANK = next_bank
            else:
                ant.SCALER.SYNCHRONIZE_GAIN_BANK = True
                ant.SCALER.READ_COEFF_BANK = next_bank
                ant.SCALER.GAIN_BANK_SWITCH_FRAME_NUMBER = when


    # def set_synchronized_gain_switching(self, enable=1):
    #     for ant in self.ANT.values():
    #         ant.SCALER.SYNCHRONIZE_GAIN_BANK = enable
    #     self._logger.debug("%r: Synchronized gains for active antennas set to %d" % (self, enable))


    # def get_synchronized_gain_switching(self):
    #     enabled = []
    #     for ant in self.ANT.values():
    #         enabled.append(ant.SCALER.SYNCHRONIZE_GAIN_BANK)
    #     self._logger.debug("%r: syncronization for gain set to %s" % (self, ', '.join([str(i) for i in enabled])))
    #     return enabled

    # def set_gain_switch_frame_number(self, frame=2147483647):
    #     for ant in self.ANT.values():
    #         ant.SCALER.GAIN_BANK_SWITCH_FRAME_NUMBER = frame
    #     self._logger.debug("%r: set gain switch number for active antennas to %d" % (self, frame))


    # def get_gain_switch_frame_number(self):
    #     frames = []
    #     for ant in self.ANT.values():
    #         frames.append(ant.SCALER.GAIN_BANK_SWITCH_FRAME_NUMBER)
    #     self._logger.debug("%r: gain switch numbers are %s" % (self, ', '.join([str(i) for i in frames])))
    #     return frames

    # def set_next_gain_bank(self, bank=0):
    #     '''
    #     Sets which gain bank (0 or 1) the SCALER will use.  if synchronized gain switching enabled, won't take effect
    #     until the bank switch frame number.  Otherwise the switch is immediate.
    #     '''
    #     for ant in self.ANT.values():
    #         if bank is None or bank < 0:
    #             ant.SCALER.READ_COEFF_BANK ^= 1
    #         else:
    #             ant.SCALER.READ_COEFF_BANK = bank
    #     # self._logger.debug("%r: set gain bank for active antennas to %d" % (self, bank))


    # def get_next_gain_bank(self):
    #     '''
    #     Gets which gain bank (0,1) scaler will use.  if syncronized gain switching enabled, won't take effect
    #     until the bank switch frame number.  Otherwise is immediate
    #     '''
    #     banks = []
    #     for ant in self.ANT.values():
    #         banks.append(ant.SCALER.READ_COEFF_BANK)
    #     self._logger.debug("%r: Got gain bank for active antennas to %s" % (self, ', '.join([str(i) for i in banks])))
    #     return banks


    # def get_current_gain_bank(self):
    #     gain_banks = []
    #     for ant in self.ANT.values():
    #         gain_banks.append(ant.SCALER.CURRENT_GAIN_BANK)
    #     return gain_banks

    def reset_fft_overflow_count(self):
        """ Reset the FFT overflow counter in all channelizers.
        """
        for ant in self.ANT.values():
            ant.FFT.reset_fft_overflow_count()

    def set_fft_shift(self, fft_shift=0b11111111111, channels=None):
        """
        Sets the FFT shift schedule for the FFT.  .

        Parameters:

            fft_shift (int): binary value that indicates whether each FFT stage will shift the
                result (i.e divide by 2) of that stage. Each bit represents a divide by 2 for that
                stage of the FFT.  Default is to shift every stage.  11 stage FFT, so default is
                2**11-1. expects a number  in the range 0b11111111111 (2047) and 0b00000000000 (0)

        History:
            2013-02-19 KMB: Added this function
        """

        if channels is None:
            channels = self.default_channels

        for ant in self.ANT.values():
            if ant.ant_number in channels:
                self._logger.debug('%r: Setting FFT shift of antenna %i' % (self, ant.ant_number))
                ant.FFT.FFT_SHIFT = fft_shift

    set_FFT_shift = set_fft_shift  # For legacy code compatibility

    def get_fft_shift(self):
        """
        Returns the FFT shift schedule for each antenna.

        Returns:

            list of int: one integer indicating the shift pattern for each channelizer.
        """
        return [ant.FFT.FFT_SHIFT for ant in self.ANT.values()]

    get_FFT_shift = get_fft_shift # for legacy compatibility

    def set_user_output_source(self, source, output=0):
        """
        Selects the signal to be sent to the user outputs (SMAs & LEDs).

        Parameters:

            source (str): is the source name

                * 0: sync : User-generated SYNC signal (sunc_out)
                * 1: pps : 1 PPS signal from the IRIG-B decoder (pps_out)
                * 2: pwm : Output from the frame-based pwm generator (pwm_out)
                * 3: irigb_trig :# not(irigb_before_target)
                * 4: bp_trig : (bp_trig_reg)
                * 5: bp_time : (bp_time_reg)
                * 6: refclk : 10 MHz reference clock (clk10)
                * 7: irigb_gen : (irigb_gen_out)
                * 8: heartbeat1 : (gpio_led_int(4))
                * 9: heartbeat2 : (gpio_led_int(7))
                * 10: debug1 : (debug1, currently crossbar2.align_pulse)
                * 11: debug2 : (debug2, currently crossbar0.lane_monitor)
                * 12: user_bit0 : (user_bit(0))
                * 13: user_bit1 : (user_bit(1))
                * 14: debug3 : (chan_lane_monitor(2)(to_integer(unsigned(user_bit))))
                * 15: fmc_refclk :# Refclk from Mezz selected by user_bits(0:1)  (fmc_refclk(to_integer(unsigned(user_bit)))


            output (str or int) is the number or name of the output to configure.

               * 0=SMA-A on the motherboard
               * 1=SMA on the backplane and FPGA LED1,
               * 2=SMA-B and FPGA LED2 on the motherboard LED on the backplane.

        """
        self.GPIO.set_user_output_source(source, output=output)

    def get_user_output_source(self):
        """ Return the name of the source currently routed to SMA-A

        Returns:

            str: name of the source currently routed to SMA-A
        """
        return self.GPIO.get_user_output_source()

    def set_sync_source(self, source):
        """ Sets the source of the signal that will trigger SYNC events.

        Parameters:

            source (str): name of the source. See `REFCLK.set_sync_source()` for valid names.

        """
        if source not in self.REFCLK.SYNC_SOURCE_TABLE:
            raise ValueError('Invalid SYNC source name. Valid names are %s' % ', '.join(self.REFCLK.SYNC_SOURCE_TABLE))
        self.REFCLK.set_sync_source(source)

    def get_sync_source(self):
        """ Return the current source used to trigger SYNC events

        Returns:
            str describing the SYNC trigger source.
        """
        return self.REFCLK.get_sync_source()

    def set_pwm(self, enable, offset, high_time, period, local_sync=False):
        """ Sets the frame-based PWM generator. All times are stated as the number of frames.

        A SYNC is needed after changes for the changes PWM signal to work properly (it might be in
        some invalid state). A global sync is needed if the PWM signal is to be synchronous across
        multiple boards.

        See GPIO.set_pwm() for more details.

        The `set_user_output_source()` method shall be used to route the PWB signal to the user SMA.

        Parameters:

            enable (bool): if `True`, PWM mode is enabled.

            offset (int): number of frames to wait before the first pulse

            high_time (int) number of frames during which the PWD signal stays high

            period (int): period of the PWM signal in number of frames

            local_sync (bool): If `True`, a local sync signal will be trigerred. Do not use if
                multiple boards need to be opearating synchronously.

        """
        self.GPIO.set_pwm(enable=enable, offset=offset, high_time=high_time, period=period, pwm_reset=False)
        if local_sync:
            self.sync()

    def set_adc_mask(self, mask=0xFF, channels=None):
        """ Set the mask that is applied on the ADC data on the specified channel.

        Parameters:

            mask (int): an 8 bit value which is ANDed with the incoming ADC bytes. mask=0xFF
                therefore disables the masking effect. Can be useful to reduce power consumption of
                the channelizer without affecting synchronization of the data processing pipeline.

            channels (list of int): list of channelizer numbers for which the ADC mask is set.

        """
        if channels is None:
            channels = self.default_channels

        for ch in channels:
            self.ANT[ch].ADCDAQ.BYTE_MASK = mask

#     def check_adc_data_acquisition(self, test_duration=1):
#         """
#         Sets the ADC in ramp mode and compare the incoming ramp in real time with an internally generated ramp to combute the total number of words in error (and an error count for each bit)
#         """
#         old_adc_mode = self.get_adc_mode()
#         self.set_adc_mode('ramp')
#         self.sync() # sync the board to make sure that data acquisition starts on the right ramp sample

#         # Clear the word and bit error counters
#         for ant in self.ANT.values():
#             print 'Clearing antenna', ant.ant_number
#             ant.ADCDAQ.RAMP_ERR_CLEAR=0
#             ant.ADCDAQ.RAMP_ERR_CLEAR=1
#         self._logger.info('%r: Measuring the data acquisition error rate over %0.1f seconds...' % (self, test_duration))
#         t0 = time.time();
#         word_error = np.zeros(len(self.ANT))
#         bit_error = np.zeros((len(self.ANT), 8))
#         try:
#             while time.time() - t0 <= test_duration:
#                 for (i, ant) in self.ANT.items():
#                     print  self.ANT[i].ADCDAQ.RAMP_ERR_CTR,
#                     word_error[i] += ant.ADCDAQ.RAMP_ERR_CTR
#                     for bit_number in range(8):
#                         bit_error[i, bit_number] += ((ant.ADCDAQ.BIT_ERR_CTR >> (bit_number*4)) & 0x0F)
#                     ant.ADCDAQ.RAMP_ERR_CLEAR = 0
#                     ant.ADCDAQ.RAMP_ERR_CLEAR = 1
#                     # self._logger.info('CH%i: %3i (%08X)' % (ant.ant_number, ant.ADCDAQ.RAMP_ERR_CTR, ant.ADCDAQ.BIT_ERR_CTR))
#         except KeyboardInterrupt:
#             pass

#         for (i, ant) in enumerate(self.ANT):
#             self._logger.info('%r: CH%i: %5i word errors, bit errors (7:0) = (%s)' % (self, ant.ant_number, word_error[i], ','.join('%3i' % e for e in bit_error[i,::-1])))
#         total_word_errors = np.sum(word_error)
#         self._logger.info('%r: There were %i word errors in total' % (self, total_word_errors))
# #        self.set_adc_mode(old_adc_mode)
#         return total_word_errors

    def test_speed(self, n=1000, timeout=0.1):
        """ Test the speed of UDP communications and mesure the number of errors.

        The results are printed on stdout.

        Parameters:

            n (int): number of UDP interactions

            timeout: time to wait for a reply before giving up and count the trial as a failed
        """
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
        """ Return the core temperature of the fpga and the ADC chips.

        Uses SYSMON for the FPGA core temperature.

        Returns:

            dict in the format {('target': temperature),...}, with an entry for the FPGA core and each of the ADC chips.

        """
        res = {}
        res['FPGA_core'] = self.SYSMON.temperature()
        for mezz_number, mezz in self.mezzanine.items():
            for (adc_number, adc) in enumerate(mezz.ADC):
                res['FMC%i ADC%i'%(mezz_number-1, adc_number)] = adc.get_temperature()
        return res

    @async
    def get_total_power(self):
        """ Return the total power used by this board.

        The power is measured by measuring the voltage and current on the VCC3V3, VCC5V5 and VCC12V0
        rails.

        Returns:
            Total power, as a float.
        """
        power = sum([(yield self.get_motherboard_voltage.async(rail)) * (yield self.get_motherboard_current.async(rail))
                     for rail in (self.RAIL.MB_VCC3V3, self.RAIL.MB_VCC5V5, self.RAIL.MB_VCC12V0)])  # have to use a list comprehension, not generator (a yield inside a generator is not consistent in Python 2.7)
        async_return(power)

    def init_crossbars(self,
                       mode=None,
                       dsmap=range(16),
                       frames_per_packet=2,
                       cb1_lanes=16, cb1_bins=64, cb1_bypass=False, cb1_combine_data_flags=0,
                       cb2_lanes=None, cb2_bins=1, cb2_bypass=False,
                       bp_shuffle_bypass=1, crate_shuffle_bypass=1,
                       remap=True, chan8_channel_map=range(16)):
        """ Initializes the Corner Turn engine in the specified operation mode.

        This method configured the 1st, 2nd and 3rd crossbars (which each can include a remap, frame
        aligner and an array of bin selectors), as well as the PCB and QSFP data shuffle links.


        Parameters:

            mode (str): String describing a predefined corner-turn engine operation mode. If None,
                the crossbars and shuffle enginecan be set manually with the other provided
                parameters. The valid modes are:

                    'chan8': Corner-turn engine is mostly bypassed and raw 8-bit data from 8
                        channelizers is sent directly to the 8 CT-Engine outputs.

                    'chan4': Corner-turn engine is mostly bypassed and raw 8-bit data from 16
                        channelizers is sent directly to the 8 CT-Engine outputs.

                    'shuffle16': A corner-turn operation is applied only within the 16 channelizer
                        outputs of this board.

                    'shuffle256': The corner-turn operation is applies between 16 channelizers within
                        a board and between the 16 boards within a crate using the backplane PCB links.

                    shuffle512: The corner-turn operation is applies between 16 channelizers within
                        a board, between the 16 boards within a crate using the backplane PCB links,
                        and between 2 crates using the backplane QSFP links.

            dsmap (list) : Defaults to range(16).

            frames_per_packet (int): Number of frames to be packaged in a packet. Defaults to 2. Is
                limited by the amount of memory used by the FPGA to store data and flags.

            cb1_lanes (int): Number of input lanes considered in the CROSSBAR1. Defaults to 16. Used only if `mode`=`None`.

            cb1_bins (int): Number of frequency bins to be included in CROSSBAR1. defaults to 64. Used only if `mode`=`None`.

            cb1_bypass (bool): If True, CROSSBAR1 will be bypassed. Used only if `mode`=`None`.

            cb1_combine_data_flags (bool): if True, the data flags at the output of CROSSBAR1 will
                be packed in 32-bit words. Defaults to False, where each data word is associated
                with a data flag word filled with only 16 flag bits, which is needed to allow the
                next crossbar to further split the flags and data as part of the 2nd stage corner
                turn operation.

            cb2_lanes (list of tuple): list of (lower_lane, upper_lane), one for each of the 2
                CROSSBAR2 bin selector, that describe the range of input lanes that shall be read
                out by the bin selectors. Only partial number of lanes is used if the data is to be
                further recombined by the 3rd stage corner turn operation. If None, both bin
                selectros will combine data from all 16 input lanes.


            cb2_bins (int): Number of bins to be included in the CROSSBAR2 bin selectors.

            cb2_bypass (bool): If True the 2nd stage cirner-turn operation (CROSSBAR2) will be
                bypassed. Defaults to `False`


            bp_shuffle_bypass (bool): If `False`, the 16 data lanes form the CROSSBAR1 will be sent
                to other boards through the backplane PCB links, and the CROSSBAR2 will receiver 16
                lanes of data from the other boards (note than lane 0 is in fact always internal).
                If `True`, the 16 data lanes from the CROSSBAR1 will be forwrded directly to the the
                input of CROSSBAR2.


            crate_shuffle_bypass (bool): If False, half of the 8 outputs lanes of CROSSBAR2 will be
                sent to other boards in the other crate through the backplane QSFP links, and half
                will be sent to the CROSSBAR3. CROSSBAR3 will receive half its data from the other
                crate the corresponding board.If True, the 8 output lanes of CROSSBAR2 go directly
                to the 8 input lanes of CROSSBAR3.

            remap (bool). Defaults to True

            chan8_channel_map (list) : channel remapping to be used in `chan8` mode. Defaults to the identity map (range(16))



        """

        cb1 = self.CROSSBAR
        cb2 = self.CROSSBAR2
        cb3 = self.CROSSBAR3

        number_of_cb1_bin_sel = 16
        number_of_cb2_bin_sel = 2
        number_of_cb3_bin_sel = 8

        def get_dest_slot_for_src_lane(src_lane):
            tx = (self.slot, src_lane)  # unique transmitter id (slot, lane)
            dest_slot = self.crate.get_matching_rx(tx)[0]
            return dest_slot

        def get_src_slot_for_dest_lane(dest_lane):
            rx = (self.slot, dest_lane)
            src_slot = self.crate.get_matching_tx(rx)[0]
            return src_slot

        if frames_per_packet < 1 or frames_per_packet > 4:
            raise ValueError('Number of frames per packet must be between 1 and 4')
        if cb1_lanes in (4, 8, 12, 16):
            cb1_lanes = [(0, cb1_lanes/4-1)] * number_of_cb1_bin_sel
        else:
            raise ValueError('Crossbar 1 number of input lanes must be 4,8,12 or 16')

        if cb2_lanes is None:
            cb2_lanes = ((0, 15), (0, 15))  # Both bin selectors

        # if cb2_lanes % 2:
        #     raise ValueError('Crossbar 2 number of input lanes must be a multiple of 2')

        cb2_timeout_period = None
        cb2_sof_window_stop = None
        if mode == 'chan8':  # get raw data from the channelizer (all 32-bit sent as is). Only 8 lanes are available to the GPU.
            cb1_bypass = True
            cb1_four_bit = False
            cb1_combine_data_flags = 0
            bp_shuffle_bypass = True
            cb2_lane_map = chan8_channel_map # Here we could select which 8 inputs we want to stream to the GPU
            cb2_bypass = True
            crate_shuffle_bypass = True
            cb3_lane_map = range(8)
            cb3_bypass = True
            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 0

        elif mode == 'chan4': # get the high nibble of every bytes from two lanes in a single word. Allows Get (4+4) bit data from all channelizers
            cb1_bypass = True
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            bp_shuffle_bypass = True
            cb2_lane_map = range(16) # All information
            cb2_bypass = True
            crate_shuffle_bypass = True
            cb3_lane_map = range(8)
            cb3_bypass = True
            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 0


        elif mode == 'shuffle16':
            """
            In shuffle16 mode, each of the GPU links output data for 128 bins,
            each bins containing the data from 16 channels. The data for each
            channel is a byte, representing the FFT output as a (4+4) bit
            compler number.

            In this mode:

                chan_bin_sel: each of the 16 chan_bin_sel select data from
                    lane groups 0-3 (i.e. lanes 0-15). They each select 1/64
                    of the bine incoming channelizer output. There are a total
                    of 128 selected bins. There are 4 words per bins (16
                    channels). The data flags of two consecutive bins are
                    combine to build a single data flag word.

            The packet format is as follows (assume one frame per packet):
                - Header: 4 words (16 bytes) for the header
                - Data block: 128 bins x 16 channels = 2056 bytes
                - Data flags block: We combine the data flags, so scaler flags
                  from two consecutive bins are combined into a single 32 bit
                  word ( one bit per channel for each of the 2 bins). This represents 128*16/32=64 words = 256 bytes
                - Frame flags: 16 bit FFT overflow + 16 bit ADC overflow flags per frame. This is 1 word = 4 bytes.
                - Packet flags: 32 bit word (4 bytes)
                - Total: 2328 bytes

            """
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            cb1_bins = 128
            cb1_bin_spacing = 1024/cb1_bins  # = 8
            cb1_combine_data_flags = 1
            cb1_bin_select_map = [np.arange(cb1_bins)*cb1_bin_spacing+(i % cb1_bin_spacing) for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb1_input_lanes_per_output_lane = 16
            cb1_output_data_flags_words_per_bin = (cb1_output_words_per_bin * 4.0) / (32 if cb1_combine_data_flags else 16)  # This is 0.5 if combine_flags
            cb1_output_frame_flags_words_per_frame = 1
            bp_shuffle_bypass = True

            cb2_lane_map = range(16)
            cb2_bypass = True
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_data_flags_words_per_bin = cb1_output_data_flags_words_per_bin
            cb2_input_frame_flags_words_per_frame = cb1_output_frame_flags_words_per_frame
            cb2_input_bins = cb1_bins
            # cb2_lanes = ((0, 1), (2, 3))  #BS0 selects sublanes 0-1, BS1 selects sublanes 2-3
            # cb2_bins = cb1_bins
            # cb2_bin_spacing = 1
            # cb2_bin_select_map = [np.arange(cb2_bins)*cb2_bin_spacing for i in range(number_of_cb2_bin_sel)]
            cb2_output_words_per_bin = cb2_input_words_per_bin
            cb2_output_bins = cb2_bins
            cb2_input_lanes_per_output_lane = cb1_input_lanes_per_output_lane
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame

            crate_number = self.crate.crate_number or 0 if self.crate else 0
            stream_type = 1

            crate_shuffle_bypass = True
            cb3_lane_map = range(8)
            cb3_bypass = True
            cb3_output_words_per_bin = cb2_input_words_per_bin
            cb3_output_bins = cb2_input_bins
            cb3_output_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_output_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame

        elif mode == 'shuffle256':
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            cb1_bins = 64
            cb1_bin_spacing = 1024/cb1_bins
            cb1_bin_select_map = [np.arange(cb1_bins)*cb1_bin_spacing+i for i in range(number_of_cb1_bin_sel)]
            cb1_bin_select_map = [cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i)-1]] for i in range(16)]  # reorder cb1_bin_select_map so slot 0 gets cb1_bin_select_map[0], slot 1 gets cb1_bin_select_map[1] etc.
            cb1_output_words_per_bin = 16/4
            cb1_output_bins = cb1_bins

            # Backplane shuffle
            bp_shuffle_bypass = False

            # CB2 has 2 BIN_SEL
            # Each BS captures data from 16 input lanes and has 4 outputs.
            # Each output covers gathers data from 4 input lanes (sublanes 0-3).
            #   Output 0: Sublanes 0-3 = Input Lanes 0-3
            #   Output 1: Sublanes 0-3 = Input Lanes 4-7
            #   Output 2: Sublanes 0-3 = Input Lanes 8-11
            #   Output 3: Sublanes 0-3 = Input Lanes 12-15
            # In this config, we will bypass the crate_shuffle. We therefore want all outputs to output the same bins. So BS0 gets data from half of its sublanes, and BS1 gets data from the other half.
            # CB2 Output Lane 0: BS0.0: all 64 bins from sublanes 0-1 (Input lanes 0-1   = CH0-31)
            # CB2 Output Lane 1: BS0.1: all 64 bins from sublanes 0-1 (Input lanes 4-5   = CH64-95)
            # CB2 Output Lane 2: BS0.2: all 64 bins from sublanes 0-1 (Input lanes 8-9   = CH128-159)
            # CB2 Output Lane 3: BS0.3: all 64 bins from sublanes 0-1 (Input lanes 12-13 = CH192-223)
            # CB2 Output Lane 4: BS1.0: all 64 bins from sublanes 2-3 (Input lanes 2-3   = CH32-63)
            # CB2 Output Lane 5: BS1.1: all 64 bins from sublanes 2-3 (Input lanes 6-7   = CH96-127)
            # CB2 Output Lane 6: BS1.2: all 64 bins from sublanes 2-3 (Input lanes 10-11 = CH160-191)
            # CB2 Output Lane 7: BS1.3: all 64 bins from sublanes 2-3 (Input lanes 14-15 = CH224-255)
            # Crate shuffle is bypassed.
            # CB3 inputs are therefore identical to CB2 output
            # CB3 lane map selects data in the order: [BS0.0, BS1.0, BS0.1, BS1.1 ...]
            # CB3 remapped BIN_SEL inputs are
            #    CB3 Input Lane 0: 64 bins CH0-31
            #    CB3 Input Lane 1: 64 bins CH32-63
            #    CB3 Input Lane 2: 64 bins CH64-95
            #    CB3 Input Lane 3: 64 bins CH96-127
            #    CB3 Input Lane 4: 64 bins CH128-159
            #    CB3 Input Lane 5: 64 bins CH160-191
            #    CB3 Input Lane 6: 64 bins CH192-223
            #    CB3 Input Lane 7: 64 bins CH224-255
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 8 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 8 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 8 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 8 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 8 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 8 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 8 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 8 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)

            # CB2 ALIGN (JM changed cb2_sof_window_stop from 70 to 50 which the default value of CROSSBAR.SOF_WINDOW_STOP in shuffle_crossbar module. When 70, get errors when establishing connection with gpu nodes, reporting wrong packet size). JFC: CHanged to 55 during backplane shuffle debugging
            cb2_timeout_period = 0
            cb2_sof_window_stop = 55
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            cb2_bypass = False
            # CB2 BIN_SEL
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_data_flags_words_per_bin = 1
            cb2_input_frame_flags_words_per_frame = 1
            cb2_lanes = ((0, 1), (2, 3))  #BS0 selects sublanes 0-1, BS1 selects sublanes 2-3
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1 # 2 input lanes per output
            cb2_bins = 64
            cb2_bin_spacing = 1
            cb2_bin_select_map = [np.arange(cb2_bins)*cb2_bin_spacing for i in range(number_of_cb2_bin_sel)]
            cb2_output_words_per_bin = 2 * cb2_input_words_per_bin
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane
            cb2_output_bins = cb2_bins
            crate_number = self.crate.crate_number
            stream_type = 2
            # QSFP SHUFFLE
            crate_shuffle_bypass = True

            cb3_lane_map = [0, 4, 1, 5, 2, 6, 3, 7]  # Reorder to get data from lanes 0-1, 2-3, 4-5 ...
            cb3_bypass = False
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1 # 8 input lanes per bin sel output
            cb3_combine_data_flags = False # hardwired to False in crossbar 3
            cb3_bins = 8  # We merge data from 8 full bandwidth input lanes, so we select 1/8th of the bins on each output lane
            cb3_bin_spacing = 8  # use maximum possible number so we minimize FIFO usage
            cb3_bin_select_map = [np.arange(cb3_bins)*cb3_bin_spacing+i for i in range(number_of_cb3_bin_sel)]
            cb3_output_words_per_bin = cb3_input_words_per_bin * 8
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane / (2 if cb3_combine_data_flags else 1)
            cb3_output_frame_flags_words_per_frame = cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane

        elif mode == 'shuffle512':
            if not self.slot:
                raise RuntimeError('The slot number is unknown. Cannot route the appropriate bins to the target boards in the same crate')

            cb1_bypass = False
            cb1_four_bit = True
            cb1_combine_data_flags = 0
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel  # get data from channel group 0 - 3. Each channel group combines data from 4 chanelizers (in 4 bit mode)
            cb1_bins = 64
            cb1_bin_spacing = 1024/cb1_bins
            cb1_bin_select_map = [np.arange(cb1_bins)*cb1_bin_spacing+i for i in range(number_of_cb1_bin_sel)]
            cb1_bin_select_map = [cb1_bin_select_map[dsmap[get_dest_slot_for_src_lane(i)-1]] for i in range(16)]  # reorder cb1_bin_select_map so slot 0 gets cb1_bin_select_map[0], slot 1 gets cb1_bin_select_map[1] etc.

            # Output packet geometry
            cb1_output_words_per_bin = 16/4
            cb1_output_bins = cb1_bins

            bp_shuffle_bypass = False

            # In this config, we do not bypass the crate_shuffle. Half the bins are sent out, and we receive bins that are the same as those of the direct lanes.
            # We therefore want BS0 to get half the bins from all input lanes, and BS1 gets the other half of the bins also from all input lanes.
            # CB2 Output lanes are:
            #    CB2 Output Lane 0: BS0.0: 32 even bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 1: BS0.1: 32 even bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 2: BS0.2: 32 even bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 3: BS0.3: 32 even bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            #    CB2 Output Lane 4: BS1.0: 32 odd  bins from sublanes 0-3 (Input lanes 0-3   = CH0-63)
            #    CB2 Output Lane 5: BS1.1: 32 odd  bins from sublanes 0-3 (Input lanes 4-7   = CH64-127)
            #    CB2 Output Lane 6: BS1.2: 32 odd  bins from sublanes 0-3 (Input lanes 8-11  = CH128-191)
            #    CB2 Output Lane 7: BS1.3: 32 odd  bins from sublanes 0-3 (Input lanes 12-15 = CH192-255)
            # Crate shuffle is *not* bypassed
            # CB3 inputs are therefore:
            #    CB3 Input Lane 0: 32 even bins CH0-63
            #    CB3 Input Lane 1: 32 even bins CH64-127
            #    CB3 Input Lane 2: 32 even bins CH128-191
            #    CB3 Input Lane 3: 32 even bins CH192-255
            #    CB3 Input Lane 4: 32 even bins CH256-319
            #    CB3 Input Lane 5: 32 even bins CH320-383
            #    CB3 Input Lane 6: 32 even bins CH384-447
            #    CB3 Input Lane 7: 32 even bins CH448-511
            # CB3 lane map selects data in the input lane order: [0, 1, 2, 3 ... 7]
            # CB3 BIN sel inputs are therefore identical to CB3 inputs
            # CB3 outputs are:
            #    CB3 Output Lane 0: BS0.0: 4 bins (0,8...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 1: BS0.1: 4 bins (1,9...)  from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 2: BS0.2: 4 bins (2,10...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 3: BS0.3: 4 bins (3,11...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 4: BS1.0: 4 bins (4,12...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 5: BS1.1: 4 bins (5,13...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 6: BS1.2: 4 bins (6,14...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)
            #    CB3 Output Lane 7: BS1.3: 4 bins (7,15...) from sublanes 0-7 (Input lanes 0-7 =CH0-511)


            # CROSSBAR 2
            # ----------

            # CB2 ALIGN
            # cb2_timeout_period = 0

            crate_number = self.crate.crate_number
            stream_type = 3 # crossbar 3-level data

            # Input packet geometry

            cb2_input_data_flags_words_per_bin = 1
            cb2_input_words_per_bin = cb1_output_words_per_bin
            cb2_input_bins = cb1_output_bins
            cb2_input_frame_flags_words_per_frame = 1

            # Configuration
            cb2_sof_window_stop = 55
            cb2_bypass = False
            # CB2 REMAP
            cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            # CB@ BIN_SEL
            cb2_lanes = [(0, 3), (0, 3)] # Every output of both bin sels get data from all the 4 sublanes they get.
            cb2_input_lanes_per_output_lane = cb2_lanes[0][1] - cb2_lanes[0][0] + 1 # 4 input lanes per output
            cb2_bins = cb1_output_bins / number_of_cb2_bin_sel # 64/2 = 32
            cb2_bin_spacing = number_of_cb2_bin_sel # 2
            cb2_bin_select_map = [np.arange(cb2_bins)*cb2_bin_spacing + (i ^ (crate_number & 1)) for i in range(number_of_cb2_bin_sel)]
            cb2_combine_data_flags = True # hardwired to True in crossbar 2

            # Output packet geometry

            cb2_output_words_per_bin = cb2_input_words_per_bin * cb2_input_lanes_per_output_lane
            cb2_output_bins = cb2_bins
            cb2_output_data_flags_words_per_bin = cb2_input_data_flags_words_per_bin * cb2_input_lanes_per_output_lane / (2 if cb2_combine_data_flags else 1)
            cb2_output_frame_flags_words_per_frame = cb2_input_frame_flags_words_per_frame * cb2_input_lanes_per_output_lane
            print("cb2_output frame flags words=%i, input frame flags words=%i, input_lanes=%i" % (cb2_output_frame_flags_words_per_frame,cb2_input_frame_flags_words_per_frame, cb2_input_lanes_per_output_lane))


            # QSFP SHUFFLE
            # ------------

            crate_shuffle_bypass = False
            # crate_shuffle_bypass = True #***debug

            # CROSSBAR 3
            # ----------

            # Input packet geometry
            cb3_input_words_per_bin = cb2_output_words_per_bin
            cb3_input_data_flags_words_per_bin = cb2_output_data_flags_words_per_bin
            cb3_input_frame_flags_words_per_frame = cb2_output_frame_flags_words_per_frame
            cb3_input_bins = cb2_output_bins

            # Configuration
            cb3_lane_map = [4,5,6,7,0,1,2,3] if (crate_number & 1) else [0,1,2,3,4,5,6,7] #JM: modified this line to have consistent input ordering between pairs of crates. Before this change this line was just range(8)
            cb3_bypass = False
            cb3_lanes = [(0, 7)] * number_of_cb3_bin_sel
            cb3_input_lanes_per_output_lane = cb3_lanes[0][1] - cb3_lanes[0][0] + 1 # 8 input lanes per bin sel output
            cb3_bins = cb2_output_bins / number_of_cb3_bin_sel # 32/8 = 4
            cb3_bin_spacing = number_of_cb3_bin_sel # = 8
            cb3_bin_select_map = [np.arange(cb3_bins)*cb3_bin_spacing+i for i in range(number_of_cb3_bin_sel)]
            cb3_combine_data_flags = False # hardwired to False in crossbar 3

            # Output packet geometry
            cb3_output_words_per_bin = cb3_input_words_per_bin * cb3_input_lanes_per_output_lane
            cb3_output_bins = cb3_bins
            cb3_output_data_flags_words_per_bin = cb3_input_data_flags_words_per_bin * cb3_input_lanes_per_output_lane / (2 if cb3_combine_data_flags else 1)
            cb3_output_frame_flags_words_per_frame = cb3_input_frame_flags_words_per_frame * cb3_input_lanes_per_output_lane


        elif mode == 'corr16':
            number_of_cb1_bin_sel = 8
            cb1_bypass = False
            cb1_four_bit = True
            # BS0 grabs data from FIFO 0-1 (lanes 0-7), BS1 from FIFO 2-3
            # (lanes 8-15), repeat... We capture 2 words per bin in 2 clocks,
            # bins are separated by 2 clocks, so we have time to empty the
            # FIFO
            cb1_lanes = [(0, 3)] * number_of_cb1_bin_sel
            cb1_bins = 128
            cb1_bin_spacing = 1024/cb1_bins  # = 8
            cb1_combine_data_flags = 1
            cb1_bin_select_map = [np.arange(cb1_bins)*cb1_bin_spacing+(i % cb1_bin_spacing) for i in range(number_of_cb1_bin_sel)]
            cb1_output_words_per_bin = 4
            cb1_output_bins = cb1_bins
            cb2_bypass = True
            cb3_bypass = True
            cb3_output_bins = cb1_bins
            cb3_output_words_per_bin = 0 # To be updated
            cb3_output_data_flags_words_per_bin = 0 # To be updated
            cb3_output_frame_flags_words_per_frame = 0 # To be updated
            stream_type = 0  # not used, as the shuffled packets are correlated never get out of the FPGA
            crate_number =  0  # idem


        elif mode is None:  # Manual config
            cb1_four_bit = True

            # cb1_bypass = False
            cb1_bin_spacing = 1024/cb1_bins
            cb1_bin_select_map = [(np.arange(cb1_bins)*cb1_bin_spacing+i) % 1024 for i in range(number_of_cb1_bin_sel)]

            crate_shuffle_bypass=1

            crate_number = self.crate.crate_number if self.crate else 0
            stream_type = 4

            cb2_input_bins = cb1_bins
            if bp_shuffle_bypass and self.slot is not None:
                cb2_lane_map = self.CROSSBAR2.compute_bp_shuffle_lane_map()
            else:
                cb2_lane_map = range(16)

            cb2_input_frame_flags_words_per_frame = 1
            cb2_input_data_flags_words_per_bin = 1


            cb3_bypass = True
            cb3_lane_map = range(8)

            cb1_output_words_per_bin = cb1_lanes[0][1]-cb1_lanes[0][0]+1
            cb1_output_bins = cb1_bins

        else:
            raise ValueError('Unknown mode')



        # cb2_payload_size = header_size + packet_flags_size + frames_per_packet * (cb2_input_words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4



        self._logger.debug('%r: Configuring crossbars 1 & 2 with frames_per_packet=%i, cb1_lanes=%s, cb1_bins=%i, cb2_lanes=%s, cb2_bins=%i, cb2_bypass=%s, bp_shuffle_bypass=%s' % (self, frames_per_packet, cb1_lanes, cb1_bins, cb2_lanes, cb2_bins, bool(cb2_bypass), bool(bp_shuffle_bypass)))

        # Put everything in reset
        self.set_ant_reset(1)
        self.set_corr_reset(1)

        #slot_number = self.slot - 1 if self.slot is not None else 0
        slot_number = self.slot - 1 if self.slot else 0 #SC 03/17/2019 changed for individual iceboard
        #-------------------------
        # Configure CROSSBAR 1
        #-------------------------
        # Select the bins so slot 0 receives bins 0-63, slot 1 has 64-127 ... slot 15 has 960-1023
        for (cb1_output_lane, bs) in enumerate(cb1):
            bs.BYPASS = cb1_bypass
            bs.COMBINE_DATA_FLAGS = cb1_combine_data_flags

            bs.GROUP_FRAMES = frames_per_packet
            bs.STREAM_ID = (stream_type << 8) | (crate_number << 4) | slot_number
            bs.FOUR_BITS = cb1_four_bit
            bs.FIRST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][0]
            bs.LAST_FIFO_NUMBER = cb1_lanes[cb1_output_lane][1]
            # bs.NUMBER_OF_LANES = cb1_lanes
            bs.select_bins(cb1_bin_select_map[cb1_output_lane])

        #-------------------------
        # Configure BP_SHUFFLE
        #-------------------------
        if self.BP_SHUFFLE:
            self.BP_SHUFFLE.BYPASS_PCB_SHUFFLE = bp_shuffle_bypass
            #-------------------------
            # Configure CRATE_SHUFFLE
            #-------------------------
            self.BP_SHUFFLE.BYPASS_QSFP_SHUFFLE = crate_shuffle_bypass
        elif not bp_shuffle_bypass:
            raise RuntimeError("The FPGA firmware must have a BP_SHUFFLE in the '%s' operational mode", mode)
        #-------------------------
        # Configure CROSSBAR 2
        #-------------------------
        if self.CROSSBAR2:
            self.CROSSBAR2.set_lane_map(cb2_lane_map)
            if cb2_timeout_period is not None:
                self.CROSSBAR2.TIMEOUT_PERIOD = cb2_timeout_period
            if cb2_sof_window_stop is not None:
                self.CROSSBAR2.SOF_WINDOW_STOP = cb2_sof_window_stop
            for (cb2_bin_sel, bs) in enumerate(cb2):
                bs.BYPASS = bool(cb2_bypass)
                if not cb2_bypass:
                    bs.STREAM_ID = (stream_type << 8) | (crate_number << 4) | slot_number
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    bs.NUMBER_OF_BINS_PER_FRAME = cb2_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb2_input_words_per_bin
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = cb2_input_data_flags_words_per_bin
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb2_input_frame_flags_words_per_frame
                    bs.FIRST_LANE = cb2_lanes[cb2_bin_sel][0]
                    bs.LAST_LANE = cb2_lanes[cb2_bin_sel][1]
                    bs.select_bins(cb2_bin_select_map[cb2_bin_sel])
        elif not cb2_bypass:
            raise RuntimeError("The FPGA firmware must have a CROSSBAR2 in the '%s' operational mode", mode)


        #-------------------------
        # Configure CROSSBAR 3
        #-------------------------
        if self.CROSSBAR3:
            self.CROSSBAR3.set_lane_map(cb3_lane_map)
            for (cb3_bin_sel, bs) in enumerate(cb3):
                bs.BYPASS = bool(cb3_bypass)
                if not cb3_bypass:
                    bs.STREAM_ID = (stream_type << 8) | (crate_number << 4) | slot_number  # The stream ID at the output of CB2 will be 0xSL (S=slot-1, L=lane)
                    bs.NUMBER_OF_FRAMES_PER_PACKET = frames_per_packet
                    bs.NUMBER_OF_DATA_FLAGS_WORDS_PER_BIN = cb3_input_data_flags_words_per_bin
                    bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME = cb3_input_frame_flags_words_per_frame
                    #print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.FIRST_LANE = cb3_lanes[cb3_bin_sel][0]
                    bs.LAST_LANE = cb3_lanes[cb3_bin_sel][1]
                    #print('CB3: FFWPF=%i' % bs.NUMBER_OF_FRAME_FLAGS_WORDS_PER_FRAME)
                    bs.NUMBER_OF_BINS_PER_FRAME = cb3_input_bins
                    bs.NUMBER_OF_WORDS_PER_BIN = cb3_input_words_per_bin
                    bs.select_bins(cb3_bin_select_map[cb3_bin_sel])

                    #bs.SEND_FLAGS = 0  # JFC debug. Does not affect data.
            #print("cb3 input frame flags words=%i" %cb3_input_frame_flags_words_per_frame)
        elif not cb3_bypass:
            raise RuntimeError("The FPGA firmware must have a CROSSBAR3 in the '%s' operational mode", mode)

        # words_per_bin = cb1_lanes / 4
        # # cb1_minimum_bin_spacing = 16
        # # cb2_minimum_bin_spacing = 8


        # # for gtx in gpu_links.CHANNEL:
        # #     gtx.LOOPBACK = bp_shuffle_bypass

        def print_packet_size(crossbar_name, frames_per_packet, bins, data_words_per_bin, data_flags_words_per_bin, frame_flags_words_per_frame):
            header_words_per_packet = 4
            packet_flags_words_per_packet = 1
            payload_size = (header_words_per_packet  + frames_per_packet * ((data_words_per_bin + data_flags_words_per_bin) * bins + frame_flags_words_per_frame) + packet_flags_words_per_packet) * 4
            ethernet_packet_overhead_bytes = 42
            ethernet_packet_size = (ethernet_packet_overhead_bytes + payload_size + 7) // 8 * 8
            # eth_data_rate = 156.25e6 * 66 * 32/33
            # bp_data_rate = 156.25e6* 50 * 32/33
            packet_rate = 800e6/2048/frames_per_packet
            ethernet_data_rate = (packet_rate * ethernet_packet_size) * 8
            self._logger.info('%r: %s Ethernet packet size: %i bytes, %0.1f Gbit/s (%i frames_per_packet, %i bins, %i data words/bin, %g data flags_words/bin, %i frame_flags_words/frame)' % (self, crossbar_name, ethernet_packet_size, ethernet_data_rate / 1e9,  frames_per_packet, bins, data_words_per_bin, data_flags_words_per_bin, frame_flags_words_per_frame))
            # self._logger.info('%r: %s config: frames_per_packet=%i, cb1_lanes=%s, cb1_bypass=%s, cb1_combine=%s, cb1_bins=%i, cb1_words_per_bin=%i' % (self, frames_per_packet, cb1_lanes, bool(cb1_bypass), bool(cb1_combine_data_flags), cb1_bins, cb1_output_words_per_bin ))
            # self._logger.debug('%r: CROSSBAR1 output packets payload = %i bytes (%i words)' % (self, cb1_payload_size, (cb1_payload_size+3)//4))

        print_packet_size('CROSSBAR3',
                          frames_per_packet=frames_per_packet,
                          bins=cb3_output_bins,
                          data_words_per_bin=cb3_output_words_per_bin,
                          data_flags_words_per_bin=cb3_output_data_flags_words_per_bin,
                          frame_flags_words_per_frame=cb3_output_frame_flags_words_per_frame)

        # cb2_payload_size = header_size + packet_flags_size + frames_per_packet * (words_per_bin * cb2_bins* cb2_lanes + 1*cb2_bins*cb2_lanes/2 + cb2_lanes) * 4
        # cb2_eth_packet_size = (cb2_payload_size + eth_overhead + 7) // 8 * 8
        # cb2_eth_data_rate = cb2_eth_packet_size * packet_rate * 8
        # cb2_fifo_load = cb2_bins * words_per_bin * frames_per_packet - ( cb2_bins * words_per_bin* cb2_minimum_bin_spacing* frames_per_packet / 16)
        # self._logger.info('%r: CROSSBAR2 output: payload = %i bytes' % (self, cb2_payload_size))
        # self._logger.info('%r: CROSSBAR2 peak FIFO load per frame: %i (Max. 16), Words per frame: %i (max %i)' % (self, cb2_fifo_load,cb2_payload_size/frames_per_packet, 512*bp_data_rate/32/200e6))

        # if cb2_bypass:
        #     self._logger.info('%r: GPU link (CROSSBAR1 data): UDP Payload = %i bytes, Ethernet packets = %i bytes, data rate = %0.2f Gbps (%0.2f%%)' % (self, cb1_payload_size, cb1_eth_packet_size, cb1_eth_data_rate/1e9, cb1_eth_data_rate/eth_data_rate*100))
        # else:
        #     self._logger.info('%r: GPU Link (CROSSBAR2 data): UDP Payload = %i bytes, Ethernet packets = %i bytes, data rate = %0.2f Gbps (%0.2f%%)' % (self, cb2_payload_size, cb2_eth_packet_size, cb2_eth_data_rate/1e9, cb2_eth_data_rate/eth_data_rate*100))

        self.set_corr_reset(0)
        self.set_ant_reset(0)




    def get_shuffle_status(self, cb1_bin_sel_overflow_reset=False):
        def cb1_gen(self):
            yield '-----------------------------------------------------'
            cb1 = self.CROSSBAR
            yield ' CROSSBAR 1 '
            yield '   * CROSSBAR1 Configuration state *'
            yield '   Chan  Corr  Align Align Align '
            yield '   RST   RST    RST  Start Stop '
            yield '   ----- ----- ----- ----- -----'
            yield '   %5s %5s %5s %5i %5i' % (
                bool(self.GPIO.ANT_RESET),
                bool(self.GPIO.CORR_RESET),
                bool(cb1.ALIGN_RESET),
                cb1.SOF_WINDOW_START,
                cb1.SOF_WINDOW_STOP)
            yield ''

            yield '   * CROSSBAR1 Status *'
            old_bin_ctr = cb1.CB1_BIN_CTR
            time.sleep(0.001)  # Wait 1 ms = approx 500 frames
            new_bin_ctr = cb1.CB1_BIN_CTR

            yield '    Bin'
            yield '    Ctr'
            yield '   -----'
            yield '   %-5s' % (
                (' OK ','Stuck')[old_bin_ctr==new_bin_ctr]
                )
            yield ''

            yield '   * Bin Selectors Configuration state *'
            yield '   Bin  Stream Inputs Words/ Frames/ Send   Data  Reset Reset'
            yield '   Sel#   ID   Lanes  Frame  Packet  Flags  Width  Ctrl State'
            yield '   ---- ------ ------ ------ ------- -----  ----- ----- -----'
            for (i, bs) in enumerate(cb1):
                messages = ''
                if not bs.FOUR_BITS and not bs.EIGHT_BIT_SUPPORT:
                    messages += '! Eight Bit mode is not supported by this firmware!'
                yield '   %02i:  0x%03X  %2i/%2i %7i %6i %05s  %5i %5s %5s  Messages:%s' % (
                    i,
                    bs.STREAM_ID,
                    bs.NUMBER_OF_LANES,
                    self.NUMBER_OF_CROSSBAR_INPUTS,
                    bs.NUMBER_OF_SELECTED_WORDS,
                    bs.GROUP_FRAMES,
                    bool(bs.SEND_FLAGS),
                    (8,4)[bs.FOUR_BITS],
                    bool(bs.RESET),
                    bool(bs.IS_RESET),
                    messages)

            yield ''
            yield '   * Bin Selectors Status *'
            yield '   Bin    Align  Data Data  Frame Data  Global Timestamp  Rst'
            yield '   Sel#   FIFO   FIFO Flags Flags FIFO  Frame     Ctr    State'
            yield '          ovfl   ovfl FIFO  FIFO  Empty  Ctr'
            yield '                      ovfl  ovfl'
            yield '   -----  ----- ----- ----- ----- ----- ------ --------- -----'
            for (i, bs) in enumerate(cb1):
                cb1.LANE_MONITOR_SEL = 6  # Fifo Overflow sticky
                cb1.LANE_MONITOR_RESET = 1
                cb1.LANE_MONITOR_RESET = 0
                lane_mon = cb1.CB1_LANE_MONITOR
                align_fifo_overflow = bool(lane_mon & (1<<i))  # Sticky bit

                old_global_frame_ctr = bs.IN_FRAME_CTR
                old_timestamp_ctr = bs.TIMESTAMP_CTR
                time.sleep(0.001)
                new_global_frame_ctr = bs.IN_FRAME_CTR
                new_timestamp_ctr = bs.TIMESTAMP_CTR

                if cb1_bin_sel_overflow_reset:
                    bs.OVERFLOW_RESET=1
                    bs.OVERFLOW_RESET=0

                yield '   %04i:  %05s %5s %5s %5s %5s %5s %9s %5s' % (
                    i,
                    (' ok ', 'OVFL!')[align_fifo_overflow],
                    bool(bs.FIFO_OVERFLOW),
                    bool(bs.DATA_FLAGS_OVERFLOW),
                    bool(bs.FRAME_FIFO_OVERFLOW),
                    bool(bs.FIFO_EMPTY),
                    (' ok ','stuck')[new_global_frame_ctr == old_global_frame_ctr],
                    (' ok ','stuck')[new_timestamp_ctr == old_timestamp_ctr],
                    bool(bs.IS_RESET)
                    )

        def bp_gen(self):
            yield '-----------------------------------------------------'
            bp = self.BP_SHUFFLE
            yield ' BP_SHUFFLE '
            yield '   * BP_SHUFFLE Configuration state *'
            yield '   Core  Bypass  TX   '
            yield '   RST           Test '
            yield '   ----- ------ ----- '
            yield '   %5s %6s %5s' % (
                bool(bp.CORE_RESET),
                bool(bp.BYPASS),
                bool(bp.TX_TEST_ENABLE))
            yield ''

            yield '   * BP_SHUFFLE Status *'
            old_test_ctr = bp.TEST_CTR
            time.sleep(0.001)  # Wait 1 ms = approx 500 frames
            new_test_ctr = bp.TEST_CTR

            yield '    BP   RST   QPLL  QPLL  Test'
            yield '    RST  Done  RST   Lock  Ctr'
            yield '   ----- ----- ----- ----- -----'
            yield '   %5s %5s %5s %5s %5s' % (
                bool(bp.RESET_MON),
                bool(bp.RESET_DONE),
                bool(bp.QPLL_RESET_MON),
                ''.join('%i'%q.QPLL_LOCK for q in bp.qpll),
                (' ok ','stuck')[new_test_ctr == old_test_ctr],
                )
            yield ''

            yield '   * BP_SHUFFLE Lane Status *'
            yield '   Lane GTX  Err    FIFO'
            yield '    #    #   ctr    Ovfl'
            yield '   ---- ---- ------ -----'
            for i in range(bp.NUMBER_OF_LINKS+1):
                gtx = bp.gtx[i-1] if i>0 else None
                bp.LANE_SEL = i
                yield '   %4i %04s:%5i %5s' % (
                    i,
                    i-1 if gtx else 'N/A',
                    bp.RX_ERROR_CTR,
                    bool(bp.FIFO_OVERFLOW)
                    )


        for x in cb1_gen(self):
            print x
        for x in bp_gen(self):
            print x


    @async
    def get_status(self):
        """
        (`async` method) Return status information on the board, and mezzanines, including voltages current, power consumption, temperatures etc.

        Parameters:
            None


        Returns:
            dict: An OrderedDict containing the status information in the format ``{metric:value, ...}`` where both ``metric`` and ``value`` are strings.
        """

        info = OrderedDict()
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)


        ####################################
        # Motherboard temperatures
        ####################################

        mb_temp_sensors = [
            ('MB FPGA Die Temp', 'FPGA DIE', self.TEMPERATURE_SENSOR.MB_FPGA_DIE),
            ('MB FPGA Temp'    , 'FPGA',     self.TEMPERATURE_SENSOR.MB_FPGA    ),
            ('MB ARM Temp'     , 'ARM',      self.TEMPERATURE_SENSOR.MB_ARM     ),
            ('MB PHY Temp'     , 'PHY',      self.TEMPERATURE_SENSOR.MB_PHY     ),
            ('MB POW Temp'     , 'Switcher', self.TEMPERATURE_SENSOR.MB_POWER   )]

        for display_name, sensor, sensor_name in mb_temp_sensors:
            value = yield self.get_motherboard_temperature.async(sensor_name)
            info[display_name] = '%0.1fC' % value
            metrics.add('fpga_motherboard_temp', value,  sensor=sensor)

        ####################################
        # Motherboard voltages and currents
        ####################################

        mb_power_sensors = [
            ('MB VCC12V'    , 'VCC12V'    , self.RAIL.MB_VCC12V0   , True),
            ('MB VCC3V3'    , 'VCC3V3'    , self.RAIL.MB_VCC3V3    , True),
            ('MB VADJ'      , 'VADJ'      , self.RAIL.MB_VADJ      , False), # VADJ is normally powered from VCC5V0
            ('MB VCC5V5'    , 'VCC5V5'    , self.RAIL.MB_VCC5V5    , True),
            ('MB VCC1V0'    , 'VCC1V0'    , self.RAIL.MB_VCC1V0    , False),
            ('MB VCC1V0 GTX', 'VCC1V0 GTX', self.RAIL.MB_VCC1V0_GTX, False),
            ('MB VCC1V2'    , 'VCC1V2'    , self.RAIL.MB_VCC1V2    , False),
            ('MB VCC1V5'    , 'VCC1V5'    , self.RAIL.MB_VCC1V5    , False),
            ('MB VCC1V8'    , 'VCC1V8'    , self.RAIL.MB_VCC1V8    , False)]

        total_power = 0
        for display_name, sensor, tuber_sensor_name, add_to_total_power in mb_power_sensors:
            voltage = yield self.get_motherboard_voltage.async(tuber_sensor_name)
            current = yield self.get_motherboard_current.async(tuber_sensor_name)
            info[display_name] = '%0.1fV@%0.3fA' % (voltage, current)
            metrics.add('fpga_motherboard_voltage', value=voltage, sensor=sensor)
            metrics.add('fpga_motherboard_current', value=current, sensor=sensor)
            if add_to_total_power:
                total_power += voltage * current


        ####################################
        # Mezzanines voltages and currents
        ####################################

        mezz_power_sensors = [
            ('Mezz %i VCC12V'    , 'VCC12V'    , self.RAIL.MEZZ_VCC12V0),
            ('Mezz %i VCC3V3'    , 'VCC3V3'    , self.RAIL.MEZZ_VCC3V3),
            ('Mezz %i VADJ'      , 'VADJ'      , self.RAIL.MEZZ_VADJ)]

        for mezz in [1, 2]:
            for display_name, sensor, sensor_name in mezz_power_sensors:
                voltage = yield self.get_mezzanine_voltage.async(sensor_name, mezz)
                current = yield self.get_mezzanine_current.async(sensor_name, mezz)
                info[display_name % mezz] = '%0.1fV@%0.3fA' % (voltage, current)
                metrics.add('fpga_mezzanine_voltage', value=voltage, sensor=sensor, mezzanine=mezz)
                metrics.add('fpga_mezzanine_current', value=current, sensor=sensor, mezzanine=mezz)

        info['MB Total power'] = '%0.1fW' % total_power
        metrics.add('fpga_motherboard_power', value=total_power)

        ####################################
        # Motherboard QSFPs present
        ####################################

        for qsfp in [1, 2]:
                is_present = yield self.is_qsfp_present.async(qsfp)
                metrics.add('fpga_motherboard_qsfp_present', value=is_present, qsfp=qsfp)

        # is_voltage_nominal
        # sysmon?
        # QSFP voltage, temp, signal
        async_return((info, metrics))

    @async
    def get_bp_shuffle_metrics(self, reset=True):
        if not self.is_open():
            async_return(Metrics())
        try:
            yield self.clear_fpga_udp_errors.async()
            # yield self.check_command_count.async(reset=True)
            metrics = yield self.BP_SHUFFLE.get_metrics.async(reset=reset)
        except IOError as e:
            self.logger.error('%r: Error getting FPGA backplane link metrics. Error is %r' % (self, e))
            metrics = Metrics()
        async_return(metrics)


    @async
    def get_channelizer_metrics(self, reset=True):
        metrics = Metrics(
            type='GAUGE',
            slot=(self.slot or 0) - 1,
            id=self.get_string_id(),
            crate_id=self.crate.get_string_id() if self.crate else None,
            crate_number=self.crate.crate_number if self.crate else None)

        if not self.is_open():
            async_return(metrics)
        try:
            # yield self.check_command_count.async(reset=True)
            yield self.clear_fpga_udp_errors.async()
            for i, ant  in self.ANT.items():
                metrics.add('fpga_fft_overflow_count', value=ant.FFT.OVERFLOW_COUNT, chan=i)
                metrics.add('fpga_scaler_overflow_count', value=ant.SCALER.STATS_SCALER_OVERFLOWS, chan=i)
                metrics.add('fpga_adc_overflow_count', value=ant.SCALER.STATS_ADC_OVERFLOWS, chan=i)
                if reset:
                    ant.FFT.reset_fft_overflow_count()
        except IOError as e:
            self.logger.error('%r: Error getting FPGA channelizer metrics. Error is %r' % (self, e))
        async_return(metrics)

    @async
    def get_crossbar_metrics(self, reset=True):
        metrics = Metrics()
        if not self.is_open():
            async_return(metrics)
        try:
            # yield self.check_command_count.async(reset=True)
            yield self.clear_fpga_udp_errors.async()
            metrics += yield self.CROSSBAR.get_metrics.async(reset=reset)
            metrics += yield self.CROSSBAR2.get_metrics.async(reset=reset)
            metrics += yield self.CROSSBAR3.get_metrics.async(reset=reset)
        except IOError as e:
            self.logger.error('%r: Error getting FPGA crossbar metrics. Error is %r' % (self, e))
        except Exception as e:
            self.logger.error('%r: Unhandled error while  getting FPGA crossbar metrics. Error is %r' % (self, e))
        async_return(metrics)

    @async
    def get_metrics(self):
        """ Get the Iceboard hardware monitoring information.

        Returns:
            a :cls:`Metrics` object.
        """
        try:
            _, metrics = yield self.get_status.async()
        except Exception as e:
            self.logger.error('%r: Error getting FPGA hardware metrics. Error is %r' % (self, e))
            metrics = Metrics()
        async_return(metrics)


    @async
    def get_backplane_metrics(self):
        """ Get the backplane hardware monitoring information, as accessed from this Iceboard.

        Returns:
            A :cls:`Metrics` object.

        Note: an 'info' dict is also created but is not returned as the metrics is sufficient for now.

        """

        info = OrderedDict()
        crate_number = self.crate.crate_number if self.crate else None
        crate_id = self.crate.get_string_id() if self.crate else None
        metrics = Metrics(crate_number=crate_number, crate_id=crate_id, type='GAUGE')

        if (yield self.is_backplane_present.async()):
            try:
                ####################################
                # Backplane temperatures
                ####################################

                bp_temp_sensors = [
                    ('BP Slot1 Temp', 'Slot1', self.TEMPERATURE_SENSOR.BP_SLOT1),
                    ('BP Slot16 Temp', 'Slot16', self.TEMPERATURE_SENSOR.BP_SLOT16)]


                for display_name, sensor, sensor_name in bp_temp_sensors:
                    value = yield self.get_backplane_temperature.async(sensor_name)
                    info[display_name] = '%0.1fC' % value
                    metrics.add('fpga_backplane_temp', value, sensor=sensor)

                ####################################
                # Backplane voltages and currents
                ####################################

                voltage = yield self.get_backplane_voltage.async()
                current = yield self.get_backplane_current.async()
                power = yield self.get_backplane_power.async()
                info['BP VCC3V3'] = '%0.1fV@%0.3fA' % (voltage, current)
                info['BP power'] = '%0.1fW' % power
                metrics.add('fpga_backplane_voltage', value=voltage)
                metrics.add('fpga_backplane_current', value=current)
                metrics.add('fpga_backplane_power', value=power)

                ####################################
                # Fan tray
                ####################################

                metrics.add('fpga_backplane_fantray_tachometer', value=(yield self.get_fantray_tachometer.async()))
                metrics.add('fpga_backplane_fantray_duty_cycle', value=(yield self.get_fantray_duty_cycle.async())/255.)

                ####################################
                # Backplane QSFPs present
                ####################################
                for slot in range(1,17):
                    is_present = yield self.is_bp_qsfp_present.async(slot)
                    metrics.add('fpga_backplane_qsfp_present',value=is_present, slot=(slot-1))

            except Exception as e:
                self.logger.error('%r: error getting backplane metrics: error is %r' % (self, e))
        async_return(metrics)

        # backplane QSFP voltage, temp, signal-level



    def get_string_id(self):
        """
        Return a string that uniquely represents the board. It is composed of the model and serial
        number, or, if the serial numbe ris unknown, the hostname of the board.

        Parameters:
            None

        Returns:
            string

        """
        if self.serial and self.part_number:
            return '%s_SN%s' % (self.part_number, self.serial)
        else:
            return self.hostname

    def __repr__(self):
        return "chFPGA%s" % (self.get_id(),) # watch out, get_id() returns a tuple...

    def get_id(self, lane=None):
        """ Returns a tuple representing a unique IceBoard ID, using numeric values whenever possible.

        Parameters:

            lane (int): caller-provided lane number to be appended to the returned tuple.

        Returns:
             - (int, int): (numeric_crate_number, zero_based_slot_number) if the board
               is in a crate for which a crate number was assigned
             - (str, int): (string_crate_id, zero_based_slot_number) Identify the
               crate with model and serial number if there is a crate but no
               crate number is specified
             - (str): (iceboard_id) If the board is not in a crate or the slot
               number is unknown, use the the iceboard model and serial number

        """
        if not self.crate or self.slot is None:
            return (self.get_string_id(), ) if lane is None else (self.get_string_id(), lane)
        else:
            return self.get_crate_id(self.slot - 1) + (tuple() if lane is None else (lane,) )


    def get_crate_id(self, slot=None):
        """ Return the crate ID tuple optionally appended by the specified slot number.slot
        Parameters:

            slot (int): slot number to append to the tuple. Should be zero-based.slot

        Returns:

            (crate_id, ) if `slot` is `None`, else (crate_id, slot).
            ``crate_id`` is the crate number if it exists, otherwise it is a
            string that uniquely defined the crate.
        """
        return self.crate.get_id(slot=slot)

    #########################################################################
    #
    #   FIRMARE CORRELATOR
    #
    #########################################################################
    def start_corr_capture(self,  integration_period=1.0, capture_period=None, corr_to_use=None, verbose=1):
        """
        Instructs chFPGA to starts integrating and capturing the correlator
        outputs at the specified period. The captures data is sent over the
        Ethernet interface.

        The capture period can be optionnaly specified independently from the
        integration period. If not specified, it is equal to the integration
        period.

        This function does not receive the frames from the ethernet port. This
        has to be done separately.

        corr_to_use -> if not None, is a list specifying which to correlators to use

        History:
            2012-10-02 JFC: Created
            2013-03-25 KMB
        """

        if not self._last_init_time:
            self._logger.warning('%r: The system is not initialized. This might not work.' % self)

        if capture_period is None:
            capture_period = integration_period

        capture_period_in_frames = int(capture_period*1.0/self.FRAME_PERIOD)
        integration_period_in_frames = int(integration_period*1.0/self.FRAME_PERIOD)

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
            self._logger.debug(
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
            self._logger.debug('%r: Disabling correlator %i' % (self, corr.instance_number))
            corr = self.CORR[corr_num]
            corr.ACC.RESET = 1
        self.set_corr_reset(0)
        self.set_ant_reset(0)
        #self.sync()

    def start_correlator(self, integration_period=16384, autocorr_only=False, correlators=None, bandwidth_limit=0.5e9, verbose=1):
        """
        (Re)starts the correlator with the specified integration time.

        Parameters:

            integration_period (32-bit int): Number of frames to integrate
               before sending the correlated products. Lower integration
               period increase the frequency at which correlated frames are
               sent and increase the require bandwidth. Longer integration
               periods will procuce larger accumulated products that will
               saturate if they exceed the accumulator limits (from -131072 to
               131071 for each if the real and imaginary component).

            autocorr_only (bool): When 'True', the correlator will only send
               the autocorrelation products, which will reduce bandwidth
               requirement (12% of the full bandwidth) and will allow shorter
               integration periods. Note that the imaginary parts are always
               zero but are sent anyways to keep the frame format identical
               despite the waste of bandwidth.

            correlators (list of int): List of correlator cores to enable. All
               other cores will be disabled. Default is None, which means all
               correlators will be enabled. Each correlator core process the
               frequency bins selected with its corresponding bin selector.
               Using a smaller number of cores will process less frequency
               bins but will proportionnally usee less data bandwidth.

            bandwidth_limit (float): Maximum acceptable data bandwidth that
               the correlator can produce, in bits/s. Default is 0.5 Gbps. If the correlator
               parameters are to make the data exceed this bandwidth, an
               exception will be raised, with a message that describe
               alternate settings. In this case, no changes are made to the
               correlator operation.

        Returns:
            None
        """
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        self.CORR.start_correlator(integration_period=integration_period,
                                   autocorr_only=autocorr_only,
                                   correlators=correlators,
                                   bandwidth_limit=bandwidth_limit,
                                   verbose=verbose)

    def stop_correlator(self):
        if not self.CORR:
            raise RuntimeError('The FPGA firmware does not contain a correlator core')

        self.CORR.stop_correlator()

    def compute_corr_output(self, data, integration_period=16384):
        """
        Compute the expected correlator output given the channelizer output :paramref:`databb`.

        Parameters:
            databb (float): some value
            data (ndarray): data[channel, bin] = complex
        Returns:
            array(bins, i, j) = complex
        """
        corr = data.T[:,None,:]* data.T[:,:,None].conj()*integration_period
        corr = np.clip(corr.real, -131072, 131071) + 1j*np.clip(corr.imag, -131072, 131071)
        i, j = np.tril_indices_from(corr[0],-1) # indices of the lower triangle excluding the diagonal
        corr[:, j, i] = corr[:, i, j].conj() # reapply upper triangle from lower, because saturation is not the same for negative and positive imaginary values
        return corr

    def test_correlator_output(self, data, integration_period=32768, verbose=0):
        """ Set the channelizer outputs to :paramref:`data` and check the correlator output.

        :param int data: super!

        Parameters:
            dataaa (float): some value
            datax (ndarray): Data that should appear at the channelizer
                output, indexed as data[channel, bin] = complex_value. channel
                ranges from 0 to 15, bin from 0 to 1023. The complex value has the
                ranged of a signed (4+4) bit, meaning that the real and imaginary
                part can range from -8 to 7.

        """
        r = self.get_data_receiver(verbose=0)
        self.set_channelizer_outputs(data)
        self.start_correlator(integration_period=integration_period, verbose=verbose)
        self.sync()
        p = self.compute_corr_output(data, integration_period=integration_period)
        f = r.read_corr_frames(flush=False, complete_set=True, max_trials=100, verbose = verbose)
        return np.all(p==f), p, f

    def test_correlator(self, test_name='rand_complex', integration_period=8192, trials=100, verbose=0):
        if test_name=='rand_complex':
            for data_set_number in xrange(trials):
                print 'Trial #%i' % data_set_number
                data=np.floor(np.random.rand(16,1024)*4-2) + 1j*np.floor(np.random.rand(16,1024)*4-2)
                trial = 0
                while True:
                    match, p, f = self.test_correlator_output(data=data, integration_period=integration_period)
                    if match:
                        break
                    trial += 1
                    if trial < 10:
                        print 'Frames did not match! Retrying after rewriting the test data again...'
                    else:
                        print 'Cannot make frames match!'
                        return match, data, p, f


        elif test_name=='rand_complex_C':
            for data_set_number in xrange(trials):

                data=(np.floor(np.random.rand(16,1024)*4-2) + 1j*np.floor(np.random.rand(16,1024)*4-2))

                trial = 0
                while True:
                    self.set_channelizer_outputs(data)

                    self.start_correlator(integration_period=integration_period, verbose=(0 if trial == 0 else 0))

                    self.sync()
                    p = self.compute_corr_output(data, integration_period=integration_period)
                    timestamp,f = ir.read_correlator_frame(verbose=verbose)
                    f = np.swapaxes(f, 0, 2)


                    match = np.all(p==f)
                    if match:
                        if data_set_number % 10 == 0 and data_set_number > 0:
                            print '[{3:s}]: Test #{0:d}/{1:d}; trial {2:d}'.format(data_set_number, trials, trial, datetime.now().strftime("%H:%M:%S.%f"))
                            print "\t{0:d}".format(timestamp)
                        break
                    trial += 1
                    if trial < 10:
                        print 'Trial {0:d} frames did not match! Retrying after rewriting the test data again...'.format(trial)
                        if(verbose):
                            print "Difference: "
                            print p - f
                    else:
                        print 'Cannot make frames match!'
                        return match, data, p, f

        else:
            raise ValueError('Unknown test name %s' % test_name)

        print '*** TEST PASSED! ***'
        return True, None, None, None


    @async
    def _call_subprocess(self, cmd):
        """
        Executes a subprocess in a non-blocking way.
        """
        PIPE = subprocess.PIPE

        # ssh_cmd = "ssh root@%s '%s'" % (self.hostname, cmd)
        split_cmd = shlex.split(cmd)
        p = subprocess.Popen(split_cmd, stdout=PIPE, stderr=PIPE)
        while p.poll() is None:
            yield tornado.gen.moment
        if p.returncode:
            raise RuntimeError("The command '%s' returned with the error code %i. stderr is displayed below:\n %s" % (cmd, p.returncode, ''.join(p.stderr.readlines())))
        async_return(p.stdout.readlines())


    @async
    def arm_exec(self, cmd):
        """
        Executes a command on the ARM over SSH.
        """
        self.logger.info("%r: Executing command '%s' on the ARM" % (self, cmd))
        ssh_cmd = 'ssh -o "StrictHostKeyChecking no" root@%s "%s"' % (self.hostname, cmd)
        result = yield self._call_subprocess.async(ssh_cmd)
        async_return(result)

    @async
    def arm_scp(self, source_filename, destination_filename='/tmp'):
        """
        Sends a file to the arm using scp.
        """
        self.logger.info('%r: Sending image file %s to the ARM in %s' % (self, source_filename, destination_filename))
        scp_cmd = 'scp -o "StrictHostKeyChecking no" %s root@%s:%s' % (source_filename, self.hostname, destination_filename)
        result = yield self._call_subprocess.async(scp_cmd)
        async_return(result)

    @async
    def _update_arm_firmware(self, image_filename, delay=120):
        """
        Overwrites the ARM firmware on the SD card with the specified image compressed with bzip2.

        !!! WARNING: This is a very ugly hack that can make the SD card
        inoperable. You must do this only if you are in a position to manually
        replace a SD card if this fails!!!

        !!! The image must be in BZIP2 format. If not, the ARM won't boot
        again unless you replace the SD card !!!

        You must power-cycle the board after this command. The normal reboot()
        method won't work because this corrupts the ARMs filesystem (did we
        say this was a bad hack?).
        """
        image_header = '\xfa\xb8\x00\x10\x8e\xd0\xbc\x00'
        with bz2.BZ2File(image_filename) as fh:
            data = fh.read(100)  # read a few bytes to make sure this is really a bz2 file
            if not data.startswith(image_header):
                raise RuntimeError('The image does not seem to contain a compressed SDcard image')
        print '%r: Sending file...' % self
        yield self.arm_scp.async(image_filename, '/tmp/image.bz2')
        print '%r: Writing SD card' % self
        yield self.arm_exec.async('bzcat /tmp/image.bz2 >/dev/mmcblk0')
        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print '%r: Waiting %i seconds' % (self, delay)
        yield tornado.gen.sleep(delay)
        async_return(True)

    @async
    def _upload_fpga_bitstream(self, filename, card_filename=None, delay=120):
        """
        Remounts the filesystem as read write
        Uploads the bit file to the SD card in folder /usr/lib/iceboard/
        Remounts the file system back to read only
        """

        if card_filename is not None:
            filename_sd_card = '/usr/lib/iceboard/' + card_filename
        else:
            filename_sd_card = '/usr/lib/iceboard/' + os.path.basename(filename)

        print '%r: Remounting the SD card file system as readwrite' % self
        yield self.arm_exec.async('mount / -o remount,rw')

        print '%r: Making /usr/lib/iceboard folder if needed' % self
        yield self.arm_exec.async('mkdir -p /usr/lib/iceboard')

        print '%r: Sending file to /usr/lib/iceboard/' % self
        yield self.arm_scp.async(filename, filename_sd_card)

        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print '%r: Waiting %i seconds' % (self, delay)
        yield tornado.gen.sleep(delay)

        print '%r: Remounting the SD card file system as readonly' % self
        yield self.arm_exec.async('mount / -o remount,ro')
        async_return(True)

    @async
    def _delete_fpga_bitstream(self, filename, delay=120):
        """
        Remounts the filesystem as read write
        Removes  the bit file on the SD card in folder /usr/lib/iceboard/
        Remounts the file system back to read only
        """

        print '%r: Remounting the SD card file system as readwrite' % self
        yield self.arm_exec.async('mount / -o remount,rw')

        remove_file = 'rm /usr/lib/iceboard/' + os.path.basename(filename)
        print '%r: Removing the requested file' % self
        yield self.arm_exec.async(remove_file)

        self.logger.info('%r: Command completed. Waiting %i seconds to ensure cache is flushed' % (self, delay))
        print '%r: Waiting %i seconds' % (self, delay)
        yield tornado.gen.sleep(delay)

        print '%r: Remounting the SD card file system as readonly' % self
        yield self.arm_exec.async('mount / -o remount,ro')
        sync_return(True)
