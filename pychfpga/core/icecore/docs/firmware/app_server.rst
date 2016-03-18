Application Server
==================

**This document is a focal point for application-server documentation on the
IceBoard.** For context, consult the :doc:`index` document for firmware.

.. figure:: ../images/banner.jpg
   :align: center

.. contents:: Table of Contents
   :local:

When to Read This Document
--------------------------

This document is useful in a few situations:

#. You want to create a new "IceCore"-based project that will run C code on
   the ARM; or
#. You want to create other front-end software talking to the Tuber
   application server.

Why Use an Embedded Application Server?
---------------------------------------

We are currently flirting with two approaches to connecting Python code with
VHDL:

#. The CHIME approach, in which Python connects with hardware at a very
   low level. On a PC, Python code includes a register map, and essentially
   uses "peek" and "poke" commands to get things done on the board.

#. The Dfmux approach, in which C code (running on the ARM) acts as an
   intermediary between Python and hardware. C code exposes a relatively
   high-level API (think: `set_frequency`, `align_sampling`, `get_timestamp`).
   Python has no knowledge at all of memory maps or hardware details, beyond
   what it can query interactively from the board.

Before describing the "Dfmux approach" in detail, we provide three quick
motivations for what it's good at.

#. It allows the board to host a web interface. To do this, the web interface
   (whether hosted on- or off-board) must have access to the same methods as
   Python does. If the board exports a high-level API, then both Python and
   JavaScript code can use it consistently.

   To pick a counterexample, pre-DAN dfmux firmware used to use a low-level
   interface where units (amplitudes, frequencies, or phases) were converted
   to "machine units" before sending them to the board. We also wanted a web
   interface, which required us to write unit-conversion code in both
   JavaScript and Python. This doubled the opportunity for bugs and opened the
   door to inconsistent behaviour between the two interfaces. It also required
   separate sanity checks in Python ("are the human units sane?") *and* on the
   board ("are the machine units sane?"). See, for example, `this nightmare.
   <http://kingspeak.physics.mcgill.ca/gitweb/?p=petalinux.git;a=blob;f=software/user-apps/www/js/dfmux.js;h=1738f18d2e5779ddb906a23f84d38c90c160d0e7;hb=HEAD>`_

#. It allows multiple programs (e.g. multiple computers) to safely share
   access to hardware.

   For example, many I2C bus accesses require exclusive access to the bus for
   a period of time (e.g. while reconfiguring I2C multiplexers to access a
   chip on a given bus segment.) Without some locking mechanism, two
   simultaneous requests from different computers will scramble these
   accesses, causing incorrect behaviour and possibly leaving hardware in an
   invalid state.

   If the ARM brokers all access to hardware, it can enforce proper locking
   and guarantee that simultaneous accesses are safely serialized. Without the
   ARM, access to hardware must *only* come from a single source.

   For example, access to the timestamp control register is locked `here,
   during set_timestamp_port() calls
   <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/7134aace2bdddc67593e84738c077dbd24372d73/c/src/dfmux_timestamp.c?at=master#cl-30>`_.

#. It keeps Python and VHDL decoupled. If Python has deep
   knowledge of the board's register map, then upgrades to VHDL must happen in
   lockstep with changes to the Python codebase.

   For example, older pywtl code often used version numbering to try and
   manage this process. An on-board script contained a set of ad-hoc version
   numbers. When a new feature was introduced, one of these version numbers
   was incremented, and any new Python support code needed to check it:

   .. code:: python

      if self._remote.get_capability('dfmux.dmfd_sync.version',0) >= 2:
         warn('''
              The sync_demod_clocks_setup method is obsolete using
              up-to-date firmware (released after Jan 4, 2011.)
              DMFD synchronization now takes place using timestamps.
              See the DfMUX_DMFDSynchronization wiki page for
              details:

                      http://kingspeak.physics.mcgill.ca/twiki/bin/view/DigitalFMux/DfMUX_DMFDSynchronization
         ''');
         return      # Short-circuit if the dfmux_clear CGI script is supported

   This approach often leads to subtle, hard-to-test code paths and dead code.
   It was abandoned in favour of placing this kind of logic on-board, where
   the bitstream and C code are always updated in tandem. Invalid calls to the
   board result in errors that are directly and automatically bubbled up into
   Python exceptions.

References
----------

`IceCore Git Repository <https://bitbucket.org/winterlandcosmology/icecore>`_

   **Here's where the "core" code lives.** Different experiments can
   effectively subclass functions in this repository. See below for an example
   of how subclassing works.

`IceCore-Dfmux Git Repository <https://bitbucket.org/winterlandcosmology/icecore-dfmux>`_

   **This is the Dfmux-specific specialization code.** You should refer to
   this as an example of "subclassing" core functionality.

`An Automatic Control Interface for Network-Accessible Embedded Instruments
<http://dl.acm.org/citation.cfm?id=2318840>`_

   This paper describes the Virtex-4 incarnation of this software stack.
   Although we're now running on a (much faster) ARM processor, the design,
   protocol and architecture of the system are largely unchanged.

Introduction
------------

The application stack sits on top of the kernel and root-filesystem builds,
and co-ordinates accesses to their services. It consists of the blue portions
shown in :ref:`ApplicationServer`.

.. _ApplicationServer:
.. figure:: images/userspace_diagram.svg
   :align: center

   Application Server

The different components of the application stack are as follows:

`Fastpath Server <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/tuber_fastpath.c?at=master>`_ (`/usr/sbin/fastpath`)
   The fastpath server connects our C code to the network. It's responsible
   for receiving requests (for web pages and JSON function calls) and
   delegating them to the appropriate destination (see below.)

`IceBoard Shared Library <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/iceboard.c?at=master>`_ (`libiceboard.so`)
   The IceBoard shared library contains all the code associated with IceBoard
   methods (like `get_motherboard_temperature`). You can find out what C files
   are included in this shared library by checking the `build instructions
   <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/mak/libiceboard.mak?at=master>`_

