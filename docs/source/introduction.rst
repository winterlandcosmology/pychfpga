Introduction
************




``pychfpga`` is a Python package for operating arrays of FPGA boards running the `chFPGA` firmware (https://winterlandcosmology.bitbucket.io/chfpga/) in order to implement radiotelescope arrays of various sizes, from a single-board desktop correlator up to arrays of hundreds of crate-mounted boards forming a multi-thousand element interferometer.

Currently supported hardware platforms are:

- **Xilinx KC705** (FPGA: Kintex 7). Off-the-shelf evaluation board. Needs one or two McGill MGADC08 8-channel ADC boards.
- **Xilinx ZCU111** (FPGA: Ultrascale+ RFSoC). Off-the-shelf evaluation board. Needs ADC breakout board.
- **IceBoard** (FPGA Xilinx Kintex 7). Custom board designed by McGill University for large synchronized arrays. Needs one or two McGill MGADC08 8-channel ADC boards.
- **CRS** (Ultrascale+ RFSoC). Custom board designed by `t0 technology <https://www.t0.technology>`_ for large synchronized arrays.

``pychfpga`` uploads the chFPGA firmware to the FPGA and initializes all of its subsystems, including:

- ADC data acquisition (including calibration)
- F-Engine (channelizer) including a function generator, PFB/FFT, Scaler and raw data capture
- Corner-turn engine for transposing data between 1 board, 1 crate (16 boards) or 2 crates (32 boards)
- Optional on-board X-Engine (N² correlator): (4 + 4j) bit data width
- Optional 40- or 100 Gbps Ethernet real-time data offload to an external X-Engine farm.

``pychfpga`` is currently used to operate telescopes such as CHIME, CHORD, HIRAX, various outrigger telescopes, and a number of small portable interferometer-based experiments.

Source code is at https://github.com/winterlandcosmology/pychfpga


Features
========

- Multi-platform: Supports multiple hardware platforms

- Hardware map manager: Keeps track of all the boards, crates and mezzanine boards in an array and how they connect together.

- Auto-discovery: FPGA boards can be added to the hardware map explicitly by specifying their IP address, or can be automatically found on the local network by board or crate serial numbers.

- Concurrent operation: The array manager can perform operations on multiple boards at the same time using asynchronous coroutines to maximize performance. Has been used to operate arrays of 128 boards.

- Array-wide synchronization: Provides methods to perform array-wide data acquisition synchronization based on GPS-provided timestamps or a distributed trigger signal

- Multiple operational modes: The FPGA can be configured in various ways (correlator, baseband streaming etc.). The FPGA firmware applicable to the desired operational mode for the hardware platform is automatically selected, uploaded to the FPGA, and initialized.




Documentation
=============

Online documentation is available at https://winterlandcosmology.github.io/pychfpga. It is built automatically when major branches are pushed to the repository.

The documentation can be locally built manually ``make docs`` from the repository root). The documentation will be in the ``docs/build/html`` directory.



Requirements
============

System requirements
-------------------

- Git + LFS (``git lfs install`` must have been run by the user to activate LFS) 
- Python 3.12.x + pip
- Python virtual environment manager (venv, conda, etc.)
- build tools (make, cpp, etc.) (for python packages that need to be compiled)
- the matching ``pythonX.X-dev`` package (needed to compile ``netifaces``).

Optionally:

- uv (to build the documentation)
- Prometheus and Grafana if you want the graphs published by ``fpga_master``.
- mDNS (for auto-discovery of boards and crates for the ICE platform)

Networking configuration
------------------------

Real-time data FPGA offload links
.................................  

In the "corner-turn" modes, the `chfpga` firmware sends its corner-turned data in real-time over dedicated platform interfaces (40G or 100G Ethernet QSFPs) as Jumbo UDP packets. Given the traffic exceeding 75 Gbps, those typically require dedicated wiring or network switches to the external X-Engine farm.

Real-time data links are not used when the embedded X-Engine is enabled.

FPGA Control and data capture interface 
.......................................

A Slow 1G Ethernet link is used both to control the FPGA and to capture data from the FPGA.

COntrol stream is sent over UDP or TCP sockets, depending on the platform. 

Data capture, which includes periodic snapshots of the raw ADC or FFT outputs, and the embedded X-Engine outputs, are sent as UDP Jumbo packets over the same link.

For data capture to work, the networking equipment and the host computer interface must be configured to support jumbo frames. Misconfiguration of the networking is one of the most common causes of problems. In addition to this, the UDP buffers of the host computer networking stack must generally be enlarged to prevent the loss of packets::

    sudo ifconfig eth0 mtu 9000
    sudo sysctl -w net.core.rmem_max=262144000
    sudo sysctl -w net.core.rmem_default=262144000

Time and clock
--------------

When multiple boards are to be used in a coherent array, you will need:

- an external 10 MHz reference clock to synchronize sampling
- a trigger or IRIG-B time signal used to synchronize the data framing.

Both can be provided by a GPS receiver. Note that the some FPGA boards can be installed in crates backplanes that can distribute these signals to all the boards in the crate.

FPGA hardware, clock, and Ethernet requirements are in :doc:`howto/hardware_setup`.


Installation
============

See :doc:`howto/installation` for platform-specific notes. The essentials are:

Clone the repository::

    git clone git@github.com:winterlandcosmology/pychfpga.git

Create and activate a virtual environment (assuming you are using venv)::

    python3 -m venv my_env
    source my_env/bin/activate

Install the package (we generally installing in-place, in "-e" editable mode)::

    pip install -e ./pychfpga


Optional steps
--------------

If you need to interact with the array, you can install ipython or jupyter::

    pip install ipython
    pip install jupyter

If you plan to run the quality control scripts, the basic AND additional dependencies can be installed with::

    pip install -e ./pychfpga[qc]

Operation
=========

Interactive mode
----------------

``pychfpga`` can be used both in interactive manner for development in ipython or Jupyter notebooks. (See :doc:`howto/fpga_array`) 

Server mode
-----------

For production operation, the `fpga_master` and `raw_acq` servers can be launched. Those will initialize the array based on a YAML configuration file, and will provide HTTP REST endpoints to operate the array and serve real-time stats and performance metrics to an aggregator such as Prometheus (See :doc:`howto/server_operation`)

.. - :doc:`howto/ssh_keys` — GitHub / Bitbucket SSH keys
.. - :doc:`howto/installation` — Python environment and ``pip install``
.. - :doc:`howto/hardware_setup` — IceBoard wiring, clock, and Ethernet
.. - :doc:`howto/fpga_array` — interactive ``fpga_array``
.. - :doc:`howto/server_operation` — ``fpga_master`` / ``raw_acq`` / Docker
.. - :doc:`howto/testing` — ice-qc hardware QC and firmware tests



* :ref:`genindex`
* :ref:`search`
