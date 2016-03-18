#!/usr/bin/python
# Disable pylint Line too long (=C0301)
# pylint: C0301

"""iceboard_hardware.py module: Provides a class to access the hardware of
IceBoard (McGill Model MGK7MB).
"""

import logging

# Import IceBoard hardware handlers
# from lib import tmp100  # I2C Temperature sensor
from lib import pca9575  # I2C 16-bit IO Expander
from lib import tca9548a  # I2C switch
# from lib import ina230  # I2C Voltage and current monitor
from lib import eeprom
from lib import qsfp


class GPIO(object):
    """Provides a single-point, abstracted access to all GPIO bits found on the IceBoard"""

    def __init__(self, gpio_table):
        self._gpio_table = gpio_table

    def read(self, name, select=True):
        if name not in self._gpio_table:
            raise RuntimeError("'%s'  is not a valid QSFP Control bit name" % name)
        (io_expander, byte, bit, width) = self._gpio_table[name]
        value = io_expander.read(byte, select=select)
        value = (value >> bit) & ((1 << width)-1)
        return value

    def write(self, name, value, select=True):
        if name not in self._gpio_table:
            raise ValueError("'%s'  is not a valid GPIO name" % name)
        (io_expander, byte, bit, width) = self._gpio_table[name]
        if width == 1:
            value = bool(value)
        elif value < 0 or value >= (1 << width):
            raise ValueError("%i is an invalid value for GPIO field '%s'" % (value, name))
        io_expander.write(byte, value << bit, mask= ((1 << width)-1) << bit, select=select)


