.. _power_supplies:

--------------
Power supplies
--------------

The power supply server controls an array of Agilent/Keysight N5700 and N8700 supplies over
Ethernet. It is driven over HTTP/REST by :class:`~pychfpga.ps.PowerSupplyAsyncRESTClient`, which
exposes the server endpoints as methods.

Command-line interface
======================

``pychfpga_ps`` uses the command line shared by all servers (see
:doc:`../../user_guide/server_operation`). The configuration is read from the
``power_supplies.servers`` section. Default port: 54324.

.. code-block:: bash

    pychfpga_ps config.yaml:my_site            # start or connect, initialize, run
    pychfpga_ps config.yaml:my_site pss0       # same, selecting one of several servers
    pychfpga_ps status                         # status of the server on localhost:54324
    pychfpga_ps config.yaml:my_site power_off p1

Client commands: ``status``, ``list_names``, ``power_on [names]``, ``power_off [names]``,
``power_cycle [names]``, ``is_enabled``, ``is_ready``, ``get_metrics``, ``stop``.

Configuration
=============

.. code-block:: yaml

    my_site:
        power_supplies:
            servers:
                pss0:
                    hostname: localhost   # used by the client only
                    port: 54324
                    units:                # supplies operated by this server
                        p1: {type: AgilentN5764, hostname: 10.0.0.30, timeout: 2, power_up_delay: 30}
                        p2: {type: AgilentN5764, hostname: 10.0.0.31, timeout: 2, power_up_delay: 30}
                    aliases:
                        crates: [p1, p2]

Setting up a new supply
=======================

A new supply must be given a default voltage and current limit that persist after a power failure.
The server never sets these: a configuration error could destroy the equipment. Set them on the
front panel, or from Python with :class:`~pychfpga.ps.AgilentN5700` (handles the N5700 and N8700
series).

.. warning:: Make sure you connect to the right power supply.

.. code-block:: python

    from pychfpga.ps import AgilentN5700

    ps = AgilentN5700('10.0.0.40')
    ps.unlock()    # software protection of this object only; the instrument is not affected
    ps.configure_power_on_state(voltage=6.2, current=360)

``configure_power_on_state`` disables the output, sets the voltage and current limit, and makes
them the power-on state. The output stays disabled. Then:

.. code-block:: python

    ps.get_voltage_setting()
    ps.get_current_limit()
    ps.power_on()
    ps.status()     # output voltage and current drawn by the load
    ps.power_off()

Notes
=====

- Each :class:`~pychfpga.ps.AgilentN5700` connects to its supply only when needed, and reconnects
  after a failure, so the server runs even if supplies or the network come and go.
- The supplies do not handle several simultaneous TCP connections well (broken pipes and other
  errors). The object opens and closes the connection for each group of commands; make sure only
  one server or interactive session talks to a supply.
