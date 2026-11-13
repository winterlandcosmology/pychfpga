# Standard Python packages
import logging

# Local packages
from pychfpga.hardware import HardwareMap, Motherboard


class Mezzanine(HardwareMap):
    """
    Provides the basic methods needed to operate a mezzanine.
    """


    # We don't define class and instance registries as we don't track mezzanines separately from the motherboards. 

    part_number = None
    _ipmi_part_numbers = None  # Must match part number in IPMI data


    def __init__(self, serial=None, mezzanine=None, iceboard=None):
        """ Create a generic Mezzanine object.

        Parameters:

            serial (str): serial number of the mezzanine (part number should be defined in a subclass so the mezzanine can be uniquely identified)

            mezzanine (int): mezzanine number. This can be an arbitrary number, but is typically 1,2...

            iceboard (Motherboard): Object that represent the motherboard on which the mezzannine is installed
        """
        self.logger = logging.getLogger(__name__)
        self.serial = serial
        self.mezzanine = mezzanine  # mezzanine slot number on IceBoard
        self.iceboard = iceboard  # carrier IceBoard object

    def get_id(self):
        """ Return a string that identifies uniquely the mezzanine board.
        Comprises the model number and the serial number.
        """
        return f'{self.part_number}_SN{self.serial}'