`Experiment-Specific Shared Library <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/HEAD/c/src/dfmux.c?at=master>`_ (e.g. `libdfmux.so`)
   The experiment-specific shared library (if it exists) contains
   code for experiment-specific calls. For example, code for `set_mezzanine_power`
   exists in both the "libiceboard.so" library and the "libdfmux.so" library.
   The IceBoard version handles generic mezzanine power-up, and the
   Dfmux-specific version augments this with knowledge of the power-up
   requiremenets for the MGMEZZ04 mezzanine. You can find out what C files
   are included in this shared library by checking the `build instructions
   <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/HEAD/c/mak/libdfmux.mak?at=master>`_

`Web Interfaces <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/HEAD/www>`_ (`/home/www/*`)
   The application server also behaves as an ordinary web server, retrieving
   web content (HTML, CSS, JavaScript, PDFs, etc) as requested across the
   network. The web interface itself uses a number of third-party libraries:

   * `Bootstrap CSS Framework <http://getbootstrap.com>`_
   * `Angular.js JavaScript Framework <http://angularjs.org>`_
   * `D3.js + Radian (for plots) <http://d3js.org>`_

   The IceBoard-specific portion of the web interface is built around a
   JavaScript "tuber" interface; you can find the source code for it `here
   <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/www/js/tuber.js>`_.

   For more information on developing web content for the IceBoard, consult
   :doc:`web_prototyping.rst`.

Building
--------

.. important:: Experiment-specific code (i.e. the icecore-dfmux repository)
   generally overlays generic code (i.e. the icecore repository). So, where an
   experiment-specific repository exists, you don't need to check out and
   build the generic version. The experiment-specific repository already
   includes and compiles it. The build instructions are identical in both
   cases.

Pre-Requisites
~~~~~~~~~~~~~~

Compiler:
   To compile application-server code, you must first build a compiler. This
   compiler is built during compilation of the :doc:`rootfs`.
Libraries:
   The application server also depends on a number of userspace libraries
   (avahi, jansson, libmicrohttpd) built during compilation of the
   :doc:`rootfs`. You will need to compile a full filesystem before you can
   compile the application-server code here.

Downloading and Compiling
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code:: bash

   $ git clone bitbucket.org:winterlandcosmology/icecore
   $ cd icecore
   icecore$ make

The output products are:

* `icecore/opkg/iceboard-fastpath/iceboard-fastpath.opk`
* `icecore/opkg/iceboard-runtime/iceboard-runtime.opk`
* `icecore/opkg/iceboard-www/iceboard-www.opk`

Tuber Protocol
--------------

The application server exposes C code on the IceBoard to the web using a
simple JSON Remote Procedure Call (RPC) interface. In the following sections,
we describe different types of requests and demonstrate low-level
interactions.

Introduction
~~~~~~~~~~~~

A basic Tuber request is a JSON dictionary, with keys that describe what to
retrieve or do:

.. code:: json

   { "object": "IceBoard", "method": "set_mezzanine_power", "args": [false, 1] }

The web and Python interfaces create these requests and transmit them to the
board over an `HTTP POST <http://en.wikipedia.org/wiki/POST_(HTTP)>`_
operation.  The board's response is also a JSON dictionary, indicating a
return value (if any), and a description of errors that occurred (if any).

To try these method calls without any help/obfuscation from Python or
JavaScript interfaces, you can use the command-line tool `curl
<http://curl.saxx.se>`_:

.. code:: bash

   $ curl -d '{
      "object": "IceBoard",
      "method": "get_backplane_slot",
   }' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : 11
   }

.. note:: Piping `curl`'s output through `json_pp` is optional, but formats
   the resulting JSON object more legibly. The `json_pp` utility is packaged
   with Perl in Debian/Ubuntu.)

This HTTP POST request was directed to the special URL
`http://iceboard004.local/tuber`; the "tuber" suffix tells the application
server to treat this request separately from an ordinary request (for example,
for a web page or an image hosted by the board.)

In Python, this interaction would be written as:

.. code:: python

   >>> import pydfmux
   >>> ib = pydfmux.IceBoard(serial='004')

   >>> ib.get_backplane_slot()
   11

The call succeeded (hence the `null` value for the `error` field in JSON), and
returned an integer (`11`). In general, though, the `error` and `result`
fields can take on arbitrary JSON values.

This example showed a single method call, which is only one of several types
of requests. In the following sections, we describe each type of request,
moving from the simplest to most complex types.

Object Descriptions
~~~~~~~~~~~~~~~~~~~

The Tuber RPC interface is object-oriented, meaning that all "useful"
interactions include an `object` field specifying what piece of code to use.
Front-end software often needs to know general information about an object;
for example, Python builds DocStrings that provide interactive documentation
and tab-completion on objects.

Object requests look like the following:

.. code:: bash

   $ curl -d '{"object": "IceBoard"}' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : {
         "name" : "IceBoard",
         "summary" : "Hardware wrapper for the McGill ICEboard.",
         "explanation" : "",
         "methods" : [
            "_motherboard_eeprom_write_base64",
            "get_motherboard_serial",
            "_get_motherboard_ipmi",
            "get_motherboard_current",
            "get_motherboard_voltage",
            "get_motherboard_temperature",
            "get_backplane_temperature",
            "_get_backplane_serial",
            "_get_backplane_version",
            "_get_backplane_type",
            "_get_backplane_ipmi",
            "_backplane_eeprom_write_base64",
            "get_backplane_slot",
            "is_backplane_present",
            "_initialize_backplane",
            "_get_mezzanine_serial",
            "_get_mezzanine_version",
            "_get_mezzanine_type",
            "_get_mezzanine_ipmi",
            "_mezzanine_eeprom_write_base64",
            "get_mezzanine_current",
            "get_mezzanine_voltage",
            "get_mezzanine_power",
            "set_mezzanine_power",
            "is_mezzanine_present",
            "_get_arm_mac",
            "_get_arm_ip",
            "_fpga_spi_poke",
            "_fpga_spi_peek",
            "_set_fpga_bitstream_base64",
            "clear_fpga_bitstream",
            "is_fpga_programmed",
            "_set_personality",
            "_get_personality",
            "_get_syslog_buffer",
            "_syslog_test",
            "_get_syslog_mask",
            "_set_syslog_mask",
            "reboot"
         ],
         "properties" : [
            "NUM_MEZZANINES",
            "RAIL",
            "TEMPERATURE_SENSOR",
            "UNITS"
         ]
      }
   }

