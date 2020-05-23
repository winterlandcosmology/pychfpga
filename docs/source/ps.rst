.. _power_supplies:

:mod:`ps` module: Power supply server and client
================================================

The power supply server is a Python web server that receives HTTP/REST commands and control an array of Agilent/Keysight N5700/8700 power supplies.

The corresponding power supply client is a  Python class that connects to a server and exposes all the server commands as simple methods.


Command-line interface
**********************

The module ``ps.py`` can be used as a script to start and control a power supply server.

The first line the python module ``ps.py`` contains a shebang that allows Linux to automatically recognize it as a python script and run it with the python interpreter. So the script can be invoked as ``./ps.py`` as well as `python ps.py`, or within ipython, `run -i ps`.

./ps.py [config] [server_name] [command {args}] {--host hostname} {--port portnumber} {--no-start} {--run | --no-run}

where:
	config: reference to a YAML config file element, in the form [[filename]:]name{.name}. Default filename is config.yaml.
		    For example:
				my_config:some.object
				:some.object
				some.object

			The config element shall refer to a `ch_master` config; the script will automatically fetch the 'ps' section within it.

			If a config is specified, it is used to obtain the address and port of the server, unless overriden by the --host and --port options. The config will also be used to initialize the server if no command is specified.


	server_name: name of the specific server config to use. A config can supports multiple power supply servers.
		If server is not specified, and there is only one power supply server defined, then the configuration for this server. Otherwise an error will be raised.

	command: name of a client method to be invoked with the following arguments as parameters. The command is
		identified as the first string that corresponds to a method in the power supply client class. Some commands might require that the server be initialized beforehand. Once the command is executed, the script exits.

		If no command is specified, and a new local server was created, the script will continue to run the server continuously until :kbd:`Ctrl-C` is pressed so the server can do its job (provide metrics, respond to client requests etc).

	args: any remaining arguments are passed as positional arguments to the power supply client method. The client method is responsible for validating and converting the strings to numeric format if needed.

If there is no server runing at the target address specified in the config file or through the --host and --port options, then a server object will be created locally at the same port. If a config file was specified, the server will be initialized with it unless the --no-start flag is specified. Attempt to initialize an already-initialized server might raise an error.

Examples::

	./ps.py jfc.drao #  Connect to an existing server or start a new server if there is none, and initialize it. If a local server was created, run continuously until Ctrl-C.
	./ps.py jfc.drao pss0  # same, buy by specifying a specific power supply server configuration. Needed only if there are multiple servers defined in the config.
	./ps.py status # invked the status command on the server located at the default port on the localhost
	./ps.py jfc.drao status # same, but run the command on the server specified in the only server specified in the config.
	./ps.py power_off ps_crate0 # call the power_off method with one argument, the power supply name

Power Supply commands:
**********************

	- ``power_on`` ps_names | alias_name | all
	- ``power_off`` ps_names | alias_name | all
	- ``status``

Power Supply server configuration
*********************************

The power supply server configuration is a Python dictionary that is typically loaded from the ``config.yaml`` YAML file. It located in the ch_master config under the `power_supplies` key::

	ch_master_config:
		...
        power_supplies:
            servers: # List all the existing servers
            	server1_name:
                    hostname: localhost # hostname of the server (used by the client only)
                    port: 54324 # port on which the server is listening (used by the client and server)
                    units: # List of power supplies operated by this server
                        p1: {type: AgilentN5764, hostname: 10.0.0.30, timeout: 2, power_up_delay: 30}
                        p2: {type: AgilentN5764, hostname: 10.0.0.31, timeout: 2, power_up_delay: 30}
                    aliases: #
                        alias1: [p1, p2]

                server2_name:
                	...

            power_on:  # used by ch_master to know which supplies to turn on and off in this config
                units: [p1, p2]
        ...



Python Module Summary
*********************
.. currentmodule:: ps
.. automodule:: ps

.. autosummary::

	ps.AgilentN5700
	ps.PowerSupplyAsyncRESTServer
	ps.PowerSupplyAsyncRESTClient

Power Supply REST Server
************************

..	autoclass:: ps.PowerSupplyAsyncRESTServer

	.. rubric:: REST Endpoint handlers

   	.. automethod:: start(self, handler, **config)
   	.. automethod:: stop(self, handler)
	.. automethod:: status(self, handler)
	.. automethod:: is_started(self, handler)
	.. automethod:: listNames(self, handler)
	.. automethod:: power_on(self, handler, ps_names=None)
	.. automethod:: power_off(self, handler, ps_names=None)
	.. automethod:: is_enabled(self, handler)
	.. automethod:: is_ready(self, handler)
	.. automethod:: get_metrics(self, handler)
	.. automethod:: monitoringMetrics(self, handler)

	.. rubric:: Support methods


Power Supply REST Client
************************

.. autoclass:: ps.PowerSupplyAsyncRESTClient
   :members:
   :undoc-members:
..   .. automethod:: PowerSupplyAsyncRESTClient.start

..   .. automethod:: stop

Power Supply interface object
*****************************
.. autoclass:: ps.AgilentN5700
	:members:


Design
******

`ps` follows the same REST client/server model as the other modules, with a similar command line interface.

The client instantiates a collection of `AgilentN5700` objects, which is a front end to access one power supply. The `AgilentN5700` maintains its own socket connection to the power supply. It uses the `SocketContext` class as a base, which allows it to differ socket connection to the moment where it is needed, and which will attempt to reopen a broken connection if needed. This allows the server to be initialized and run even if units (or the networking) randomly go online and offline, which is sometimes the case during comissioning, testing, or when a container shuts down as a precautionary measure.

In the case of the `AgilentN5700` object, `SocketContext` will open and close the TCP connection for each series of command grouped under a ``with socket()`` block, since the units do not like multiple simulataneous connections.

The `AgilentN5700` and underlying socket access methods are **not** implemented as coroutines. This would probably be very beneficial to the response time if the gode that uses it is designed to take advantage of that. Since we have a small number of supplies, this was not a priority.



FAQ, troubleshooting and known issues
*************************************

- The N5700/8700 do not seem to handle multiple TCP connections well. Sometimes it works, but sometimes you can establish a conneciton but the Python code fails with a broken pipe or some other errors.

  If you have such problems, make sure that only one server (or an ipython session that has crated a power supply object) is running.


