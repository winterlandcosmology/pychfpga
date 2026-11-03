Scaler firmware tests
=====================

The scaler tests live in :file:`firmware_tests/scaler_test.py`. They program
a live board, feed FUNCGEN ramps (FFT bypass), apply gains, and compare the
captured scaler stream to a host integer model.

How to run
----------

From :file:`firmware_tests/`::

    pytest scaler_test.py -v

Use :file:`config_crs.yaml` or :file:`config_ice.yaml` as described in
:doc:`testing`. ``connection_config.mode`` must be in
``test_config.supported_modes.scaler_test`` (typically ``shuffle8`` or
``corr8``).

What is covered
---------------

- Unity / zero / ramped digital gains and postscaler
- 4-bit capture vs capture mode 3 when science output is in another mode
- Rounding, symmetric saturation, and bypass data types
- Overflow statistics
- **Saturation flags** on CRS: scaler capture type 6 (``sat_flags``). The
  payload is +1 / −1 / 0 per Re/Im field. ICE still uses PROBER low-nibble
  flags on capture type 0.

ADC overflow counts on CRS are checked in FUNCGEN / scaler stats tests, not
by stuffing TUSER into UCAP sample bytes. Platform-specific skips are in
the test module.

See also :doc:`testing` for the full firmware-test layout.