This response shows a description of the object with the following fields:

`name`:
   The name of the object (this was also included with the request.)
`summary`:
   A one-line description of the object.
`explanation`:
   A more detailed, possibly longer description of the object. This
   explanation is combined with the `summary` field when forming Python
   DocStrings for the object.
`methods`:
   A JSON array containing the names of valid method calls on this object.
   Methods "do things", and are described in more detail below.
`properties`:
   A JSON array containing the names of valid properties on this object.
   Properties are just contants, and are described in more detail below.

Method Calls
~~~~~~~~~~~~

A method call uses the following JSON fields:

`object`:
   The object that "owns" or contains this method call.
`method`:
   The name of the method.
`args`:
   If provided, `args` is an array of "positional" arguments to the method
   call.  Positional and keyword arguments (below) are combined according to
   Python's calling rules (see the `Python documents
   <https://docs.python.org/2/glossary.html#term-argument>`_.)
`kwargs`:
   If provided, `kwargs` is a dictionary of "keyword" arguments to the method
   call.  Positional and keyword arguments (below) are combined according to
   Python's calling rules (see the `Python documents
   <https://docs.python.org/2/glossary.html#term-argument>`_.)

The following examples show different, and totally equivalent, ways to encode
a single Python call. We emphasize the mapping between Python and RPC
encodings.

Positional Arguments
^^^^^^^^^^^^^^^^^^^^

Python:

.. code:: python

   >>> ib.set_mezzanine_power(False, 1)
   None

Direct JSON / curl:

.. code:: bash

   $ curl -d '{
         "object": "IceBoard",
         "method": "set_mezzanine_power",
         "args": [false, 1]
      }' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : null
   }

Keyword Arguments
^^^^^^^^^^^^^^^^^

Python:

.. code:: python

   >>> ib.set_mezzanine_power(power=False, mezzanine=1)
   None

Direct JSON / curl:

.. code:: bash

   $ curl -d '{
         "object": "IceBoard",
         "method": "set_mezzanine_power",
         "kwargs": {
            "power":false,
            "mezzanine":1
         }
      }' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : null
   }

Mixed Positional and Keyword Arguments
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Python:

.. code:: python

   >>> ib.set_mezzanine_power(False, mezzanine=1)
   None

Direct JSON / curl:

.. code:: bash

   $ curl -d '{
         "object": "IceBoard",
         "method": "set_mezzanine_power",
         "args": [ false ],
         "kwargs": { "mezzanine":1 }
      }' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : null
   }

Parameter Requests
~~~~~~~~~~~~~~~~~~

Parameters are just like method calls, except they return only constant
values. They are useful for retrieving things like geometries
(`IceBoard.NUM_MEZZANINES`) and string constants (`IceBoard.UNITS.VOLTS`).

.. note:: It might seem like method calls would do the trick here. For
   example, imagine replacing NUM_MEZZANINES with a fictitious
   `IceBoard.get_num_mezzanines()` call. We support parameters as separate
   entities because they allow client software to cache and re-use these
   values instead of going to the board every time, as we do for method calls.
   As a result, parameter fetches are "free", resulting in much higher
   performance for code using them.

   For example, the following code is common in pydfmux:

   .. code:: python

      >>> import pydfmux
      >>> d = pydfmux.Dfmux(serial='004')
      >>> d.set_amplitude(0.01, d.UNITS.NORMALIZED, d.TARGET.CARRIER, 1, 1, 1)
      None

   This method call involves casual use of two properties. Since they're just
   string constants, the following would have worked just as well:

   .. code:: python

      >>> d.set_amplitude(0.01, 'Normalized', 'carrier', 1, 1, 1)

   ...but the use of properties allows tab-completion and produces code that
   is closer to Python best practices.

Parameter requests are like method calls without any arguments. For example:

.. code:: python

   >>> ib.UNITS
   TuberResult(HZ='Hz', WATTS='Watts', ADC_COUNTS='ADC Counts', VOLTS='Volts', RADIANS='Radians', OHMS='Ohms', DEGREES='Degrees', NORMALIZED='Normalized', DAC_COUNTS='DAC Counts', RAW='RAW', AMPS='Amps')

   >>> ib.UNITS.VOLTS
   'Volts'

They are encoded in JSON and returned as follows:

.. code:: bash

   $ curl -d '{
         "object": "IceBoard",
         "property": "UNITS"
      }' http://iceboard004.local/tuber | json_pp
   {
      "error" : null,
      "result" : {
         "DEGREES" : "Degrees",
         "HZ" : "Hz",
         "OHMS" : "Ohms",
         "NORMALIZED" : "Normalized",
         "WATTS" : "Watts",
         "ADC_COUNTS" : "ADC Counts",
         "RADIANS" : "Radians",
         "AMPS" : "Amps",
         "RAW" : "RAW",
         "VOLTS" : "Volts",
         "DAC_COUNTS" : "DAC Counts"
      }
   }

In this example, `ib.UNITS` is a property; `ib.UNITS.VOLTS` is an element
*within* that property. (Nested or "containered" properties are new additions
since pywtl; they're useful because they don't clutter up the object
namespace.)

Method Descriptions
~~~~~~~~~~~~~~~~~~~

Methods, like objects, can be documented in Python via DocStrings. So, it is
useful for the IceBoard to describe methods in addition to calling them.
Method descriptions are returned by treating methods as properties:

