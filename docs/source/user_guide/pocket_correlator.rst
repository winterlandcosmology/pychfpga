---------------------------
Single-board correlator
---------------------------

A single IceBoard (``corr16``, 16 inputs) or CRS (``corr8``, 8 inputs) can run as a complete
correlator using the on-board X-engine. ``fpga_master`` configures the board and ``raw_acq``
receives, integrates and stores the correlator output.

Prerequisites
=============

- ``pychfpga`` installed and checked out at a release validated for ``ice_corr16`` or
  ``crs_corr8`` (see :doc:`../getting_started/installation` and
  :doc:`../development/workflow`)
- Host network configured (see :doc:`networking`): jumbo frames, UDP buffers, and for the
  IceBoard a /16 or /22 netmask. A wrong netmask shows up as ``FPGA command timeout`` errors.
- IceBoard: the current SD card image (see :doc:`../platforms/ice/arm_firmware`)

Hardware setup
==============

.. figure:: ../images/pocket_corr/example1.jpg
    :width: 75%
    :alt: IceBoard setup
    :align: center

    IceBoard on the bench

.. figure:: ../images/pocket_corr/example2.jpg
    :width: 60%
    :alt: Ethernet wiring
    :align: center

    Ethernet wiring: at least four cables and a gigabit switch

.. figure:: ../images/pocket_corr/example3.jpg
    :width: 60%
    :alt: Ethernet cables on the IceBoard
    :align: center

    Ethernet cables connected to the IceBoard

.. figure:: ../images/pocket_corr/example4.jpg
    :width: 60%
    :alt: IceBoard front panel
    :align: center

    IceBoard front panel

Configuration
=============

Start from an example configuration:

- IceBoard: ``tests/configs/example_corr16.yaml``, section ``EXAMPLE``
- CRS: ``tests/configs/corr8.yaml``, section ``corr8``

Each file starts with a default block (``def``) that the application section extends. Edit only
the application section; keys that are not listed below are best left at their defaults.

``corr_name``, ``comment``
    Free text, used in the names of the data folders.

``data_folder``
    Where the data is written. Subfolders are created automatically.

``hwm``
    Hardware map of the board, e.g. ``'mb 498:1'`` (IceBoard) or ``'CRS 77:1'``. Keep the crate
    number (after the colon) at ``1`` for a single board. An IP address also works.

``fft_shift``
    FFT shift schedule. The default ``1367`` (``0b10101010111``) balances noise and saturation;
    the best value depends on the input signal.

``compute_gains: enable``
    Compute the digital gains at start-up. Recommended unless the input signal level is known
    not to change.

``firmware_integration_period``
    Integration in the FPGA, in frames. A frame is 2048 samples; at 800 MSPS (IceBoard) that is
    2.56 µs, so the default 16384 frames is 41.94 ms. Keep it close to the default.

``software_integration_period``
    Number of firmware integrations summed by ``raw_acq``. 250 × 41.94 ms ≈ 10.5 s.

Running the correlator
======================

In one terminal:

.. code-block:: bash

    raw_acq

In a second terminal:

.. code-block:: bash

    fpga_master tests/configs/example_corr16.yaml:EXAMPLE

Finding and initializing the board takes a minute or so.