class IceBoardHardware(object):
    """
    Provides access to the hardware of an IceBoard Rev2/Rev3, including:
        - LED control
        - GPIO input/output
        - Temperature sensors
        - Power monitoring for every rail (voltage,current)
        - Motherboard EEPROM

    This class implements these methods by issuring I2C commands
    through the I2C interface provided either by the FPGA or by the
    ARM.

    Part or all of of functionnalities described in this class might
    eventually be implemented in the ARM processor itself. In thise
    case, these those will be available through the ARM's Tuber
    interface.
    """

    #------------------------------------
    # Define hardware-specific constants
    #------------------------------------
    NUMBER_OF_FMC_SLOTS = 2  # Indicates the number of FMC slots supported by this platform

    # I2C switch addresses (visible on all buses on a specific port)
    _FPGA_I2C_SWITCH_ADDR = 0b1110100  # 0x74
    _ARM0_I2C_SWITCH_ADDR = 0b1110000  # 0x70
    _ARM1_I2C_SWITCH_ADDR = 0b1110001  # 0x71
    _ARM2_I2C_SWITCH_ADDR = 0b1110010  # 0x72
    _ARM3_I2C_SWITCH_ADDR = 0b1110011  # 0x73

    FPGA_I2C_BUS_LIST = {
        # Bus name, (FPGA I2C port, I2C switch enable bit)
        "FMC":   (0, 0),  # equivalent to FMCA. Included for backwards compatibility with single-FMC code
        "FMCA":  (0, 0),
        "FMCB":  (0, 1),
        "QSFPA": (0, 2),
        "QSFPB": (0, 3),
        "SFP":   (0, 4),
        "SMPS":  (0, 5),
        "BP":    (0, 6),
        "GPIO":  (0, 7)
        }

    # FMC EEPROM, on FMCA or FMCB
    _FMC_EEPROM_ADDR = 0x50  # 0x50 (0x51 is also used for the 2nd page of large eeprom with address width > 16 bits).
    _FMC_EEPROM_ADDR_WIDTH = 7  # FMC EEPROM internal addresses are 7 bits wide.
    _FMC_EEPROM_PAGE_SIZE = 8  #

    # Oversize , non-FMC-standard EEPROM found on some McGill Mezzanines
    _MCGILL_FMC_EEPROM_ADDR_WIDTH = 17  # FMC EEPROM internal addresses are is 17 bits wide. (2 bytes as data, 1 bit in lsb of I2C address)
    _MCGILL_FMC_EEPROM_PAGE_SIZE = 256  #

    # Motherboard EEPROM
    _MOTHERBOARD_EEPROM_DATA_ADDR = 0x57  #
    _MOTHERBOARD_EEPROM_SERIAL_ADDR = 0x5F  #
    _MOTHERBOARD_EEPROM_ADDR_WIDTH = 7  # EEPROM internal addresses are is 17 bits wide. (2 bytes as data, 1 bit in lsb of I2C address)
    _MOTHERBOARD_EEPROM_PAGE_SIZE = 8  #


    # IO Expanders, on GPIO bus
    _GPIO_POWER_I2C_ADDR    = 0b0100000  # 0x20
    _GPIO_SFP_QSFP_I2C_ADDR = 0b0100001  # 0x21
    _GPIO_SW_LEDS_ADDR      = 0b0100010  # 0x22
    _GPIO_ARM_PHY_LEDS_ADDR = 0b0100011  # 0x23

    # # Temperature sensors, on GPIO bus
    # _TMP_ARM_I2C_ADDR   = 0b1001010 #0x4A
    # _TMP_PHY_I2C_ADDR   = 0b1001100 #0x4C
    # _TMP_FPGA_I2C_ADDR  = 0b1001011 #0x4B
    # _TMP_POWER_I2C_ADDR = 0b1001000 #0x48

    # # Power monitors, on SMPS bus
    # _POWER_ICEVADJ_I2C_ADDR   = 0b1000011 #0x43
    # _POWER_ICE12V0_I2C_ADDR   = 0b1000111 #0x47
    # _POWER_ICE5V0_I2C_ADDR    = 0b1001000 #0x48
    # _POWER_ICE3V3_I2C_ADDR    = 0b1001001 #0x49
    # _POWER_ICE1V5_I2C_ADDR    = 0b1001100 #0x4C
    # _POWER_ICE1V2_I2C_ADDR    = 0b1001101 #0x4D
    # _POWER_ICE1V0_I2C_ADDR    = 0b1001110 #0x4E
    # _POWER_ICE1V8_I2C_ADDR    = 0b1001011 #0x4B
    # _POWER_ICE1V0GTX_I2C_ADDR = 0b1001111 #0x4F

    # _POWER_FMCA12V0_I2C_ADDR  = 0b1000000 #0x40
    # _POWER_FMCA3V3_I2C_ADDR   = 0b1000001 #0x41
    # _POWER_FMCAVADJ_I2C_ADDR  = 0b1000010 #0x42

    # _POWER_FMCB12V0_I2C_ADDR  = 0b1000100 #0x44
    # _POWER_FMCB3V3_I2C_ADDR   = 0b1000101 #0x45
    # _POWER_FMCBVADJ_I2C_ADDR  = 0b1000110 #0x46

    def __init__(self, iceboard):
        """
        Creates all the I2C objects needed to interface the hardware. In order
        to do this, the following methods will be required from the iceboard
        object:

            - i2c_set_port(...) # Port number 0 (connected to the FPGA I2C switch) is used for all accesses
            - i2c_write_read(...) # FPGA I2C engine

        Those methods can be provided either by the ARM or the core FPGA firmware.

        For FPGA-based I2C:
            - fpga_core is not Null
            - fpga_core provides the following methods
        """

        self._logger = logging.getLogger(__name__)
        self._logger.debug('Initializing Iceboard hardware')
        self._iceboard = iceboard
        self._i2c = self._iceboard.i2c

        self._logger.info(' Instantiating Motherboard EEPROM managers')
        self._motherboard_eeprom_data = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_DATA_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)
        self._motherboard_eeprom_serial = eeprom.eeprom(self._i2c, self._MOTHERBOARD_EEPROM_SERIAL_ADDR, 'GPIO', self._MOTHERBOARD_EEPROM_ADDR_WIDTH, self._MOTHERBOARD_EEPROM_PAGE_SIZE)

        self._logger.info(' Instantiating FMC EEPROM managers')

        # We check if the EEPROM has multiple pages, and if so, we *assume* that
        # the EEPROM is a large (non-FMC compliant) EEPROM with 2-byte addresses.
        # Otherwise we assume the EEPROM has a single byte of addressing.
        #
        # If the EEPROM is multipage but has a single address byte (4Kbit,
        # 8kbit or 16kbit EEPROMs), then the EEPROM contents will be
        # corrupted, even by read operations, because the second address byte
        # will be interpreted as data to be written. It would be equally bad
        # if there has another I2C device at the address following the EEPROM
        # address.
        if self._i2c.is_present(self._FMC_EEPROM_ADDR+1, bus_name='FMCA'):
            self._logger.info('Detected multipage EEPROM on FMCA. Assuming >16-bit addressing.')
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmca_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCA', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        if self._i2c.is_present(self._FMC_EEPROM_ADDR+1, bus_name='FMCB'):
            self._logger.info('Detected multipage EEPROM on FMCB. Assuming >16-bit addressing.')
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._MCGILL_FMC_EEPROM_ADDR_WIDTH, self._MCGILL_FMC_EEPROM_PAGE_SIZE)
        else:
            self._fmcb_eeprom = eeprom.eeprom(self._i2c, self._FMC_EEPROM_ADDR, 'FMCB', self._FMC_EEPROM_ADDR_WIDTH, self._FMC_EEPROM_PAGE_SIZE)

        self._FMC_EEPROM_TABLE = {
            1: self._fmca_eeprom,
            2: self._fmcb_eeprom
            }


        self._logger.info(' Instantiating I2C GPIO manager')
        self._gpio_power = pca9575.pca9575(self._i2c, self._GPIO_POWER_I2C_ADDR, 'GPIO')
        self._gpio_sw_leds = pca9575.pca9575(self._i2c, self._GPIO_SW_LEDS_ADDR, 'GPIO')
        self._gpio_arm_phy_leds = pca9575.pca9575(self._i2c, self._GPIO_ARM_PHY_LEDS_ADDR, 'GPIO')
        self._gpio_sfp_qsfp = pca9575.pca9575(self._i2c, self._GPIO_SFP_QSFP_I2C_ADDR, 'GPIO')




        self._gpio = GPIO(gpio_table={
            # name : (expander object, byte, lsb bit number,  width)
            'GP_SW1': (self._gpio_sw_leds, 0, 0, 1),
            'GP_SW2': (self._gpio_sw_leds, 0, 1, 1),
            'GP_SW3': (self._gpio_sw_leds, 0, 2, 1),
            'GP_SW4': (self._gpio_sw_leds, 0, 3, 1),
            'GP_SW5': (self._gpio_sw_leds, 0, 4, 1),
            'GP_SW6': (self._gpio_sw_leds, 0, 5, 1),
            'GP_SW7': (self._gpio_sw_leds, 0, 6, 1),
            'GP_SW8': (self._gpio_sw_leds, 0, 7, 1),
            'GP_LED1': (self._gpio_sw_leds, 1, 7, 1),
            'GP_LED2': (self._gpio_sw_leds, 1, 6, 1),
            'GP_LED3': (self._gpio_sw_leds, 1, 5, 1),
            'GP_LED4': (self._gpio_sw_leds, 1, 4, 1),
            'GP_LED5': (self._gpio_sw_leds, 1, 3, 1),
            'GP_LED6': (self._gpio_sw_leds, 1, 2, 1),
            'GP_LED7': (self._gpio_sw_leds, 1, 1, 1),
            'GP_LED8': (self._gpio_sw_leds, 1, 0, 1),
            'GP_LED9': (self._gpio_arm_phy_leds, 0, 0, 1),
            'GP_LED10': (self._gpio_arm_phy_leds, 0, 1, 1),
            'GP_LED11': (self._gpio_arm_phy_leds, 0, 2, 1),
            'GP_LED12': (self._gpio_arm_phy_leds, 0, 3, 1),
            'GTX1V8PowerFault': (self._gpio_arm_phy_leds, 0, 4, 1),
            'PHYAPowerFault': (self._gpio_arm_phy_leds, 0, 5, 1),
            'PHYBPowerFault': (self._gpio_arm_phy_leds, 0, 6, 1),
            'ArmPowerFault': (self._gpio_arm_phy_leds, 0, 7, 1),
            'BP_GPIO0': (self._gpio_arm_phy_leds, 1, 0, 1),
            'BP_GPIO1': (self._gpio_arm_phy_leds, 1, 1, 1),
            'BP_GPIO2': (self._gpio_arm_phy_leds, 1, 2, 1),
            'BP_GPIO3': (self._gpio_arm_phy_leds, 1, 3, 1),
            'BP_GPIO4': (self._gpio_arm_phy_leds, 1, 4, 1),
            'BP_GPIO5': (self._gpio_arm_phy_leds, 1, 5, 1),
            'BP_SLOT_NUMBER': (self._gpio_arm_phy_leds, 1, 0, 4),  # Also corresponds to BP_GPIO0-3
            'QSFPA_ModPrsL': (self._gpio_sfp_qsfp, 0, 0, 1),
            'QSFPA_ResetL': (self._gpio_sfp_qsfp, 0, 2, 1),
            'QSFPA_IntL': (self._gpio_sfp_qsfp, 0, 1, 1),
            'QSFPA_LPMode': (self._gpio_sfp_qsfp, 0, 4, 1),
            'QSFPA_ModSelL': (self._gpio_sfp_qsfp, 0, 3, 1),
            'QSFPB_ModPrsL': (self._gpio_sfp_qsfp, 0, 5, 1),
            'QSFPB_ResetL': (self._gpio_sfp_qsfp, 0, 7, 1),
            'QSFPB_IntL': (self._gpio_sfp_qsfp, 0, 6, 1),
            'QSFPB_LPMode': (self._gpio_sfp_qsfp, 1, 5, 1),
            'QSFPB_ModSelL': (self._gpio_sfp_qsfp, 1, 4, 1),
            'SFP_LOS': (self._gpio_power, 0, 7, 1),
            'SFP_TxFault': (self._gpio_sfp_qsfp, 1, 0, 1),
            'SFP_TxDisable': (self._gpio_sfp_qsfp, 1, 1, 1),
            'SFP_RS0': (self._gpio_sfp_qsfp, 1, 2, 1),
            'SFP_RS1': (self._gpio_sfp_qsfp, 1, 3, 1),
            'SFP_ModSelL': (self._gpio_power, 1, 7, 1)
        })

        self._qsfpa = qsfp.QSFP(self._i2c, 'QSFPA', self._gpio)
        self._qsfpb = qsfp.QSFP(self._i2c, 'QSFPB', self._gpio)

        # self._logger.info(' Instantiating I2C temperature sensors')
        # self._tmp_power = tmp100.tmp100(self._i2c, self._TMP_POWER_I2C_ADDR, 'GPIO')
        # self._tmp_phy = tmp100.tmp100(self._i2c, self._TMP_PHY_I2C_ADDR, 'GPIO')
        # self._tmp_fpga = tmp100.tmp100(self._i2c, self._TMP_FPGA_I2C_ADDR, 'GPIO')
        # self._tmp_arm = tmp100.tmp100(self._i2c, self._TMP_ARM_I2C_ADDR, 'GPIO')

        # self._logger.info(' Instantiating I2C current/power monitors')
        # self._power_ice_3v3 = ina230.ina230(self._i2c, self._POWER_ICE3V3_I2C_ADDR, 'SMPS')
        # self._power_ice_12v0 = ina230.ina230(self._i2c, self._POWER_ICE12V0_I2C_ADDR, 'SMPS')
        # self._power_ice_5v0 = ina230.ina230(self._i2c, self._POWER_ICE5V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0_gtx = ina230.ina230(self._i2c, self._POWER_ICE1V0GTX_I2C_ADDR, 'SMPS')
        # self._power_ice_vadj = ina230.ina230(self._i2c, self._POWER_ICEVADJ_I2C_ADDR, 'SMPS')
        # self._power_ice_1v2 = ina230.ina230(self._i2c, self._POWER_ICE1V2_I2C_ADDR, 'SMPS')
        # self._power_ice_1v5 = ina230.ina230(self._i2c, self._POWER_ICE1V5_I2C_ADDR, 'SMPS')
        # self._power_ice_1v0 = ina230.ina230(self._i2c, self._POWER_ICE1V0_I2C_ADDR, 'SMPS')
        # self._power_ice_1v8 = ina230.ina230(self._i2c, self._POWER_ICE1V8_I2C_ADDR, 'SMPS')

        # self._power_fmca_12v0 = ina230.ina230(self._i2c, self._POWER_FMCA12V0_I2C_ADDR, 'SMPS')
        # self._power_fmca_3v3 = ina230.ina230(self._i2c, self._POWER_FMCA3V3_I2C_ADDR, 'SMPS')
        # self._power_fmca_vadj = ina230.ina230(self._i2c, self._POWER_FMCAVADJ_I2C_ADDR, 'SMPS')

        # self._power_fmcb_12v0 = ina230.ina230(self._i2c, self._POWER_FMCB12V0_I2C_ADDR, 'SMPS')
        # self._power_fmcb_3v3 = ina230.ina230(self._i2c, self._POWER_FMCB3V3_I2C_ADDR, 'SMPS')
        # self._power_fmcb_vadj = ina230.ina230(self._i2c, self._POWER_FMCBVADJ_I2C_ADDR, 'SMPS')


        # self.TEMPERATURE_SENSOR_TABLE = {
        #     # sensor name: tmp object
        #     'TEMP_POWER': self._tmp_power,
        #     'TEMP_PHY': self._tmp_phy,
        #     'TEMP_FPGA': self._tmp_fpga,
        #     'TEMP_ARM': self._tmp_arm
        # }

        # self.POWER_SENSOR_TABLE = {
        #     # sensor name : (ina230 object, output voltage(volts), rshunt(inductor) (mohm), typical current(amps), current tolerance (0<tol<1))
        #     'ICE_3V3': (self._power_ice_3v3, 3., 2.36, 8., 0.5),         #SER1360-182L
        #     'ICE_12V0': (self._power_ice_12v0, 12., 5.5, 3., 0.5),       #SER1360-602L
        #     'ICE_5V0': (self._power_ice_5v0, 5., 2.36, 11., 0.5),        #SER1360-182L
        #     'ICE_1V0_GTX': (self._power_ice_1v0_gtx, 1., 0.77, 16., 0.5),#SER1360-331L
        #     'ICE_1V2': (self._power_ice_1v2, 1.2, 0.77, 8., 0.5),        #SER1360-651L
        #     'ICE_1V5': (self._power_ice_1v5, 1.5, 2.36, 3., 0.5),        #SER1360-182L
        #     'ICE_1V0': (self._power_ice_1v0, 1., 0.77, 16., 0.5),        #SER1360-331L
        #     'ICE_1V8': (self._power_ice_1v8, 1.8, 5.5, 1., 0.5),         #SER1360-602L
        #     'ICE_VADJ': (self._power_ice_vadj, 2.5, 2.36, 8., 0.5),
        #     'FMCA_12V0': (self._power_fmca_12v0, 12., 5, 2., 0.5),
        #     'FMCB_12V0': (self._power_fmcb_12v0, 12., 5, 2., 0.5),
        #     'FMCA_3V3': (self._power_fmca_3v3, 3., 5, 5., 0.5),
        #     'FMCB_3V3': (self._power_fmcb_3v3, 3., 5, 5., 0.5),
        #     'FMCA_VADJ': (self._power_fmca_vadj, 2.5, 5, 1., 0.5),
        #     'FMCB_VADJ': (self._power_fmcb_vadj, 2.5, 5, 1., 0.5)
        # }

    def open(self):
        """
        """
        pass


    def close(self):
        self._logger.info('Closing Iceboard hardware')
        if self._i2c:
            self._i2c = None

    def init(self):
        """Initializes the motherboard hardware to a known state.
        This will turn off FMC power.
        """
        self._init_gpio_expanders()
        # self._init_temperature_sensors()
        # self.set_fmc_power()
        # self._init_power_sensors()

        # Initialize the QSFPs. This sets the reset and LowPower mode. Some
        # QSFP+ modules (like the 3M AOCs) will not work without this.
        self._qsfpa.init()
        self._qsfpb.init()

    def _init_gpio_expanders(self):
        """
        Initializes GPIO expanders

        History

        140304 JM: created. todo: make more flexible for I/O pin configuration
        of each expander. Need to confirm I/O pin config with JF
        """
        self._gpio_power.init(cfg0_def=0b10101000, cfg1_def=0b10101000,
                              out0_default=0, out1_default=0)
        self._gpio_sw_leds.init(cfg1_def=0b00000000)
        self._gpio_arm_phy_leds.init(cfg0_def=0b11110000)
        self._gpio_sfp_qsfp.init(cfg0_def=0b01100011, cfg1_def=0b11001111)

    def get_i2c_interface(self):
        """
        Returns an I2C interface object that provides a standardized bus selection.
        """
        return self._i2c

    def get_number_of_fmc_slots(self):
        return self.NUMBER_OF_FMC_SLOTS

    # def get_slot_number(self):
    #     return self._gpio.read('BP_SLOT_NUMBER') + 1

    def read_motherboard_eeprom(self, addr, length, **kwargs):
        return self._motherboard_eeprom_data.read(addr, length, **kwargs)

    def write_motherboard_eeprom(self, addr, data, **kwargs):
        return self._motherboard_eeprom_data.write(addr, data, **kwargs)

    def read_mezzanine_eeprom(self, mezzanine, addr, length, **kwargs):
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.read(addr, length, **kwargs)

    def write_mezzanine_eeprom(self, mezzanine, addr, data, **kwargs):
        eeprom_object = self._FMC_EEPROM_TABLE[mezzanine]
        return eeprom_object.write(addr, data, **kwargs)

    def set_mezzanine_power(self, fmc_number=range(NUMBER_OF_FMC_SLOTS), state=[True]*NUMBER_OF_FMC_SLOTS):
        """
        Enables or disables power of the specified FMC slot.
        Proper power sequencing is done to prevent the FMC board switchers to create too much a current spike when enabled.

        History:
            140223 JFC: Modified to use register names.
            140304 JM: Modified it so a state for every fmc can be specified. For now, state is either a boolean or a list of booleans with the same length as 'fmc_number'
        Todo:
            140223 JFC: used masked writes to avoid side effects.
        """
        if isinstance(fmc_number, int):
            fmc_number = [fmc_number]

        if isinstance(state, (bool, int)):
            state = [state] * len(fmc_number)

        for (fmc,fmc_state) in zip(fmc_number,state):
            if fmc not in range(self.NUMBER_OF_FMC_SLOTS):
                raise ValueError('FMC number %i is not a valid value' % fmc)
            else:
                # out_reg = 'OUT%i' % fmc # sets the register name to access based on the FMC number
                #cfg_reg = 'CFG%i' % fmc
                # self._gpio_power.write(out_reg, 0b00000000) # Turn off all power signals before we enable the GPIO outputs
                #self._gpio_power.write(cfg_reg, 0b10101000)
                self._gpio_power.write(fmc, 0b00000111*bool(fmc_state))  # Turn on power to board
                self._gpio_power.write(fmc, 0b01010111*bool(fmc_state))  # Set Power Good and CLKDIR to 1

    def set_led(self, led_name, state):
        """
        Set the LED(s) specified in 'led_name' to the the 'state'.
        'led_name' can be a list of LED names found in
        gpio object.  'state' can be a single boolean value, or
        an array with the same length as 'led_name'
        """
        if isinstance(led_name, str):
            led_name = [led_name]

        if isinstance(state, (bool, int)):
            state = [state] * len(led_name)

        for (led, led_state) in zip(led_name, state):
            self._gpio.write(led, led_state)

    def get_led(self, led_name):
        """
        Returns the status of specified LED(s) in a dictionary
        led_status where each key is a led_name and the respective value
        is the led status.
        """
        led_status = {}
        if isinstance(led_name, str):
            led_name = [led_name]

        for led in led_name:
            led_status[led] = self._gpio.read(led)

        return led_status

    # def _init_temperature_sensors(self, temperature_sensor_name=None, bit_resolution=12):
    #     """ Initialize temperature sensors.

    #     'temperature_sensor_name' can be a list of temperature sensor names
    #     found in TEMPERATURE_SENSOR_TABLE. If temperature_sensor_name=None,
    #     all sensors in the list are initialized.

    #     'bit_resolution' is the number of bits of resolution of the
    #     temperature register. It can take values 9, 10, 11, 12

    #     History:
    #     140318 JM: created
    #     """
    #     if bit_resolution<9 or bit_resolution>12:
    #         raise self.ValueError('Bit_resolution is out of range. Must be 9,10,11 or 12 bits')
    #     else:
    #         if temperature_sensor_name == None:
    #             temperature_sensor_name = self.TEMPERATURE_SENSOR_TABLE.keys()
    #         elif isinstance(temperature_sensor_name, str):
    #             temperature_sensor_name = [temperature_sensor_name]

    #         for temp_sensor in temperature_sensor_name:
    #             if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
    #                 raise ValueError('Invalid temperature sensor name. Valid names are %s' % ','.join(self.TEMPERATURE_SENSOR_TABLE.keys()))
    #             else:
    #                 tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
    #                 try:
    #                     tmp_object.init(bit_resolution)
    #                 except:
    #                     self._logger.info('%.32r: Temperature sensor %s failed to initialize.' % (self._iceboard, temp_sensor))

    # def _init_power_sensors(self, power_sensor_name=None):
    #     """
    #     initializes current/power monitors
    #     'power_sensor_name' can be a list of current/power monitor names found in POWER_SENSOR_TABLE. If power_sensor_name=None, all sensors in
    #     POWER_SENSOR_TABLE are initialized.

    #     History:
    #     140320 JM: created
    #     """
    #     if power_sensor_name == None:
    #         power_sensor_name = self.POWER_SENSOR_TABLE.keys()
    #     elif isinstance(power_sensor_name, str):
    #         power_sensor_name = [power_sensor_name]

    #     for power_sensor in power_sensor_name:
    #         if power_sensor not in self.POWER_SENSOR_TABLE:
    #            raise ValueError('Invalid power sensor name. Valid names are %s' % ','.join(self.POWER_SENSOR_TABLE.keys()))
    #         else:
    #             power_sensor_object, v_out, r_shunt, i_typ, tol_i = self.POWER_SENSOR_TABLE[power_sensor]
    #             try:
    #                 power_sensor_object.init(v_out=v_out, r_shunt=r_shunt, i_typ=i_typ, tol_i=tol_i)
    #             except:
    #                 self._logger.info('%.32r: Power sensor %s failed to initialize.' % (self._iceboard, power_sensor))


    # def get_temperature(self, temperature_sensor_name=None):
    #     """
    #     Returns the current temperature measured on the specified
    #     sensor(s).  NOTE: some temperatures are taken from the FPGA
    #     inetrnal SYSTEM monitor.

    #     initializes temperature expanders
    #     'temperature_sensor_name' can be a list of temperature sensor
    #     names found in TEMPERATURE_SENSOR_TABLE

    #     Returns a dictionary with keys corresponding to the temperature_sensor_name names.

    #     History:
    #     140318 JM: created
    #     """
    #     temperature_dict = {}
    #     if temperature_sensor_name == None:
    #         temperature_sensor_name = self.TEMPERATURE_SENSOR_TABLE.keys()
    #     elif isinstance(temperature_sensor_name, str):
    #         temperature_sensor_name = [temperature_sensor_name]

    #     for temp_sensor in temperature_sensor_name:
    #         if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
    #             raise ValueError('Invalid temperature sensor name')
    #         else:
    #             tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
    #             temperature_dict[temp_sensor] = tmp_object.get_temperature()

    #     return temperature_dict

    # def get_power(self, power_sensor_name=None):
    #     """
    #     Returns the voltage, current and power of the power monitoring system.
    #     Includes power measured internally from  the FPGA's system monitor.
    #     Multiple targets can be specified.

    #     Arguments:

    #        'power_sensor_name' can be a list of temperature sensor
    #         names found in POWER_SENSOR_TABLE. If
    #         power_sensor_name=None, measurements of all sensors in
    #         TEMPERATURE_SENSOR_TABLE are returned.

    #     Returns dictionary with keys corresponding to the
    #     power_sensor_name names. The respective value is a (bus
    #     voltage (V), shunt voltage (V), current (A), power (W)) tuple.

    #     History:
    #     140320 JM: created
    #     """
    #     power_dict = {}
    #     if power_sensor_name == None:
    #         power_sensor_name = self.POWER_SENSOR_TABLE.keys()
    #     elif isinstance(power_sensor_name, str):
    #         power_sensor_name = [power_sensor_name]

    #     for power_sensor in power_sensor_name:
    #         if power_sensor not in self.POWER_SENSOR_TABLE:
    #             raise ValueError('Invalid power sensor name. Valid names are %s.' % ','.join(self.POWER_SENSOR_TABLE.keys()))
    #         else:
    #             power_sensor_object = self.POWER_SENSOR_TABLE[power_sensor][0]

    #             try:
    #                 bus_voltage = power_sensor_object.get_bus_voltage()
    #                 shunt_voltage = power_sensor_object.get_shunt_voltage()
    #                 current = power_sensor_object.get_current()
    #                 power =  power_sensor_object.get_power()
    #             except:
    #                 bus_voltage = None
    #                 shunt_voltage = None
    #                 current = None
    #                 power = None
    #             power_dict[power_sensor]=(bus_voltage, shunt_voltage, current, power)

    #     return power_dict


    # def get_serial_number(self):
    #     """
    #     Returns the board's serial number. which is actually the FPGA's
    #     serial number.
    #     """
    #     return self._iceboard.get_serial_number(); # tentative code

    # def get_info(self):
    #     """Loads the info data on the motherboard"""
    #     pass

    # def status(self):
    #     """Displays the status of the motherboard"""