.. code:: bash

   $ curl -d '{
         "object": "IceBoard",
         "property": "set_mezzanine_power"
      }' http://iceboard004.local/tuber | json_pp
   {
      "result" : {
         "categories" : [
            "IceBoard",
            "Mezzanine"
         ],
         "explanation" : "Rails are powered in the following order:
            * Vadj,
            * 3.3v,
            * 12v

            Power-off sequencing follows the same process in reverse. FMC
            specifications state (Obs. 5.28) that any power sequencing is
            acceptable; the ICEboard's hardware is capable of accomodating
            arbitrary rail ordering (but software does not play along at the
            moment.)",
         "summary" : "Turn on/off an FMC mezzanine",
         "name" : "set_mezzanine_power",
         "args" : [
            {
               "type" : 133,
               "name" : "power",
               "description" : "True or False"
            },
            {
               "type" : 130,
               "name" : "mezzanine",
               "description" : "Mezzanine number (1/2)"
            }
         ]
      },
      "error" : null
   }

Like object descriptions, the DocStrings are taken from the one-line `summary`
entry and the longer `explanation` entry. Each argument is described by `name`
and `description`. (The argument `type` entry is not currently used;
typechecking occurs on the board itself.)

Errors
~~~~~~

So far, we've focused on "correct" function calls that succeed. The Tuber
application server also includes error handling code that automatically
translates errors in the C runtime into errors suitable for Python or
JavaScript code.

For example, consider the following (incorrect, nonsensical) Python call:

.. code:: python

   >>> ib.get_motherboard_temperature(3)
   TuberRemoteError: Argument sensor was unspecified or wasn't the expected type!

.. ' # fix syntax highlighting with dangling quote

The `get_motherboard_temperature` call expects a string constant (as shown
above); a numerical argument makes no sense. The application server returns an
error, which the Python stack converts into an Exception.

To see how this error manifests itself in the call's returned JSON, run:

.. code:: bash

   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": [3]}' http://iceboard004.local/tuber | json_pp
   {
      "error" : {
         "message" : "Argument sensor was unspecified or wasn't the expected type!",
         "source" : "../../src/iceboard_hk.c"
      },
      "result" : null
   }

In this case, `error` (which is usually `null`) is a dictionary containing an
error message and the source file in which the error occurred.

.. todo:: Currently, all errors are translated into TuberRemoteError
   instances. It would be great if C could specify different types of errors;
   for example, parameter-checking errors could be translated into standard
   Python `TypeError` or `ValueError`. Allowing standard exceptions to be
   emitted by C code would probably improve the quality of upstream Python
   code.

Common errors (invalid argument types, missing arguments, et cetera) are
automatically checked by the C runtime and will not be visible from glancing
at the C code itself. To see where these errors are checked, you'll need to
look through `the headers that define the tuber_method macro
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-229>`_

Many errors are also checked as part of user code. For example, the string
constant specifying a particular motherboard temperature sensor is checked
`here
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/iceboard_hk.c?at=master#cl-73>`_.

Bulk Requests
~~~~~~~~~~~~~

Until now, we have focused on single requests formated as a dictionary.
However, each such request represents a complete HTTP interaction.

Imagine a script wants a list of temperatures from motherboard sensors:

.. code:: python

   >>> temps = []
   >>> for ts in (ib.TEMPERATURE_SENSOR.MB_ARM,
   ...            ib.TEMPERATURE_SENSOR.MB_FPGA,
   ...            ib.TEMPERATURE_SENSOR.MB_FPGA_DIE,
   ...            ib.TEMPERATURE_SENSOR.MB_POWER,
   ...            ib.TEMPERATURE_SENSOR.MB_PHY):
   ...   temps.append(ib.get_motherboard_temperature(ts))
   >>> print temps
   [41.0, 30.5, 57.81429519653324, 31.0, 39.5]

This code involves 5 method calls in sequence. Coding it naively (like above)
results in 5 round-trips on 5 separate HTTP sessions. Doing this repeatedly is
unnecessarily hard on the board and slower than it could be.

On the command line, these 5 interactions are replicated in 5 separate
commands:

.. code:: bash

   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_ARM"]}' http://iceboard004.local/tuber
   {"error": null, "result": 41.0}
   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_FPGA"]}' http://iceboard004.local/tuber
   {"error": null, "result": 30.5}
   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_FPGA_DIE"]}' http://iceboard004.local/tuber
   {"error": null, "result": 57.81429519653324}
   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_POWER"]}' http://iceboard004.local/tuber
   {"error": null, "result": 31.0}
   $ curl -d '{"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_PHY"]}' http://iceboard004.local/tuber
   {"error": null, "result": 39.5}

We provide a method for making bulk calls in a single HTTP POST transaction.
Using `curl`:

.. code:: bash

   $ curl -d '[
      {"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_ARM"]},
      {"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_FPGA"]},
      {"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_FPGA_DIE"]},
      {"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_POWER"]},
      {"object": "IceBoard", "method": "get_motherboard_temperature", "args": ["MOTHERBOARD_TEMPERATURE_PHY"]}
   ]' http://iceboard004.local/tuber | json_pp
   [
      {
         "error" : null,
         "result" : 41.0
      },
      {
         "error" : null,
         "result" : 30.5
      },
      {
         "error" : null,
         "result" : 57.6604942321778
      },
      {
         "error" : null,
         "result" : 31.0
      },
      {
         "error" : null,
         "result" : 39.5
      }
   ]

The HTTP POST operation contains an array of dictionaries, instead of a single
dictionary. Each dictionary in the array is a stand-alone request. These calls
are executed in order, one-by-one.

In Python, these requests could be bundled as follows:

.. code:: python

   >>> temps = []
   >>> with ib.tuber_context() as ctx:
   ...    for ts in (ib.TEMPERATURE_SENSOR.MB_ARM,
   ...               ib.TEMPERATURE_SENSOR.MB_FPGA,
   ...               ib.TEMPERATURE_SENSOR.MB_FPGA_DIE,
   ...               ib.TEMPERATURE_SENSOR.MB_POWER,
   ...               ib.TEMPERATURE_SENSOR.MB_PHY):
   ...       temps.append(ctx.get_motherboard_temperature(ts))
   >>> print [ t.result() for t in temps ]
   [41.0, 30.5, 57.81429519653324, 31.0, 39.5]

