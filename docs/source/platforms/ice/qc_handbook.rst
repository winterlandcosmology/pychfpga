---------------------------------
IceBoard quality control handbook
---------------------------------

Companion to the QC test menus (see :doc:`../../development/testing`): pictures that help
identify the steps of the QC process. Follow the instructions on screen and refer to these
pages as needed.

Authors:
    JF Cliche, Antoine Parise

MGK7MB motherboard
==================

.. figure:: ../../images/iceqc/IceBoardPhotos/main_view.jpg
    :width: 75%
    :alt: Motherboard
    :align: center

    Motherboard

Inspection test
---------------
**Step 1:** Is the FPGA heatsink securely attached?

**Step 2:** Are the FPGA heatsink pins cut near the stiffener?

.. figure:: ../../images/iceqc/IceBoardPhotos/InspTest/inspection_step12.jpeg
    :width: 60%
    :alt: example
    :align: center

    Points of interest for Steps 1 and 2

**Step 3:** Is the board stiffener installed, and the board reasonably flat?

**Step 4:** Are the PLL heatsinks attached?

**Step 5:** Does the arm shield fence look straight?

.. figure:: ../../images/iceqc/IceBoardPhotos/InspTest/inspection_step345.jpeg
    :width: 60%
    :alt: example
    :align: center

    Points of interest for Steps 3,4 and 5

**Step 6:** Are the dipswitches set correctly?

**Step 7:** Are the jumpers set correctly?

.. figure:: ../../images/iceqc/IceBoardPhotos/InspTest/inspection_step67.jpeg
    :width: 60%
    :alt: example
    :align: center

    Front of the ICE board

**Step 8:** Are the 90 pin Molex backplane connectors screwed down? (backside)

**Step 9:** Are the QSFP and SFP connectors soldered in place? (backside)

**Step 10:** Do all the buck converters have the additional hand-soldered capacitor? (backside)

.. figure:: ../../images/iceqc/IceBoardPhotos/InspTest/inspection_step8910.jpeg
    :width: 60%
    :alt: example
    :align: center

**Step 11:** Do all the FMC power switches look well soldered?

**Step 12:** Is the blue patch wire in place and secured?

.. figure:: ../../images/iceqc/IceBoardPhotos/InspTest/inspection_step1112.jpeg
    :width: 60%
    :alt: example
    :align: center

Impedance test
--------------

Before taking measurements, make sure that the board setup is as shown below.

.. figure:: ../../images/iceqc/IceBoardPhotos/ImpTest/CroppedIceboard.jpeg
    :width: 60%
    :alt: example
    :align: center

    Iceboard with ground connection and one-slot backplane

Apply the red test probe to the test points in the following order: +V
supply followed by the buck converters in a counter clockwise order. Refer to probe location 10 in the diagram for instructions on
where to apply the test probe for all buck converters.

.. figure:: ../../images/iceqc/IceBoardPhotos/ImpTest/Impedance_test-points.jpeg
    :width: 60%
    :alt: example
    :align: center

    Impedance measurement test locations

Power test
----------

Connect the power cable to the backplane as shown below.

.. figure:: ../../images/iceqc/IceBoardPhotos/PowerUpTest/power_cable.jpeg
    :width: 60%
    :alt: example
    :align: center

    Power cable attached to one-slot backplane with motherboard

Check that all 9 power LEDs are turned on as shown below.

.. figure:: ../../images/iceqc/IceBoardPhotos/PowerUpTest/power_leds.jpeg
    :width: 60%
    :alt: example
    :align: center

    Motherboards 9 power LEDS

PLL programming
---------------

Connect the PLL programming cable to the board, making sure that the
black marked side faces the SFP connectors.

.. figure:: ../../images/iceqc/IceBoardPhotos/PLLTest/PLLDongle.jpeg
    :width: 60%
    :alt: example
    :align: center

    PLL Programming dongle

Check that both PLL lock lights are turned on (green & yellow on the right)

.. figure:: ../../images/iceqc/IceBoardPhotos/PLLTest/PLLLock.jpeg
    :width: 60%
    :alt: example
    :align: center

    PLL Lock LEDs
