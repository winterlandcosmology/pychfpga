


----------------
Hardware testing
----------------


A number of test suites are available for checking the proper operation of the ICE and CRS hardware at the fabrication and integration (pre-deployment) stages.

The tests are located under the ``qc/`` directory. 

The test suites are written as ``pytest`` unit tests. The
``wtl.pytest_xreport`` plugin provides additional functionality such as a  test selection and sequencing menus, interactive prompts, test result aggregation and summarization, and HTML and PDF reports. 
The ``labpy`` package (an extension of
pymeasure) drives the bench instruments.


Installation
------------

The dependencies for the test suites are not part of the base ``pychfpga`` installation. They must be installed separately using the followin extras:

.. code-block:: bash

    uv sync --extra qc          # or: pip install -e ".[qc]"


Test suites
-----------

The test suites cover the following hardware tests:

- ICE Hardware:

  - :doc:`ICE Motherboard: IceBoard (Model MGK7MB) <ice/mgk7mb>`
  - :doc:`ICE ADC Mezzanine (Model MGADC08) <ice/mgadc08>`
  - :doc:`ICE 16-slot backplane (Model MGK7BP16) <ice/mgk7bp16>`
  - :doc:`ICE F-Engine pre-deployment crate-level tests <ice/ice_crate>`

- CRS Hardware:

  - :doc:`CRS F-Engine pre-deployment crate-level tests <crs/crs_crate>`




.. toctree::
   :maxdepth: 2
   :hidden:

   ice/index
   crs/index


..    ice/mgadc08
..    ice/mgk7mb
..    ice/mgk7bp16
..    ice/ice_crate


Running the tests
-----------------

Each test suite is located in a subdirectory of the ``qc/`` directory and has a ``test_config.yaml`` file that defines the test parameters and equipment.
A python script named ``<suite_name>_tests.py`` in the test folder launches the test menu that allow the user to select the tests to run. Tests can be re-run if needed, and the summary will pick the latest results. Test interdependencies and handled by the menu. The menu script also generates an overall test summary that combines all tests, in HTML or PDF format. The tests results, including plots, graphs, binary data, logs are stored in a folder specified in the config file (this folder should be outside of the pychfpga repository). 


One can optionally use the top menu to select which test suite to run by executing from the repository root:

.. code-block:: bash

    python qc/run_qcengine.py

Additional documentation
------------------------

``qc/README.txt`` describes the suites. ``qc/ice_test_equipment.txt`` lists the bench equipment.


