:mod:`kotekan_master` module: GPU node array master controller
==============================================================

.. .. automodule:: kotekan_master

.. _kotekan_master_cli:

Command-line interface
**********************

The module ``kotekan_master.py`` module can can be used as a script to start and operate the X-engine GPU nodes.

The first line this module contains a shebang (``#!/usr/bin/env python``) that allows Linux to automatically recognize it as a python script and run it with the python interpreter when invoked as ``./fpga_master.py``. Alternatively, the script can also be launched from a shell with ``python kotekan_master.py`` or from within ipython with ``run -i kotekan_master``.

In a nutshell, the script operates by creating a ``kotekan_master`` REST client object that provides an interface to the kotekan_master REST server that is specified in the provided configuration. If the server is not found, a local server is created. The server is then initialized with the provided configuration file by sending the `start` REST command along with the configuration file.

If a command and arguments are provided in the command line, the corresponding client method is called with those arguments, and control will be returned to the user. If no command is given, the server will run continuously until interrupted by :kbd:`Ctrl-C`. When the server is running, it can responds to REST requests to get monitoring metrics, get status information, start or stop nodes etc.

The command-line syntax is::

   ./kote_master.py [config] [command {args}] {--host hostname} {--port portnumber} {--no-start} {--run | --no-run}

where:

   ``config``:
      reference to a *fpga_master* configuration element found in the specified YAML file. The command line code will automatically
      The ``config`` field is in the form [[filename]:]name{.name}. Default filename is
      ``config.yaml``. For example::

            my_config:some.object
            :some.object
            some.object

      If a config is specified, it is used to obtain the address and port of the server, unless overriden by the --host and --port options. The config will also be used to initialize the server if no command is specified.


   ``command``:
      Name of a client method to be invoked with the following arguments as parameters. The command is
      identified as the first string that corresponds to a method in the ``kotekan_master`` client class. Some commands might require that the server be initialized beforehand. Once the command is executed, the script exits.

      If no command is specified, and a new local server was created, the script will continue to run the server continuously until :kbd:`Ctrl-C` is pressed so the server can do its job (provide metrics, respond to client requests etc).

   ``args``:
      Any remaining arguments are passed as positional arguments to the power supply client method. The client method is responsible for validating and converting the strings to numeric format if needed.

   ``--no-start``:
      Do not send the start command with the configuration to the server even if a configuration file is provided. Use if you don't want the server to check if it is running the same configuration, or to initialize the server if it is not initialized.

   ``--run , --no-run``:
      ``--run`` forces the server to keep running even if a command was specified. ``--no-run`` forces the script to exit even if no command is provided and a new server was created. Both option cannot be used at the same time. Useful mainly for interactive sessions.


If there is no server runing at the target address specified in the config file or through the --host and --port options, then a server object will be created locally at the same port. If a config file was specified, the server will be initialized with it unless the --no-start flag is specified. Attempt to initialize an already-initialized server might raise an error.

Examples::

   ./kotekan_master.py jfc.erh #  Start and run kotekan_master server continuously with the configuration jfc.erh until Ctrl-C.

Commands:
*********

   - ``start`` (implicit)
   - ``stop``
   - ``status``
   - ...

The configuration file
**********************

A configuration contains **all** the information that is required to set-up the hardware in order to run an experiment. The configuration is saved along with the run results in order to provide traceability of the results.

The configuration is a Python dictionary, which is usually loaded from a YAML file. The YAML configuration file can contains multiple configurations, one of which  is selected and passed th `ChimeMaster` by specifying the path to the desired configuration object.  When not specified explicitely, the default configuration file is ``config.yaml``.

The YAML standard is human readable and editable. The standard allows references to be made internally to other blocks, which allows multiple configuration to be created by only specifying the differences. Once loaded, the references are resolved,a dn the fully resolved dictionary is passed to the server  ``start()`` method.


A configuration describes:
   - Server address and listening port number
   - Correlator name
   - Logging set-up
   - Power supplies server configurations
   - GPS server configurations
   - FPGA F-engine and corner-turn engine configurations
   - GPU Node X-engine controller (kotekan) configuration
   - Raw data server (raw_acq) configurations
   - Correlated data acquisition server (chrx) configurations

The configuration file example is::

    fpga_master_config_name:
            servers: # List all the existing servers (multiple servers could operate different sets of nodes)
                default_server:
                        hostname: localhost
                        port: 54323
                        nodes: # node-specific config
                            'csDg5': {hostname: csDg5, port: 12048}  # define kotekan nodes. The key name is arbitrary.
                        aliases: # Lists names that can be used to access subsets of nodes in some client methods
                            alias1: [csDg5]

                        common_config: # config appended to every-node-specific config
                            <<: *kotekan_shuffle
                other_server:
                  ...


Module documentation
********************




Class summary
-------------

.. autosummary::

   .. kotekan_master.KotekanMaster
   .. kotekan_master.KotekanMasterAsyncRESTServer
   .. kotekan_master.KotekanMasterAsyncRESTClient




Classes
-------

.. .. autoclass:: kotekan_master.KotekanMaster
..    :members:
..    :undoc-members:



.. .. autoclass:: kotekan_master.KotekanMasterAsyncRESTServer
..    :members:
..    :undoc-members:

.. .. autoclass:: kotekan_master.KotekanMasterAsyncRESTClient
..    :members:
..    :undoc-members:



Design
******

Logging
-------