.. note:: The Python interface for multiple calls is powerful, but a little
   more subtle than we've seen so far. It's a topic treated separately in
   software documentation; see :doc:`/software/index` for details.

C Code
------

The application server itself consists of a stable of C code. It is not
compiled into a single binary; rather, it exists as a program (`fastpath`) and
a number of shared-object libraries (`.so` files; `.dll` files in Windows
terminology.) In addition, much of the magic is buried in the `tuber.h` header
file.

In the following sections, we describe each of these components.

References
~~~~~~~~~~

`icecore/c/src/iceboard.c <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/iceboard.c?at=master#cl-13>`_
   Definition and constructor for the IceBoard object using Tuber macros.

`icecore/c/include/iceboard.h <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/iceboard.h?at=master#cl-24>`_
   Declaration of the actual IceBoard structure in memory.

`icecore-dfmux/c/src/dfmux.c <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/HEAD/c/src/dfmux.c?at=master#cl-16>`_
   Definition and constructor for the Dfmux object using Tuber macros.

`icecore-dfmux/c/include/dfmux.h <https://bitbucket.org/winterlandcosmology/icecore-dfmux/src/HEAD/c/include/dfmux.h?at=master#cl-68>`_
   Declaration of the actual Dfmux structure in memory.

`icecore/c/include/tuber.h <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-92>`_
   Tuber macros are defined here.

`Jansson Documentation <http://jansson.readthedocs.org/en/2.7/>`_
   Jansson is our humble JSON library. This library is heavily referenced in
   the Tuber headers and the fastpath source code. It is also occasionally
   used in object code, where direct access to arbitrary JSON structures is
   useful.

Introduction
~~~~~~~~~~~~

ANSI C is not object-oriented. However, object-oriented idioms in C are
actually quite common:

* `Object-Oriented Programming in C
  <http://www.cs.rit.edu/~ats/books/ooc.pdf>`_, a textbook on the subject;
* `GLib <https://developers.gnome.org/glib>`_, which is used in many
  open-source projects including the GNOME desktop environment; and
* `The Linux kernel <http://lwn.net/Articles/444910/>`_ gets in on the act,
  too.

To do object-oriented stuff in C, we use the `cpp, the C preprocessor
<https://gcc.gnu.org/onlinedocs/cpp/>`_. Think of `cpp` as a translator that
turns C code *with instructions about how to read it* into another set of C
code.

Our library code is idiomatic. It's absolutely valid to be hesitant about
abusing the C preprocessor like we do; it's also important to consider how
much better (more maintainable, shorter, easier to write, and correct-er) this
approach is than the C code it replaced. So -- I know, and I'm sorry. The code
is odd, but it really works well.

Why not C++? To expose object-oriented code to the network, we need to be able
to do *introspection* -- that is, to ask a class what members and properties
it has, and to provide this data to Python client code. This is not possible
in "plain" C++ any more than "plain" C
(http://stackoverflow.com/questions/41453/how-can-i-add-reflection-to-a-c-application).
In this C implementation, we use shared-library techniques (`dlopen`). In C++,
even this avenue is complicated due to `name mangling
<http://stackoverflow.com/questions/13886887/shared-library-symbol-names>`_.

Library Code
~~~~~~~~~~~~

We begin by describing library code, since it's the most relevant part of the
system, and because it anchors later discussion about the rest of the system.

The runtime is object-oriented, meaning it's built around objects (`IceBoard`,
`Dfmux`) that own or contain methods (`set_mezzanine_power`) and properties
(`NUM_MEZZANINES`).

Declarations
^^^^^^^^^^^^

Objects are declared using the `tuber_object or tuber_object_inherits
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-92>`_
macros:

.. code:: c

   tuber_object(IceBoard,
         "Hardware wrapper for the McGill ICEboard.",
         "");

These macros simply create a structure (of type \*tuber_object_entry_t) in the
shared library that describes the object (including DocString content, and
placeholders for its methods and properties.)

.. tip:: If you look at the preprocessor output, this macro generates C code
   that looks something like:

   .. code:: c

      tuber_object_entry_t __tuber_object_IceBoard = {
         .name = "IceBoard",
         .instance = NULL,
         .parent = NULL,
         .property_count = 0,
         .method_count = 0,
         .summary = "Hardware wrapper for the McGill ICEboard.",
         .explanation = "",
      };

   This structure is largely a placeholder containing DocString details; other
   macros (defining properties and methods, for example) fill it in.

.. note:: Inheritance allows objects to be specialized. For example, the Dfmux
   object provides all the same functions as an IceBoard and many more
   besides. This simplifies our experiment-specific Python code, by allowing a
   Dfmux object reference to be used for both IceBoard and Dfmux tasks. Users
   don't know they're mixing code from the two libraries.

   .. warning:: Inheritance means that C code written for the Dfmux object
      often makes use of C code written for the IceBoard object. When you
      change the structure of the IceBoard object, you *must* recompile the
      Dfmux code too. Otherwise, it will be using an old, and incorrect,
      memory layout.

Constructors
^^^^^^^^^^^^

Objects have constructors, also defined by macros:

.. code:: c

   tuber_constructor(IceBoard,
         "Create a new ICEboard object.",
         "") {

      IceBoard *self = NULL;

      if(!((self = calloc(1, sizeof(*self)))))
         fatal("Out of memory!");

      /* ... */

      return(self);
   }

.. vim_unstuff*

The constructor allocates space for an IceBoard object, and affixes it to the
structure created in the `tuber_object` macro above. Objects are `singletons
<http://en.wikipedia.org/wiki/Singleton_Pattern>`_, meaning there is only one
"live" IceBoard object on the system, and its constructor is only run once.

