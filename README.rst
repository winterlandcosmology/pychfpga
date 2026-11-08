.. Use UTF-8 encoding
Quick links
===========

- Source code: https://github.com/winterlandcosmology/pychfpga

- Online documentation: https://winterlandcosmology.github.io/pychfpga (use ``make docs`` to build the documentation locally in ``docs/build/html``)


Introduction
============

The ``pychfpga`` package is a Python framework for operating arrays of FPGA boards that run the `chFPGA` firmware (https://winterlandcosmology.github.io/chfpga). It supports interactive control (Jupyter, ``fpga_array``) and automated site operation (``fpga_master``, REST, metrics).

Supported platforms: t0 CRS (Ultrascale+ RFSoC), McGill IceBoard, and Xilinx ZCU111. Xilinx KC705 is potentially supported (chFPGA firmware exists; the platform class is left to the user).

Requirements
============

- Python 3.12
- git-lfs (bitstreams and ARM firmware are stored with git-lfs)
- make
- uv (used by the Makefile for building docs, and useful to create a virtual environment for the project)

Installation
============

(examples below use ``uv`` to manage the virtual environment and install dependencies, but you can use the plain ``pip``/``venv``/``activate`` method if you prefer)

Clone the repository, create a virtual environment, and install the package::

    git lfs install
    git clone https://github.com/winterlandcosmology/pychfpga.git
    cd pychfpga
    git checkout 2.2.0+crs_ct  # release (or branch) validated for your platform and configuration
    uv sync # install dependencies immediately

To run scripts::

    uv run raw_acq # start the raw acquisition server
    uv run fpga_master # start the FPGA master server
    uv run crs_discovery # list the CRS boards on the network

If you want to use interactive sessions, install and run the interactive tools:

ipython::

    uv pip install ipython
    uv run ipython

Jupyter lab::

    uv pip install jupyter ipykernel  # ipykernel makes this environment available to jupyter
    uv run jupyter lab

Then, in the python interactive session, create and initialize an FPGA array with::

    from pychfpga.fpga_array import FPGAArray
    ca = await FPGAArray("CRS 77:1 123:2", mode='corr8')
    print(ca.ib)  # print each board in the array
