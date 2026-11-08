------------
Installation
------------


Clone the repository
====================

Make sure `git-lfs` is configured for your account (run ``git lfs install`` if you haven't already -- this needs to be done only once per user).
Clone the repository and check out the release tag or branch that was validated for your platform and configuration:

.. code-block:: bash

    git clone https://github.com/winterlandcosmology/pychfpga.git -b 1.2.3+ice_ct  # tag is in the format x.y.z+<platform>_<config>
    cd pychfpga

Raplace the branch/tag name with one that points to the platform and configuration you are using.
Tag and branch names are described in :doc:`../development/workflow`.

If a bitstream or ARM firmware file is only a few hundred bytes, git-lfs did not download it. Run
``git lfs pull`` from the repository root to fetch the binaries.

Install package
===============

The simplest and fastest way to install all dependencies in an isolated environment is to use ``uv``:

.. code-block:: bash

    uv sync                    # creates .venv and installs pychfpga in editable mode

You can also use the old-fashion virtual environment and pip:

.. code-block:: bash

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -e .

If you plan to run hardware or firmware tests, build the docs or run special applications,  install the extra dependencies:

.. code-block:: bash

    uv sync --extra qc  # or pip install -e ".[qc]" , for testing environment
    uv sync --extra ber_dashboard  # or pip install -e ".[ber_dashboard]", for CRS BER test dashboard (PyQt6)
    uv sync --extra docs  # or pip install -e ".[docs], for building Sphinx documentation

Verify the installation (optional)
==================================

.. code-block:: bash

    uv run python -c "import pychfpga.fpga_array"
    uv run fpga_master --help

Running servers
===============

You can run servers that automatically create the array, initialize it, and continuously monitors the array and publishes monitoring information by launching the two servers:

.. code-block:: bash

    uv run raw_acq  # start the raw data acquisition server
    uv run fpga_master  <configuration_file.yaml>:config_name  # start the FPGA master server (raw_acq must be running)

You can also equivalently issue the ``raw_acq`` and ``fpga_master`` commands after activating the virtual environment.  


Interactive work:
=================

If you want to work interactively, install and run IPython and/or Jupyter:

.. code-block:: bash

    uv pip install ipython  # if you need ipython only
    uv run ipython


    uv pip install jupyter ipykernel   # ipykernel makes this environment available to Jupyter
    uv run jupyter lab

Then, in the python interactive session, create and initialize an FPGA array with:

.. code-block:: bash

    from pychfpga.fpga_array import FPGAArray
    ca = await FPGAArray("CRS 77:1 123:2", mode='corr8')
    print(ca.ib)  # print each board in the array





Build the documentation
=======================

.. code-block:: bash

    make docs     # output in docs/build/html
