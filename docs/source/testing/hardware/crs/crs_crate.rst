------------------------------------
Pre-deployment CRS crate-level tests
------------------------------------

Descriptions
============

Pre-deployment tests of a fully populated CRS crate. The crate power supply is controlled through
``psucontrol``. The tests, in menu order, are:

- Power supply connection
- Crate initialization: every board in the hardware map is found, every FPGA is programmed, and
  the crate synchronizes
- IRIG-B synchronization, repeated for the number of tries set in the configuration
- ADC clocks on every motherboard and mezzanine (200 MHz, within the configured tolerance)
- Power cycle: repeated initializations, counting ADC clock and UDP errors

A single iteration that runs past its time limit is aborted and the crate is power-cycled, so a
board that stops answering does not stall the run.

Running the tests
=================

Crate tests in ``qc/CRS_crate/f_engine_tests.py``.

.. code-block:: bash

    cd qc/CRS_crate
    python f_engine_tests.py

Testing Instructions
====================

There is no illustrated bench guide for this suite. Follow the menu.
