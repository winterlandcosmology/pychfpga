"""Object allowing access to the CRS backplane hardware."""

from pychfpga.hardware import Crate
from . import crs_discovery

class CRSBackplane(Crate):
    """Hardware-map object for CRS backplanes discovered from CRS beacons."""

    part_number = 'CRC4'
    _ipmi_part_numbers = ['CCR', 'CRC4']
    NUMBER_OF_SLOTS = 16

    MANUFACTURER = 't0.technology'

    BACKPLANE_MANUFACTURER = MANUFACTURER
    BACKPLANE_MODEL = '4SBP'
    BACKPLANE_PRODUCT_NAME = '4-slot backplane'
    BACKPLANE_REVISION = '2'
    BACKPLANE_SERIAL_NUMBER_LENGTH = 3

    CRATE_MANUFACTURER = MANUFACTURER
    CRATE_MODEL = part_number
    CRATE_PRODUCT_NAME = 'CRS Crate'
    CRATE_REVISION = '1'
    CRATE_SERIAL_NUMBER_LENGTH = 3

    CHASSIS_PART_NUMBER = 'Vector'

    # PCB link topology is not modeled yet; stubs keep ICE-style callers working.
    _BP_RX_TO_TX_MAP = {}
    _BP_TX_TO_RX_MAP = {}
    _BP_RX_NET_LENGTH = {}

    @classmethod
    def get_network_discovery_function(cls):
        """Return the function to use for network discovery of CRS backplanes."""
        return crs_discovery.discover_crs

    def init(self):
        """Initialize crate/backplane hardware (no-op until CRS BP control exists)."""
        self.logger.debug(f'{self!r}: init()')

    @classmethod
    def get_matching_tx(cls, rx_slot_lane_tuple):
        return cls._BP_RX_TO_TX_MAP.get(rx_slot_lane_tuple, rx_slot_lane_tuple)

    @classmethod
    def get_matching_rx(cls, tx_slot_lane_tuple):
        return cls._BP_TX_TO_RX_MAP.get(tx_slot_lane_tuple, tx_slot_lane_tuple)

    @classmethod
    def get_pcb_link_map(cls):
        return dict(cls._BP_RX_TO_TX_MAP)

    @classmethod
    def get_rx_net_length(cls, rx_slot_lane_tuple):
        return cls._BP_RX_NET_LENGTH.get(rx_slot_lane_tuple, 0.0)

    def get_string_id(self):
        """Return a model/serial string that uniquely identifies this crate."""
        return f'{self.part_number}_SN{self.serial!s}'

    def get_id(self, slot=None):
        """Return a tuple identifying this crate and an optional slot number."""
        if self.crate_number is not None:
            return (self.crate_number,) if slot is None else (self.crate_number, slot)
        return (self.get_string_id(),) if slot is None else (self.get_string_id(), slot)

    def get_number_of_slots(self):
        return self.NUMBER_OF_SLOTS
