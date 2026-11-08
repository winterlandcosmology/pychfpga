----------------
Server operation
----------------

For unattended operation, two servers run side by side:

- ``fpga_master`` initializes and operates the boards from a YAML configuration. It accepts REST
  commands over HTTP and publishes metrics for Prometheus.
- ``raw_acq`` receives, analyzes and stores the data the boards send on the 1G control link:
  raw ADC snapshots, channelized data, and on-board correlator output. It does not handle the
  real-time corner-turned data on the 40G/100G links, which goes to the X-engine.

Starting the servers
====================

.. code-block:: bash

    raw_acq
    fpga_master path/to/config.yaml:section

The configuration selects the boards, the mode, and the settings of every subsystem. Examples
are in ``tests/configs/``.

Command-line interface
======================

``fpga_master``, ``raw_acq`` and the auxiliary servers (:doc:`../platforms/aux/index`) share the
same command line:

.. code-block:: text

    <command> [config] [server_name] [method [args ...]] [--host HOST] [--port PORT]
              [--run | --no-run] [--no-start]

``config``
    ``[file.yaml]:section.subsection``. The section is a complete configuration.

``server_name``
    Server configuration to use when the configuration defines several. Optional otherwise.

``method``, ``args``
    A client method and its arguments. The command sends it to the server and exits.

``--host``, ``--port``
    Server address. Override the address in the configuration.

``--run``, ``--no-run``
    Keep running, or exit, regardless of whether a method was given.

``--no-start``
    Do not send the configuration to the server.

The command first looks for a server at the configured address. If none answers, it creates one
locally, sends it the configuration (unless ``--no-start``), and runs until :kbd:`Ctrl-C` if no
method was given.

Examples:

.. code-block:: bash

    fpga_master config.yaml:my_array          # start or connect, initialize, run
    fpga_master stop                          # send 'stop' to the server on localhost:54321

Default ports
=============

================ =====
Server           Port
================ =====
``fpga_master``  54321
``raw_acq``      54322
power supplies   54324
GPS              54325
================ =====

.. note::

    ``chime_raw_acq`` and ``pychfpga_raw_acq`` are deprecated aliases of ``raw_acq``.

Containers
==========

To run the servers in Docker, see :doc:`docker`.
