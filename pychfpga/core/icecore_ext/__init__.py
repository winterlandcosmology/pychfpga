""" Icecore Extensions

This package provide IceBoard and IceCrate classes that extends the features offered by the Icecore library:
   - Defines an IceBoardPlus handler (IceBoardExtHandler) that allows:
      - Access the FPGA memory map over a direct Ethernet link with the FPGA (with proper firmware)
      - Gives access to the hardware on the IceBoard
   - Defines a IceCrateExt and IceCrateExtHandler:
      - Defines functionnalities specific to the MGK7BP16 backplane
      - Can be instantiated automatically by backplane auto-discovered based on the IPMI model type
      - Provides backplane-specific information and gives direct access to the backplane hardware
"""
from iceboard_ext import IceBoardExtHandler
from icecrate_ext import IceCrateExt, IceCrateExtHandler
from fpga_bitstream import FpgaBitstream
