Configuring FPGA hardware
=========================

This page covers bench and crate wiring for IceBoard-class systems. For
board-specific software notes, see :doc:`/hardware/ice/index`. For CRS
pocket-correlator work, see :doc:`pocket_correlator`.

FPGA hardware requirements
--------------------------

To operate a board you need:

- One or more IceBoards, on the benchtop or in a crate.
- A generic IceBoard SD card in each board (ARM Linux). The image that
  matches the current release is under ``./pychfpga/arm_firmware``.
- An SFP+ to RJ45 adapter in each IceBoard SFP+ cage.
- Power for the board(s) or crate(s).
- Cooling: FPGA fans and some airflow over the ADC mezzanines.
- A **1G** Ethernet switch or router (not 10/100) that reaches the control
  computer.
- Two Ethernet cables per IceBoard (FPGA and ARM).
- A control computer (Windows or Linux; macOS is untested) on the same
  subnet as the boards.
- Jumbo frames on every switch/router between the boards and the control
  computer if you will capture raw ADC, FFT, or correlator data over 1G
  Ethernet (or QSFP+).
- A host firewall that accepts UDP from the FPGA.

The IceBoard/computer subnet shall have:

- A netmask of at least ``255.255.252.0`` (``/22``), or larger (for example
  ``255.255.0.0``), so FPGA addresses in ``*.*.3.*`` are included.
- A DHCP server that does **not** assign ``*.*.3.*`` (for example
  ``192.168.0.*``).

If several boards must run synchronized, feed a 10 MHz clock and IRIG-B
(often from a GPS receiver) to each board on SMA connectors or through the
crate backplane.

Configuring the hardware
------------------------

- Install the SD card in the IceBoard.
- Set the reference-clock jumper.
- If required, connect clock and IRIG-B. **Watch the levels of any external
  signals.**
- Connect the power supply.
- Power up. The two PLL LEDs at the bottom of the front panel should turn on
  immediately (clock present). After about 25 seconds the upper LEDs blink
  then stay on: the ARM has an IP address and has finished booting.

Connect two Ethernet cables between the IceBoard and the 1G switch/router
that reaches the control PC:

- ARM Ethernet: dual RJ-45 connectors. That port works at any speed.
- FPGA Ethernet: SFP+ cage with an SFP-to-RJ45 1G adapter. That port is
  **1G only**.
