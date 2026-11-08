------------------------------------
Pre-deployment ICE crate-level tests
------------------------------------

Descriptions
============

Pre-deployment tests of a fully populated IceBoard crate (motherboards, mezzanines, switch, power
supply and GPS). The tests, in menu order, are:

- Power supply connection
- Crate initialization: every board in the hardware map is found, every FPGA is programmed, and
  the crate synchronizes
- Backplane errors, from ``print_shuffle_status``
- IRIG-B synchronization, repeated for the number of tries set in the configuration
- ADC clocks on every motherboard and mezzanine (200 MHz, within the configured tolerance), and
  the same check with a power cycle between passes
- ADC synchronization delays
- ADC eye diagrams
- Network: repeated power cycles, counting missing slots and errors
- Power cycle: repeated initializations, counting ADC clock and UDP errors

Running the tests
=================

Crate tests in ``qc/ICE_crate/f_engine_tests.py``.

.. code-block:: bash

    cd qc/ICE_crate
    python f_engine_tests.py

Testing Instructions
====================

There is no illustrated bench guide for this suite. Follow the menu.
