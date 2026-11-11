---------------------
Interactive operation
---------------------

:class:`~pychfpga.fpga_array.FPGAArray` represents any number of boards and crates. It discovers
them, programs the FPGAs and initializes the firmware, then hands control back to you. For
unattended operation, see :doc:`server_operation`.

From Python or Jupyter
======================

``FPGAArray`` is awaitable. In IPython or Jupyter, which allow top-level ``await``:

.. code-block:: python

    from pychfpga.fpga_array import FPGAArray

    ca = await FPGAArray("crs 77:1 123:2", mode="corr8")
    ca.ib          # collection of all boards in the array

Operations on ``ca.ib`` are applied to every board.

From the command line
=====================

``fpga_array.py`` can be run as a script in IPython. It creates the array and leaves these
variables in the session: ``ca`` (the array), ``mb`` (``ca.ib``), ``cr`` (``ca.ic``, the crates)
and ``ps`` (power supplies, if any).

.. code-block:: bash

    ipython
    run -i pychfpga/fpga_array.py <hardware map> --mode <mode> [options]

Hardware map
============

The hardware map lists the boards and crates to operate. It is a space-separated string:

- ``<board part number> <serial> [<serial> ...]``: boards by serial number
- ``<crate part number> <serial>[:<crate number>] ...``: all boards in the given crates
- an IP address or hostname

Part numbers match case-insensitively on their ending, so ``mb`` means MGK7MB, ``bp16`` means
MGK7BP16 and ``crs`` means CRS. Boards given by serial number are found by discovery (mDNS for
IceBoard, beacons for CRS).

Examples: ``"crs 247 224"``, ``"mb 123"``, ``"bp16 12:0 13:1"``, ``"10.10.3.7"``.

How the map is built, kept consistent, and completed by discovery is described
in :doc:`../development/principles`.

Common options
==============

``--mode``
    Operational mode. It selects the bitstream and the initialization. See
    :doc:`../introduction` for the modes of each platform.

``--prog``
    FPGA programming: ``0`` never, ``1`` only if not already programmed with the same
    firmware, ``2`` always (default when ``--mode`` is given).

``--init``
    Initialization level: ``0`` connect to the platform only, ``1`` also program the FPGA,
    ``2`` also connect to the firmware, ``3`` also initialize the mode (default when
    ``--mode`` is given).

``--sync_method``
    ``distributed_time``, ``centralized_time_trigger``, ``centralized_soft_trigger`` or
    ``local_soft_trigger``.

``--ignore_missing_boards``
    Do not fail if some boards are not found.

``-y <file.yaml> <section>``
    Load array parameters from a YAML configuration (``fpga_array_params``). Command-line
    options override it.

Run ``python pychfpga/fpga_array.py --help`` for the full list.

Examples
========

Single IceBoard by IP address, on-board correlator:

.. code-block:: bash

    run -i pychfpga/fpga_array.py 10.10.10.7 --mode corr16 --sync_method local_soft_trigger

Two IceBoard crates (serials 12 and 13 become crate numbers 0 and 1), 512 inputs corner-turned
to an external X-engine, synchronized with IRIG-B:

.. code-block:: bash

    run -i pychfpga/fpga_array.py bp16 12:0 13:1 --mode shuffle512 --sync_method distributed_time

Two CRS boards, corner-turn with 100G offload, from Python:

.. code-block:: python

    ca = await FPGAArray("crs 247 224", mode="shuffle8")

One CRS board, on-board correlator:

.. code-block:: python

    ca = await FPGAArray("crs 77", mode="corr8")
