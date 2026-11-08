---
CRS
---

The CRS is an Ultrascale+ RFSoC board by `t0 technology <https://www.t0.technology>`_. Its ARM
processor runs U-Boot from flash; ``pychfpga`` supplies the rest of the software.

Boot and ARM firmware
=====================

At power-up the board boots U-Boot and broadcasts discovery beacons. When an array is created,
``pychfpga`` uploads the TCPipe ARM firmware (``tcpipe_crs.elf``) to each board through U-Boot,
then programs the FPGA. There is nothing to flash for normal operation.

The firmware files are in ``pychfpga/hardware/crs/arm_firmware/``. Use
``--arm_firmware_pathname`` (or the ``platform_firmware_pathname`` argument of
:class:`~pychfpga.fpga_array.FPGAArray`) to upload a different TCPipe image.

Discovery
=========

.. code-block:: bash

    crs_discovery            # live table of boards, refreshed continuously
    crs_discovery --dump     # print the table once and exit

The table shows each board's serial number, crate, slot, IP address, and whether it is in U-Boot
or running TCPipe. See :doc:`../../user_guide/networking` for the network requirements.

Upgrading U-Boot
================

Over the network
----------------

Recent U-Boot builds can be reflashed over the network with ``fwupgrade.py``. Targets must be
listed explicitly; use ``--dry-run`` first to check which boards match.

.. code-block:: bash

    cd pychfpga/hardware/crs/arm_firmware
    uv run fwupgrade.py --help
    uv run fwupgrade.py 37 55                 # CRS serials 0037 and 0055
    uv run fwupgrade.py "*" --dry-run         # every CRS on the network: check first
    uv run fwupgrade.py "CRC4 11"             # every board in crate 11
    uv run fwupgrade.py "CRC? *"              # every board in any crate
    uv run fwupgrade.py "* 1"                 # board, crate or backplane serial 1
    uv run fwupgrade.py 37 --firmware uboot.bin

Without uv, activate the ``pychfpga`` environment and run ``./fwupgrade.py``. Turn off or
disconnect boards you do not want to upgrade.

Over JTAG
---------

Boards with a very old U-Boot, or none, are programmed over JTAG with Vitis, using the micro-USB
cable:

.. code-block:: bash

    source /tools/Xilinx/Vitis/2022.2/settings64.sh
    cd pychfpga/hardware/crs/arm_firmware
    program_flash -f uboot.bin -offset 0 -flash_type qspi-x8-dual_parallel \
        -fsbl fsbl-t0-crs.elf -blank_check -verify

``-blank_check`` and ``-verify`` can be omitted to save time. Watch the boot on the serial
console (``minicom -D /dev/ttyUSB1``). Full instructions are in
``CRS_arm_firmware_README.txt`` in the same folder.

Tools
=====

.. toctree::
   :maxdepth: 1

   cge_signal_integrity
   ber_dashboard
