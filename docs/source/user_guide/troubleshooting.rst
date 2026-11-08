---------------
Troubleshooting
---------------

FPGA programming fails ("DONE did not raise")
=============================================

- **Invalid bitstream**: git-lfs did not download the file and left a small text pointer in
  place of the bitstream. Run ``git lfs pull`` and check the file size (several MB).
- **Power supply current limit too low**: programming the FPGA draws extra current. The board,
  or another board on the same supply, reboots. Communication with the board still works, but
  the FPGA is not programmed, which is confusing. Raise the current limit.

A board does not answer
=======================

If a board does not answer pings, it usually did not boot. Check the cabling first, then
power-cycle the board or its crate. ``fpga_array`` and ``fpga_master`` report missing boards with
the identity they know (hostname or IP address, serial number, crate and slot; slot numbers are
zero-based). Pass ``--ignore_missing_boards`` to continue without them.

Lost packets or "FPGA command timeout"
======================================

Check the jumbo frame, UDP buffer and netmask settings in :doc:`networking`. A switch without
jumbo frames, or a netmask that excludes the IceBoard FPGA addresses (``*.*.3.*``), are the most
common causes.

"tag ... will be stripped of its suffix"
========================================

.. code-block:: text

    UserWarning: tag '1.3.3+ice.co' will be stripped of its suffix '+ice.co'

The environment has setuptools-scm 8 or older, which drops the ``+<platform>_<config>`` part of
the version. Upgrade it (``pip install "setuptools-scm>=9"``) rather than silencing the warning.
Old Python 3.8 notebook kernels are a typical source.
