--------------
Hardware setup
--------------

Requirements
============

- One or more IceBoards, on the bench or in a crate
- An SD card with the IceBoard ARM image in each board (see :doc:`arm_firmware`)
- An SFP+ to RJ45 1G adapter in each IceBoard SFP+ cage
- Power for the boards or crates. Set the supply current limit high enough for FPGA
  programming (see :doc:`../../user_guide/troubleshooting`).
- Cooling: FPGA fans, and some airflow over the ADC mezzanines
- A 1G Ethernet switch (not 10/100) that reaches the host
- Two Ethernet cables per IceBoard (ARM and FPGA)
- The network settings in :doc:`../../user_guide/networking`

For synchronized multi-board operation, feed a 10 MHz clock and IRIG-B (often from a GPS receiver)
to each board through the SMA connectors or the crate backplane.

Bring-up
========

1. Install the SD card.
2. Set the reference-clock jumper.
3. If needed, connect the clock and IRIG-B. **Check the levels of any external signal.**
4. Connect the power supply and power up. The two PLL LEDs at the bottom of the front panel turn on
   immediately (clock present). After about 25 seconds the upper LEDs blink, then stay on: the ARM
   has an IP address and has finished booting.
5. Connect two Ethernet cables to the 1G switch:

   - ARM: the lower of the dual RJ45 connectors. Any speed.
   - FPGA: the SFP+ cage with the SFP-to-RJ45 adapter. **1G only.**
