.. _weather:

:mod:`weather` module: Weather data scraper/server and client
=============================================================

The weather server is a Python web server that scrapes weather data from the SQLLite database of the ``wview`` (http://www.wviewweather.com) weather data aggregator and publishes this data as metrics over HTTP/REST. This is essentially a bridge between ``wview`` and ``Prometheus``.


Command-line interface
**********************

The module ``weather.py`` can be used as a script to start and query the weather server.

The first line the python module ``weather.py`` contains a shebang (``#!``) that allows Linux to automatically recognize it as a python script and run it with the python interpreter. So the script can be invoked as ``./weather.py`` as well as ``python weather.py``, or within ipython, ``run -i weather``.

./weather.py [config] [server_name] [command {args}] {--host hostname} {--port portnumber} {--no-start} {--run | --no-run}

where:

	config:
		reference to a YAML config file element, in the form [[filename]:]name{.name}. Default filename is config.yaml.

		    For example:
				my_config:some.object
				:some.object
				some.object

			The config element shall refer to a `ch_master` config; the script will automatically fetch the 'weather' section within it.

			If a config is specified, it is used to obtain the address and port of the server, unless overriden by the --host and --port options. The config will also be used to initialize the server if no command is specified.


	server_name:
		name of the specific server config to use. A config can supports multiple servers.
		If there is only one server defined in the specified configuration, then ``server_name`` does not have to be specified, and the configuration for this server will be used. Otherwise an error will be raised.

	command:
		name of a client method to be invoked with the following arguments as parameters. The command is
		identified as the first string that corresponds to a method in the power supply client class. Some commands might require that the server be initialized beforehand. Once the command is executed, the script exits.

		If no command is specified, and a new local server was created, the script will continue to run the server continuously until :kbd:`Ctrl-C` is pressed so the server can do its job (provide metrics, respond to client requests etc).

	args:
		any remaining arguments are passed as positional arguments to the power supply client method. The client method is responsible for validating and converting the strings to numeric format if needed.

If there is no server runing at the target address specified in the config file or through the --host and --port options, then a server object will be created locally at the same port. If a config file was specified, the server will be initialized with it unless the --no-start flag is specified. Attempt to initialize an already-initialized server might raise an error.

Examples::

	./weather.py jfc.drao #  Connect to an existing server or start a new server if there is none, and initialize it. If new a local server was created, run continuously until Ctrl-C.

Command-line/ REST commands:
****************************

	- ``start`` (implicit if a configuration is specified)
	- ``stop``
	- ``get_monitoring_data`` (used by Prometheus)

Weather server configuration
****************************

The weather server configuration is a Python dictionary that is typically loaded from the ``config.yaml`` YAML file. It located in the ch_master config under the `weather` key::

	ch_master_config:
		...
        weather:
            servers: # List of all servers
                server1_name:
                    hostname: localhost
                    port: 54326
                    units:
                        wview_database_name: {db_path: wview_database_path}
        ...



Python Module Summary
*********************
.. currentmodule:: weather
.. automodule:: weather

.. autosummary::

	weather.get_wview_metrics
	weather.WeatherAsyncRESTServer
	weather.WeatherAsyncRESTClient

Weather data scraping function
******************************

.. autofunction:: weather.get_wview_metrics


Weather REST Server
*******************

..	autoclass:: weather.WeatherAsyncRESTServer

	.. rubric:: REST Endpoint handlers

   	.. automethod:: start(self, handler, **config)
   	.. automethod:: stop(self, handler)
	.. automethod:: get_monitoring_data(self, handler)

..	.. rubric:: Support methods


Weather REST Client
*******************

.. autoclass:: weather.WeatherAsyncRESTClient
   :members:
   :undoc-members:
..   .. automethod:: PowerSupplyAsyncRESTClient.start

..   .. automethod:: stop



Design
******

`weather` follows the same REST client/server model as the other `ch_acq` modules, with a similar command line interface.

When the `weather.get_monitoring_data!` REST endpoint is accessed, the client calls `weather.get_wview_metrics` on each unit defined in the ``units`` section of the configuration passed with the ``start`` command. The function opens the SQLLite database specified in ``db_path``, and extracts the most recent entry. If new data is available, it is formatted into metrics and sent back.


FAQ, troubleshooting and known issues
*************************************

- weather.py must be run on a machine where the ``wview`` database file can be accessed.
- No metrics are returned if there if the data does not have a  timestamp that is different from the previous one (as opposed to return a metric with the same value)
- Each call to `get_monitoring_data` cause the SQLLite database file to be opened, queried, and closed. There is no rate-limiting, even if we query faster than the data is refreshed.

.. _weather_quick_start:

Weather Quick-start
*******************

See :ref:`weather_quick_start`