class I2CInterface(object):
    """
    This class wraps all that is needed to access an I2C device in
    a standardized way, whether the access is done through the
    FPGA or through the ARM.
    """

    I2CException = SystemError # Exception object to expect from I2C communication errors

    def __init__(self, write_read_fn, port_select_fn, bus_table, _switch_addr, verbose=None):
        self.write_read_fn = write_read_fn
        self.set_port_fn = port_select_fn
        self._I2C_BUS_LIST = bus_table

        self._i2c_switch = tca9548a.tca9548a(self, _switch_addr)
        self._logger = logging.getLogger(__name__)

    def select_bus(self, bus_names, *args, **kwargs):
        """
        Configure the I2C port and I2C switch so the following
        communications will access the desired I2C bus. 'bus_id'
        can be a bus name or bus number, or a list of those if
        multiple buses are to be accessed at the same time. An
        error will be provided if all the buses are not accessible
        through the same FPGA I2C port. This function assumes that
        each FPGA I2C port has an identical I2C switch.
        """
        if isinstance(bus_names, (str, int)):
            bus_names = [bus_names]
        selected_fpga_port_number = None
        selected_switch_port_numbers = []
        for bus_name in bus_names:
            if bus_name not in self._I2C_BUS_LIST:
                self._logger.error("I2C bus '%s' is not part of the available buses. Valid values are %s" % (bus_name, ','.join(str(self._I2C_BUS_LIST.keys()))) )
            (fpga_port_number, switch_port_number) = self._I2C_BUS_LIST[bus_name]
            if selected_fpga_port_number is None:
                selected_fpga_port_number = fpga_port_number
            elif selected_fpga_port_number != fpga_port_number:
                self._logger.error("I2C bus '%s' is not on the same FPGA port as the other buses" % (bus_name) )
            selected_switch_port_numbers.append(switch_port_number)
            # self._logger.debug("Enabling I2C bus %s" % bus_name)

        if selected_fpga_port_number is not None:
            self.set_port_fn(selected_fpga_port_number)

        self._i2c_switch.set_port(selected_switch_port_numbers, *args, **kwargs)

    def write_read(self, *args, **kwargs):
        """
        Writes up to 3 bytes to the addressed I2C device and/or
        reads up to 4 bytes from that device after a restart. See
        the FPGA I2C module for detailed method description.
        """
        # self._logger.debug("Accessing I2C bus...")
        return self.write_read_fn(*args, **kwargs)

    def is_present(self, addr, bus_name=None):
        """ Test the presence of an I2C device at the specified address.
        """
        if bus_name:
            self.select_bus(bus_name, retry=3)
        try:
            self.write_read(addr, data=[], read_length=0, retry=0 ) #dummy I2C acces
        except self.I2CException:
            return False
        return True
