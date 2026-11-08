-------
Testing
-------

``pychfpga`` ships two test suites:

- **Hardware QC** (``qc/``): acceptance tests for IceBoard motherboards, mezzanines and crates.
- **Firmware tests** (``firmware_tests/``): functional tests of the chFPGA firmware (function
  generator, FFT, scaler, correlator) on a live board.

Both use pytest and the ``wtl.pytest_xreport`` plugin for menus, HTML reports and plots. Install
the test dependencies with:

.. code-block:: bash

    uv sync --extra qc          # or: pip install -e ".[qc]"

Hardware QC
===========

The ``qc/`` tree tests:

- IceBoard motherboard (MGK7MB)
- ADC mezzanine (MGADC08)
- 16-slot backplane (MGK7BP16)
- complete IceBoard crate, before deployment

The ``labpy`` instrument library (an extension of pymeasure) drives the bench equipment.

Each suite is an interactive xreport menu:

.. code-block:: bash

    python qc/MGK7MB/mgk7mb_test.py
    python qc/MGK7BP16/mgk7bp16_tests.py
    python qc/ICE_crate/f_engine_tests.py
    python qc/run_qcengine.py

The menu loads the YAML configuration next to the script, talks to the device under test, and
writes HTML/PDF reports. ``qc/README.txt`` lists the required equipment. The IceBoard
inspection steps are illustrated in :doc:`../platforms/ice/qc_handbook`.

Firmware tests
==============

The tests program a board, inject known waveforms with the function generator (FUNCGEN), and
compare the captured scaler, FFT and correlator output with a host model.

Configuration
-------------

Always run from ``firmware_tests/`` so that ``pytest.ini`` is used:

.. code-block:: bash

    cd firmware_tests
    pytest -v                                      # CRS: config_crs.yaml (default)
    pytest --xreport-config=config_ice.yaml -v     # IceBoard

Edit the YAML file before a run:

``connection_config.hwm``
    Board to test, e.g. ``crs 55``.

``connection_config.mode``
    Firmware mode, e.g. ``corr8`` or ``shuffle8``.

``connection_config.prog``
    ``0``: do not program; ``1``: program if outdated; ``2``: always program.

``test_config.supported_modes``
    Modules that can run in each mode. Tests of other modules are skipped.

Modules
-------

- ``funcgen_test.py``
- ``fft_test.py``
- ``scaler_test.py``
- ``corr_test.py`` (needs a correlator mode such as ``corr8``)

.. code-block:: bash

    pytest funcgen_test.py -v
    pytest scaler_test.py::TestScaler::test_flags -v
    pytest corr_test.py -m corr -v

Plots and XML results go to ``fw_test_results/`` (see ``--xreport-path``).

The simulation helpers in ``firmware_tests/fp_fft/`` and ``firmware_tests/casper_simulations/``
are not part of the pytest collection.

Scaler tests
------------

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
