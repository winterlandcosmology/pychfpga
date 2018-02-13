:mod:`pychfpga` Package
=======================

The `pychfpga` package defines the modules and classes needed to operate an array of ICE hardware (ICE motherboards, ICE backplanes, CHIME ADC Mezzanines) and the CHIME-specific chFPGA firmware functions provided by the motherboard's FPGA.


.. toctree::
   :hidden:

   fpga_array
   chFPGA_controller
   chFPGA_receiver
   icecore
   icecore_ext
   IceBoard
   metrics

List of Modules
---------------

.. currentmodule:: pychfpga

.. autosummary::
   :nosignatures:

   fpga_array
   fpga_array.FPGAArray
   core.chFPGA_controller
   core.chFPGA_controller.chFPGA_controller
   core.chFPGA_receiver
   core
   core.icecore
   core.icecore_ext
   core.icecore.IceBoard
   core.icecore.IceBoardHandler
   core.icecore.IceBoardHandler
   core.icecore.IceBoardPlusHandler
   core.icecore.async
   core.icecore.Ccoll
   core.icecore.NameSpace
   core.metrics




