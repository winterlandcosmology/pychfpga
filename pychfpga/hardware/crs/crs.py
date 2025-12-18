""" Defines the class to operate the CRS.
"""
# Standard Python packages
import logging
import datetime
import time
import base64
import socket
import asyncio

# Pypi packages

import nest_asyncio

# External private packages

from wtl.metrics import Metrics

# Local packages

from pychfpga.hardware import Motherboard
from pychfpga.common import run_async, async_to_sync, Ccoll
from pychfpga.hardware.interfaces import TCPipe, TCPipe_I2C, TCPipe_SPI, ipmi_fru
from pychfpga.fpga_firmware import FPGAFirmware

from ..i2c_devices.pca9575 import pca9575 # I2C 16-bit IO Expander
from ..i2c_devices.pca6524 import pca6524 # I2C 24-bit IO Expander
from ..i2c_devices.pca9546a import pca9546a  # I2C switch
from ..i2c_devices.pca8574 import PCA8574
from ..i2c_devices.tmp421 import tmp421  # Temperature sensor
from ..i2c_devices.ina230 import ina230 as ina231  # Temperature sensor
from ..i2c_devices.eeprom import eeprom
from ..i2c_devices.qsfp import QSFP as qsfp
from ..i2c_devices.gpio import GPIO
from ..i2c_devices.hmc7044 import hmc7044  # Dual PLL
from ..i2c_devices.ssd1306 import SSD1306

class iic_dummy:
    def __init__(self, *args, **kwargs):
        pass

# ina226 = iic_dummy
# irps5401 = iic_dummy
# zynq_sysmon = iic_dummy
# si5341b = iic_dummy
# si570 = iic_dummy
# si5382 = iic_dummy


