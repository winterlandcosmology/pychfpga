.. _gps:

:mod:`gps` module: GPS server and client
================================================

The GPS server is a Python web server that receives HTTP/REST commands and control and monitor an array of SpectrumInstruments TM-4/TM-4D GPS receiver units.

The corresponding power supply client is a  Python class that connects to a GPS server and exposes all the server commands as simple methods.


Command-line interface
**********************

The module ``gps.py`` can be used as a script to start and query the GPS server.

The first line the python module ``gps.py`` contains a shebang that allows Linux to automatically recognize it as a python script and run it with the python interpreter. So the script can be invoked as ``./ps.py`` as well as `python ps.py`, or within ipython, `run -i ps`.

./ps.py [config] [server_name] [command {args}] {--host hostname} {--port portnumber} {--no-start} {--run | --no-run}

where:
	config: reference to a YAML config file element, in the form [[filename]:]name{.name}. Default filename is config.yaml.
		    For example:
				my_config:some.object
				:some.object
				some.object

			The config element shall refer to a `ch_master` config; the script will automatically fetch the 'gps' section within it.

			If a config is specified, it is used to obtain the address and port of the server, unless overriden by the --host and --port options. The config will also be used to initialize the server if no command is specified.


	server_name: name of the specific server config to use. A config can supports servers.
		If ``server_name`` is not specified, and there is only one defined, then the configuration for this server will be used. Otherwise an error will be raised.

	command: name of a client method to be invoked with the following arguments as parameters. The command is
		identified as the first string that corresponds to a method in the power supply client class. Some commands might require that the server be initialized beforehand. Once the command is executed, the script exits.

		If no command is specified, and a new local server was created, the script will continue to run the server continuously until :kbd:`Ctrl-C` is pressed so the server can do its job (provide metrics, respond to client requests etc).

	args: any remaining arguments are passed as positional arguments to the power supply client method. The client method is responsible for validating and converting the strings to numeric format if needed.

If there is no server runing at the target address specified in the config file or through the --host and --port options, then a server object will be created locally at the same port. If a config file was specified, the server will be initialized with it unless the --no-start flag is specified. Attempt to initialize an already-initialized server might raise an error.

Examples::

	./gps.py jfc.drao #  Connect to an existing server or start a new server if there is none, and initialize it. If a local server was created, run continuously until Ctrl-C.

Command-line commands:
**********************

	- ``start`` (implicit)
	- ``stop``
	- ``list_names``

GPS server configuration
************************

The GPS server configuration is a Python dictionary that is typically loaded from the ``config.yaml`` YAML file. It located in the ch_master config under the `gps` key::

	ch_master_config:
		...
        gps:
            servers: # List of all servers
                server1_name:
                    hostname: localhost
                    port: 54325
                    units:
                        gps1_name: {hostname: 10.0.0.10, timeout: 5}
                        gps2_name: {hostname: 10.0.0.11, timeout: 5}
        ...



Python Module Summary
*********************
.. .. currentmodule:: gps
.. .. automodule:: gps

.. .. autosummary::

.. 	gps.SpectrumInstrumentsTM4D
.. 	gps.GPSAsyncRESTServer
.. 	gps.GPSAsyncRESTClient

GPS REST Server
***************

..	.. autoclass:: gps.GPSAsyncRESTServer

	.. rubric:: REST Endpoint handlers

   	.. automethod:: start(self, handler, **config)
   	.. automethod:: stop(self, handler)
	.. automethod:: monitoringMetrics(self, handler)

	.. rubric:: Support methods


GPS REST Client
***************

.. .. autoclass:: gps.GPSAsyncRESTClient
..    :members:
..    :undoc-members:
.. ..   .. automethod:: PowerSupplyAsyncRESTClient.start

.. ..   .. automethod:: stop

GPS interface object
********************

.. .. autoclass:: gps.SpectrumInstrumentsTM4D
..    :members:
..    :undoc-members:


Design
******

`gps` follows the same REST client/server model as the other modules, with a similar command line interface.

The client instantiates a collection of `SpectrumInstrumentsTM4D` objects, which is a front end to access each GPS unit over a TCP socket. This asumes that the GPS unit is connected to a RS232-to-Ethernet converter (such as Startech NETRS232) , and that the RS232 stream is accessible through a simple TCP socket.

The `SpectrumInstrumentsTM4D` uses the `SocketContext` class as a base, which allows it to differ socket connection to the moment where it is needed, and which will attempt to reopen a broken connection if needed. This allows the server to be initialized and run even if units (or the networking) randomly go online and offline, which is sometimes the case during comissioning, testing, or when a container shuts down as a precautionary measure.

In the case of the `SpectrumInstrumentsTM4D` object, `SocketContext` will try to keep the TCP socket open all the time because data is lost then the socket is closed.

The `SpectrumInstrumentsTM4D` and underlying socket access methods are **not** implemented as coroutines. This would probably be very beneficial to the response time if the code that uses it is designed to take advantage of that. Since we have a small number of units, this was not a priority.



FAQ, troubleshooting and known issues
*************************************

- The NETRS232 seems to lose data when the TCP socket to it is closed and re-opened. For this reason, we keep the socket open as much as possible, reconnecting only in case of failure.
-


