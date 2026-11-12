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
- Backplane and corner-turn health, from ``ca.measure_crate_health`` (lock, CRC, aligner
  stickies, packet errors, and advancing frame counters on every board)
- IRIG-B synchronization, repeated for the number of tries set in the configuration, with the
  configured frame-0 alignment limit
- ADC clocks on every motherboard and mezzanine (200 MHz, within the configured tolerance), and
  the same check with a power cycle between passes
- ADC synchronization delays, measured by ``check_sync_delays`` rather than judged by the operator
- ADC eye diagrams
- Network: repeated power cycles, counting missing slots and errors
- Power cycle: repeated initializations, counting ADC clock, UDP, and corner-turn health errors
- ADC ramp: every channel through the firmware ramp checker. The menu entry was already present;
  the test method is ``test_crate_ramp``.

Running the tests
=================

Crate tests in ``qc/ICE_crate/f_engine_tests.py``.

.. code-block:: bash

    cd qc/ICE_crate
    python f_engine_tests.py

Testing Instructions
====================

There is no illustrated bench guide for this suite. Follow the menu.
