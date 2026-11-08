------------------------------
MGK7BP16 16-slot ICE backplane
------------------------------

Descriptions
============

The tests are divided in two phases: bench tests and crate tests.

Bench tests
-----------

These tests are performed with bench instruments, before boards are installed in the crate. The
tests are:

- Visual inspection
- Impedance
- Power
- Clock, trigger and time distribution: SMA inputs, fanout and the backplane connector, using a
  function generator and an oscilloscope
- SYNC fanout
- The on-board 10 MHz clock
- Reset LEDs

Crate tests
-----------

These tests are performed with boards installed in the crate. The tests are:

- Sensors
- QSFP
- Reset
- Bit error rate of the backplane links (two crates, unless the QSFP ports are looped)
- Mezzanine ramp on every slot

Running the tests
=================

Tests of the MGK7BP16 16-slot backplane, in ``qc/MGK7BP16/mgk7bp16_tests.py``.

.. code-block:: bash

    cd qc/MGK7BP16
    python mgk7bp16_tests.py

Testing Instructions
====================

There is no illustrated bench guide for this suite. Follow the menu.
