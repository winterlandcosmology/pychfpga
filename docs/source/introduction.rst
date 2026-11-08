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


Supported platforms and modes
=============================

The operational mode (the ``mode`` argument) selects the chFPGA firmware configuration that is
loaded on the FPGA. Platform and configuration names follow chFPGA.

- **CRS** (`t0 technology <https://www.t0.technology>`_, Ultrascale+ RFSoC), with CRS backplanes

  - ``corr8``, ``corr32``, ``corr64``: on-board correlator (``crs_corr8``, ``crs_corr32``,
    ``crs_corr64``)
  - ``chan8``, ``shuffle8``: channelizer and corner-turn with 100G offload (``crs_ct``)

- **IceBoard** (MGK7MB, Kintex-7), standalone or in MGK7BP16 and MGK7BP1 crates

  - ``shuffle16``, ``shuffle128``, ``shuffle256``, ``shuffle512``, ``chan8``, ``chan4``:
    channelizer and corner-turn (``ice_ct``)
  - ``corr16``: on-board correlator (``ice_corr16``)

- **ZCU111** (Xilinx Ultrascale+ RFSoC evaluation board)

  - ``corr4``, ``corr8``, ``chan8``

- **KC705** (Xilinx Kintex-7 evaluation board): potentially supported. chFPGA has a
  ``kc705_corr8`` configuration, but ``pychfpga`` has no KC705 platform class yet. See
  :doc:`platforms/kc705`.

- **ZUBoard**: platform discovery only, no chFPGA mode.

Some bitstreams may not be included in the current release.


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
