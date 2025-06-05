"""icecrate_handler.py module: Provides a class to access the hardware of ICE backplane (McGill Model MGK7BP).
"""

import logging
import time

from pychfpga.common import Ccoll
from pychfpga.hardware import Crate

from .icecore.hardware_assets import IceCrateBase

from ..i2c_devices.eeprom import eeprom as EEPROM
from ..i2c_devices import ina230  # I2C Voltage and current monitor
from ..i2c_devices import tmp421  # I2C temperature sensor
from ..i2c_devices import pca9698  # I2C 40-bit IO Expander
from ..i2c_devices import amc6821  # I2C fan Controller
from ..i2c_devices import pca9575  # 1-slot backplane I2C IO Expander
from ..i2c_devices import gpio
from ..i2c_devices import qsfp


class MasterIceboardObjectProxy(object):
    def __init__(self, crate_object, iceboard_object_name):
        """ Create a proxy object that can be used to access attributes of a master motherbord's
        object while deferring resolution of that master motherboard.

        Accessing any attribute of this proxy object will 1) cause the master_iceboard to be resolved,
        2) the proxied object will be obtained from it, and 3) the desired attribute will be fetched from
        that object.

        A proxy object is useful when we need to provide an object that accesses a master
        motherboard object before the list of motherboards is not yet available.

        Parameters:

            crate_object (IceCrate): Icecrate object through which the ``master_iceboard`` will be accessed.

            iceboard_object_name (str): Name of the ``master_iceboard`` object to be proxied
        """
        self._crate = crate_object
        self._iceboard_object_name = iceboard_object_name

    def __getattr__(self, name):
        """ Return the attribute of a ``master_iceboard`` proxied object.

        Parameters:

            name (str): name of the attribute to be fetched from the proxied object.

        Returns:

            Requested attribute.
        """
        obj = getattr(self._crate.master_iceboard, self._iceboard_object_name)
        return getattr(obj, name)


class CRSBPBase(Crate):
    """
    Generic IceCrate base class that defines the methods and attributes common to various IceCrate models.
    """

    # This is still a generic class, so set part number to ``None``. Part numbers will be defined in subclases.
    part_number = None
    _ipmi_part_numbers = None  # Must match part number in IPMI data


    def __init__(self, **kwargs):

        super().__init__(**kwargs)

        # Create an I2C proxy object that will resolve the master motherboard and get its I2C object
        # only when its attributes are accessed. This allows us to create the I2C devices at object
        # instantiatiation (``__init__``) even if the motherboards have not yet been associated with
        # the crate (i.e. ``slot`` is empty).
        self.i2c_proxy = MasterIceboardObjectProxy(self, 'i2c')

    def init(self):
        pass

    @property
    def master_iceboard(self):
        """ Return the first valid iceboard available in the crate.

        Notes:
            - The ``slots`` dict must be properly populated before this property is used. It
              therefore cannot be accessed directly during the ``__init__`` method of the crate,
              because the list of slots it not yet populated. All references to a master iceboard
              attribute must be done through an object that will defer access to the master
              iceboard.
        """

        # Create a list of (slot, motherboard) tuples that are available to this crate, keeping only
        # those with valid hostname and serial numbers.
        mbs = [(slot, iceboard)
                            for (slot, iceboard) in self.slot.items()
                            if iceboard.hostname or iceboard.serial]
        # Sort the tuple list in increasing slot number and take the first element
        first_slot, first_mb = sorted(mbs)[0]
        return first_mb

    _BP_RX_TO_TX_MAP = {}
    _BP_TX_TO_RX_MAP = {tx: rx for (rx, tx) in _BP_RX_TO_TX_MAP.items()}
    _BP_RX_NET_LENGTH = {}

    @classmethod
    def get_matching_tx(cls, rx_slot_lane_tuple):
        return cls._BP_RX_TO_TX_MAP[rx_slot_lane_tuple]

    @classmethod
    def get_matching_rx(cls, tx_slot_lane_tuple):
        return cls._BP_TX_TO_RX_MAP[tx_slot_lane_tuple]

    @classmethod
    def get_pcb_link_map(cls):
        """
        Return a dictionnary that maps all receiver id to transmitter id. It is in the format:
            {(rx_slot,rx_lane): (tx_slot:tx_lane),...}
        """
        return cls._BP_RX_TO_TX_MAP

    @classmethod
    def get_rx_net_length(cls, rx_slot_lane_tuple):
        return cls._BP_RX_NET_LENGTH[rx_slot_lane_tuple]

    def get_string_id(self):
        """ Return a string composed of the backplane model and serial number
        that can be used to uniquely identify a crate in a system


        Returns:

            str: a string in the format backplane_model_name_crate_serial_number.

        """
        return '%s_SN%s' % (self.part_number, str(self.serial))

    def get_id(self, slot=None):
        """ Return a tuple that describes the crate and an optional slot number.

        Parameters:

            slot: If not None, the `slot` parameter is appnded to the tuple.


        Returns:

            (int, ): (crate_number, )  if a crate_number is not None
            (str, ): (string_crate_id, ) Use the model/serial string instead if there is no crate number
            (int, slot) or (str, slot) if a slot is provided
        """
        if self.crate_number is not None:
            return (self.crate_number, ) if slot is None else (self.crate_number, slot)
        else:
            return (self.get_string_id(), ) if slot is None else (self.get_string_id(), slot)

    def get_number_of_slots(self):
        return self.NUMBER_OF_SLOTS



