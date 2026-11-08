------------
Introduction
------------

``pychfpga`` is a Python package that operates arrays of FPGA boards running the `chFPGA
<https://winterlandcosmology.github.io/chfpga>`_ firmware. It scales from a single benchtop board
to hundreds of crate-mounted boards forming the digital front-end of a radio-telescope array.

``pychfpga`` finds the boards, uploads the chFPGA bitstream that matches the requested operational
mode, and initializes every firmware subsystem:

- ADC data acquisition, including calibration
- F-engine (channelizer): function generator, PFB/FFT, scaler and raw data capture
- Corner-turn engine, within a board, a crate or between crates
- Optional on-board X-engine (N² correlator) with (4 + 4j)-bit data
- Optional 40G/100G Ethernet real-time offload to an external X-engine

Source code: https://github.com/winterlandcosmology/pychfpga


Features
========

- **Hardware map**: tracks boards, crates and mezzanines, and how they connect.
- **Auto-discovery**: boards are added by IP address, hostname, or board or crate serial number.
- **Concurrent operation**: array operations run on all boards at once using asyncio. Used on
  arrays of 128 boards.
- **Array-wide synchronization**: data acquisition is aligned across boards with GPS-derived
  timestamps or a distributed trigger.
- **Mode-based firmware selection**: the bitstream matching the platform and mode is selected,
  uploaded and initialized automatically.
- **Servers**: ``fpga_master`` and ``raw_acq`` run the array from a YAML configuration, accept
  REST commands, and publish Prometheus metrics.


Supported platforms and modes
=============================

The operational mode (the ``mode`` argument) selects the chFPGA firmware configuration that is
loaded on the FPGA. Platform and configuration names follow chFPGA convention ``<platform>_<config>``.

.. list-table::
   :header-rows: 1
   :widths: 5 5 5 5
   :stub-columns: 1

   * - Platform
     - Config
     - Bitstream
     - Description
   * - CRS
     -
     -
     - `t0 technology <https://www.t0.technology>`_, Ultrascale+ RFSoC, CRS backplanes
   * -
     - ``corr8``
     - ``chfpga_crs_corr8.bit``
     - 8-input, 1-board :math:`N^2` correlator
   * -
     - ``corr32``
     - ``chfpga_crs_corr32.bit``
     - 32-input, 4-board :math:`N^2` correlator
   * -
     - ``corr64``
     - ``chfpga_crs_corr64.bit``
     - 64-input, 8-board :math:`N^2` correlator
   * -
     - ``shuffle8``
     - ``chfpga_crs_ct.bit``
     - Single-board channelizer and corner-turn with 100G offload
   * - IceBoard
     -
     -
     - MGK7MB, Kintex-7, standalone or in MGK7BP16 and MGK7BP1 crates
   * -
     - ``shuffle16``
     - ``chfpga_ice_ct.bit``
     - Sincle-board channelizer and corner-turn with 8x10G offload
   * -
     - ``shuffle128``
     - ``chfpga_ice_ct.bit``
     - Half-crate (128 inputs) and corner-turn with 8x10G offload
   * -
     - ``shuffle256``
     - ``chfpga_ice_ct.bit``
     - Single crate (256 inputs) channelizer and corner-turn with 8x10G offload
   * -
     - ``shuffle512``
     - ``chfpga_ice_ct.bit``
     - Dual-crate (512 inputs) channelizer and corner-turn with 8x10G offload
   * -
     - ``chan8``
     - ``chfpga_ice_ct.bit``
     - 8-input 8-bit ADC or (8+8i)-bit FFT data  streaming on 8x10G links
   * -
     - ``chan4``
     - ``chfpga_ice_ct.bit``
     - 16-input 4-bit ADC (high nibble) or (4+4i)-bit FFT data streaming 8 8x10G links
   * -
     - ``corr16``
     - ``chfpga_ice_corr16_pbg_jun16.bit``
     - 16-input, 1-board :math:`N^2` correlator
   * - ZCU111
     -
     -
     - Xilinx Ultrascale+ RFSoC evaluation board
   * -
     - ``corr4``
     - ``sifpga_zcu111_wrapper.bit``
     - 4-input, 1-board :math:`N^2` correlator
   * -
     - ``corr8``
     - ``sifpga_zcu111_wrapper.bit``
     - 8-input, 1-board :math:`N^2` correlator
   * -
     - ``chan8``
     - ``chfpga_zcu111.bit``
     - 8-input 8-bit ADC or (8+8i)-bit FFT data  streaming on 8x10G links
   * - KC705
     -
     -
     - Xilinx Kintex-7 evaluation board. Potentially supported; ``pychfpga`` has no KC705
       platform class yet. See :doc:`platforms/kc705`.
   * -
     - ``corr8``
     - ``chfpga_kc705_corr8.bit``
     - 8-input, 1-board :math:`N^2` correlator
   * - ZUBoard
     -
     -
     - platform discovery only, no chFPGA mode



Some bitstreams may not be included in the current release.



Where to go next
================

- :doc:`getting_started/requirements`
- :doc:`getting_started/installation`
- :doc:`user_guide/interactive_operation`
- :doc:`user_guide/server_operation`


Documentation
=============

Online documentation: https://winterlandcosmology.github.io/pychfpga. It is rebuilt when major
branches are pushed.

To build it locally, run ``make docs`` from the repository root. The output is in
``docs/build/html``.
