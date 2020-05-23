:mod:`fpga_master` module: CHIME telescope array master controller
==================================================================

.. automodule:: fpga_master

.. _fpga_master_cli:

Command-line interface
**********************

The module ``fpga_master.py`` module can can be used as a script to start and control the fpga_master server which configures and controls all the hardware required to run a specific experiment.

The first line this module contains a shebang (``#!/usr/bin/env python``) that allows Linux to automatically recognize it as a python script and run it with the python interpreter when invoked as ``./fpga_master.py``. Alternatively, the script can also be launched  as ``python fpga_master.py``, or, within ipython, with ``run -i fpga_master``.

In a nutshell, the operation of the script is as follows: the script operates by creating a fpga_master object that connects to the specified fpga_master server. If the server is not started, a local server is created and initialized with the provided configuration file. If a command and arguments are provided, that command is sent to the server through the corresponding client method. If no command is given, the server will run continuously until interrupted by :kbd:`Ctrl-C`. When the server is running, it can responds to REST requests made to its internal web server to get monitoring metrics, start raw data acquisition dumps, switch gains etc.

The command-line syntax is::

   ./fpga_master.py [config] [command {args}] {--host hostname} {--port portnumber} {--no-start} {--run | --no-run}

where:

   ``config``:
      reference to a YAML config file element, in the form [[filename]:]name{.name}. Default filename is
      config.yaml. For example::

            my_config:some.object
            :some.object
            some.object

      If a config is specified, it is used to obtain the address and port of the server, unless overriden by the --host and --port options. The config will also be used to initialize the server if no command is specified.


   ``command``:
      Name of a client method to be invoked with the following arguments as parameters. The command is
      identified as the first string that corresponds to a method in the fpga_master client class. Some commands might require that the server be initialized beforehand. Once the command is executed, the script exits.

      If no command is specified, and a new local server was created, the script will continue to run the server continuously until :kbd:`Ctrl-C` is pressed so the server can do its job (provide metrics, respond to client requests etc).

   ``args``:
      Any remaining arguments are passed as positional arguments to the power supply client method. The client method is responsible for validating and converting the strings to numeric format if needed.

   ``--no-start``:
      Do not send the start command with the configuration to the server even if a configuration file is provided. Use if you don't want the server to check if it is running the same configuration, or to initialize the server if it is not initialized.

   ``--run , --no-run``:
      ``--run`` forces the server to keep running even if a command was specified. ``--no-run`` forces the script to exit even if no command is provided and a new server was created. Both option cannot be used at the same time. Useful mainly for interactive sessions.


If there is no server runing at the target address specified in the config file or through the --host and --port options, then a server object will be created locally at the same port. If a config file was specified, the server will be initialized with it unless the --no-start flag is specified. Attempt to initialize an already-initialized server might raise an error.

Examples::

   ./fpga_master.py jfc.erh #  Start and run fpga_master server continuously with th econfiguration jfc.erh until Ctrl-C.

Commands:
*********

   - ``start`` (implicit)
   - ``stop``
   - ``status``
   - ...

The configuration file
**********************

A configuration contains **all** the information that is required to set-up the hardware in order to run an experiment. The configuration is saved along with the run results in order to provide traceability of the results.

The configuration is a Python dictionary, which is usually loaded from a YAML file. The YAML configuration file can contains multiple configurations, one of which  is selected and passed to `ChimeMaster` by specifying the path to the desired configuration object.  When not specified explicitely, the default configuration file is ``config.yaml``.

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
        corr_name: 'some_name'# Name of the correlator
        comment:  'some comment' # A comment to be inserted into the header.
        base_path: "~/data/"  # base local folder where to store logs, run data etc.


        # fpga_master REST server address
        hostname: localhost
        port: 54321

        # Global array parameters
        n_input: 256  # Number of receiver elements/inputs (used?).
        n_freq : 1024 # Number of frequency bins (used?).

        enable_gain_switching : false # Enable digital gain switching

        debug: # debugging flags
            allow_empty_fpga_array: True
            # skip_power_supply: True

        logging: # Logging configuration. See logging section.
            ...
        power_supplies: # Power supply server configuration. See power supply section
            ...
        fpga: # FPGA board configuration
            ...
        chrx: # chrx server config
            ...
        raw_acq: # raw_acq server config
            ...
        kotekan: # GPU node config


Module documentation
********************




Class summary
-------------

.. autosummary::

   fpga_master.ChimeMaster
   fpga_master.DummyChimeMaster
   fpga_master.ChimeMasterAsyncRESTServer
   fpga_master.ChimeMasterAsyncRESTClient




Classes
-------

.. autoclass:: fpga_master.ChimeMaster
   :members:
   :undoc-members:


.. autoclass:: fpga_master.DummyChimeMaster
   :members:
   :undoc-members:

.. autoclass:: fpga_master.ChimeMasterAsyncRESTServer
   :members:
   :undoc-members:

.. autoclass:: fpga_master.ChimeMasterAsyncRESTClient
   :members:
   :undoc-members:



Design
******

Logging
-------

We tried to keep an unified logging structure throughout the design of pychfpga modules in order to make it easy to configure where the logs are being sent, and how much logging info is colleected by each destination. The design philosophy is summarized below:


- Logging is done using the standard Python `logging` package.
- The `log` module provides helper function to set-up logging
- Logger objects are created at the class instance level, not at the module level. This makes it easier to programatically control the logger, set proper logger name based on contaxt, and avoid pitfalls caused by the main module initializing the loggers *after* thay are created by the import statements (see https://fangpenlin.com/posts/2012/08/26/good-logging-practice-in-python/)
- We create logger liberally. Creating loggers is cheap.
- We do **not** use the root logger. Loggers are all named at least using the module name (using the global variable ``__name__``, and can optionally be furthermore specialized by class and method name. Note that ``__name__`` includes the package name.  Typical logger names are therefore "pychfpga.fpga_master", "pychfpga.fpga_master.ChimeMaster" or "pychfpga.fpga_master.ChimeMaster.some_method". The Python logger module understands the dot-separated name hierarchy and allows us to configure logging at any point of the hierarchy.
- fpga_master sets the top-level logging on the logger named after the pychfpga package (and not the root logger). This will handle any events generated by any module directly in the pychfpga package (fpga_master, ps, raw_acq etc) or modules in sub-packages such as the pychfpga package (which also respect the same logger naming convention). This also allows pychfpga package to be potentially used as a sub-package while maintaining fine logging control.
- ...
-

.. .. automethod:: chFPGA_controller.__init__(*see below*)