.. tip:: If you look at the preprocessor output for this macro, you'll see
   something like the following:

   .. code:: c

      IceBoard *new_IceBoard(void) {
         return (IceBoard *) __tuber_object_IceBoard.instance;
      }

      __attribute__ ((constructor(1000)))
      static void
      __dlctor_new_IceBoard(void)
      {
         tuber_object_entry_t *self = &__tuber_object_IceBoard;
         if (!(self->instance = __actual_new_IceBoard()))
            fatal_call("../../src/iceboard.c"
                     ":" "19" " " "Error during " "IceBoard"
                  " constructor call!" "\n");
         memset(&self->method_htab, 0, sizeof(self->method_htab));
         if (hcreate_r(512 * 2, &self->method_htab) == 0)
            fatal_call("../../src/iceboard.c"
                  ":" "19" " " "Error creating method hash!" "\n");
         do {
            (&self->method_list)->lh_first = NULL;
         } while (0);
         memset(&self->property_htab, 0, sizeof(self->property_htab));
         if (hcreate_r(512 * 2, &self->property_htab) == 0)
            fatal_call("../../src/iceboard.c"
                  ":" "19" " " "Error creating property hash!" "\n");
         do {
            (&self->property_list)->lh_first = NULL;
         } while (0);
      }

      static IceBoard *__actual_new_IceBoard(void) {

         IceBoard *self = NULL;
         int n;
         const struct iceboard_gpio *gpio;

         if (!((self = calloc(1, sizeof(*self)))))
            fatal_call("../../src/iceboard.c" ":" "26" " " "Out of memory!"
                  "\n");

         /* ... */

         return (self);
      }

   The first (boiilerplate) function is executed by the C runtime,
   automatically, and adds the method to the structure defined above in
   `tuber_object`. The second function is the one that's actually visible in
   user code.

   Constructors and destructors are executed using the `constructor
   and destructor attributes
   <https://gcc.gnu.org/onlinedocs/gcc/Function-Attributes.html>`_ provided by
   `gcc`. This means you won't actually see them executed anywhere in the
   code; it's handled automatically by the shared-library loader.

.. note:: The singleton pattern means, for example, that we couldn't add
   Mezzanine objects to the C runtime since there are two of them. Adding
   multiple references is an interesting idea, but it's not motivated by much
   at the moment.

Destructors
^^^^^^^^^^^

To go with the constructor is a destructor macro:

.. code:: c

   tuber_destructor(IceBoard,
         "Clean up after an ICEboard reference.",
         "") {

      /* ... */

      free(self);
   }

.. vim_unstuff*

The destructor macro is rarely run, since the IceBoard instance is created
when the fastpath starts running and is only destroyed if the fastpath notices
it's being upgraded "live". I consider destructors nearly-dead code at the
moment.

.. tip:: If you look at the preprocessor output for this macro, you'll see
   something like the following:

   .. code:: c

      __attribute__ ((destructor(2000)))
      static void
      __dldtor_delete1_IceBoard(void)
      {
         tuber_object_entry_t *self = &__tuber_object_IceBoard;
         tuber_property_entry_t *prop;
         hdestroy_r(&self->method_htab);
         hdestroy_r(&self->property_htab);
         for (prop = self->property_list.lh_first; prop;
              prop = prop->property_list_entry.le_next)
            json_decref(prop->value);
         self->property_count = 0;
         self->method_count = 0;
      }

      __actual_delete_IceBoard(IceBoard * self) {

         /* ... */

         free(self);
      }

   Here, too, the `tuber_destructor` macro actually emits two functions. The
   first (__dldtor_delete1_iceboard) is automatically emitted (and
   unreadable); the second is recognizable user code.

Methods
^^^^^^^

With a declaration, a constructor, and a destructor, we can define a skeleton
object for Tuber calls. We now investigate how to affix methods to this object
so it can be useful.

Methods use the `tuber_method
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-229>`_
macro. For example:

.. code:: c

   tuber_method(VOID, IceBoard, reboot,
         "Reboots an iceboard.",
         0, (),
         1, (CATEGORY_ICEBOARD),
         "Reboots the dfmux."
   ) {
      if(!fork())
         execv("/sbin/reboot", (char *[]){"/sbin/reboot",});
   }

.. vim_unstuff*

This method is nearly trivial, and issues the `/sbin/reboot` command on the
board. (We avoid this kind of shell call in general, since it's messy C code
-- but this should be the last thing the board sees before a clean reboot,
so we grudgingly accept it here.)

The `tuber_method` macro takes a number of arguments. In order:

* A return type (VOID, STRING, SIGNED_INTEGER, UNSIGNED_INTEGER, INTEGER,
  DOUBLE, BOOLEAN, STRING_CONST, or JSON) that determines how to encode the
  function's response as a JSON structure (VOID in this example);
* A one-line description of the function, used to build Python DocStrings;
* The object this method applies to (IceBoard);
* The method name (reboot),
* The number of arguments, followed by a tuple of argument specifiers;
* The number of categories, followed by a tuple of categories; and
* A longer description of the function.

.. note:: Units are represented via CAPITALIZED_NAMES, which are actually
   just numerical constants, since the C preprocessor isn't smart to do logic
   on its own types. It would be greatly preferrable to just use C types like
   "int", "char \*", or "json_t", but cpp isn't up to the job. Under the hood,
   tuber.h translates these constants into the regular C types you expect.

This macro is followed by ordinary C code that performs the method.

Our `reboot` example didn't take any arguments. Here is a more complex example
(taken from `iceboard_mezz.c`) that shows how arguments are specified:

.. code:: c

   tuber_method(VOID, IceBoard, set_mezzanine_power,
         "Turn on/off an FMC mezzanine",
         2, (
            (BOOLEAN, power, NULL, "True or False"),
            (INTEGER, mezzanine, NULL, "Mezzanine number (1/2)")
         ),
         2, (CATEGORY_ICEBOARD, CATEGORY_MEZZANINE),
         "Long-form description goes here."
   ) {
      /* C code goes here */
   }

