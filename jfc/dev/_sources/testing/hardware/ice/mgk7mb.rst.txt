---------------------------
MGK7MB IceBoard Motherboard
---------------------------

Descriptions
============

The tests are divided in two phases: bench tests and networked tests.

Bench tests
-----------

These tests are performed on a standalone board, with no network, to make sure a defective board
will not damage the rest of the setup. The tests are:

- Visual inspection
- Resistance at the test points
- Power-up current
- PLL programming
- ARM memory, over RS-232

Networked tests
---------------

These tests are performed after the board is on the network. The tests are:

- Power and temperature sensors
- Serial number programmed into the EEPROM and flash
- Presence of every I2C device
- Boot from the backplane clock
- FPGA programming
- Basic mezzanine operation, then a ramp on its high-speed data lines
- QSFP: the cable is detected over I2C and its EEPROM can be read
- High-speed links (GTX), on a single-slot backplane

Running the tests
=================

Tests of the MGK7MB IceBoard motherboard, in ``qc/MGK7MB/mgk7mb_test.py``.

.. code-block:: bash

    cd qc/MGK7MB
    python mgk7mb_test.py

Testing Instructions
====================

.. figure:: ../../../images/iceqc/IceBoardPhotos/main_view.jpg
    :width: 75%
    :alt: Motherboard
    :align: center

    Motherboard

Inspection
----------

**Step 1:** Is the FPGA heatsink securely attached?

**Step 2:** Are the FPGA heatsink pins cut near the stiffener?

.. figure:: ../../../images/iceqc/IceBoardPhotos/InspTest/inspection_step12.jpeg
    :width: 60%
    :alt: Inspection steps 1 and 2
    :align: center

    Points of interest for steps 1 and 2

**Step 3:** Is the board stiffener installed, and the board reasonably flat?

**Step 4:** Are the PLL heatsinks attached?

**Step 5:** Does the arm shield fence look straight?

.. figure:: ../../../images/iceqc/IceBoardPhotos/InspTest/inspection_step345.jpeg
    :width: 60%
    :alt: Inspection steps 3, 4 and 5
    :align: center

    Points of interest for steps 3, 4 and 5

**Step 6:** Are the dipswitches set correctly?

**Step 7:** Are the jumpers set correctly?

.. figure:: ../../../images/iceqc/IceBoardPhotos/InspTest/inspection_step67.jpeg
    :width: 60%
    :alt: Inspection steps 6 and 7
    :align: center

    Points of interest for steps 6 and 7

**Step 8:** Are the 90-pin Molex backplane connectors screwed down? (backside)

**Step 9:** Are the QSFP and SFP connectors soldered in place? (backside)

**Step 10:** Do all the buck converters have the additional hand-soldered capacitor? (backside)

.. figure:: ../../../images/iceqc/IceBoardPhotos/InspTest/inspection_step8910.jpeg
    :width: 60%
    :alt: Inspection steps 8, 9 and 10
    :align: center

    Points of interest for steps 8, 9 and 10

**Step 11:** Do all the FMC power switches look well soldered?

**Step 12:** Is the blue patch wire in place and secured?

.. figure:: ../../../images/iceqc/IceBoardPhotos/InspTest/inspection_step1112.jpeg
    :width: 60%
    :alt: Inspection steps 11 and 12
    :align: center

    Points of interest for steps 11 and 12

Impedance
---------

Set the board up as shown before measuring.

.. figure:: ../../../images/iceqc/IceBoardPhotos/ImpTest/CroppedIceboard.jpeg
    :width: 60%
    :alt: Impedance-test setup
    :align: center

    IceBoard with ground connection and one-slot backplane

Apply the red probe in this order: the +V supply, then the buck converters counter-clockwise.
Probe location 10 in the diagram is the point used for every buck converter.

.. figure:: ../../../images/iceqc/IceBoardPhotos/ImpTest/Impedance_test-points.jpeg
    :width: 60%
    :alt: Impedance test points
    :align: center

    Impedance measurement test locations

Power-up
--------

Connect the power cable to the backplane as shown, and check that all 9 power LEDs are on.

.. figure:: ../../../images/iceqc/IceBoardPhotos/PowerUpTest/power_cable.jpeg
    :width: 60%
    :alt: Power cable on the one-slot backplane
    :align: center

    Power cable attached to the one-slot backplane

.. figure:: ../../../images/iceqc/IceBoardPhotos/PowerUpTest/power_leds.jpeg
    :width: 60%
    :alt: Motherboard power LEDs
    :align: center

    The nine power LEDs

