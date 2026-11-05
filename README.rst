.. Use UTF-8 encoding
Quick links
===========

- Source code: https://github.com/winterlandcosmology/pychfpga

- Online documentation: https://winterlandcosmology.github.io/pychfpga (use ``make docs`` to build the documentaiton locally in ``docs/build/html``)


Introduction
============

The ``pychfpga`` package is a Python framework for operating arrays of FPGA boards that run the `chFPGA` firmware (https://winterlandcosmology.bitbucket.io/chfpga/). It supports interactive control (Jupyter, ``fpga_array``) and automated site operation (``fpga_master``, REST, metrics).

Supported platforms: Xilinx KC705, Xilinx ZCU111, McGill IceBoard, and t0 CRS (Ultrascale+ RFSoC).

Requirements
============

- Python 3.12
- make
- uv (used by the Makefile for building docs, and useful to create a virtual environment for the project)

Installation
============

(examples below use ``uv`` to manage the virtual environment and install dependencies, but you can use the plain ``pip``/``venv``/``activate`` method if you prefer)

Clone the repository, create a virtual environment, and install the package::

    git clone https://github.com/winterlandcosmology/pychfpga.git
    cd pychfpga
    uv sync # install dependencies immediately

To run scripts::

    uv run raw_acq # start the raw acquisition server
    uv run fpga_master # start the FPGA master server
    uv run pychfpga/hardware/crs/crs_discovery # start the CRS discovery app

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