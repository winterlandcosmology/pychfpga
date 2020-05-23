.. pychfpga documentation master file, created by
   sphinx-quickstart on Wed Jan 20 12:29:25 2016.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.


.. role:: ul
    :class: underline

Welcome to pychfpga's documentation!
====================================

The ``pychfpga`` package is the Python framework that is used to operate the FPGA-based F-engine hardware, corner-turn and optional firmware-basd X-engine that is used for a number of big and small interferometer radio telescopes, including the Canadian Hydrogen Intensity Mapping Experiment (CHIME).

.. toctree::
   :maxdepth: 1

   howto/setup_python
   howto/takeData
   howto
   pychfpga
   pychfpga.core
   ch_acq
   script_documentation
   installation
   quick_start

.. toctree::
   :maxdepth: 1
   :caption: Main modules

   fpga_master
   raw_acq
   fpga_array
   pychfpga.core.chFPGA_controller
   chFPGA_controller
   ps
   gps
   firmware


CHIME-specific modules:

.. toctree::
   :maxdepth: 1
   :caption: CHIME-specific modules

   chime/kotekan
   chime/kotekan_master
   chime/networking_configuration
   chime/weather
   chime/index


External modules

.. toctree::
   :maxdepth: 1
   :caption: External modules

   external_packages/metrics
   external_packages/rest


.. include:: ../../README

Indices and tables
------------------

* :ref:`genindex`
* :ref:`search`