PLL programming
---------------

Connect the PLL programming cable with the black marked side facing the SFP connectors. Both PLL
lock lights must be on (green and yellow, on the right).

.. figure:: ../../../images/iceqc/IceBoardPhotos/PLLTest/PLLDongle.jpeg
    :width: 60%
    :alt: PLL programming dongle
    :align: center

    PLL programming dongle

.. figure:: ../../../images/iceqc/IceBoardPhotos/PLLTest/PLLLock.jpeg
    :width: 60%
    :alt: PLL lock LEDs
    :align: center

    PLL lock LEDs

Memory
------

Insert a valid SD card in the front slot. Connect the RS-232 dongle to the bottom pins on the
front of the board, and the other end to a USB port on the test computer.

.. figure:: ../../../images/iceqc/IceBoardPhotos/MemTest/SD_card.jpeg
    :width: 60%
    :alt: SD card in the motherboard
    :align: center

    SD card plugged into the motherboard

.. figure:: ../../../images/iceqc/IceBoardPhotos/MemTest/RS232_dongle.jpeg
    :width: 60%
    :alt: RS-232 dongle
    :align: center

    RS-232 dongle attached to the motherboard

On the network
--------------

The remaining tests need an Ethernet cable in the second port from the edge, and a valid SD card
in the front slot.

.. figure:: ../../../images/iceqc/IceBoardPhotos/NetTest/ethernet.jpeg
    :width: 60%
    :alt: Ethernet cable on the ARM port
    :align: center

    Ethernet cable attached to the ARM processor

.. figure:: ../../../images/iceqc/IceBoardPhotos/NetTest/SD_card.jpeg
    :width: 60%
    :alt: SD card for the networked tests
    :align: center

    SD card plugged into the motherboard

Sensors
-------

No extra setup. Leave the mezzanines off the board.

Serial number
-------------

No extra setup.

I2C
---

No extra setup.

Backplane clock
---------------

Terminate the SMA clock jumper whenever it is not in use. That termination matters for step 3.

**Step 1.** Onboard crystal. Set the jumper to the crystal position (left).

.. figure:: ../../../images/iceqc/IceBoardPhotos/ClockTest/clock_jumper.jpeg
    :width: 70%
    :alt: Clock jumper in the crystal position
    :align: center

    Clock-select jumper in the onboard-crystal position

**Step 2.** Front-panel SMA. Set the jumper to the front SMA position (middle) and connect the SMA
cable to the front-panel connector. The other end goes to the backplane.

**Step 3.** Backplane clock. Set the jumper back to its original position (right), and terminate
the front-panel SMA cable.

.. figure:: ../../../images/iceqc/IceBoardPhotos/ClockTest/front_sma.jpeg
    :width: 70%
    :alt: Front-panel SMA clock
    :align: center

    Jumper in front-panel SMA mode, clock cable attached

.. figure:: ../../../images/iceqc/IceBoardPhotos/ClockTest/backplane_clock.jpeg
    :width: 70%
    :alt: Clock cable on the backplane
    :align: center

    Clock cable attached to the backplane

FPGA
----

This setup is also used for the mezzanine, ramp, QSFP and GTX tests. With the earlier setups
already done, the menu can run this group in order and stop at the first failure.

Plug an SFP adapter into the motherboard and an Ethernet cable into the adapter. Mount the FPGA
fan on the heatsink and attach its cable.

.. figure:: ../../../images/iceqc/IceBoardPhotos/FPGATest/fpga_ethernet.jpeg
    :width: 70%
    :alt: SFP adapter and Ethernet cable
    :align: center

    Ethernet cable and SFP adapter

.. figure:: ../../../images/iceqc/IceBoardPhotos/FPGATest/fpga_fan.jpeg
    :width: 70%
    :alt: FPGA fan
    :align: center

    Fan on the FPGA heatsink

Mezzanine
---------

Mount both mezzanines. Use the extender cables so the mezzanine connectors are not mated and
unmated on every board.

Ramp
----

Both mezzanines stay mounted, on the extender cables.

QSFP
----

Complete the FPGA setup first. Connect the QSFP cable in a loop. The GTX test uses the same loop.

.. figure:: ../../../images/iceqc/IceBoardPhotos/QSFPTest/qsfp_loop.jpeg
    :width: 70%
    :alt: QSFP loopback cable
    :align: center

    QSFP cable in a loopback

GTX
---

Complete the FPGA setup and the QSFP loop first. Mezzanines may be loaded or left off.