.. vim_unstuff*/

This example takes 2 arguments: `power` and `integer`, specifying a mezzanine
and whether to turn it on or off.

Each of these two arguments is described in a tuple, with its type, name,
default value, and a string description (used for DocStrings) attached.

The `category` tuple is also shown here. Categories are used by the Python
front-end to export Tuber methods to other objects in the Object-Relational
Mapping (ORM); please see the `@TuberCategory decorator in tuber.py
<https://bitbucket.org/winterlandcosmology/pydfmux/src/HEAD/core/tuber.py?at=master#cl-237>`_
for details.

.. tip:: If you look at the preprocessor output for this macro, you'll see
   a number of definitions.

   First, we see the `__tuber_json_method_IceBoard__reboot` function. This
   function is responsible for translating between JSON and C -- first, when
   arguments are converted from a JSON object to suitable C arguments, and
   second, when the output of the function is packed back into a JSON
   structure.

   .. code:: c

      void IceBoard_reboot(IceBoard * self);
      __attribute__ ((visibility("hidden")))
      void __tuber_json_method_IceBoard__reboot(
            void *self,
            json_t * json_args,
            json_t * json_kwargs,
            json_t ** json_results,
            json_t ** json_err)
      {
         char *__error;
         json_t *__tidy_stack = NULL;
         int num_used_kwargs = 0;
         *json_results = *json_err = json_null();
         if (json_args && !(json_args && ((json_args)->type) == JSON_ARRAY)) {
            *json_err = ( {
                    char *__json_err_buf = malloc(256);
                    json_t * __json_err_ret;
                    snprintf(__json_err_buf, 256,
                        "Invalid args provided to " "reboot" "!");
                    __json_err_ret =
                    json_pack("{s:s,s:s}", "source",
                         "../../src/iceboard.c", "message",
                         __json_err_buf);
                    free(__json_err_buf); __json_err_ret;});
            return;
         }
         if (json_kwargs
             && !(json_kwargs && ((json_kwargs)->type) == JSON_OBJECT)) {
            *json_err = ( {
                    char *__json_err_buf = malloc(256);
                    json_t * __json_err_ret;
                    snprintf(__json_err_buf, 256,
                        "Invalid kwargs provided to " "reboot"
                        "!");
                    __json_err_ret =
                    json_pack("{s:s,s:s}", "source",
                         "../../src/iceboard.c", "message",
                         __json_err_buf);
                    free(__json_err_buf); __json_err_ret;});
            return;
         }
         if (json_args && json_array_size(json_args) > 0) {
            *json_err = ( {
                    char *__json_err_buf = malloc(256);
                    json_t * __json_err_ret;
                    snprintf(__json_err_buf, 256,
                        "Too many positional arguments provided to "
                        "reboot" "! Expected ( " " )");
                    __json_err_ret =
                    json_pack("{s:s,s:s}", "source",
                         "../../src/iceboard.c", "message",
                         __json_err_buf);
                    free(__json_err_buf); __json_err_ret;});
            return;
         }
         if (json_args) {
         }
         if (json_kwargs) {
         }
         if (num_used_kwargs < json_object_size(json_kwargs)) {
            *json_err = ( {
                    char *__json_err_buf = malloc(256);
                    json_t * __json_err_ret;
                    snprintf(__json_err_buf, 256,
                        "Extra associative arguments provided to "
                        "reboot" "! Expected ( " " )");
                    __json_err_ret =
                    json_pack("{s:s,s:s}", "source",
                         "../../src/iceboard.c", "message",
                         __json_err_buf);
                    free(__json_err_buf); __json_err_ret;});
            return;
         }
         tuber_error_clear();
         IceBoard_reboot(self);
         if ((__error = tuber_error_fetch())) {
            *json_results = json_null();
            *json_err = ( {
                    char *__json_err_buf = malloc(256);
                    json_t * __json_err_ret;
                    snprintf(__json_err_buf, 256, "%s", __error);
                    __json_err_ret =
                    json_pack("{s:s,s:s}", "source",
                         "../../src/iceboard.c", "message",
                         __json_err_buf);
                    free(__json_err_buf); __json_err_ret;});
            json_decref(__tidy_stack);
            return;
         }
         *json_err = json_null();
         *json_results = json_null();
         json_decref(__tidy_stack);
      }

   Next, we see the `__tuber_method_IceBoard__reboot` object definition.

   .. code:: c

      tuber_method_entry_t __tuber_method_IceBoard__reboot = {
         .method = __tuber_json_method_IceBoard__reboot,
         .name = "reboot",
         .summary = "Reboots an iceboard.",
         .explanation = "Reboots the dfmux.",
         .num_args = 0,
         .categories = NULL,
         .args = {},
      };

      __attribute__ ((constructor(2000)))
      static void __dlctor_register_IceBoard__reboot(void)
      {
         extern tuber_object_entry_t __tuber_object_IceBoard;
         tuber_object_entry_t *self = &__tuber_object_IceBoard;
         tuber_method_entry_t *meth = &__tuber_method_IceBoard__reboot;
         ENTRY hent = {.key = "reboot",.data = meth }, *hentp;
         if (++self->method_count >= 512)
            fatal_call("../../src/iceboard.c" ":" "105" " "
                  "Method overflow! Increase TUBER_MAX_NUM_METHODS."
                  "\n");
         do {
            if (((meth)->method_list_entry.le_next =
                 (&self->method_list)->lh_first) != NULL)
               (&self->method_list)->lh_first->
                   method_list_entry.le_prev =
                   &(meth)->method_list_entry.le_next;
            (&self->method_list)->lh_first = (meth);
            (meth)->method_list_entry.le_prev =
                &(&self->method_list)->lh_first;
         } while (0);
         if (!hsearch_r(hent, ENTER, &hentp, &self->method_htab))
            fatal_call("../../src/iceboard.c" ":" "105" " "
                  "Error initializing object hashtable!" "\n");
         meth->categories = json_pack("[" "s" "]", CATEGORY_ICEBOARD);
      }

      void IceBoard_reboot(IceBoard * self)
      {
         if (!fork())
            execv("/sbin/reboot", (char *[]) { "/sbin/reboot",});
      }