class CRS(Motherboard):
    """ Provide the basic code needed to operate the ZCU111


    If is assumed that the board is running the bridge software.

    Provides:

    - Lightweight list-based Hardware map management
    - Model, serial, slot, crate and mezzanine self discovery through the Iceboard (no mDNS required)
    - Access to the memory-mapped registers in the FPGA's firmware using the TCP link.

    Parameters:

        hostname (str): hostname or IP address of the ICEBoard ARM processor (mandatory)
        serial (str): Serial number of the board. Can be provided by the ARM.
        slot (int): Slot number in which the board is installed ona backplane. None if there is no backplane.

    ..        crate (IceCrateHandler): = object that handle the backplane on which the board is connected. `None` if the board is not connected to a backplane.

    """

    # Define model number for hardware map management and discovery
    part_number = 'CRS'
    _ipmi_part_numbers = ['CRS']
    SERIAL_NUMBER_LENGTH = 4  # number of digits in the serial number. Used to convert integers to a valid serial number.

    NUMBER_OF_CHANNELIZERS = 4
    # List serial numbers of Rev 0 boards. This is a temporary hack that is used to properly select the SPI port of the PLL.
    # One day we'll be able to query the board directly.
    REV0_SERIALS = ('429', '0429', # returned by SN003 with old TCPipe firmware that didn't read the EEPROM and used the FPGA DNA
                    '003',)
    NO_I2C = ('000') # could not read eeprom, so assuming I2C is bad

    port = 7  # port number on which to access the platform `hostname`


    TX_TO_RX_LANE_MAP = { # (Tx_slot, Tx_lane):(Rx_slot, Rx_lane), slots are 0-based
     (0,0): (0,0),    (0, 1): (3, 3),   (0, 2): (2, 3),   (0, 3): (1, 3),
     (1,0): (1,0),    (1, 1): (0, 3),   (1, 2): (2, 2),   (1, 3): (3, 2),
     (2,0): (2,0),    (2, 1): (3, 1),   (2, 2): (1, 2),   (2, 3): (0, 2),
     (3,0): (3,0),    (3, 1): (2, 1),   (3, 2): (1, 1),   (3, 3): (0, 1)}
    RX_TO_TX_LANE_MAP = {rx:tx for tx,rx in TX_TO_RX_LANE_MAP.items()}
    # (Tx_slot, Rx_slot): (tx_lane, rx_lane)
    SLOT_TO_LANE_MAP = {(ss,ds):(sl,dl) for (ss,sl),(ds,dl) in TX_TO_RX_LANE_MAP.items()}
    # ---------------

    REFCLK_BP = 1
    REFCLK_MB_SMA = 2

    def __init__(self, hostname=None, serial=None, slot=None, subarray=None, **kwargs):
        super().__init__(
            hostname=hostname,
            serial=serial,
            slot=slot,
            subarray=subarray, **kwargs)

        self.iic = None
        self.spi = None
        # self.mmi = None
        self.tcpipe = None
        self._is_open = None
        self.fpga = None  # firmware object

        self.firmware_crc = None  # temporary local storage of the CRC since we currently can't read it until the firmware is programmed.
        self.revision = None


    def open(self):
        run_async(self.open_async())


    async def open_platform_async(self, **kwargs):

        self.logger.debug(f'{self!r}: open() is called')

        # if not self.serial:
        #     raise RuntimeError('Cannot determine revision number: there is no serial number')
        self.revision = 0 if self.serial in self.REV0_SERIALS else 1
        self.pll_spi_port = 1 if self.revision >0 else 0  # PS SPI peripheral port on which the PLL is connected. This changed from Rev0 to Rev 1
        self.logger.debug(f'{self!r}: Board revision is {self.revision}. PLL will be on SPI port {self.pll_spi_port}')



        # Open tcp communication with the board
        self.logger.debug(f'{self!r}: Opening TCP connection to the board')
        self.tcpipe = TCPipe(self.hostname, self.port)

        # create I2C interface
        self.iic = TCPipe_I2C(self.tcpipe)

        # create SPI interface
        self.spi = TCPipe_SPI(self.tcpipe)

        # Programmable PLL (via SPI bus)
        self.pll = hmc7044(self.spi, spi_port=self.pll_spi_port) # programmable PLL, to be initialized when FPGA is programmed.

        # I2C0, Backplane

        # I2C1: Motherboard internal bus
        # I2C1 switch0 :EEPROM, clocks
        self.i2c1_switch0 = i2c1_switch0 = pca9546a(self.iic, address=0x70, port=1)
        # I2C1 Switch 0 port 0 devices
        self.i2c1_tmp421_5v0 = tmp421(self.iic, address=0x1C, port=(i2c1_switch0, 0))
        self.i2c1_tmp421_3v3 = tmp421(self.iic, address=0x1D, port=(i2c1_switch0, 0))
        self.i2c1_tmp421_2v5 = tmp421(self.iic, address=0x1E, port=(i2c1_switch0, 0))
        self.i2c1_tmp421_1v8 = tmp421(self.iic, address=0x1F, port=(i2c1_switch0, 0))

        self.i2c1_tmp421_1v2a = tmp421(self.iic, address=0x2A, port=(i2c1_switch0, 0))

        self.i2c1_ina231_vbp = ina231(self.iic, address=0x40, port=(i2c1_switch0, 0))
        self.i2c1_ina231_0v85a = ina231(self.iic, address=0x41, port=(i2c1_switch0, 0))
        self.i2c1_ina231_0v85b = ina231(self.iic, address=0x42, port=(i2c1_switch0, 0))
        self.i2c1_ina231_5v0 = ina231(self.iic, address=0x43, port=(i2c1_switch0, 0))
        self.i2c1_ina231_3v3 = ina231(self.iic, address=0x44, port=(i2c1_switch0, 0))
        self.i2c1_ina231_2v5 = ina231(self.iic, address=0x45, port=(i2c1_switch0, 0))
        self.i2c1_ina231_1v8 = ina231(self.iic, address=0x46, port=(i2c1_switch0, 0))
        self.i2c1_ina231_1v2a = ina231(self.iic, address=0x47, port=(i2c1_switch0, 0))
        self.i2c1_ina231_1v4 = ina231(self.iic, address=0x4A, port=(i2c1_switch0, 0))
        self.i2c1_ina231_1v2b = ina231(self.iic, address=0x4B, port=(i2c1_switch0, 0))

        self.i2c1_tmp421_1v4 = tmp421(self.iic, address=0x4C, port=(i2c1_switch0, 0))
        self.i2c1_tmp421_1v2b = tmp421(self.iic, address=0x4D, port=(i2c1_switch0, 0))
        self.i2c1_tmp422_0v85 = tmp421(self.iic, address=0x4f, port=(i2c1_switch0, 0), n_ext=2)

        self.i2c0_tmp421_pll = tmp421(self.iic, address=0x4E, port=(i2c1_switch0, 0)) # PLL temp monitor U45: int: Bot 1cm left of VCXO, ext: Q12 Bot Under Prog PLL

        if self.revision > 0:
            self.i2c1_disp = PCA8574(self.iic, address=0x22, port=(i2c1_switch0, 0))
        else:
            self.i2c1_disp = None
        self.i2c1_eeprom_data = eeprom(self.iic, address=0x57, bus_name=(i2c1_switch0, 0), address_width=7, max_read_length=255, max_write_length=8, write_page_size=8)
        self.i2c1_eeprom_serial = eeprom(self.iic, address=0x5F, bus_name=(i2c1_switch0, 0), address_width=8, max_read_length=255)  # must read 16 bytes from memory address 0x80

        self.display = disp = SSD1306(self.iic, address=0x3C, port=(i2c1_switch0, 0))

        # I2C1 Switch 0 port 1 devices
        #   0x18: DDR4 SODIMM Temp sensor
        #   0x3x: DDR4 SODIMM Write protect settings
        #   0x50: DDR4 SODIMM EEPROM

        # I2C1 Switch 0 port 2 devices
        #   0x35: NVMe Basic management command (BMC)
        #   0x53: NVMe Virtual Product Data (VPD)

        # I2C1 Switch 0 port 3 devices:
        #   External I2C header

        if self.serial in self.NO_I2C:
            self.logger.warning(f'{self!r}: Board is assumed to have no I2C. Skipping I2C device initialization')
            return


        # I2C1 Switch 1: SFP/QSFP
        self.i2c1_switch1 = i2c1_switch1 = pca9546a(self.iic, address=0x71, port=1)
        # Switch port 7: GPIOs
        self.i2c1_gpio0 = pca9575(self.iic, address=0x20, port=(i2c1_switch1, 7)) #   0x20: PCA9757 GPIO for SFP/QSFP
        self.i2c1_gpio1 = pca9575(self.iic, address=0x21, port=(i2c1_switch1, 7)) #   0x21: PCA9757 GPIO for SFP/QSFP
        self.i2c1_gpio0.init(cfg0_def=0b11110010, out0_default=0b11110111)
        self.i2c1_gpios = GPIO(gpio_table={
            # name : (io_expander_object, byte, LSB bit, width)
            'QSFP_ModPrsL': (self.i2c1_gpio0, 0, 1, 1),
            'QSFP_ResetL': (self.i2c1_gpio0, 0, 2, 1),
            'QSFP_IntL': (self.i2c1_gpio0, 0, 4, 1),
            'QSFP_LPMode': (self.i2c1_gpio0, 0, 3, 1),
            'QSFP_ModSelL': (self.i2c1_gpio0, 0, 0, 1),
            })

        # Switch port 0: QSFP
        self.i2c1_qsfp = qsfp(self.iic, bus_name=(i2c1_switch1, 0), gpio_prefix='QSFP_', gpio=self.i2c1_gpios, address=0x50)
        # Switch port 1-6: SFPs

        self.i2c0_bp_eeprom_data = eeprom(self.iic, address=0x50, bus_name=0, address_width=7, max_read_length=255, max_write_length=8, write_page_size=8)
        self.i2c0_bp_eeprom_serial = eeprom(self.iic, address=0x58, bus_name=0, address_width=8, max_read_length=255)
        self.i2c0_bp_tmp421 = tmp421(self.iic, address=0x4E, port=0)
        self.i2c0_bp_gpio = pca6524(self.iic, address=0x22, port=0) #

        # list of sensors
        self.i2c1_ina231_list = {
            'vbp': dict(device=self.i2c1_ina231_vbp, rshunt=0.01, imax=10),
            '0v85a': dict(device=self.i2c1_ina231_0v85a, rshunt=0.01, imax=40),
            '0v85b': dict(device=self.i2c1_ina231_0v85b, rshunt=0.01, imax=40),
            '5v0': dict(device=self.i2c1_ina231_5v0, rshunt=0.01, imax=16),
            '3v3': dict(device=self.i2c1_ina231_3v3, rshunt=0.01, imax=16),
            '2v5': dict(device=self.i2c1_ina231_2v5, rshunt=0.01, imax=16),
            '1v8': dict(device=self.i2c1_ina231_1v8, rshunt=0.01, imax=16),
            '1v2a': dict(device=self.i2c1_ina231_1v2a, rshunt=0.01, imax=16),
            '1v4': dict(device=self.i2c1_ina231_1v4, rshunt=0.01, imax=16),
            '1v2b': dict(device=self.i2c1_ina231_1v2b, rshunt=0.01, imax=16),
        }


        return
        self.logger.info(f'Initializing Voltage/current monitor chips')
        for name, info in self.i2c1_ina231_list.items():
            d = info['device']
            d.init(r_shunt=info['rshunt'], i_typ=info['imax'], avg=3)





        # Print board voltages/currents
        # The delay of programming the PLL is sufficient to allow the first voltage average to refresh
        # await asyncio.sleep(1)
        for name, info in self.i2c1_ina231_list.items():
            d = info['device']
            self.logger.info(f'Rail {name}: Vbus={d.get_bus_voltage():.3f}V, I={d.get_current():.3f}A, P={d.get_power():.1f}W')

    def get_power_supply_status(self):
        """ Return the voltage, current and power use by each power supply rail """
        status = {}
        for name, info in self.i2c1_ina231_list.items():
            d = info['device']
            status[name] = dict(Vbus=d.get_bus_voltage(),I=d.get_current(), P=d.get_power(), Vshunt=d.get_shunt_voltage())
        return status


    def close_platform(self):
        if self.tcpipe:
            self.tcpipe.close()
            self.tcpipe = None
        self._is_open = False


    # Name of each PLL reference input
    PLL_INPUT_NAMES = ( 'ETH_REG_125MHz', 'BP_CLK_10MHZ', 'SMA_CLK_10MHZ', 'PL_RECCLK')

    async def pll_init_async(
            self,
            input_sel = None,  # 1 = backplane, 2=motherboard SMA, None: use input_priorities
            input_priorities = (2, 1, 0, 3), # ignored if input_sel is not None
            fref=10e6,  # external 10 MHz reference from backplane or SMA
            fosc=50e6,  # on-board VCXO nominal frequency
            fvco=3000e6, # PLL2 VCO frequency, which is also the ADC sampling frequency
            fsys=10e6, # system clock. Divider = 3000/250 = 12 (200 MHz is not possible because divider is odd)
            fsysref=2.5e6 # Is a submultiple of both 3000/16 and 3200/16 (Dividers = 1200 or 1280, both even)
        ):
        """ Initialize the Programmable PLL.

        Because initializing the PLL changes the board state and it not needed for platform operations (PHY has a fixed clock), the PLL init
        should be done just before we configure the firmware so the proper reset sequences can be performed when it starts. Furthermore, this
        allows us to set PLL frequencies based on the requested application-specific firmware.

        Parameters:

            input_sel (int): Selects which clock reference to use. If `None`, the input is
                automatically selected based on `input_priorities`. On the CRS, the clock inputs
                are:

                - 0: CLKIN0/RFSYNC: ETH_REG_125MHz: 125 MHz clock recivered by the Ethernet PHY
                - 1: CLKIN1/FIN: BP_CLK_10MHZ: 10 MHz reference from the backplane
                - 2: CLKIN2/OSCOUT0: SMA_CLK_10MHZ: 10 MHz reference from the motherboard's SMA connector
                - 3: CLKIN3: PL_RECCLK: Clock generated by the FPGA's programmable logic.

                When `input_sel` is specified, the PLL is forced to use this input
                (`input_priorities` is ignored) and all other inputs are disabled.


            input_priorities (list of int): List of inputs to automatically select, in order of
                highest to lowest priority. Is ignored if `input_sel` is not `None`.

            fref (float): Input reference frequency in Hz.

            fosc (float):  On-board oscillator frequency in Hz. The CRS has a 50 MHz on-board
                oscillator which is disciplined to the reference input if present, otherwise it is free
                running but is still sufficiently stable and accurate to operate the board.

            fvco (float): Frequency of the 2nd stage PLL VCO in Hz. Corresponds also to the ADC
                sampling frequency (the output divider is bypassed). The processing clock is fvco/8.

            fsys (float): system clock frequency in Hz. Is typically 10 MHz and is typically aligned
                to the external 10 MHz reference once the PLL SYNC sequence is performed. This clock
                is used to run the IRIG-B synchronization sequence and drives the internal FPGA
                MMCMs to generate other system frequencies (200 MHz etc.). `fsys` must be an even
                submultiple of fvco (10 MHz is an even submultiples of both fvco=3000 or 3200 MHz).

            fsysref is used to synchronize/align the FPGA's RF ADC . It must be a submultiple of
                fvco/16, must be < 10 MHz, and must be an even submultiple of fvco (because PLL
                channel divider values must be even, although the first constraint guarantees this).
                `fsysref` =2.5 MHz is selected because it is a submultiple of both 3000/16 and
                3200/16 MHz, the two frequencies at which the CRS have been used.

        Returns:

            int: Number of the reference input currently being used

        """
        self.logger.info(f'{self!r}: Initializing programmable PLL at fvco=frf={fvco/1e6} MHz')
        frfdc = fvco # divider: 1
        fpl = frfdc / 8 # signal processing clock, typ. 375 MHz

        self.pll.init(
            input_sel=input_sel, # 0: Ethernet recovery, 1: backplane REFCLK, 2 = motherboard REFCLK, 3: FPGA
            input_priorities=input_priorities, # not used if input_sel is specified and is not None
            fref=fref, # external 10 MHz reference from backplane or SMA
            fosc=fosc, # on-board VCXO nominal frequency
            fvco=fvco,
            fout={
                0: frfdc,    # RF_CLK (FPGA RFDC 229)
                1: fsys,     # DDR4_CLK (FPGA Bank 67 LVDS)- used as system clock
                2: fsysref,  # CLKOUT_SMP (SMP connector P2 - Back row, 1st from M2)- to SMP connector, for debugging
                3: fsysref,  # SYSREF_SMP (SMP connector P27, Bak row, 2nd from M2)- to SMP connector, for debugging
                4: fpl,      # PL_CLK (FPGA Bank 69 LVDS) - used as processing clock
                5: fsysref,  # PL_SYSREF (FPGA Bank 69 LVDS) - used as 10 MHz reference
                6: fsys,     # GTY_CLK0_128 - not used
                7: fsys,     # GTY_CLK0_130 - not used
                8: frfdc,    # RF_CLK (FPGA RFDC)
                9: frfdc,    # RF_CLK (FPGA RFDC)
                10: frfdc,   # RF_CLK (FPGA RFDC)
                11: frfdc,   # RF_CLK (FPGA RFDC)
                12: frfdc,   # RF_CLK (FPGA RFDC)
                13: fsysref, # RF_SYSCLK (FPGA RFDC)
            })


        # Note: SMP connector P22/P23 (3rd/4th from M2 slot) are OUT0_P/N (25/50 MHz from fixed PLL)

        self.logger.info(f'{self!r}: Done programming PLL')

        await asyncio.sleep(0.05) # wait for PLL1 to lock

        active_input = self.pll.get_active_clkin()
        active_input_name = 'None' if active_input is None else self.PLL_INPUT_NAMES[active_input]
        self.logger.info(f'{self!r} PLL1 current active input is {active_input} ({active_input_name})')

        return active_input

    async def open_fpga_async(self, **kwargs):
        """ Open communication link with the FPGA. This creates the MMI interface, gather configuration information from the firmware, and instantiate the objects that will handle the firmware."""


        # if False:
        #     # Before we start the firmware, make sure we have our clocks.
        #     rf_pll_spi_port = 2
        #     # Set SPI mux to route PLL output mux pin to I2C-SPI MISO input
        #     self.i2c_gpio.select()
        #     # self.i2c_gpio.write_reg('CFG1',0b000, mask=0b00000110)  # set GPIO mux pins to output
        #     # self.i2c_gpio.write_reg('CFG1',rf_pll_spi_port << 1, mask=0b00000110)  # set mux pins to 0b10 (LMK04208)
        #     self.i2c_spi.select()
        #     self.i2c_rf_pll.init()
        #     self.i2c_adc0_pll.init()
        #     self.i2c_adc1_pll.init()
        #     # self.i2c_dac_pll.init()

        # from .. import FreqCtr, GPIO
        # self.GPIO = GPIO.GPIO_base(self, self._SYSTEM_GPIO_BASE_ADDR)
        # self.FreqCtr = FreqCtr.FreqCtr_base(self, self._SYSTEM_FREQ_CTR_BASE_ADDR)
        # self._is_open = True

        # self.fpga = self.firmware(self)
        await self.fpga.open_async()



        self.fpga.GPIO.PLL_SYNC=0
        await asyncio.sleep(0.5)  # test: wait for the PLL1 to stabilize before we sync. SYNC Won't work if OSCOUT is not stable vs REFCLK.
        if self.input_reference is None:
            self.logger.info(f"{self!r}: No external reference clock was detected on the motherboard SMA or from the backplane.")
        elif self.input_reference == 1:
            self.logger.info(f'{self!r}: Synchronizing external PLL to the backplane reference clock')
            self.fpga.GPIO.CLK10_SEL = 1 # use backplane reference
            self.fpga.GPIO.PLL_SYNC = 1
        elif self.input_reference == 2:
            self.logger.info(f'{self!r}: Synchronizing external PLL to the motherboard SMA clock input ')
            self.fpga.GPIO.CLK10_SEL=0 # use MB SMA
            self.fpga.GPIO.PLL_SYNC = 1
        else:
            raise RuntimeError(f'Invalid PLL input reference number {self.input_reference}')
        self.fpga.GPIO.PLL_SYNC=0


        self.logger.info(f'{self!r}: Initializing CRS display')
        if disp := self.display:
            await asyncio.sleep(0.2) # make sure the display has time to finish resetting since the FPGA was programmed and started sending a clock
            for trial in range(30):
                try:
                    disp.select()
                    disp.init()
                    disp.clear()
                    disp.hline(0,127,0)
                    disp.hline(0,127,31)
                    disp.vline(0,0,31)
                    disp.vline(127,0,31)
                    disp.vline(13,0,31)
                    disp.print(f'{self.serial}', x=1, y=28, fg=1, bg=0, font_size=12, rotate=True)
                    disp.print(f'{self.hostname}', x=20, y=2, font_size=12)
                    t=self.fpga.SYSMON.temperature()
                    disp.print(f'Core={t:0.1f}°C', x=20, y=14, font_size=12)
                    disp.fill(120,0,127,31)
                    disp.print(f'chFPGA', x=120, y=30, fg=0, bg=1, font_size=7, rotate=True)
                    disp.update()
                    break
                except OSError as e:
                    # self.logger.warn(f'{self!r}: Error Initializing CRS display on trial {trial}. Error is: {e!r}')
                    await asyncio.sleep(0.01)
            else:
                self.logger.warn(f'{self!r}: Failed Initializing CRS display after {trial} trials')
                self.display = None



        # await self.open_hw()
        # await self.hw.init()
        # await self.hw.set_led('GP_LED2', 1)  # Hardware link is on
        # await self.hw.set_led('GP_LED1', 0)  # Full FPGA firmware is not yet on

        # If we want to use the FPGA's I2C firmware interface instead of the onboard processor's
        # if False:
        #     # -------------------------------------------------------------------------
        #     # Open FPGA's I2C interfaces
        #     # -------------------------------------------------------------------------
        #     self.core_i2c = i2c.I2C_base(self.fpga, self._SYSTEM_I2C_BASE_ADDR)
        #     await asyncio.sleep(0)
        #     # Create standardized I2C interface
        #     self.i2c = I2CInterface(
        #         write_read_fn=self.fpga_i2c_write_read,  # write-read function
        #         port_select_fn=self.fpga_i2c_set_port,
        #         bus_table=IceBoardHardware.FPGA_I2C_BUS_LIST,
        #         switch_addr=IceBoardHardware._FPGA_I2C_SWITCH_ADDR,
        #         parent=self)  # parent object, whose repr() is used to tag messages

    async def close_fpga_async(self):
        await self.fpga.close()

    def is_open(self):
        return self._is_open

    def is_fmc_present(self, slot):
        return False

    async def ping_async(self, timeout=0.1):
        """
        Returns a boolean indicating whether the board is responding to network queries.
        """
        # print(f'{self!r} Ping_async()')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.logger.info(f'{self!r}: Pinging {self.hostname} at {s.getsockname()}')
        s.settimeout(timeout)
        s.setblocking(0)
        loop = asyncio.get_event_loop()
        try:
            await loop.sock_connect(s, (self.hostname, self.port))
            if_addr = s.getsockname()
            s.close()
        except (socket.timeout, Exception) as e:
            self.logger.warn(f'Could not establish a TCP connection with {self.hostname}.{self.port}. Error is:\n {e}')
            return False
        return True





    ###################################
    # Auto discovery methods
    ###################################
    # Called after open_platform_async(), but before the FPGA firmware is set.


    async def discover_serial_async(self, update=True):
        """
        Discover the serial number of this IceBoard from its IPMI data, and update the hardware map accordingly if `update=True`
        """
        self.logger.debug(f'{self!r}: discovering the serial number of board at {self.hostname}')
        return "003"

    async def discover_slot_async(self, update=True):
        """ Discover the slot number of this IceBoard, and update the hardware map accordingly if `update=True`"""
        return None

    async def discover_mezzanines_async(self, update=True):
        """Detect mezzanines attached to the motherboard and update the hardware map accordingly if
        update=True.

        """
        return None


    async def discover_crate_async(self, update=True):
        """ Detect the crate in which the board is installed and update the hardware map accordingly.
        """
        return None



   ###################################
    # Firmware management
    ###################################
    # Called after open_platform_async(), but before the FPGA firmware is set.

    async def is_fpga_programmed_async(self):
        return True
        # return self.tcpipe.is_fpga_programmed()

    async def set_fpga_bitstream_async(self, firmware_mode=None, force=False, bitfile_override=None):
        """
        Configures the FPGA with the specified bitstream.


         Parameters:

            firmware_mode (str): The desired operational mode. This will be
                used to automatically select the proper bitstream file for
                this platform and create the proper FPGAFirmware class.

            force (bool or None): Determine when the FPGA shall be configured

                - force = True: FPGA will always be configured independent of the signature of the currently programmed firmware
                - force = False: FPGA will be configured if it is not configured or
                        if its bitstream CRC differ from the provided bitstream
                - force = None: FPGA will be configured only if it is not configured

            bitfile_override (str): Specifies the path to a folder in which to
                search for the default bitstream file,  or the path to the
                bitstream file to use instead of the default one.


        """


        t0 = time.time()
        self.logger.debug(f'{self!r}: Called set_fpga_bitstream')

        if hasattr(self, 'close'):
            self.close()

        fw_cls, buf, fw_params = FPGAFirmware.get_firmware(self.part_number, firmware_mode, bitfile_override=bitfile_override)
        crc32 = buf.crc32
        bitstream = buf.raw_bitstream


        self.logger.info(f'{self!r}: Programming PLL before configuring the FPGA. {fw_params=}')
        self.input_reference = await self.pll_init_async(fvco=fw_params['sampling_frequency'])  # add fw params here if we want to have mode/application-specific frequencies sent to the FPGA

        # self.logger.warn(f'{self!r}: Using backplane clock to synchronize PLL outputs to REFCLK. This mignt not be right')
        # self.GPIO.CLK10_SEL=1 # Select clock source to re-sync the PLL: 0: MB SMA, 1: BP
        # self.GPIO.PLL_SYNC=0
        # self.GPIO.PLL_SYNC=1
        # self.GPIO.PLL_SYNC=0

        is_fpga_programmed = await self.is_fpga_programmed_async()
        self.logger.debug(f'{self!r}: Is the FPGA already programmed: {is_fpga_programmed}')
        is_crc_valid = False
        if self.fpga and is_fpga_programmed:
            self.logger.debug(f'{self!r}: Getting FPGA crc')
            fpga_bitstream_crc = await self.get_fpga_bitstream_crc_async()
            self.logger.debug(
                f'{self!r}: fpga_programmed={is_fpga_programmed}, force={force}, '
                f'fpga_crc={fpga_bitstream_crc or 0:08X}, bitstream_crc={crc32 or 0:08X}')
            is_crc_valid = fpga_bitstream_crc == crc32

        if not is_fpga_programmed or force or (force is not None and not is_crc_valid):
            self.logger.debug(f'{self!r}: Configuring FPGA')
            self.tcpipe.set_fpga_bitstream(bitstream, crc=crc32)
        else:
            self.logger.debug(
                f'{self!r}: FPGA is already configured. Skipping configuration.')

        self.fpga = fw_cls(self, mode=firmware_mode, **fw_params)

    # Mezzanine management


    async def get_fpga_bitstream_crc_async(self):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        fpga_is_programmed = await self.is_fpga_programmed_async()
        return self.tcpipe.get_fpga_bitstream_crc() if fpga_is_programmed else None

    async def set_fpga_bitstream_crc_async(self, crc32):
        """ Return the signature of the firmware currently configured in the
        FPGA.

        Returns None if the FPGA is not configured.
        """
        fpga_is_programmed = await self.is_fpga_programmed_async()
        self.tcpipe.get_fpga_bitstream_crc(crc32 if fpga_is_programmed else None)

    # def clear_fpga_bitstream(self):
    #     """ Stop the operation of the FPGA.

    #     Could be used if we detect that we don't have the right kind of
    #     mezzanines.
    #     """
    #     raise NotImplementedError()


    ###################################
    # Mezzanine EEPROM access methods
    ###################################

    async def _mezzanine_eeprom_read_async(self, mezzanine):
        """ Returns the contents of the specified mezzanine's EEPROM.
        """
        return None

    # Backplane/crate-related methods

    ###################################
    # Motherboard EEPROM access methods
    ###################################

    async def _write_motherboard_spi_eeprom_base64(self, *args, **kwargs):
        return None

    def _eeprom_write_ipmi(self, serial_number, product_version):
        """Write IPMI-formatted EEPROM.

        These fields are read back and parsed by software, so you have
        to get them right or things will misbehave. This method currently
        expects the following formatting:

            m._eeprom_write_ipmi(serial_number="004", product_version="2")

        """

        fru = ipmi_fru.FRU(
            board=ipmi_fru.Board(
                mfg_date=datetime.datetime.now(),
                manufacturer="t0 technology",
                product_name=self.part_number,
                part_number=self.part_number,
                serial_number=serial_number,
                fru_file="",
            ),
            product=ipmi_fru.Product(
                manufacturer="t0 technology",
                product_name=self.part_number,
                part_number=self.part_number,
                product_version=product_version,
                serial_number=serial_number,
                asset_tag="",
                fru_file="",
            )
        )
        # Convert IPMI structures into a byte stream to be written
        ipmi_bytes = fru.encode()
        self.i2c1_eeprom_data.write(0, ipmi_bytes)

    ###################################
    # Backplane info methods
    ###################################


    async def is_backplane_present_async(self):
        return False

    async def get_slot_number(self):
        return None

    async def get_iceboard_clock_source_async(self):
        return "CRYSTAL"

    def get_iceboard_clock_source_sync(self):
        return "CRYSTAL"

    def get_motherboard_temperature(self, sensor):
        """ Synchronous wrapper to return motherboard temperature sensor value.
        """
        return None


    #################################
    # Metrics
    #################################

    async def get_metrics_async(self):
        """ Get the motherboard hardware monitoring information.

        Returns:
            a :class:`Metrics` object.
        """

        metrics = await super().get_metrics_async()

        # Add ZCU111 monitoring data to metrics here

        return metrics

