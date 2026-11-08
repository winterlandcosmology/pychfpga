.. _gps:

---
GPS
---

The GPS server monitors an array of Spectrum Instruments TM-4/TM-4D GPS receivers and publishes
their status as metrics. It is driven over HTTP/REST by
:class:`~pychfpga.gps.GPSAsyncRESTClient`.

The receivers are reached through an RS232-to-Ethernet converter (e.g. Startech NETRS232) that
exposes the serial stream on a TCP port.

Command-line interface
======================

``pychfpga_gps`` uses the command line shared by all servers (see
:doc:`../../user_guide/server_operation`). The configuration is read from the ``gps.servers``
section. Default port: 54325.

.. code-block:: bash

    pychfpga_gps config.yaml:my_site           # start or connect, initialize, run

Client commands: ``list_names``, ``stop``.

Configuration
=============

.. code-block:: yaml

    my_site:
        gps:
            servers:
                gps_server:
                    hostname: localhost
                    port: 54325
                    units:
                        gps1: {hostname: 10.0.0.10, timeout: 5}
                        gps2: {hostname: 10.0.0.11, timeout: 5}

Using a receiver directly
=========================

.. code-block:: python

    from pychfpga.gps import SpectrumInstrumentsTM4D

    g = SpectrumInstrumentsTM4D('10.0.0.10')
    g.set_polling_mode(1)    # stop the continuous broadcast
    g.get_date_time()
    g.configure_gps(location=None, lat=49.3207, lon=-119.6233, alt=562.0)

``configure_gps`` puts the receiver in static (time-only) mode at the given position. Predefined
positions can be selected by name with ``location`` (see the method docstring).

To survey the position (average 10000 fixes, then return to time-only static mode):

.. code-block:: python

    g.set_polling_mode(1)
    g.set_timing_mode(3)
    g.get_timing_mode()

A command may have to be repeated to flush earlier replies from the buffer.

Notes
=====

- :class:`~pychfpga.gps.SpectrumInstrumentsTM4D` keeps its TCP socket open and reconnects only on
  failure: the NETRS232 loses data when the socket is closed and reopened.
