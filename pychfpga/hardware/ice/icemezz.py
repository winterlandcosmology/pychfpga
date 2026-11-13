# Standard Python packages
import logging

# Local packages

from pychfpga.hardware.mezzanine import Mezzanine
from .icecore.hardware_assets import FMCMezzanineBase


class FMCMezzanine(Mezzanine, FMCMezzanineBase):
    """
    Provides the basic methods needed to operate a mezzanine.
    """

    part_number = None
    _ipmi_part_numbers = None  # Must match part number in IPMI data

    async def set_mezzanine_power_async(self, state):
        """ Turn the mezzanine power on or off using power sequencing.

        This method does not use the equivalenet Tuber method which does not perform power sequencing.

        Parameters:

            state (bool): True=power ON, False = power OFF
        """
        await self.iceboard.set_mezzanine_power_async(self.mezzanine, state)

    async def set_mezzanine_reset_async(self, state):
        """ Sets the reset line of the mezzanine.

        Parameters:

            state (bool): Reste state
        """
        await self.iceboard.set_mezzanine_reset_async(self.mezzanine, state)

    async def get_mezzanine_power_async(self):
        return await self.iceboard.tuber_get_mezzanine_power_async(self.mezzanine)

    async def is_mezzanine_present_async(self):
        return await self.iceboard.tuber_is_mezzanine_present_async(self.mezzanine)

    def is_mezzanine_present(self):
        return self.iceboard.is_mezzanine_present(self.mezzanine)