Properties
^^^^^^^^^^

Properties are defined using the `tuber_property
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-174>`_
macro.

For example, the NUM_MEZZANINES integer is defined as follows:

.. code:: c

   tuber_property(IceBoard, NUM_MEZZANINES, json_pack("i", NUM_MEZZANINES));

Properties are always returned using `our JSON library
<http://www.digip.org/jansson>`_'s "json_t" type. In this example,
we use json_pack to generate a single integer (which has the constant value 2,
but is accessed through the NUM_MEZZANINES macro defined in `iceboard.h`.)

To take a more complex example, the UNITS property is `defined as follows
<https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/iceboard_constants.c?at=master>`_:

.. code:: c

   tuber_property(IceBoard, UNITS, json_pack(
         "{"
         " s:s, s:s, s:s, s:s, s:s,"
         " s:s, s:s, s:s, s:s, s:s,"
         " s:s "
         "}",
         "HZ", HZ,
         "RAW", RAW,
         "VOLTS", VOLTS,
         "AMPS", AMPS,
         "WATTS", WATTS,
         "DAC_COUNTS", DAC_COUNTS,
         "ADC_COUNTS", ADC_COUNTS,
         "NORMALIZED", NORMALIZED,
         "DEGREES", DEGREES,
         "RADIANS", RADIANS,
         "OHMS", OHMS
   ));

Here, `UNITS` is a dictionary with key/value pairs that are strings. Again,
the actual values are defined elsewhere in the C code and referred to here by
name.

Webserver Library
~~~~~~~~~~~~~~~~~

The application server exposes access to C methods via a web interface. It
also acts as an ordinary web server, transferring HTML, JavaScript, CSS, and
images as requested. Since the cosmology lab is not in the business of
developing web servers, we'd much rather offload as much of this task to
someone else's code as possible.

We make use of an "embedded webserver" called `libmicrohttpd
<http://www.gnu.org/software/libmicrohttpd>`_. It is configured and built as
part of the :doc:`rootfs`.

.. todo:: I've been watching `websockets
   <http://en.wikipedia.org/wiki/WebSocket>`_ mature with interest. If I had
   to reevaluate our use of libmicrohttpd, I'd be tempted to swap in
   `libwebsockets <http://libwebsockets.org>`_ or equivalent if it's possible.
   The use of "push" requests, and longer-lasting connections, could really be
   powerful.

Daemon
~~~~~~

The visible end of the application server is the `fastpath` daemon. This is
the C program you will notice in the board's list of active processes. It's
the glue that brings up the webserver library, directs ordinary requests to
files on the filesystem, and directs Tuber requests to the library code
described below.

Main Program
   https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/tuber_fastpath.c?at=master#cl-481

Dispatcher
   https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/src/tuber_fastpath.c?at=master#cl-333

Build Script (you will have to start with the `top-level Makefile <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/Makefile>`_ to see how this fits together)
   https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/mak/fastpath.mak?at=master

.. warning:: The daemon includes code that watches for libraries
   (libiceboard.so and friends) to disappear from the filesystem (e.g. during
   updates.) If you replace a library as follows:

   .. code:: bash

      # mount / -o remount,rw
      # cp /path/to/libiceboard.so /usr/share/tuber/libiceboard.so
      # mount / -o remount,ro

   ...you will crash the application server, in spite of this code. This is
   because you're modifying the shared library, not unlinking and replacing it
   -- and since the file is mapped into memory while the application server is
   running, this scrambles the code that's running and you end up with a
   segfault. The program `/usr/bin/install` is a standard Unix alternative to
   `cp` for moving files around; it does the correct thing.

   I habitually still use cp, but I also expect the fastpath to crash when I
   do.

Tuber Headers and Runtime
~~~~~~~~~~~~~~~~~~~~~~~~~

Because we use the preprocessor so heavily, we use the `Boost::Preprocessor
<http://www.boost.org/doc/libs/1_57_0/libs/preprocessor/doc/index.html>`_
library of preprocessor macros. This makes our preprocessor abuse shorter and
more focused.

Here are some key definitions in `tuber.h`:

* `tuber_object
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-92>`_
* `tuber_object_inherits
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-103>`_
* `tuber_constructor
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-124>`_
* `tuber_destructor
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-162>`_
* `tuber_property
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-174>`_
* `tuber_method
  <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/c/include/tuber.h?at=master#cl-229>`_

Along with the headers is runtime code. This code manages object registration
and look-up:

* `tuber_register_library
  <https://bitbucket.org/winterlandcosmology/icecore/src/f83d64d85ad331f290576d7821faa095a5f6e499/c/src/tuber_server.c?at=master#cl-71>`_
* `tuber_server_lookup_object
  <https://bitbucket.org/winterlandcosmology/icecore/src/f83d64d85ad331f290576d7821faa095a5f6e499/c/src/tuber_server.c?at=master#cl-51>`_

The runtime also handles method invocation. Here's where method calls and
parameter retrievals are looked up against library code and dispatched:

* `tuber_server_invoke
  <https://bitbucket.org/winterlandcosmology/icecore/src/f83d64d85ad331f290576d7821faa095a5f6e499/c/src/tuber_server.c?at=master#cl-263>`_
* `json_object_handler
  <https://bitbucket.org/winterlandcosmology/icecore/src/f83d64d85ad331f290576d7821faa095a5f6e499/c/src/tuber_server.c?at=master#cl-318>`_
* `json_method_handler
  <https://bitbucket.org/winterlandcosmology/icecore/src/f83d64d85ad331f290576d7821faa095a5f6e499/c/src/tuber_server.c?at=master#cl-362>`_

.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