class CRS4SBP(CRSBPBase):
    """ IceCrate handler that provides access to the backplane through an IceBoard.
    """
    part_number = '4SBP'
    _ipmi_part_numbers = ['4SBP']  # Must match part number in IPMI data

    #####################################
    # Define hardware-specific constants
    #####################################
    NUMBER_OF_SLOTS = 4  #
    BACKPLANE_EEPROM_DATA_ADDRESS = 0x54  # covers 0x54 - 0x57 ( 4 pages of 256 bytes, 1024 Bytes total)
    BACKPLANE_EEPROM_SERIAL_ADDRESS = 0x5C  # 16 byte serial number starting at memory address 0x80
    BACKPLANE_EEPROM_ADDRESS_WIDTH = 10  # 2 bits are in the device address,
    #   the remaining are in the address byte following the command byte
    BACKPLANE_EEPROM_PAGE_SIZE = 16

    BACKPLANE_QSFP_ADDRESS = 0x50  # QSFP standard address (it is the same for all QSFPs)
    BACKPLANE_QSFP_ADDRESS_WIDTH = 8

    _QSFP_CTRL_SETA_ADDR = 0x20
    _QSFP_CTRL_SETB_ADDR = 0x22
    _RESETS_CTRL_ADDR = 0x24

    _TMP_SLOT1_ADDR = 0x4E
    _TMP_SLOT16_ADDR = 0x4D

    _FAN_CTRL_ADDR = 0x18  # AMC6821 Fan controller, connected on backplane external I2C connector

    _POWER_3V3_ADDR = 0x40

    # The following dictionnary describes the connectivity of the 10 Gbps mesh.
    # It indicates which transmitter (slot and lane number) is feeding a specified receiver.
    # The dictionnary is indexed by receiver number.
    _BP_TX_TO_RX_MAP = { # (Tx_slot, Tx_lane):(Rx_slot, Rx_lane), slots are 1-based
     (1, 1): (4, 3), (1, 2): (3, 3), (1, 3): (2, 3),
     (2, 1): (1, 3), (2, 2): (3, 2), (2, 3): (4, 2),
     (3, 1): (4, 1), (3, 2): (2, 2), (3, 3): (1, 2),
     (4, 1): (3, 1), (4, 2): (2, 1), (4, 3): (1, 1)}

    _BP_RX_TO_TX_MAP = {rx: tx for (tx, rx) in _BP_TX_TO_RX_MAP.items()}

    # length of the backplane PCB traces that connect two GTXes, in mils, indexed by the (slot,
    # lane) id of the receiver node. Slot number is one-based, lane is zero-based. Lane 0 is not
    # physically present (it is the internal lane)
    _BP_RX_NET_LENGTH = { # (rx_slot, rx_lane): length_in_mils
        (1, 0): 0,
        (1, 1): 13131.376, (1, 2): 11758.891, (1, 3): 10895.916, (1, 4): 10765.111, (1, 5): 8343.244,
        (1, 6): 5610.788, (1, 7): 10499.916, (1, 8): 2251.116, (1, 9): 14138.428, (1, 10): 15522.835,
        (1, 11): 12234.071, (1, 12): 13136.188, (1, 13): 13206.662, (1, 14): 7691.596, (1, 15): 7987.345,

        (2, 0): 0,
        (2, 1): 11124.978, (2, 2): 15584.631, (2, 3): 14869.477, (2, 4): 9722.815, (2, 5): 7578.678,
        (2, 6): 14074.08, (2, 7): 9082.473, (2, 8): 2090.903, (2, 9): 12981.864, (2, 10): 11834.697,
        (2, 11): 10400.101, (2, 12): 9642.671, (2, 13): 2437.647, (2, 14): 6931.064, (2, 15): 6688.326,

        (3, 0): 0,
        (3, 1): 8394.233, (3, 2): 15200.568, (3, 3): 14434.448, (3, 4): 10605.683, (3, 5): 6948.454,
        (3, 6): 2040.877, (3, 7): 7407.712, (3, 8): 2090.903, (3, 9): 8421.202, (3, 10): 5549.809,
        (3, 11): 13843.897, (3, 12): 14180.565, (3, 13): 9448.714, (3, 14): 11780.901, (3, 15): 5891.432,

        (4, 0): 0,
        (4, 1): 12438.695, (4, 2): 13217.23, (4, 3): 9463.142, (4, 4): 6703.4, (4, 5): 15219.163,
        (4, 6): 2102.121, (4, 7): 6893.787, (4, 8): 2151.083, (4, 9): 5353.125, (4, 10): 5981.645,
        (4, 11): 12486.929, (4, 12): 7058.997, (4, 13): 8534.292, (4, 14): 12718.623, (4, 15): 4774.782,

        (5, 0): 0,
        (5, 1): 5899.434, (5, 2): 15246.84, (5, 3): 11682.228, (5, 4): 14157.666, (5, 5): 12484.28,
        (5, 6): 6033.559, (5, 7): 6620.503, (5, 8): 2152.62, (5, 9): 5548.213, (5, 10): 6522.7,
        (5, 11): 7989.084, (5, 12): 8766.109, (5, 13): 2460.308, (5, 14): 10647.818, (5, 15): 3161.519,

        (6, 0): 0,
        (6, 1): 15384.024, (6, 2): 14202.352, (6, 3): 5213.583, (6, 4): 12843.446, (6, 5): 11451.217,
        (6, 6): 6185.288, (6, 7): 5911.775, (6, 8): 2212.097, (6, 9): 6298.076, (6, 10): 7098.358,
        (6, 11): 6868.901, (6, 12): 9788.995, (6, 13): 2528.858, (6, 14): 5852.603, (6, 15): 8774.817,

        (7, 0): 0,
        (7, 1): 13934.62, (7, 2): 12807.943, (7, 3): 10900.704, (7, 4): 11674.562, (7, 5): 4955.973,
        (7, 6): 6740.449, (7, 7): 1930.764, (7, 8): 2219.849, (7, 9): 6978.013, (7, 10): 7885.079,
        (7, 11): 4162.8, (7, 12): 9958.301, (7, 13): 7370.388, (7, 14): 6281.698, (7, 15): 8488.357,

        (8, 0): 0,
        (8, 1): 4950.405, (8, 2): 9537.772, (8, 3): 7623.096, (8, 4): 8478.028, (8, 5): 5825.926,
        (8, 6): 7359.715, (8, 7): 2110.033, (8, 8): 2159.28, (8, 9): 7876.757, (8, 10): 8692.76,
        (8, 11): 4116.646, (8, 12): 6790.19, (8, 13): 5734.886, (8, 14): 6872.901, (8, 15): 4613.62,

        (9, 0): 0,
        (9, 1): 6175.016, (9, 2): 7927.647, (9, 3): 9333.019, (9, 4): 10368.077, (9, 5): 6591.689,
        (9, 6): 8280.523, (9, 7): 1556.778, (9, 8): 2200.965, (9, 9): 8620.932, (9, 10): 9482.499,
        (9, 11): 5736.888, (9, 12): 6621.687, (9, 13): 4715.076, (9, 14): 7449.623, (9, 15): 3705.239,

        (10, 0): 0,
        (10, 1): 5967.156, (10, 2): 8770.085, (10, 3): 4457.519, (10, 4): 2949.672, (10, 5): 7437.995,
        (10, 6): 8950.525, (10, 7): 2170.895, (10, 8): 2208.202, (10, 9): 9889.087, (10, 10): 10295.806,
        (10, 11): 6936.655, (10, 12): 6183.788, (10, 13): 4833.609, (10, 14): 3428.478, (10, 15): 2494.009,

        (11, 0): 0,
        (11, 1): 8206.255, (11, 2): 5005.379, (11, 3): 4421.502, (11, 4): 4081.863, (11, 5): 6051.148,
        (11, 6): 9739.858, (11, 7): 2107.874, (11, 8): 2016.311, (11, 9): 10441.592, (11, 10): 11385.772,
        (11, 11): 9264.139, (11, 12): 5031.549, (11, 13): 4275.474, (11, 14): 3590.566, (11, 15): 2736.974,

        (12, 0): 0,
        (12, 1): 6629.96, (12, 2): 5711.805, (12, 3): 4946.236, (12, 4): 4191.797, (12, 5): 8846.665,
        (12, 6): 10472.176, (12, 7): 1691.623, (12, 8): 2229.949, (12, 9): 11276.357, (12, 10): 12104.001,
        (12, 11): 3314.999, (12, 12): 4784.985, (12, 13): 2519.396, (12, 14): 9448.016, (12, 15): 7957.923,

        (13, 0): 0,
        (13, 1): 8441.844, (13, 2): 7191.1, (13, 3): 6169.519, (13, 4): 5414.659, (13, 5): 4658.298,
        (13, 6): 10635.975, (13, 7): 1515.203, (13, 8): 2183.776, (13, 9): 12529.891, (13, 10): 12858.422,
        (13, 11): 3413.873, (13, 12): 10182.247, (13, 13): 2989.034, (13, 14): 9964.449, (13, 15): 9927.224,

        (14, 0): 0,
        (14, 1): 8920.113, (14, 2): 7774.449, (14, 3): 6828.819, (14, 4): 5970.981, (14, 5): 5407.159,
        (14, 6): 4108.82, (14, 7): 1613.146, (14, 8): 2305.122, (14, 9): 12327.087, (14, 10): 13690.052,
        (14, 11): 11200.154, (14, 12): 10414.064, (14, 13): 12065.649, (14, 14): 11920.867, (14, 15): 2755.219,

        (15, 0): 0,
        (15, 1): 8600.002, (15, 2): 7846.819, (15, 3): 7121.911, (15, 4): 6467.004, (15, 5): 5449.227,
        (15, 6): 4857.292, (15, 7): 1696.647, (15, 8): 2223.447, (15, 9): 13609, (15, 10): 14610.615,
        (15, 11): 11278.824, (15, 12): 9966.145, (15, 13): 12496.163, (15, 14): 13626.36, (15, 15): 14162.375,

        (16, 0): 0,
        (16, 1): 9580.145, (16, 2): 8719.981, (16, 3): 7798.304, (16, 4): 6780.144, (16, 5): 6010.335,
        (16, 6): 5144.09, (16, 7): 1636.564, (16, 8): 4373.744, (16, 9): 11123.971, (16, 10): 15700.299,
        (16, 11): 12557.924, (16, 12): 11689.21, (16, 13): 13797.941, (16, 14): 14954.197, (16, 15): 16081.059}

    @classmethod
    def get_rx_net_length(cls, rx_slot_lane_tuple):
        return cls._BP_RX_NET_LENGTH[rx_slot_lane_tuple]

    def __init__(self, **kwargs):
        """ Create all the I2C objects needed to interface the backplane hardware.

        This instance keeps a local reference to 'iceboard', which is a
        reference to the IceBoard handler (not the HWM object, as this is a
        transient object). 'iceoard' must provide:

            - iceboard.i2c: a I2C interface object that provides standardized
              I2C access (handles switch config, bus names etc)

            - iceoard.slot_number: the backplane slot numbe ron which this
              iceboard is, so we don't reset ourself

        NOTE: attempting to access an unknown attribute might cause an
        infinite recursion loop as Tuber tries to access the master_iceboard
        object that may not already exist.
        """
        super().__init__(**kwargs)

        # self.logger = logging.getLogger(__name__)
        self.logger.debug(f'{self!r}: Instantiating backplane hardware')


        self.logger.debug('{self!r}: Instantiating Backplane I2C resource managers')
        self._eeprom_data = EEPROM(
            self.i2c_proxy, bus_name='BP',
            address=self.BACKPLANE_EEPROM_DATA_ADDRESS,
            address_width=self.BACKPLANE_EEPROM_ADDRESS_WIDTH,
            write_page_size=self.BACKPLANE_EEPROM_PAGE_SIZE)
        self._eeprom_serial = EEPROM(
            self.i2c_proxy, bus_name='BP',
            address=self.BACKPLANE_EEPROM_SERIAL_ADDRESS,
            address_width=self.BACKPLANE_EEPROM_ADDRESS_WIDTH,
            write_page_size=self.BACKPLANE_EEPROM_PAGE_SIZE)
        self._qsfp_eeprom = EEPROM(
            self.i2c_proxy, bus_name='BP',
            address=self.BACKPLANE_QSFP_ADDRESS,
            address_width=self.BACKPLANE_QSFP_ADDRESS_WIDTH)

        self.logger.debug('%r: Instantiating Backplane I2C temperature sensors' % self)
        self._tmp_slot1 = tmp421.tmp421(self.i2c_proxy, self._TMP_SLOT1_ADDR, 'BP')
        self._tmp_slot16 = tmp421.tmp421(self.i2c_proxy, self._TMP_SLOT16_ADDR, 'BP')

        self.logger.debug('%r: Instantiating Backplane I2C current/power monitor' % self)
        self._power_3v3 = ina230.ina230(self.i2c_proxy, self._POWER_3V3_ADDR, 'BP')

        self.logger.debug('%r: Instantiating Backplane I2C I/O expanders' % self)
        self._qsfp_ctrla = pca9698.pca9698(self.i2c_proxy, self._QSFP_CTRL_SETA_ADDR, 'BP')
        self._qsfp_ctrlb = pca9698.pca9698(self.i2c_proxy, self._QSFP_CTRL_SETB_ADDR, 'BP')
        self._reset_ctrl = pca9698.pca9698(self.i2c_proxy, self._RESETS_CTRL_ADDR, 'BP')

        self._fan_ctrl = amc6821.AMC6821(self.i2c_proxy, self._FAN_CTRL_ADDR, 'BP')

        self._gpio = gpio.GPIO(gpio_table={
            # name : (expander object, byte, lsb bit number,  width)
            'QSFP1_ModPrsL':  (self._qsfp_ctrla, 2, 0, 1),
            'QSFP1_ResetL':   (self._qsfp_ctrla, 2, 1, 1),
            'QSFP1_IntL':     (self._qsfp_ctrla, 2, 2, 1),
            'QSFP1_ModSelL':  (self._qsfp_ctrla, 2, 3, 1),

            'QSFP2_ModPrsL':  (self._qsfp_ctrla, 2, 4, 1),
            'QSFP2_ResetL':   (self._qsfp_ctrla, 2, 5, 1),
            'QSFP2_IntL':     (self._qsfp_ctrla, 2, 6, 1),
            'QSFP2_ModSelL':  (self._qsfp_ctrla, 2, 7, 1),

            'QSFP3_ModPrsL':  (self._qsfp_ctrla, 1, 0, 1),
            'QSFP3_ResetL':   (self._qsfp_ctrla, 1, 1, 1),
            'QSFP3_IntL':     (self._qsfp_ctrla, 1, 2, 1),
            'QSFP3_ModSelL':  (self._qsfp_ctrla, 1, 3, 1),

            'QSFP4_ModPrsL':  (self._qsfp_ctrla, 1, 4, 1),
            'QSFP4_ResetL':   (self._qsfp_ctrla, 1, 5, 1),
            'QSFP4_IntL':     (self._qsfp_ctrla, 1, 6, 1),
            'QSFP4_ModSelL':  (self._qsfp_ctrla, 1, 7, 1),

            'QSFP5_ModPrsL':  (self._qsfp_ctrla, 0, 0, 1),
            'QSFP5_ResetL':   (self._qsfp_ctrla, 0, 1, 1),
            'QSFP5_IntL':     (self._qsfp_ctrla, 0, 2, 1),
            'QSFP5_ModSelL':  (self._qsfp_ctrla, 0, 3, 1),

            'QSFP6_ModPrsL':  (self._qsfp_ctrla, 0, 4, 1),
            'QSFP6_ResetL':   (self._qsfp_ctrla, 0, 5, 1),
            'QSFP6_IntL':     (self._qsfp_ctrla, 0, 6, 1),
            'QSFP6_ModSelL':  (self._qsfp_ctrla, 0, 7, 1),

            'QSFP7_ModPrsL':  (self._qsfp_ctrla, 3, 0, 1),
            'QSFP7_ResetL':   (self._qsfp_ctrla, 3, 1, 1),
            'QSFP7_IntL':     (self._qsfp_ctrla, 3, 2, 1),
            'QSFP7_ModSelL':  (self._qsfp_ctrla, 3, 3, 1),

            'QSFP8_ModPrsL':  (self._qsfp_ctrla, 3, 4, 1),
            'QSFP8_ResetL':   (self._qsfp_ctrla, 3, 5, 1),
            'QSFP8_IntL':     (self._qsfp_ctrla, 3, 6, 1),
            'QSFP8_ModSelL':  (self._qsfp_ctrla, 3, 7, 1),

            'QSFP9_ModPrsL':  (self._qsfp_ctrlb, 2, 0, 1),
            'QSFP9_ResetL':   (self._qsfp_ctrlb, 2, 1, 1),
            'QSFP9_IntL':     (self._qsfp_ctrlb, 2, 2, 1),
            'QSFP9_ModSelL':  (self._qsfp_ctrlb, 2, 3, 1),

            'QSFP10_ModPrsL': (self._qsfp_ctrlb, 2, 4, 1),
            'QSFP10_ResetL':  (self._qsfp_ctrlb, 2, 5, 1),
            'QSFP10_IntL':    (self._qsfp_ctrlb, 2, 6, 1),
            'QSFP10_ModSelL': (self._qsfp_ctrlb, 2, 7, 1),

            'QSFP11_ModPrsL': (self._qsfp_ctrlb, 1, 0, 1),
            'QSFP11_ResetL':  (self._qsfp_ctrlb, 1, 1, 1),
            'QSFP11_IntL':    (self._qsfp_ctrlb, 1, 2, 1),
            'QSFP11_ModSelL': (self._qsfp_ctrlb, 1, 3, 1),

            'QSFP12_ModPrsL': (self._qsfp_ctrlb, 1, 4, 1),
            'QSFP12_ResetL':  (self._qsfp_ctrlb, 1, 5, 1),
            'QSFP12_IntL':    (self._qsfp_ctrlb, 1, 6, 1),
            'QSFP12_ModSelL': (self._qsfp_ctrlb, 1, 7, 1),

            'QSFP13_ModPrsL': (self._qsfp_ctrlb, 0, 0, 1),
            'QSFP13_ResetL':  (self._qsfp_ctrlb, 0, 1, 1),
            'QSFP13_IntL':    (self._qsfp_ctrlb, 0, 2, 1),
            'QSFP13_ModSelL': (self._qsfp_ctrlb, 0, 3, 1),

            'QSFP14_ModPrsL': (self._qsfp_ctrlb, 0, 4, 1),
            'QSFP14_ResetL':  (self._qsfp_ctrlb, 0, 5, 1),
            'QSFP14_IntL':    (self._qsfp_ctrlb, 0, 6, 1),
            'QSFP14_ModSelL': (self._qsfp_ctrlb, 0, 7, 1),

            'QSFP15_ModPrsL': (self._qsfp_ctrlb, 3, 0, 1),
            'QSFP15_ResetL':  (self._qsfp_ctrlb, 3, 1, 1),
            'QSFP15_IntL':    (self._qsfp_ctrlb, 3, 2, 1),
            'QSFP15_ModSelL': (self._qsfp_ctrlb, 3, 3, 1),

            'QSFP16_ModPrsL': (self._qsfp_ctrlb, 3, 4, 1),
            'QSFP16_ResetL':  (self._qsfp_ctrlb, 3, 5, 1),
            'QSFP16_IntL':    (self._qsfp_ctrlb, 3, 6, 1),
            'QSFP16_ModSelL': (self._qsfp_ctrlb, 3, 7, 1),

            'QSFP1_Led': (self._qsfp_ctrla, 4, 0, 1),
            'QSFP2_Led': (self._qsfp_ctrla, 4, 1, 1),
            'QSFP3_Led': (self._qsfp_ctrla, 4, 2, 1),
            'QSFP4_Led': (self._qsfp_ctrla, 4, 3, 1),
            'QSFP5_Led': (self._qsfp_ctrla, 4, 4, 1),
            'QSFP6_Led': (self._qsfp_ctrla, 4, 5, 1),
            'QSFP7_Led': (self._qsfp_ctrla, 4, 6, 1),
            'QSFP8_Led': (self._qsfp_ctrla, 4, 7, 1),
            'QSFP9_Led': (self._qsfp_ctrlb, 4, 0, 1),
            'QSFP10_Led': (self._qsfp_ctrlb, 4, 1, 1),
            'QSFP11_Led': (self._qsfp_ctrlb, 4, 2, 1),
            'QSFP12_Led': (self._qsfp_ctrlb, 4, 3, 1),
            'QSFP13_Led': (self._qsfp_ctrlb, 4, 4, 1),
            'QSFP14_Led': (self._qsfp_ctrlb, 4, 5, 1),
            'QSFP15_Led': (self._qsfp_ctrlb, 4, 6, 1),
            'QSFP16_Led': (self._qsfp_ctrlb, 4, 7, 1),
            'LED1': (self._reset_ctrl, 0, 7, 1)
            })

        self.qsfp = Ccoll(qsfp.QSFP(
            self.i2c_proxy, 'BP',
            gpio_prefix='QSFP%i_' % (i + 1),
            gpio=self._gpio,
            parent=self) for i in range(self.NUMBER_OF_SLOTS))

        self.SLOT_RESETS_MAP = {
            # Slot num : (expander object, ARM Register, Power Down Register, Bit number)
            1: (self._reset_ctrl, 1, 2, 0),
            2: (self._reset_ctrl, 1, 2, 1),
            3: (self._reset_ctrl, 1, 2, 2),
            4: (self._reset_ctrl, 1, 2, 3),
            5: (self._reset_ctrl, 1, 2, 4),
            6: (self._reset_ctrl, 1, 2, 5),
            7: (self._reset_ctrl, 1, 2, 6),
            8: (self._reset_ctrl, 1, 2, 7),
            9: (self._reset_ctrl,  3, 4, 0),
            10: (self._reset_ctrl, 3, 4, 1),
            11: (self._reset_ctrl, 3, 4, 2),
            12: (self._reset_ctrl, 3, 4, 3),
            13: (self._reset_ctrl, 3, 4, 4),
            14: (self._reset_ctrl, 3, 4, 5),
            15: (self._reset_ctrl, 3, 4, 6),
            16: (self._reset_ctrl, 3, 4, 7)
        }

        self.FULLBP_RESETS_MAP = {
             # ResetType : (expander object, Register, mask, inactive, active)
             'ARM':       (self._reset_ctrl, 0, 0b01000011, 0b00000001, 0b01000010),
             'POWER':     (self._reset_ctrl, 0, 0b01001100, 0b00000100, 0b01001000),
             'FPGA':      (self._reset_ctrl, 0, 0b01110000, 0b00010000, 0b01100000)
        }

        self.TEMPERATURE_SENSOR_TABLE = {
             # sensor name: tmp object
             'TEMP_SLOT1': self._tmp_slot1,
             'TEMP_SLOT16': self._tmp_slot16,
         }

        self.POWER_SENSOR_TABLE = {
             # sensor name : (ina230 obj, output voltage, rshunt(inductor) (mohm), typical current(amps), current tolerance (0<tol<1))
             'BP_3V3': (self._power_3v3, 3.3, 2.6, 2., 0.5),
        }

    def open(self):
        """ Establish communications with the backplane.
        """
        self.model = self._get_backplane_type()
        self.id = '%s SN%s' % (self.model, self.serial)

    def close(self):
        pass

    def init(self):
        """Initializes the backplane hardware to a known state.

        This requires I2C communication with the backplane.
        """
        super().init()

        self.logger.info('%r: Starting backplane initialization' % self)
        for trial in range(10):
            self.logger.info('%r: Backplane initialization trial #%i' % (self, trial))
            try:
                # Check if the fan controller is connected
                self._fan_ctrl_present = self._fan_ctrl.is_present()
                self.logger.info('%r: Fan controller %s present' % (self, ('is NOT', 'IS')[self._fan_ctrl_present]))
                # Check if the power/reset control IO expander is accessible
                # self._reset_ctrl_present = self._reset_ctrl.is_present()

                # self._init_qsfp_ctrl()
                # if self._reset_ctrl_present:
                #    self._init_reset_ctrl()  # The power I2c bus needs to be bridged
                # #             to the monitor I2C bus for this to work
                # self._init_temperature_sensors()
                # self._init_power_sensors()

                if self._fan_ctrl_present:
                    self._fan_ctrl.init()
                    self.logger.info('%r: Initialized fan controller from FPGA' % (self))
                self.logger.info('%r: Successfully completed backplane initialization' % self)
                return
            except (IOError, RuntimeError) as e:
                self.logger.error('%r: IO Error during backplane INIT on trial %i. retrying. Error was:\n%s'
                                   % (self, trial+1, e))
            except Exception as e:
                self.logger.info('%r: Unexpected exception during backplane INIT on trial %i. Retrying. Error was:\n%s'
                                  % (self, trial+1, e))
            finally:
                try:
                    self.i2c_proxy.select_bus([])  # Make sure we don't load the bus
                except (IOError, RuntimeError) as e:
                    self.logger.info('%r: IO Error while trying to deselect bus. Error was:\n%s' % (self, e))
                    pass
        raise IOError('%r: Cannot initialize backplane peripherals' % self)

    def _init_temperature_sensors(self, temperature_sensor_name=None):
        """
        initializes temperature sensors
        'temperature_sensor_name' can be a list of temperature sensor names found in TEMPERATURE_SENSOR_TABLE.

        History:
        140318 JM: created
        """
        if temperature_sensor_name is None:
            temperature_sensor_name = list(self.TEMPERATURE_SENSOR_TABLE.keys())
        elif isinstance(temperature_sensor_name, str):
            temperature_sensor_name = [temperature_sensor_name]

        for temp_sensor in temperature_sensor_name:
            if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
                raise ValueError('Invalid temperature sensor name')
            else:
                tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
                try:
                    tmp_object.init()
                except IOError:
                    self.logger.error('%r: Error initializing the Backplane temperature sensors' % self)

    def _init_power_sensors(self, power_sensor_name='BP_3V3'):
        """
        initializes current/power monitors

        Parameters:

        power_sensor_name (str, list): Can be a list of current/power monitor
            names found in POWER_SENSOR_TABLE. If power_sensor_name=None, all
            sensors in POWER_SENSOR_TABLE are initialized.

        History:
        140320 JM: created
        """
        if power_sensor_name == None:
            power_sensor_name = list(self.POWER_SENSOR_TABLE.keys())
        elif isinstance(power_sensor_name, str):
            power_sensor_name = [power_sensor_name]

        for power_sensor in power_sensor_name:
            if power_sensor not in self.POWER_SENSOR_TABLE:
                raise ValueError('Invalid current/power monitor name')
            else:
                power_sensor_list = self.POWER_SENSOR_TABLE[power_sensor]
                power_sensor_object = power_sensor_list[0]
                try:
                    power_sensor_object.init(
                        v_out=power_sensor_list[1],
                        r_shunt=power_sensor_list[2],
                        i_typ=power_sensor_list[3],
                        tol_i=power_sensor_list[4])
                except IOError:
                    self.logger.error('%r: Error initializing the Backplane Power sensors.' % self)

    def _init_qsfp_ctrl(self):
        """
        initializes QSFP control
        History:
        141015 AJG: created
        """
        qsfpa_ctrl = self._qsfp_ctrla
        qsfpb_ctrl = self._qsfp_ctrlb

        try:
            qsfpa_ctrl.init(
                cfg0_def=0x55, cfg1_def=0x55, cfg2_def=0x55, cfg3_def=0x55, cfg4_def=0xff,
                out0_def=0xaa, out1_def=0xaa, out2_def=0xaa, out3_def=0xaa, out4_def=0)
            qsfpb_ctrl.init(
                cfg0_def=0x55, cfg1_def=0x55, cfg2_def=0x55, cfg3_def=0x55, cfg4_def=0xff,
                out0_def=0xaa, out1_def=0xaa, out2_def=0xaa, out3_def=0xaa, out4_def=0)
            # By default LEDs are off (dir=inputs , outputs=0), ModPrsL and
            # IntL (dir=input, output = 0), ResetL and ModselL (dir=output,
            # output=1)
        except IOError:
            self.logger.error('%r: Error initializing the Backplane QSFP GPIO control lines' % self)

    def _init_reset_ctrl(self):
        """
        initializes reset control
        History:
        141075 AJG: created
        """
        self._reset_ctrl.init(
            cfg0_def=0xFF, cfg1_def=0xFF, cfg2_def=0xFF,
            cfg3_def=0xFF, cfg4_def=0xFF,
            out0_def=0x15, out1_def=0xFF, out2_def=0xFF,
            out3_def=0xFF, out4_def=0xFF)
        # By default setting all pins to inputs, with default output level
        # logic 1 (no reset possible) for all banks except 0
        #
        # On bank 0, default levels are such that LED default is 0, Reset
        # clear is active, and reset pins are functionality is maximily off

    def read_backplane_eeprom(self, addr, length=1, **kwargs):
        return self._eeprom_data.read(addr, length, **kwargs)

    def write_backplane_eeprom(self, addr, data, **kwargs):
        return self._eeprom_data.write(addr, data, **kwargs)

    # def is_backplane_present(self):
    #     """ Detect if the backplane is present by probing its EEPROM with a dummy I2C acces.
    #     """
    #     return self._eeprom_data.is_present() # perform a dummy access

    # def read_eeprom(self, addr, length=1):
    #     return self._eeprom.read(addr, length = length)

    def get_backplane_eeprom_serial_number(self):
        """ return the 128-bit hardware-coded EEPROM serial number as a hex string. """
        return ''.join(['%02X' % ord(v) for v in self._eeprom_serial.read(0x80, length=16)])

    def _get_backplane_type(self):
        """TODO: Implement as IPMI read"""
        return self._ipmi_part_numbers[0]

    def set_led(self, led_name, state):
        """
        Set the LED(s) specified in 'led_name' to the the 'state'. 'led_name'
        can be a list of LED names found in LED_MAP.  'state' can be a single
        boolean value, or an array with the same length as 'led_name'
        """
        self._gpio.write(led_name, state)

    def get_led(self, led_name):
        """
        Returns the status of specified LED(s)

        Parameters:
            led_name (str): String indicating the name of the led to query

        Returns:

            Status of specified LED
        """
        return self._gpio.read(led_name)

    def get_temperature(self, temperature_sensor_name=None):
        """
        Returns the current temperature measured on the specified
        sensor(s).


        Parameters:

            temperature_sensor_name (str, or list of str): list of temperature sensor names found in
        TEMPERATURE_SENSOR_TABLE

        Returns:
             A dictionary with keys corresponding to the temperature_sensor_name names.

        Notes:

            Some temperatures are taken from the FPGA inetrnal system monitor core (SYSMON).


        History:
            140318 JM: created
        """
        temperature_dict = {}
        if temperature_sensor_name is None:
            temperature_sensor_name = list(self.TEMPERATURE_SENSOR_TABLE.keys())
        elif isinstance(temperature_sensor_name, str):
            temperature_sensor_name = [temperature_sensor_name]

        for temp_sensor in temperature_sensor_name:
            if temp_sensor not in self.TEMPERATURE_SENSOR_TABLE:
                raise ValueError('Invalid temperature sensor name')
            else:
                tmp_object = self.TEMPERATURE_SENSOR_TABLE[temp_sensor]
                temperature_dict[temp_sensor] = tmp_object.get_temperature()

        return temperature_dict

    def get_power(self, power_sensor_name=None):
        """
        Returns the voltage, current and power of the power monitoring system.
        Includes power measured internally from  the FPGA's system monitor.
        Multiple targets can be specified.

        Parameters:

           'power_sensor_name' can be a list of power sensor
            names found in POWER_SENSOR_TABLE. If
            power_sensor_name=None, measurements of all sensors in
            power_sensor_table are returned.

        Returns dictionary with keys corresponding to the
        power_sensor_name names. The respective value is a (bus
        voltage (V), shunt voltage (V), current (A), power (W)) tuple.

        History:
        140320 JM: created
        """
        power_dict = {}
        if power_sensor_name is None:
            power_sensor_name = list(self.POWER_SENSOR_TABLE.keys())
        elif isinstance(power_sensor_name, str):
            power_sensor_name = [power_sensor_name]

        for power_sensor in power_sensor_name:
            if power_sensor not in self.POWER_SENSOR_TABLE:
                raise ValueError('Invalid power sensor name')
            else:
                power_sensor_list = self.POWER_SENSOR_TABLE[power_sensor]
                power_object = power_sensor_list[0]
                power_dict[power_sensor] = (
                    power_object.get_bus_voltage(),
                    power_object.get_shunt_voltage(),
                    power_object.get_current(),
                    power_object.get_power())

        return power_dict

    def get_serial_number(self):
        """
        Returns the board's serial number.
        """
        return self.get_backplane_eeprom_serial_number()

    def reset_slot(self, slots, state, reset_type='ARM'):
        """
        Function performs Resets. If slots 'ALL' will perform full backplane reset, Type can be 'ARM', 'POWER' or'FPGA'
        For full backplane reset state must be True.

        For individual slot reset (can be a list)
        State must be True, False, or 'pulse', reset type must be 'ARM' or 'POWER'

        History:
        141015 AJG & JF: created
        """
        if not self._reset_ctrl:
            raise RuntimeError('The Power/Reset backplane I/O Expander was not '
                               'detected at init. Was the POW I2C bus accessible?')

        if slots == 'ALL' and state == 1:  # We wish to perform a full crate reset

            if reset_type not in self.FULLBP_RESETS_MAP:
                raise ValueError('Unknown reset type %s' % reset_type)
            else:
                (reset_control_obj, controlreg, mask, inactive, active) = self.FULLBP_RESETS_MAP[reset_type]
                reset_cfg_register = 'CFG%i' % controlreg
                reset_output_register = 'OUT%i' % controlreg

                # Flipping state of LED so that we know a reset was performed
                self.set_led('LED1', not(self.get_led('LED1')['LED1']))
                # not sure what the default LED state will be so this is a
                # flip at the moment

                # Setting output register to reset value
                reset_control_obj.write(reset_output_register, active, mask)
                # Setting direction register to output (this performs the reset)
                reset_control_obj.write(reset_cfg_register, 0, mask)

        else:  # We wish to perform individual resets
            if isinstance(slots, int):
                slots = [slots]

            if isinstance(state, (bool, int, str)):
                state = ([state] * len(slots))

            if isinstance(reset_type, (str)):
                reset_type = ([str(reset_type)] * len(slots))

            for (slot, isenabled, resettype) in zip(slots, state, reset_type):
                if slot == self.master_iceboard.slot_number:
                    self.logger.warning(f'Warning, will not perform reset on the controlling slot {slot}')

                # if isenabled and slot != self._iceboard.slot_number  :
                elif slot not in range(1, self.NUMBER_OF_SLOTS + 1):
                    raise ValueError(f'Invalid Slot number {slot}')
                else:
                    (reset_control_obj, arm_reset_reg, power_down_reg, bitnumber) = self.SLOT_RESETS_MAP[slot]
                    if resettype == 'ARM':
                        reset_cfg_register = 'CFG%i' % arm_reset_reg
                        reset_output_register = 'OUT%i' % arm_reset_reg
                    elif resettype == 'POWER':
                        reset_cfg_register = 'CFG%i' % power_down_reg
                        reset_output_register = 'OUT%i' % power_down_reg
                    else:
                        raise ValueError(f'Unknown reset type, will not perform reset on slot {slot}')

                    mask = 1 << bitnumber
                    if isenabled == 1 or isenabled == 'pulse':  # Turning reset on

                        #Setting output register to logic 0 (reset active)
                        reset_control_obj.write(reset_output_register, 0, mask)
                        # Setting direction register from input to output -
                        # Performing reset
                        reset_control_obj.write(reset_cfg_register, 0, mask)

                    if isenabled == 0 or isenabled == 'pulse':  #Turning reset off
                        if isenabled=='pulse':
                            time.sleep(2)
                        # Setting output register to logic 1 (reset inactive)
                        # - Removing reset
                        reset_control_obj.write(reset_output_register,  mask, mask)
                        # Setting direction register from output to input -
                        # Back to default state
                        reset_control_obj.write(reset_cfg_register,  mask, mask)

    def set_fan_speed(self, speed):
        """ Set the speed of the crate fan.

        Parameters:
            speed (int): Percentage value from 0 to 100 that determines the fan speed.
        """
        # if not self._fan_ctrl_present:
        #    raise RuntimeError('There is no fan controller connected on the backplane I2C bus')
        # self._fan_ctrl.set_duty_cycle(speed)

        self.master_iceboard.set_fantray_duty_cycle(int(255.*speed/100))

    # def get_pcb_links(self):
    #     """
    #     Return a list describing all possible data links that can be provided over the PCB tracks.

    #     All link sin the list are resolved (i.e. the id of both end is known):
    #     Returns:

    #         [('pcb', tx_id, rx_id), ...]

    #     """
    #     tx_crate = rx_crate = self.get_id()[0]
    #     links = [('pcb', (tx_crate, tx_slot - 1, tx_lane), (rx_crate, rx_slot - 1, rx_lane))
    #               for (rx_slot, rx_lane), (tx_slot, tx_lane) in self.get_pcb_link_map()]

    def get_qsfp_cable_map(self, get_cable_id=True, use_qsfp_link_id=False):
        """
        Return a list describing all data links that could be provided by the
        backplane QSFP cables, along with each UID if `get_cable_id` is True.

        Parameters:

            get_cable_id (bool): if True, the method will query the cable and
                provide a string that uniquely identifies each link across the
                system.

            use_qsfp_link_id: If True, index the map using the qsfp link id
                instead of the backplane link id.

        Returns:
            A list in the format:

                {bp_link_id or qsfp_link_id: cable_link_id, ...}

            `bp_link_id` is a tuple that describes the link from the point of
                view of the backplane connector. It is in the format::

                    (crate_id, bp_slot, bp_lane).

                where `bp_slot` and `bp_lane` indicates to which backplane
                slot and backplane QSFP lane the link is routed to. The slot
                number is zeroo-based. The `bp_lane` field ranges from 0 to 3.
                The caller needs to use its knowledge of the motherboard
                routing to associate this number to a GTX transceiver.

            `qsfp_link_id` is a tuple that describes the link from the point
                of view of the QSFP connector. It is in the format:

                    (crate_id, qsfp_slot, qsfp_lane).

            `cable_link_id` a unique identifier that is unique for each bi-
                directional links. It is provided only if `get_cable_id' is
                `True`, otherwise this field is set to `None`. It is a string
                based on the cable manufacturer, model, serial number and lane
                number, and is in the format "VENDOR_MODEL_SERIAL_LANE.
                `link_uid` requires a slow access to the QSFP cable over I2C.



        This method is typically used by BER tests to obtain the information
        needed to resolve the connectivity of the IceBoard GTXes and determine
        which links are to be tested, but could also be used to validate the
        connectivity of an installed system.

        This method provides information that is only related to the backplane
        QSFPs: it does not assess whether a motherboard is connected in the
        slot where the specific QSFP link is routed. Also, it does not know to
        which physical GTX each the motherboard has connected the QSFP link.

        This method does not resolve cable connectivity since this requires
        knowledge from other crates. The connectivity should be resolved by
        the code that has access to all of the crates and by using the
        `link_uid` that will be common to both ends of each link.

        """
        # tx_nodes = {}
        # rx_nodes = {}
        links = {}
        crate_id = self.get_id()[0]
        for qsfp_slot, qsfp in enumerate(self.qsfp):

            # Get the UID of the cable connected to this QSFP cage
            if get_cable_id and qsfp.is_present():  # qsfp not accessed if get_link_uid=False (slow)
                cable_id = qsfp.read_str('VendName') + "_" + qsfp.read_str('VenPN') + "_" + qsfp.read_str('VenSN')
            else:
                cable_id = None

            for qsfp_lane in range(4):
                cable_link_id = '%s_%i' % (cable_id, qsfp_lane) if cable_id else None
                # establish backplane connectivity (routing is inferred by this code)
                if use_qsfp_link_id:
                    qsfp_link_id = (crate_id, qsfp_slot, qsfp_lane)
                    links[qsfp_link_id] = cable_link_id
                else:
                    bp_lane = qsfp_slot % 4
                    bp_slot = (qsfp_slot // 4) * 4 + qsfp_lane   # slot number is zero-based for tuples
                    bp_link_id = (crate_id, bp_slot, bp_lane)
                    links[bp_link_id] = cable_link_id
        return links

    def get_qsfp_cable_info(self):
        """ Return a dictionary that lists the backplane QSFP cable information for the cables
        connected on each of the backplane QSFP cages.

        Returns:
            A dict in the format::

                {slot_number:{'Manufacturer':..., 'PN':..., 'SN':...}}.

            Slots that have no detectet cables are not persent in the dictionary.
        """
        ids = {}
        for qsfp_slot, qsfp in enumerate(self.qsfp):
            if qsfp.is_present():
                cable_link_id = {
                    'Manufacturer': qsfp.read_str('VendName'),
                    'PN': qsfp.read_str('VenPN'),
                    'SN': qsfp.read_str('VenSN')}
                ids[qsfp_slot] = cable_link_id
        return ids

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#  /$$      /$$  /$$$$$$  /$$   /$$ /$$$$$$$$ /$$$$$$$  /$$$$$$$    /$$
# | $$$    /$$$ /$$__  $$| $$  /$$/|_____ $$/| $$__  $$| $$__  $$ /$$$$
# | $$$$  /$$$$| $$  \__/| $$ /$$/      /$$/ | $$  \ $$| $$  \ $$|_  $$
# | $$ $$/$$ $$| $$ /$$$$| $$$$$/      /$$/  | $$$$$$$ | $$$$$$$/  | $$
# | $$  $$$| $$| $$|_  $$| $$  $$     /$$/   | $$__  $$| $$____/   | $$
# | $$\  $ | $$| $$  \ $$| $$\  $$   /$$/    | $$  \ $$| $$        | $$
# | $$ \/  | $$|  $$$$$$/| $$ \  $$ /$$/     | $$$$$$$/| $$       /$$$$$$
# |__/     |__/ \______/ |__/  \__/|__/      |_______/ |__/      |______/
#     Single-slot Test backplane
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# Generated with http://patorjk.com/software/taag/#p=display&f=Big Money-ne&t=MGK7BP1


# @session.register_yaml_object()
# class IceCrate_MGK7BP1(IceCrateExt):
#     handler_name = 'IceCrate_MGK7BP1_Handler'
#     __mapper_args__ = {'polymorphic_identity': 'IceCrate_MGK7BP1'}
#     _ipmi_part_numbers = ['MGK7BP1']  # Must match part number in IPMI data


class IceCrate_MGK7BP1(IceCrate):
    """
    Provides access to the 1-slot test backplane.
    """
    part_number = 'MGK7BP1'
    _ipmi_part_numbers = ['MGK7BP1']  # Must match part number in IPMI data

    #####################################
    # Define hardware-specific constants
    #####################################
    NUMBER_OF_SLOTS = 1
    BACKPLANE_EEPROM_DATA_ADDRESS = 0x54  # covers 0x54 - 0x57 ( 4 pages of 256 bytes, 1024 Bytes total)
    BACKPLANE_EEPROM_SERIAL_ADDRESS = 0x5C  # 16 byte serial number starting at memory address 0x80
    # 2 bits are in the device address, the remaining are in the address byte following the command byte
    BACKPLANE_EEPROM_ADDRESS_WIDTH = 10
    BACKPLANE_EEPROM_PAGE_SIZE = 16

    _GPIO_CTRL_ADDR = 0b0101111

    # The following dictionary describes the connectivity of the 10 Gbps mesh.
    # It indicates which transmitter (slot and lane number) is feeding a specified receiver.
    # The dictionary is indexed by receiver number.
    _BP_RX_TO_TX_MAP = {(slot, lane): (slot, lane) for slot in range(17) for lane in range(16)}
    _BP_TX_TO_RX_MAP = {tx: rx for (rx, tx) in _BP_RX_TO_TX_MAP.items()}


    def __init__(self, **kwargs):
        """
        Creates all the I2C objects needed to interface the hardware.

        For FPGA-based I2C:
            - fpga_core is not Null
            - fpga_core provides the following methods
                - i2c_set_port(...) # Port number 0 (connected to the FPGA I2C switch) is used for all accesses
                - i2c_write_read(...) # FPGA I2C engine
        """
        super().__init__(**kwargs)


        self._I2C_BACKPLANE_BUS_NAME = 'BP'
        # self.logger.debug('Initializing Iceboard hardware')

        self.logger.debug(' Instantiating Backplane I2C resource managers')

        # Define the I2C devide objects
        # Note that we use a I2C proxy objects so we don't need yet to resolve which is the master motherboard.
        # (the motherboards are added to ``slot`` only after instantiation and are ot available right now)

        self._eeprom_data = EEPROM(
            self.i2c_proxy, bus_name='BP',
            address=self.BACKPLANE_EEPROM_DATA_ADDRESS,
            address_width=self.BACKPLANE_EEPROM_ADDRESS_WIDTH,
            write_page_size = self.BACKPLANE_EEPROM_PAGE_SIZE)
        self._eeprom_serial = EEPROM(
            self.i2c_proxy, bus_name='BP',
            address=self.BACKPLANE_EEPROM_SERIAL_ADDRESS,
            address_width=self.BACKPLANE_EEPROM_ADDRESS_WIDTH,
            write_page_size = self.BACKPLANE_EEPROM_PAGE_SIZE)

        self.logger.debug(' Instantiating Backplane I2C I/O expanders')
        self._gpio_ctrl = pca9575.pca9575(self.i2c_proxy, self._GPIO_CTRL_ADDR, 'BP')

        self._GPIO_CTRL_MAP = {
             # Slot num : (expander object, Register, bit number)
             'SLOTADDR0': (self._gpio_ctrl, 0, 0),
             'SLOTADDR1': (self._gpio_ctrl, 0, 1),
             'SLOTADDR2': (self._gpio_ctrl, 0, 2),
             'SLOTADDR3': (self._gpio_ctrl, 0, 3),

             'SYNC':  (self._gpio_ctrl, 0, 6),
             'TIME':  (self._gpio_ctrl, 1, 3),
             'TRIG':  (self._gpio_ctrl, 1, 4),

             'PLLSYNC': (self._gpio_ctrl, 0, 7),

             'BPIO3': (self._gpio_ctrl, 1, 0),
             'BPIO4': (self._gpio_ctrl, 1, 1),
             'BPIO5': (self._gpio_ctrl, 1, 2)
        }

        self.LED_MAP = {
             # LEDName : (expander object, Register, bit number)
             'LED1': (self._gpio_ctrl, 1, 5),
             'LED2': (self._gpio_ctrl, 1, 6),
             'LED3': (self._gpio_ctrl, 1, 7)
        }

        self._RESETS_MAP = {
             # ResetType : (expander object, Register, bit)
             'ARM':       (self._gpio_ctrl, 0, 4),
             'FPGA':      (self._gpio_ctrl, 0, 5)
        }

    def init(self):
        """Initializes the backplane to a known state"""
        super().init()
        self._init_gpio_ctrl()  # The power I2c bus needs to be bridged to the monitor I2C bus for this to work

    def _init_gpio_ctrl(self):
        """
        initializes reset control
        History:
        141075 AJG: created
        """
        gpio_ctrl = self._gpio_ctrl

        gpio_ctrl.init(cfg0_def=0xFF, cfg1_def=0xFF)
        # By default setting all pins to inputs, with default output level logic 0

    def read_eeprom(self, addr, length=1):
        return self._eeprom_data.read(addr, length=length)

    def get_eeprom_serial_number(self):
        """ return the 128-bit hardware-coded EEPROM serial number as a hex string. """
        return ''.join(['%02X' % v for v in self._eeprom_serial.read(0x80, length=16)])

    def set_led(self, led_name, state):
        """
        Set the LED(s) specified in 'led_name' to the the 'state'.
        'led_name' can be a list of LED names found in
        LED_MAP.  'state' can be a single boolean value, or
        an array with the same length as 'led_name'
        """
        if isinstance(led_name, (str, int)):
            led_name = [led_name]

        for pos, name in enumerate(led_name):
            if isinstance(name, int):
                name = 'LED%i' % name
            led_name[pos] = name

        if isinstance(state, (bool, int)):
            state = [state] * len(led_name)

        for (led, led_state) in zip(led_name, state):
            if led not in self.LED_MAP:
                raise ValueError('Invalid LED name')
            else:
                (led_control_object, led_control_register, led_control_bitnumber) = self.LED_MAP[led]
                mask = 1 << led_control_bitnumber
                led_control_object.write('CFG%i' % led_control_register, 0, mask=mask)  # Setting LED pin to output
                # Turning LED on and off
                led_control_object.write('OUT%i' % led_control_register, mask * bool(not(led_state)), mask=mask)

    def get_led(self, led_name):
        """
        Returns the status of specified LED(s) in a dictionary
        led_status where each key is a led_name and the respective value
        is the led status.
        """
        led_status = {}
        if isinstance(led_name, (str, int)):
            led_name = [led_name]

        for pos, name in enumerate(led_name):
            if isinstance(name, int):
                name = 'LED%i' % name
            led_name[pos] = name

        for led in led_name:
            if led not in self.LED_MAP:
                raise ValueError('Invalid LED name')
            else:
                (led_control_object, led_control_register, led_control_bitnumber) = self.LED_MAP[led]
                # Converting the resister in the map into the correct string format
                led_control_register = 'IN%i' % led_control_register
                # Note that we are cheating here, we are flipping the bits on
                # the IO Expander from input mode to output mode, inputs are
                # default floating

                # Turning on the LED requires a output of 0 which is the
                # default state in output mode
                regout = led_control_object.read(led_control_register)
                led_status[led] = not bool((regout & (1 << led_control_bitnumber)) >> led_control_bitnumber)
        return led_status

    def set_slot_addr(self, slotnum):
        """
        Sets the backplane slot number to the number specified slot number from 1 to 16
        """

        if slotnum not in range(1, 16 + 1):
            raise ValueError('Invalid slot number')
        slotnum -= 1  # Slot 1 is binary 0000, slot 16 is binary 1111

        for addr in range(0, 4):
            (ctrlobj, reg, bitnum) = self._GPIO_CTRL_MAP['SLOTADDR%i' % addr]
            mask = 1 << bitnum
            ctrlobj.write('CFG%i' % reg, 0, mask=mask)  # Setting addr pin to output

            bitlevel = (slotnum >> addr) & 1
            ctrlobj.write('OUT%i' % reg, mask * bitlevel, mask=mask)  # Turning pin off

    def get_serial_number(self):
        """
        Returns the board's serial number.
        """
        return self.get_eeprom_serial_number()  # tentative code

    def get_info(self):
        """Loads the info data on the motherboard"""
        pass

    def status(self):
        """Displays the status of the motherboard"""

    def get_qsfp_links(self, get_link_uid=True):
        """
        Return a list describing the data links that are provided by the
        backplane QSFP-equivalent loopback links on this backplane, along with
        each UID if `get_link_uid` is True.

        Each link node is in the format (link_type='BP_QSFP, qsfp_link_id,
        bp_link_id, link_uid). Each entry represents both the receiver and
        transmitter that are connected on that bidirectional lane.

        `qsfp_link_id` is a tuple that describes the link from the point of
        view of the QSFP connector. It is in the format (crate_id, qsfp_slot,
        qsfp_lane).

        `bp_link_id` is a tuple that describes the link from the point of view
        of the backplane connector. It is in the format `(crate_id, bp_slot,
        bp_lane)`. `bp_slot` and `bp_lane` indicates to which backplane slot
        and backplane QSFP lane the link is routed to.

        `link_uid` a unique identifier that is unique for each bi- directional
        links. It is provided only if `get_link_uid' is `True`, otherwise this
        field is set to `None`.


        Returns:
            A list in the format:

                [('BP_QSFP', qsfp_link_id, bp_link_id, link_uid), ...]


        """
        # tx_nodes = {}
        # rx_nodes = {}
        crate_id = self.get_id()
        links = []
        # Get Cable unique ID. Include crates string id so the cable UID will
        # be unique even if multiple backplanes are in the array
        cable_uid = '%s_QSFP_Loopback' % self.get_string_id()
        qsfp_slot = 0  # there is only one QSFP slot on this backplane
        for qsfp_lane in range(4):
            qsfp_link_id = (crate_id, qsfp_slot, qsfp_lane)
            bp_link_id = (crate_id, qsfp_lane, qsfp_slot + 1)
            link_uid = '%s_%i' % (cable_uid, qsfp_lane)
            links.append(('BP_QSFP', qsfp_link_id, bp_link_id, link_uid))

        return links

    def get_qsfp_cable_map(self, get_cable_id=True, use_qsfp_link_id=False):

                #{(link_type, (tx_crate, tx_slot, tx_lane), (rx_crate, rx_slot, rx_lane)) : (tx_gtx_instance, rx_gtx_instance)}

        crate_id = self.get_id()[0]

        links = {(crate_id, 0, 0): (crate_id, 0, 4),
                 (crate_id, 0, 1): (crate_id, 0, 5),
                 (crate_id, 0, 2): (crate_id, 0, 6),
                 (crate_id, 0, 3): (crate_id, 0, 7)}
        return links
