"""Object allowing access to the CRS backplane hardware."""

from pychfpga.hardware import Crate
from . import crs_discovery

class CRSBackplane(Crate):
    """Hardware-map object for CRS backplanes discovered from CRS beacons."""

    part_number = 'CRS_BP'
    _ipmi_part_numbers = ['CRS_BP', 'CRSBP', 'CRS-BP', 'CRS']
    NUMBER_OF_SLOTS = 16

    @classmethod
    def get_network_discovery_function(cls):
        """Return the function to use for network discovery of CRS backplanes."""
        return crs_discovery.discover_crs
