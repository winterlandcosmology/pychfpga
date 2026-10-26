""" Defines the class to operate the CRS.
"""
# Standard Python packages
import logging
import datetime
import time
import base64
import socket
import asyncio
from os import path

# External private packages

from wtl.metrics import Metrics

# Local packages

from pychfpga.hardware import Crate, Motherboard
from pychfpga.common import run_async, async_to_sync, Ccoll
from pychfpga.hardware.interfaces import TCPipe, TCPipe_I2C, TCPipe_SPI, ipmi_fru
from pychfpga.fpga_firmware import FPGAFirmware
from . import crs_discovery
from .crs_bp import CRSBackplane

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
    """ Provide the basic code needed to operate the CRS


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

    # List serial numbers of Rev 0 boards. This is a temporary hack that is used to properly select the SPI port of the PLL.
    # One day we'll be able to query the board directly.
    REV0_SERIALS = ('429', '0429', # returned by SN003 with old TCPipe firmware that didn't read the EEPROM and used the FPGA DNA
                    '003',)
    NO_I2C = ('000',)  # Serials of boards that could not read eeprom, so assuming I2C is bad

    port = 7  # port number on which to access the platform `hostname`

    REQUIRED_TCPIPE_VERSION = "1.0"  # Fallback when the ELF has no TCPIPE_GIT= marker.
    DEFAULT_ARM_FIRMWARE_NAME = "tcpipe_crs.elf"
    DEFAULT_ARM_FIRMWARE_PATHNAME = path.join(path.dirname(__file__), 'arm_firmware', DEFAULT_ARM_FIRMWARE_NAME)


    TX_TO_RX_LANE_MAP = { # (Tx_slot, Tx_lane):(Rx_slot, Rx_lane), slots are 0-based
     (0,0): (0,0),    (0, 1): (3, 3),   (0, 2): (2, 3),   (0, 3): (1, 3),
     (1,0): (1,0),    (1, 1): (0, 3),   (1, 2): (2, 2),   (1, 3): (3, 2),
     (2,0): (2,0),    (2, 1): (3, 1),   (2, 2): (1, 2),   (2, 3): (0, 2),
     (3,0): (3,0),    (3, 1): (2, 1),   (3, 2): (1, 1),   (3, 3): (0, 1)}
    RX_TO_TX_LANE_MAP = {rx:tx for tx,rx in TX_TO_RX_LANE_MAP.items()}
    # (Tx_slot, Rx_slot): (tx_lane, rx_lane)
    SLOT_TO_LANE_MAP = {(ss,ds):(sl,dl) for (ss,sl),(ds,dl) in TX_TO_RX_LANE_MAP.items()}
    # ---------------

    # Backplane QSFP ports (4SBP backplane schematic, U11 PCAL6524): connector: (U11 port, first bit of ModSelL/ResetL/IntL/ModPrsL,
    # LPMode bit on port 2). Data lanes: P5A, P5B and P6A carry one lane of each slot to backplanes BD1, BD2 and BD3; P6B has none.
    BP_QSFP_PINS = {'P5A': (1, 4, 3), 'P5B': (0, 0, 0), 'P6A': (0, 4, 1), 'P6B': (1, 0, 2)}

    @classmethod
    def bp_qsfp_gpio_table(cls, expander):
        """GPIO table for the backplane QSFP ports: '<connector>_<signal>': (expander, port, bit, width)."""
        table = {}
        for port, (byte, bit, lp_bit) in cls.BP_QSFP_PINS.items():
            for i, name in enumerate(('ModSelL', 'ResetL', 'IntL', 'ModPrsL')):
                table[f'{port}_{name}'] = (expander, byte, bit + i, 1)
            table[f'{port}_LPMode'] = (expander, 2, lp_bit, 1)
        return table

    @staticmethod
    def qsfp_media(connector, device_tech):
        """'optical' or 'copper' from SFF-8636 byte 130 (connector) and byte 147[7:4] (transmitter technology).
        Copper unless the connector is optical (LC, MPO, MXC) and the technology is not a copper cable (>= 0xA)."""
        return 'optical' if connector in (0x07, 0x0B, 0x0C, 0x0D, 0x24) and (device_tech >> 4) < 0xA else 'copper'

    def bp_qsfp_media(self):
        """Media in each backplane QSFP port, read through U11: {'P5A': 'copper' | 'optical' | None (empty), ...}.
        Meaningful only on the board in slot position 1 of a backplane (the only one on U11's bus)."""
        media = {}
        for port, q in self.i2c0_bp_qsfp.items():
            if not q.is_present():
                media[port] = None
                continue
            media[port] = self.qsfp_media(q.read('Connector')[0], q.read('DeviceTech')[0])
        return media

    REFCLK_BP = 1
    REFCLK_MB_SMA = 2

    OLED_CONFIG_NAME_X = 118
    OLED_CONFIG_NAME_Y = 30
    OLED_ROTATED_CLOCKWISE_TEXT_ROTATION = 3

    def __init__(self, hostname=None, serial=None, slot=None, subarray=None, **kwargs):
        super().__init__(
            hostname=hostname,
            serial=serial,
            slot=slot,
            subarray=subarray, **kwargs)

        self.iic = None
        self.spi = None
        self.tcpipe = None
        self.tcpipe_version = None
        self._is_open = None
        self.fpga = None  # firmware object

        self.firmware_crc = None  # temporary local storage of the CRC since we currently can't read it until the firmware is programmed.
        self.revision = None


    def open(self):
        run_async(self.open_async())

    @classmethod
    def get_network_discovery_function(cls):
        """Return the function to use for network discovery of CRS backplanes."""
        return crs_discovery.discover_crs

    @classmethod
    async def set_platform_firmware(cls, boards=None, firmware_pathname=None, **kwargs):
        firmware_pathname = firmware_pathname or cls.DEFAULT_ARM_FIRMWARE_PATHNAME
        required_version = kwargs.pop("required_version", None)
        if required_version is None:
            required_version = (
                crs_discovery.tcpipe_version_from_elf(firmware_pathname)
                or cls.REQUIRED_TCPIPE_VERSION
            )
        return await crs_discovery.set_platform_firmware(
            boards=boards or [],
            required_version=required_version,
            firmware_pathname=firmware_pathname,
            **kwargs)


    async def open_platform_async(self, **kwargs):

        self.logger.debug(f'{self!r}: open() is called')

        # Open tcp communication with the board
        self.logger.debug(f'{self!r}: Opening TCP connection to the board')
        self.tcpipe = TCPipe(self.hostname, self.port)

        # Get the board revision so we can instantiate the right peripherals
        self.revision = int(self.tcpipe.get_motherboard_ipmi_info().get('revision') or 1)

        self.pll_spi_port = 1 if self.revision > 0 else 0  # PS SPI peripheral port on which the PLL is connected. This changed from Rev0 to Rev 1
        self.logger.debug(f'{self!r}: Board revision is {self.revision}. PLL will be on SPI port {self.pll_spi_port}')
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

        # self.display = disp = SSD1306(self.iic, address=0x3C, port=(i2c1_switch0, 0))
        self.display = None  # OLED initialization and status refresh are handled by tcpipe_rtos.

        # I2C1 Switch 0 port 1 devices
        #   0x18: DDR4 SODIMM Temp sensor
        #   0x3x: DDR4 SODIMM Write protect settings
        #   0x50: DDR4 SODIMM EEPROM

        # I2C1 Switch 0 port 2 devices
        #   0x35: NVMe Basic management command (BMC)
        #   0x53: NVMe Virtual Product Data (VPD)

        # I2C1 Switch 0 port 3 devices:
        #   External I2C header

        if self.revision == 0:
            self.logger.warning(f'{self!r}: Board is Rev 0 and is assumed to have no I2C. Skipping I2C device initialization')
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

        # I2C0: Backplane
        # 24C02, 256 bytes, 8-bit word address (I2C0 @ 0x51, no mux)
        self.i2c0_bp_eeprom_data = eeprom(self.iic, address=0x51, bus_name=0, address_width=8, max_read_length=255, max_write_length=8, write_page_size=8)
        self.i2c0_bp_tmp421 = tmp421(self.iic, address=0x4E, port=0) # only on slot 0
        # U11 PCAL6524: 7-bit address 0x22 (the backplane schematic's "0x44" is the 8-bit form). Only on slot 1.
        self.i2c0_bp_gpio = pca6524(self.iic, address=0x22, port=0)
        self.i2c0_bp_gpios = GPIO(gpio_table=self.bp_qsfp_gpio_table(self.i2c0_bp_gpio))
        # Backplane QSFP ports, all at 0x50; ModSelL selects one. Only on slot 1. P6B has no data lanes.
        self.i2c0_bp_qsfp = {port: qsfp(self.iic, bus_name=0, gpio_prefix=f'{port}_', gpio=self.i2c0_bp_gpios, address=0x50)
                             for port in self.BP_QSFP_PINS}

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

    def enable_qsfp_high_power_mode(self, ready_timeout=2.5, poll_interval=0.1):
        """Bring the QSFP module into high-power mode at startup.

        Waits for the module to be present and for its management interface
        (EEPROM at I2C addr 0x50) to respond -- per SFF-8636 the module is
        allowed up to ~2 s (t_init) after ResetL deassert before it must answer
        on the management bus -- then drives LPMode low and writes byte 93
        (PowerSet) so that modules declaring Power Class 5-8 (>3.5 W, e.g. a
        4.5 W Class 7 transceiver) are actually allowed to power up.

        Silently does nothing if no module is present.
        """
        if not getattr(self, 'i2c1_qsfp', None):
            return
        try:
            if not self.i2c1_qsfp.is_present():
                self.logger.debug(f'{self!r}: No QSFP module present; skipping high-power enable')
                return

            # Poll the QSFP EEPROM until it answers, up to t_init (SFF-8636: up to 2 s
            # after ResetL deassert). The GPIO defaults already release ResetL well
            # before this code runs, but a freshly hot-plugged module may still be
            # in its init window.
            deadline = time.monotonic() + ready_timeout
            ready = False
            while time.monotonic() < deadline:
                try:
                    self.i2c1_qsfp.enable_i2c(True)
                    try:
                        if self.i2c1_qsfp.is_present():
                            ready = True
                            break
                    finally:
                        self.i2c1_qsfp.enable_i2c(False)
                except Exception:
                    pass
                time.sleep(poll_interval)

            if not ready:
                self.logger.warning(
                    f'{self!r}: QSFP module present but did not respond on I2C within '
                    f'{ready_timeout:.1f}s; skipping high-power enable')
                return

            # set_power_mode(1) programs byte 93 according to the module's declared
            # Power Class (via enable_high_power_class()) and then deasserts LPMode.
            self.i2c1_qsfp.set_power_mode(1)
            self.logger.info(f'{self!r}: QSFP high-power mode enabled')
        except Exception as e:
            self.logger.warning(f'{self!r}: Error enabling QSFP high-power mode: {e}')


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

    def reboot(self):
        """Reboot the CRS processor via the TCPipe UDP ``REBOOT`` command.

        The board issues PSCI SYSTEM_RESET. Local TCP/MMI state is dropped
        because it cannot survive the reset. Works with or without an open
        TCPipe session; a hostname is required.
        """
        if not self.hostname:
            raise RuntimeError(f'{self!r}: Cannot reboot without a hostname')
        self.logger.info(f'{self!r}: Sending UDP REBOOT to {self.hostname}')
        if self.tcpipe is not None:
            self.tcpipe.reboot()
            self.tcpipe = None
        else:
            TCPipe.send_reboot(self.hostname)
        self.fpga = None
        self._is_open = False


    # Name of each PLL reference input
    PLL_INPUT_ETH_REG_125MHZ = 0
    PLL_INPUT_BP_CLK_10MHZ = 1
    PLL_INPUT_SMA_CLK_10MHZ = 2
    PLL_INPUT_PL_RECCLK = 3
    PLL_INPUT_NAMES = {PLL_INPUT_ETH_REG_125MHZ: 'ETH_REG_125MHz', 
                       PLL_INPUT_BP_CLK_10MHZ: 'BP_CLK_10MHZ', 
                       PLL_INPUT_SMA_CLK_10MHZ: 'SMA_CLK_10MHZ', 
                       PLL_INPUT_PL_RECCLK: 'PL_RECCLK'}

    async def pll_init_async(
            self,
            input_sel = None,  # 1 = backplane, 2=motherboard SMA, None: use input_priorities
            input_priorities = (PLL_INPUT_SMA_CLK_10MHZ, PLL_INPUT_BP_CLK_10MHZ), # ignored if input_sel is not None
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
        t0 = time.perf_counter()
        self.logger.info(f'{self!r}: Initializing programmable PLL at fvco=frf={fvco/1e6} MHz')
        frfdc = fvco # divider: 1
        fpl = frfdc / 8 # signal processing clock, typ. 375 MHz

        self.pll.init(
            input_sel=input_sel, # 0: Ethernet recovery, 1: backplane REFCLK, 2 = motherboard REFCLK, 3: FPGA
            input_priorities=input_priorities, # not used if input_sel is specified and is not None
            fref=fref, # external 10 MHz reference from backplane or SMA
            fosc=fosc, # on-board VCXO nominal frequency
            fvco=fvco,
            check=0,
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
        self.logger.info(
            f'{self!r}: Init phase pll_init: {time.perf_counter() - t0:.3f} s')

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
        t0 = time.perf_counter()
        await self.fpga.open_async(**kwargs)
        t_mmi = time.perf_counter() - t0

        t_sync = time.perf_counter()
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
            self.logger.warning(
                f'{self!r}: PLL locked to {self.PLL_INPUT_NAMES[3]}; This is not a valid input.'
                f'skipping CLK10_SEL/PLL_SYNC (no 10 MHz backplane or SMA reference)')
            self.input_reference = None

        self.fpga.GPIO.PLL_SYNC=0
        t_pll_sync = time.perf_counter() - t_sync
        self.logger.info(
            f'{self!r}: Init phase open_fpga: MMI={t_mmi:.3f} s, '
            f'PLL_SYNC={t_pll_sync:.3f} s, total={time.perf_counter() - t0:.3f} s')

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
        target = (self.hostname, self.port)
        self.logger.info(
            f'{self!r}: Pinging TCP {target[0]}:{target[1]} (timeout={timeout}s)')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(loop.sock_connect(s, target), timeout=timeout)
            return True
        except (asyncio.TimeoutError, OSError, socket.error) as exc:
            self.logger.warning(
                f'{self!r}: Could not establish a TCP connection with '
                f'{target[0]}:{target[1]}: {exc}')
            return False
        finally:
            try:
                s.close()
            except OSError:
                pass





    ###################################
    # Auto discovery methods
    ###################################
    # Called after open_platform_async(), but before the FPGA firmware is set.


    async def discover_serial_async(self, update=True):
        """Discover the board serial from TCPipe init-time motherboard IPMI."""
        self.logger.debug(f'{self!r}: discovering the serial number of board at {self.hostname}')
        if self.tcpipe is None:
            return self.serial
        info = await self.tcpipe.get_motherboard_ipmi_info_async()
        actual_serial = info.get('serial') or None
        if update and actual_serial:
            if self.serial and str(actual_serial) != str(self.serial):
                self.logger.warning(
                    f'{self!r}: Discovered serial {actual_serial} differs from hardware-map '
                    f'serial {self.serial}; updating to the discovered value')
            self.update_instance(serial=actual_serial)
        return self.serial

    async def discover_slot_async(self, update=True):
        """Discover the crate slot from TCPipe init-time backplane IPMI."""
        self.logger.debug(f'{self!r}: discovering slot from TCPipe backplane IPMI at {self.hostname}')
        if self.tcpipe is None:
            return self.slot
        info = await self.tcpipe.get_backplane_ipmi_info_async()
        crate_slot = info.get('crate_slot', -1)
        actual_slot = crate_slot if crate_slot > 0 else None
        if update and actual_slot:
            if self.slot and actual_slot != self.slot:
                self.logger.warning(
                    f'{self!r}: Discovered slot {actual_slot} differs from hardware-map '
                    f'slot {self.slot}; updating to the TCPipe value')
            self.update_instance(slot=actual_slot)
        return self.slot

    async def discover_mezzanines_async(self, update=True):
        """Detect mezzanines attached to the motherboard and update the hardware map accordingly if
        update=True.

        """
        return None


    async def discover_crate_async(self, update=True):
        """Discover crate model/serial from TCPipe init-time backplane IPMI."""
        self.logger.debug(f'{self!r}: discovering crate from TCPipe backplane IPMI at {self.hostname}')
        if self.tcpipe is None:
            return self.crate
        info = await self.tcpipe.get_backplane_ipmi_info_async()
        crate_part = info.get('crate_model') or info.get('chassis_part')
        crate_serial = info.get('crate_serial') or info.get('chassis_serial')
        if not crate_part or not crate_serial:
            return self.crate
        crate_cls = Crate.get_class_by_ipmi_part_number(crate_part) or CRSBackplane
        crate = Crate.get_unique_instance(new_class=crate_cls, serial=crate_serial)
        if update:
            self.update_instance(crate=crate)
        return crate



   ###################################
    # FPGA Firmware management
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


        self.logger.debug(f'{self!r}: Called set_fpga_bitstream')
        t0 = time.perf_counter()

        if self.fpga is not None:
            await self.close_fpga_async()
            self.fpga = None

        fw_cls, buf, fw_params = FPGAFirmware.get_firmware(
            self.part_number, firmware_mode, bitfile_override=bitfile_override)
        crc32 = buf.crc32
        bitstream = buf.raw_bitstream

        # PLL: program the HMC7044 before the bitstream so the FPGA comes up
        # on the clocks for this firmware mode.
        self.logger.info(f'{self!r}: Programming PLL before configuring the FPGA. {fw_params=}')
        t_pll = time.perf_counter()
        self.input_reference = await self.pll_init_async(fvco=fw_params['sampling_frequency'])
        self.logger.info(
            f'{self!r}: Init phase PLL: {time.perf_counter() - t_pll:.3f} s')

        # Bitstream: send if unprogrammed, forced, or CRC mismatch, then attach the firmware object.
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

        t_bit = time.perf_counter()
        if not is_fpga_programmed or force or (force is not None and not is_crc_valid):
            self.logger.debug(f'{self!r}: Configuring FPGA')
            await self.tcpipe.set_fpga_bitstream_async(bitstream, crc=crc32)
            self.logger.info(
                f'{self!r}: Init phase bitstream send: {time.perf_counter() - t_bit:.3f} s')
        else:
            self.logger.debug(
                f'{self!r}: FPGA is already configured. Skipping configuration.')
            self.logger.info(
                f'{self!r}: Init phase bitstream send skipped: '
                f'{time.perf_counter() - t_bit:.3f} s')

        # Create the firmware object and register it
        fpga_fw = fw_cls(self, mode=firmware_mode, **fw_params)
        self.set_fpga_firmware(fpga_fw)

        # OLED: these RPCs wait until the board finishes PM_FPGA_LOAD.
        t_oled = time.perf_counter()
        # Note: although we finished uploading the firmware, TCPIPE is still busy
        # programming the FPGA and initializing DMAs. Next command will block
        # until this is done (~200 ms).
        await self.tcpipe.oled_set_firmware_label_async('chFPGA')
        # await self.tcpipe.oled_draw_text_async(
        #     self.OLED_CONFIG_NAME_X,
        #     self.OLED_CONFIG_NAME_Y,
        #     firmware_mode,
        #     rotation=self.OLED_ROTATED_CLOCKWISE_TEXT_ROTATION)
        self.logger.info(
            f'{self!r}: Init phase OLED: {time.perf_counter() - t_oled:.3f} s')
        self.logger.info(
            f'{self!r}: Init phase set_fpga_bitstream total: '
            f'{time.perf_counter() - t0:.3f} s')


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
        self.tcpipe.set_fpga_bitstream_crc(crc32 if fpga_is_programmed else None)

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

    @staticmethod
    def _format_hw_serial(value, width):
        """Format a hardware serial number to the documented digit width."""
        if isinstance(value, int):
            return f'{value:0{width}d}'
        serial = str(value).strip()
        if serial.isdigit() and len(serial) < width:
            return serial.zfill(width)
        return serial

    def _backplane_identity(self):
        """Return :class:`CRSBackplane` for backplane FRU EEPROM layout defaults."""
        return CRSBackplane

    @staticmethod
    def _slot_from_chassis_custom(custom):
        """Parse ``slot=N`` from IPMI chassis custom fields (1-based crate slot)."""
        prefix = 'slot='
        for item in custom or []:
            text = str(item).strip()
            if text.lower().startswith(prefix):
                slot = int(text[len(prefix):])
                if slot > 0:
                    return slot
        return None

    def _read_backplane_fru(self):
        """Read and decode the segment backplane FRU, or None if unavailable."""
        eeprom_dev = getattr(self, 'i2c0_bp_eeprom_data', None)
        if eeprom_dev is None or not eeprom_dev.is_present():
            return None
        eeprom_size = 1 << eeprom_dev.address_width

        def read_fn(addr, length):
            if length < 0:
                length = eeprom_size - addr
            return bytes(eeprom_dev.read(addr, length))

        try:
            return ipmi_fru.FRU.decode(read_fn)
        except ValueError as exc:
            self.logger.debug(f'{self!r}: Backplane FRU decode failed: {exc}')
            return None

    def _slot_from_backplane_fru(self):
        fru = self._read_backplane_fru()
        if fru is None or fru.chassis is None:
            return None
        return self._slot_from_chassis_custom(fru.chassis.custom)

    def set_backplane_eeprom(
            self,
            backplane_serial,
            crate_serial,
            backplane_revision=None,
            crate_revision=None,
            backplane_model=None,
            backplane_product_name=None,
            crate_model=None,
            crate_product_name=None,
            backplane_manufacturer=None,
            crate_manufacturer=None,
            mfg_date=None,
            verify=True):
        """Initialize the backplane FRU EEPROM (IPMI v1.2, CRS spec v1.6).

        Manufacturer, model, and revision defaults come from
        :class:`CRSBackplane`. The crate slot is ``self.slot``. If those
        defaults are acceptable, only the backplane and crate serials are
        required::

            m.set_backplane_eeprom(
                backplane_serial='027',
                crate_serial='123',
            )

        EEPROM areas (see ``Backplane IPMI and BEACON Specifications v1.6``):

        - Chassis: crate model (CRC4) and serial, plus custom ``slot=N`` (1-16).
        - Board: 4SBP segment (manufacturer, product name, serial, model),
          plus custom ``revision=N``.
        - Product: CRC4 crate (manufacturer, ``CRS Crate``, model, revision,
          serial).

        Parameters:
            backplane_serial: 3-digit backplane serial (Board Serial Number).
            crate_serial: 3-digit crate serial (Product Serial Number).
            backplane_revision: Backplane revision, stored as ``revision=N``.
            crate_revision: Crate revision (Product Version).
            backplane_model: Board Part Number (default ``4SBP``).
            backplane_product_name: Board Product Name.
            crate_model: Product Model Number (default ``CRC4``).
            crate_product_name: Product Name.
            backplane_manufacturer: Board Manufacturer.
            crate_manufacturer: Product Manufacturer.
            mfg_date: Board manufacturing date. Default now.
            verify: If True, read the EEPROM back and check the image.

        Returns:
            The encoded FRU object.
        """
        eeprom_dev = getattr(self, 'i2c0_bp_eeprom_data', None)
        if eeprom_dev is None:
            raise RuntimeError(f'{self!r}: Backplane EEPROM is not initialized; open the board first')
        if not eeprom_dev.is_present():
            raise RuntimeError(f'{self!r}: Backplane EEPROM is not present on I2C0')
        if not self.slot:
            raise RuntimeError(f'{self!r}: Cannot program the backplane EEPROM without a crate slot')

        crate_slot = int(self.slot)
        bp = self._backplane_identity()
        if not 1 <= crate_slot <= bp.NUMBER_OF_SLOTS:
            raise ValueError(
                f'crate slot must be in 1-{bp.NUMBER_OF_SLOTS}, got {crate_slot}')

        backplane_manufacturer = (
            backplane_manufacturer if backplane_manufacturer is not None
            else bp.BACKPLANE_MANUFACTURER)
        crate_manufacturer = (
            crate_manufacturer if crate_manufacturer is not None
            else bp.CRATE_MANUFACTURER)
        backplane_model = backplane_model if backplane_model is not None else bp.BACKPLANE_MODEL
        backplane_product_name = (
            backplane_product_name if backplane_product_name is not None
            else bp.BACKPLANE_PRODUCT_NAME)
        backplane_revision = (
            str(backplane_revision).strip() if backplane_revision is not None
            else bp.BACKPLANE_REVISION)
        crate_model = crate_model if crate_model is not None else bp.CRATE_MODEL
        crate_product_name = (
            crate_product_name if crate_product_name is not None
            else bp.CRATE_PRODUCT_NAME)
        crate_revision = (
            str(crate_revision).strip() if crate_revision is not None
            else bp.CRATE_REVISION)
        mfg_date = mfg_date if mfg_date is not None else datetime.datetime.now()

        backplane_serial = self._format_hw_serial(
            backplane_serial, bp.BACKPLANE_SERIAL_NUMBER_LENGTH)
        crate_serial = self._format_hw_serial(
            crate_serial, bp.CRATE_SERIAL_NUMBER_LENGTH)

        chassis_custom = [f'slot={crate_slot}']

        fru = ipmi_fru.FRU(
            chassis=ipmi_fru.Chassis(
                type_code=ipmi_fru.CHASSIS_RACK_MOUNT,
                part_number=crate_model,
                serial_number=crate_serial,
                custom=chassis_custom,
            ),
            board=ipmi_fru.Board(
                mfg_date=mfg_date,
                manufacturer=backplane_manufacturer,
                product_name=backplane_product_name,
                serial_number=backplane_serial,
                part_number=backplane_model,
                fru_file='',
                custom=[f'revision={backplane_revision}'],
            ),
            product=ipmi_fru.Product(
                manufacturer=crate_manufacturer,
                product_name=crate_product_name,
                part_number=crate_model,
                product_version=crate_revision,
                serial_number=crate_serial,
                asset_tag='',
                fru_file='',
            ),
        )
        ipmi_bytes = fru.encode()
        eeprom_size = 1 << eeprom_dev.address_width
        if len(ipmi_bytes) > eeprom_size:
            raise ValueError(
                f'FRU image is {len(ipmi_bytes)} bytes, which exceeds the '
                f'{eeprom_size}-byte backplane EEPROM')

        self.logger.info(
            f'{self!r}: Writing backplane EEPROM: backplane {backplane_model} '
            f'SN{backplane_serial} rev {backplane_revision}, crate {crate_model} '
            f'SN{crate_serial} rev {crate_revision}, slot {crate_slot}')
        eeprom_dev.write(0, ipmi_bytes)

        if verify:
            readback = eeprom_dev.read(0, len(ipmi_bytes))
            if bytes(readback) != ipmi_bytes:
                raise RuntimeError(f'{self!r}: Backplane EEPROM readback does not match the written FRU image')

        self.logger.info(f'{self!r}: Backplane EEPROM written ({len(ipmi_bytes)} bytes)\n{fru}')
        return fru


    async def is_backplane_present_async(self):
        return False

    async def get_slot_number(self):
        return None

    def _pll_input_to_iceboard_clock_source(self, pll_input):
        """Map CRS PLL input index to IceBoard-style clock source names."""
        if pll_input == self.PLL_INPUT_BP_CLK_10MHZ:
            return 'CLOCK_SOURCE_BP'
        if pll_input == self.PLL_INPUT_SMA_CLK_10MHZ:
            return 'CLOCK_SOURCE_SMA'
        if pll_input == self.PLL_INPUT_ETH_REG_125MHZ:
            return 'CLOCK_SOURCE_XTAL'
        if pll_input is None:
            return 'CLOCK_SOURCE_XTAL'
        return f'CLOCK_SOURCE_PLL_{pll_input}'

    def _get_iceboard_clock_source(self):
        """Return the reference clock source used by the motherboard PLL."""
        pll_input = getattr(self, 'input_reference', None)
        if pll_input is None and hasattr(self, 'pll') and self.pll is not None:
            pll_input = self.pll.get_active_clkin()
        return self._pll_input_to_iceboard_clock_source(pll_input)

    async def get_iceboard_clock_source_async(self):
        return self._get_iceboard_clock_source()

    def get_iceboard_clock_source_sync(self):
        return self._get_iceboard_clock_source()

    def get_motherboard_temperature(self, sensor):
        """ Synchronous wrapper to return motherboard temperature sensor value.
        """
        return None


    #################################
    # Metrics
    #################################

    async def _get_motherboard_metrics_async(self):
        """ Get the CRS hardware monitoring information.

        Returns:
            A (info, metrics) tuple, where:

                ``info`` is a dict containing the status information in the format ``{metric_name:value,
                ...}`` where both ``metric_name`` and ``value`` are strings.

                ``metrics`` is a Metrics object containing the same status information.
        """

        info = dict()
        metrics = await super().get_metrics_async()

        # Add CRS-specific power monitoring data
        try:
            for name, info_dict in self.i2c1_ina231_list.items():
                d = info_dict['device']
                voltage = d.get_bus_voltage()
                current = d.get_current()
                power = d.get_power()
                shunt_voltage = d.get_shunt_voltage()
                
                # Add individual rail metrics
                metrics.add('crs_power_voltage', voltage, rail=name)
                metrics.add('crs_power_current', current, rail=name)
                metrics.add('crs_power_consumption', power, rail=name)
                metrics.add('crs_power_shunt_voltage', shunt_voltage, rail=name)
                
                # Add to info dict with string formatting
                info[f'CRS {name.upper()} Power'] = f'{voltage:.2f}V@{current:.3f}A'
        except Exception as e:
            self.logger.warning(f'{self!r}: Error reading power metrics: {e}')

        # Add temperature metrics (explicitly requested)
        try:
            # Temperature sensors for power rails
            temp_sensors = [
                ('5v0', self.i2c1_tmp421_5v0),
                ('3v3', self.i2c1_tmp421_3v3),
                ('2v5', self.i2c1_tmp421_2v5),
                ('1v8', self.i2c1_tmp421_1v8),
                ('1v2a', self.i2c1_tmp421_1v2a),
                ('1v4', self.i2c1_tmp421_1v4),
                ('1v2b', self.i2c1_tmp421_1v2b),
                ('pll', self.i2c0_tmp421_pll),
            ]
            
            for sensor_name, sensor in temp_sensors:
                try:
                    # get_temperature() returns a tuple (local_temp, remote_temp, remote_fault)
                    # We'll use the local temperature (first element)
                    temp_tuple = sensor.get_temperature()
                    if isinstance(temp_tuple, tuple):
                        temperature = temp_tuple[0]  # Use local temperature
                    else:
                        temperature = temp_tuple  # In case it returns a single value
                    
                    metrics.add('crs_temperature', temperature, sensor=sensor_name)
                    info[f'CRS {sensor_name.upper()} Temp'] = f'{temperature:.1f}°C'
                except Exception as e:
                    self.logger.warning(f'{self!r}: Error reading temperature sensor {sensor_name}: {e}')
                    
        except Exception as e:
            self.logger.warning(f'{self!r}: Error reading temperature metrics: {e}')

        # Add PLL status and reference clock information
        try:
            if hasattr(self, 'pll') and self.pll:
                active_input = self.pll.get_active_clkin()
                active_input_name = 'None' if active_input is None else self.PLL_INPUT_NAMES[active_input]
                metrics.add('crs_pll_active_input', active_input, input_name=active_input_name)
                info['CRS PLL Active Input'] = active_input_name
                
                # PLL reference clock status
                if hasattr(self, 'input_reference'):
                    metrics.add('crs_pll_reference', self.input_reference)
                    info['CRS PLL Reference'] = str(self.input_reference)
        except Exception as e:
            self.logger.warning(f'{self!r}: Error reading PLL metrics: {e}')

        # Add QSFP status
        try:
            if hasattr(self, 'i2c1_qsfp') and self.i2c1_qsfp:
                qsfp_present = self.i2c1_qsfp.is_present()
                metrics.add('crs_qsfp_present', int(qsfp_present))
                info['CRS QSFP Present'] = 'Yes' if qsfp_present else 'No'
        except Exception as e:
            self.logger.warning(f'{self!r}: Error reading QSFP metrics: {e}')

        # Add GPIO states for QSFP control
        try:
            if hasattr(self, 'i2c1_gpios') and self.i2c1_gpios:
                gpio_states = self.i2c1_gpios.read_all()
                for gpio_name, state in gpio_states.items():
                    metrics.add('crs_gpio_state', int(state), gpio=gpio_name)
                    info[f'CRS {gpio_name}'] = 'High' if state else 'Low'
        except Exception as e:
            self.logger.warning(f'{self!r}: Error reading GPIO metrics: {e}')

        return (info, metrics)
    
    async def get_metrics_async(self):
        """ Get the CRS hardware monitoring information.

        Returns:
            a :class:`Metrics` object containing the IceBoard monitoring information.
        """
        try:
            _, metrics = await self._get_motherboard_metrics_async()
        except Exception as e:
            self.logger.error('%r: Error getting CRS hardware metrics. Error is %r' % (self, e))
            metrics = Metrics()
        return metrics

    async def get_total_power_async(self):
        """ Return the total power used by this board.

        This reads power only from the 'vbp' (backplane voltage) rail,
        which represents the total board consumption.

        Returns:
            Total power in watts as a float.
        """
        try:
            # Get power from the backplane voltage rail (vbp)
            # This represents the total power consumption of the board
            vbp_info = self.i2c1_ina231_list['vbp']
            d = vbp_info['device']
            total_power = d.get_power()
            return total_power
            
        except Exception as e:
            self.logger.error(f'{self!r}: Error reading vbp power: {e}')
            return None

