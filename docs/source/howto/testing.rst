Testing
=======

This page describes two test suites shipped with ``pychfpga``:

1. **ice-qc** — hardware quality-control (acceptance) tests for Ice
   motherboards, mezzanines, and crates.
2. **Firmware tests** — functional tests of chFPGA firmware (function
   generator, FFT, scaler, correlator) on a live board.

Both use pytest and McGill's ``wtl.pytest_xreport`` plugin for menus, HTML
reports, and plots.

ice-qc (hardware QC)
--------------------

The :file:`qc/` tree tests:

- IceBoard motherboard (MGK7MB)
- CHIME ADC mezzanine (MGADC08)
- 16-slot backplane (MGK7BP16)
- Single-crate F-engine (pre-deployment)

Install QC extras::

    pip install -e ./pychfpga[qc]

You also need the ``labpy`` instrument library (extension of pymeasure) for
the bench equipment.

Each suite is an interactive xreport menu. Typical entry points::

    python qc/MGK7MB/mgk7mb_test.py
    python qc/MGK7BP16/mgk7bp16_tests.py
    python qc/ICE_crate/f_engine_tests.py
    python qc/run_qcengine.py

The menu loads a YAML config next to the script, talks to the DUT, and
writes HTML/PDF reports. See :file:`qc/README.txt` for the hardware list.

Firmware tests
--------------

The :file:`firmware_tests/` suite programs a board, injects known waveforms
through FUNCGEN, and compares captured scaler/FFT/correlator output against
a host-side model.

Working directory and config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Always run from :file:`firmware_tests/` so ``pytest.ini`` is picked up::

    cd firmware_tests
    pytest -v

Default config is :file:`config_crs.yaml` (see ``addopts`` in
:file:`pytest.ini`). For Ice::

    pytest --xreport-config=config_ice.yaml -v

Edit the YAML before a run:

- ``connection_config.hwm`` — board identity (for example ``crs 55``)
- ``connection_config.mode`` — bitstream (for example ``corr8``, ``shuffle8``)
- ``connection_config.prog`` — ``0`` skip, ``1`` program if outdated, ``2`` always
- ``test_config.supported_modes`` — which modules may run in that mode;
  tests whose module is not listed are skipped

Modules
~~~~~~~

``pytest.ini`` collects:

- :file:`funcgen_test.py`
- :file:`fft_test.py`
- :file:`scaler_test.py`
- :file:`corr_test.py` (requires a correlator mode such as ``corr8``)

Examples::

    pytest funcgen_test.py -v
    pytest scaler_test.py::TestScaler::test_flags -v
    pytest corr_test.py -m corr -v

Plots and XML land under :file:`fw_test_results/` (see ``--xreport-path``).

A short scaler-flag walkthrough is in :doc:`scaler_test`. Simulation helpers
under :file:`firmware_tests/fp_fft/` and
:file:`firmware_tests/casper_simulations/` are **not** part of this pytest
collection and are not in the API docs.
