----------------
Firmware testing
----------------

A number of test suites are available for checking the proper operation of the chFPGA firmware on
a live board. The tests program the board, inject known waveforms with the function generator
(FUNCGEN), and compare the captured scaler, FFT and correlator output with a host model.

The tests are located under the ``firmware_tests/`` directory.

The test suites are written as ``pytest`` unit tests. The ``wtl.pytest_xreport`` plugin provides
additional functionality such as test selection, interactive prompts, test result aggregation and
summarization, and HTML and PDF reports.

Installation
------------

The dependencies for the test suites are not part of the base ``pychfpga`` installation. They must
be installed separately using the following extra:

.. code-block:: bash

    uv sync --extra qc          # or: pip install -e ".[qc]"

Test suites
-----------

The test suites cover the following firmware modules:

- ``funcgen_test.py``
- ``fft_test.py``
- ``scaler_test.py``
- ``corr_test.py`` (needs a correlator mode such as ``corr8``)

The simulation helpers in ``firmware_tests/fp_fft/`` and ``firmware_tests/casper_simulations/``
are not part of the pytest collection.

Running the tests
-----------------

Always run from ``firmware_tests/`` so that ``pytest.ini`` is used:

.. code-block:: bash

    cd firmware_tests
    pytest -v                                      # CRS: config_crs.yaml (default)
    pytest --xreport-config=config_ice.yaml -v     # IceBoard

A single module or test can be selected:

.. code-block:: bash

    pytest funcgen_test.py -v
    pytest scaler_test.py::TestScaler::test_flags -v
    pytest corr_test.py -m corr -v

Plots and XML results go to ``fw_test_results/`` (see ``--xreport-path``).

Each suite uses a YAML configuration file. Edit it before a run:

``connection_config.hwm``
    Board to test, e.g. ``crs 55``.

``connection_config.mode``
    Firmware mode, e.g. ``corr8`` or ``shuffle8``.

``connection_config.prog``
    ``0``: do not program; ``1``: program if outdated; ``2``: always program.

``test_config.supported_modes``
    Modules that can run in each mode. Tests of other modules are skipped.

Additional documentation
------------------------

``scaler_test.py`` feeds FUNCGEN ramps with the FFT bypassed, applies gains, and compares the
captured scaler stream with an integer model. ``connection_config.mode`` must be listed in
``test_config.supported_modes.scaler_test`` (typically ``shuffle8`` or ``corr8``).

Coverage:

- unity, zero and ramped digital gains, and the postscaler
- 4-bit capture, and capture mode 3 when the science output is in another mode
- rounding, symmetric saturation, and bypass data types
- overflow statistics
- saturation flags. CRS: scaler capture type 6 (``sat_flags``), with +1 / -1 / 0 per real and
  imaginary field. IceBoard: PROBER low-nibble flags on capture type 0.

ADC overflow counts on the CRS are checked by the FUNCGEN and scaler statistics tests.
Platform-specific skips are in the test module.
