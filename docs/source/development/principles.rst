------------------------
Principles of operation
------------------------

``pychfpga`` is an in-process Python control plane for arrays of FPGA boards.
The **hardware map** is a first-class object: an in-memory registry of boards,
crates and mezzanines. It is not a database — there is no SQLAlchemy session
and no persistence layer.

:class:`~pychfpga.fpga_array.FPGAArray` populates that map, opens each board's
on-board processor, programs the FPGA, then exposes platform, firmware and
array-wide methods. Three layers sit on top of one another:

- **Array** — :class:`~pychfpga.fpga_array.FPGAArray`
- **Platform** — :class:`~pychfpga.hardware.motherboard.Motherboard` subclasses
  (:class:`~pychfpga.hardware.ice.iceboard.IceBoard`,
  :class:`~pychfpga.hardware.crs.crs.CRS`, …)
- **Firmware** — :class:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA`, stored as
  ``mb.fpga``

.. code-block:: text

    HWM string or list of dicts
        -> HardwareMap (Motherboard / Crate / Mezzanine objects)
        -> network discovery + self-discovery
        -> open platform (Tuber / TCPipe)
        -> program FPGA + instantiate chFPGA
        -> open MMI + array_init

For interactive use of ``FPGAArray``, see
:doc:`../user_guide/interactive_operation`.


Hardware map
============

The live map is :class:`~pychfpga.hardware.hardware_map.HardwareMap`: one
process-global Python list per hardware kind
(:class:`~pychfpga.hardware.motherboard.Motherboard`,
:class:`~pychfpga.hardware.crate.Crate`,
:class:`~pychfpga.hardware.mezzanine.Mezzanine`). Instances register themselves
in ``__new__``. :meth:`~pychfpga.hardware.hardware_map.HardwareMap.clear_hardware_map`
empties the lists in place so existing ``FPGAArray`` bindings stay live.

Platform classes are declared in
``pychfpga/hardware/supported_hardware.py`` and loaded lazily from that
manifest. An older SQLAlchemy ``!HardwareMap`` YAML loader existed and is
unused.

``__init__`` on hardware objects must not talk to the hardware. Objects are
created, updated and replaced while the map is still fluid.

Identity
--------

A board matches an existing instance if **either** its hostname matches **or**
its ``(part_number, serial)`` pair matches. A crate matches on ``crate_number``
**or** ``(part_number, serial)``. ``part_number is None`` means a generic
placeholder, not real hardware.

Always create objects with
:meth:`~pychfpga.hardware.motherboard.Motherboard.get_unique_instance` (or the
crate equivalent), never by calling ``IceBoard(...)`` / ``CRS(...)`` directly.
Direct construction cannot keep identity unique or rewrite crate and mezzanine
links.

After ``FPGAArray`` finishes, select boards from the snapshot collections
``ca.ib`` / ``ca.ic`` by serial, hostname, ``(crate, slot)`` tuple, or the
object itself (:meth:`~pychfpga.fpga_array.FPGAArray.get_iceboard`).

Gradual construction and class changes
--------------------------------------

Class change is how the map is built gradually. The user often names hardware
incompletely — an IP, a serial, a crate number, or a generic
``Motherboard``. That creates a placeholder.

Discovery then fills in what the hardware actually is (hostname, serial, slot,
crate). :meth:`~pychfpga.hardware.motherboard.Motherboard.update_instance` with
``new_class=...`` replaces the Python class of the existing object: a generic
``Motherboard`` becomes ``IceBoard`` or ``CRS``; a generic crate becomes
``IceCrate_MGK7BP16`` or ``CRSBackplane``. The old instance is deleted; crate
slot dictionaries and mezzanine back-references are rewritten to the new
object.

Same-class updates fill missing hostname, serial or slot without replacing the
object. ``FPGAArray`` refuses to finish while any board or crate still has
``part_number is None``.

Consistency
-----------

Consistency is enforced **procedurally**, not by a separate validator or
schema. Each ``get_unique_instance`` / ``update_instance`` call runs
hardware-specific bookkeeping so the map stays coherent:

- bidirectional ``crate.slot[n]`` ↔ ``motherboard.crate``
- mezzanine ``iceboard`` pointers rewritten on class change
- duplicate identity keys raise ``RuntimeError``
- a generic crate cannot have a serial

After discovery, ``FPGAArray`` does a final pass: drop unused crates,
unresolved hostnames, leftover generics and excluded boards, then snapshot
into sorted ``self.ib`` / ``self.ic``.

Object graph
------------

.. code-block:: text

    Motherboard.crate          -->  Crate
    Crate.slot[n]              -->  Motherboard
    Motherboard.mezzanine[m]   -->  Mezzanine
    Mezzanine.iceboard         -->  Motherboard

Mezzanines are children of motherboards. They are not unique-tracked by serial
the way boards and crates are.

Specifying the map
------------------

Two input forms become the same in-memory map.

A **string** is convenient for interactive use. Tokens are space-separated.
A part-number token selects the class (suffix match, so ``mb`` means
``MGK7MB``, ``crs`` means ``CRS``, ``bp16`` means ``MGK7BP16``). Following
tokens are serials, optional ``serial:slot`` / ``serial:slot:crate_number``,
an IP or hostname, or ``*`` (wildcard serial).

.. code-block:: python

    ca = await FPGAArray("crs 247 224", mode="corr8")
    ca = await FPGAArray("mb 003 007")
    ca = await FPGAArray("bp16 12:0 13:1", mode="shuffle512")
    ca = await FPGAArray("10.10.3.7", mode="corr16")

A **list of dicts** (including YAML) can pass more than identity. Each entry
is ``{class, serial, hostname, slot, crate_number, ...}``. Extra keys go to
the object (``extra_hwm_args``) so platform or firmware init can receive them
— for example ``fpga_ip_addr``.

.. code-block:: python

    hwm = [
        {"class": "IceCrate_MGK7BP16", "serial": "003", "crate_number": 0},
        {"class": "IceBoard", "serial": "0234", "hostname": "10.10.10.5",
         "slot": 3, "crate_number": 0, "fpga_ip_addr": "10.10.3.5"},
    ]
    ca = await FPGAArray(hwm, mode="corr16")

String syntax for operators is also described in
:doc:`../user_guide/interactive_operation`.


Populating the map
==================

After placeholders exist, two stages complete the map: **network discovery**
(find a hostname or IP) then **self-discovery** (ask the board for serial,
slot, crate and mezzanines). Boards are grouped by
``get_network_discovery_function()``, so a mixed IceBoard and CRS array uses
the right protocol for each.

Network discovery
-----------------

Network discovery runs **only** for boards and crates whose hostname the user
did not supply — typically those named by serial or crate. If the hardware
map already has a hostname or IP, that board is skipped here; self-discovery
later fills identity.

The approach is platform-dependent.

**IceBoard** advertises itself with mDNS / DNS-SD
(``_tuber-jsonrpc._tcp.local.``), browsed with zeroconf. TXT records carry
motherboard part and serial, and backplane part, serial and slot. The
hostname is ``iceboardNNNN.local``. There is no IP scan. Tuber is the RPC
used after the board is found, not the discovery protocol.

**CRS** broadcasts UDP beacons on port 9875. The host sends ``BEACONREQ``;
boards reply ``FWREQ`` (still in U-Boot) or ``TCPIPE``. Headers carry
product, serial, crate, backplane and slot. Boards and the host must be on
the same L2 broadcast domain. Avahi is not required.

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * - 
     - IceBoard
     - CRS
   * - Advertise
     - mDNS ``_tuber-jsonrpc._tcp``
     - UDP broadcast beacons
   * - Port
     - 5353 (multicast)
     - 9875 (broadcast; ``BEACONREQ`` solicits)
   * - Board software
     - ARM Linux + Tuber already up
     - U-Boot (``FWREQ``) or TCPipe (``TCPIPE``)
   * - Extra step
     - none
     - upload ``tcpipe_crs.elf`` if still in U-Boot
   * - Control plane after open
     - HTTP Tuber
     - TCP TCPipe port 7

Self-discovery
--------------

Once every remaining board has a hostname, ``FPGAArray`` opens the platform
and asks each board for what the map still lacks:
``discover_serial_async``, ``discover_slot_async``,
``discover_crate_async``, ``discover_mezzanines_async``.

- **IceBoard** reads motherboard and backplane IPMI over Tuber, then FMC
  mezzanine EEPROMs.
- **CRS** reads the same IPMI fields over TCPipe. Mezzanine discovery is a
  no-op. If the board is still in U-Boot, ``set_platform_firmware()`` uploads
  ``tcpipe_crs.elf`` between network discovery and open.

Crate and slot information from mDNS TXT or CRS beacons is confirmed at this
step, not always invented. Boards given only as an IP rely on self-discovery
for serial, crate and slot.


Initialization
==============

:class:`~pychfpga.fpga_array.FPGAArray` is awaitable. Construction only stores
arguments; hardware work starts when the object is awaited:

.. code-block:: python

    ca = await FPGAArray("crs 77", mode="corr8")

Once the hardware map is complete, ``FPGAArray`` snapshots ``self.ib`` /
``self.ic`` and drives a four-step contract that every motherboard
implements:

1. ``open_platform_async()`` — talk to the on-board processor; platform
   methods become usable.
2. ``set_fpga_bitstream_async()`` — program the FPGA. ``mb.fpga`` is created
   but not yet opened.
3. ``open_fpga_async()`` — open MMI, instantiate firmware submodule objects.
4. ``init_fpga_async()`` — put the firmware into the requested operational
   state.

The ``init`` argument selects how far to go (when ``mode`` is set it defaults
to 3; ``prog`` defaults to 2):

- **0** — map and ``open_platform_async`` only
- **1** — also program the FPGA (``prog``) and create ``mb.fpga``
- **2** — also ``open_fpga_async``
- **3** — also firmware-level ``array_init_async``

Role of the platform
--------------------

Platform code owns the processor link and the programming path only.

- **IceBoard** — Tuber over HTTP. The ARM Linux image is assumed already
  running. The bitstream is sent through the ARM.
- **CRS** — TCPipe over TCP port 7. If the board is still in U-Boot, platform
  firmware (``tcpipe_crs.elf``) is uploaded first. The HMC7044 PLL is
  initialized before the bitstream so the FPGA comes up on the right clocks.

Role of the firmware
--------------------

:class:`~pychfpga.fpga_firmware.chfpga.chfpga.chFPGA` is the Python handler for
the bitstream, not a motherboard. Firmware-level
``array_init_async`` (on :class:`~pychfpga.fpga_firmware.chfpga.chfpga_array_methods.chFPGAArrayMethods`)
initializes channelizers and corner-turn engines, calls ``ic.init()`` on
crates, sets the sync method, then applies the operational mode.

Bitstream programming, MMI open, and the program/open retry loop stay in
``FPGAArray``.


Firmware management
===================

Operational ``mode`` selects both the ``.bit`` file **and** the Python
firmware class plus its init parameters (sampling frequency, MMI transport,
…). ``bitfile=`` still requires ``mode``: the file is the bitstream, ``mode``
is how Python talks to it.

:meth:`~pychfpga.fpga_firmware.fpga_firmware.FPGAFirmware.get_firmware` looks
up ``(platform part_number, mode)`` in every
:class:`~pychfpga.fpga_firmware.fpga_firmware.FPGAFirmware` subclass
``PLATFORM_SUPPORT`` table. The chFPGA table and the platform/mode/bitstream
list are in :doc:`../introduction`.

Bitstreams live in ``pychfpga/fpga_firmware/bitstreams/`` and are stored with
Git LFS. CRC32 and MD5 are computed on the raw payload. IceBoard can skip
reprogramming when the on-FPGA CRC already matches (``prog=1``). CRS cannot
reliably detect an already-programmed FPGA, so ``prog=2`` (always program) is
the practical clean-start path.

After upload, ``mb.fpga = chFPGA(motherboard, mode, **fw_params)`` with no
FPGA I/O yet. ``open_async`` opens MMI (UDP on IceBoard, TCPipe on CRS),
checks cookie ``0x42``, and instantiates GPIO, channelizers, CT and correlator
objects from the geometry the FPGA reports.

ARM / platform firmware is separate from the FPGA bitstream: IceBoard Tuber
is already running; CRS may still need ``tcpipe_crs.elf``.


Method visibility
=================

There are three access paths. Methods are not mixed into ``FPGAArray`` as a
single class.

Platform methods
----------------

These live on the motherboard object (``IceBoard``, ``CRS``, …): open the
platform, program the FPGA, I2C, sensors, Tuber or TCPipe.

IceBoard installs ARM Tuber methods as instance attributes after
``open_platform_async``. CRS uses explicit TCPipe RPCs, not Tuber.

Firmware methods
----------------

``Motherboard.__getattr__`` forwards unknown names to ``self.fpga`` once it
exists, so ``mb.GPIO``, ``mb.chan`` and ``mb.CT`` are firmware objects. Platform attributes win if both define the
same name. ``__dir__`` unions both, which is why tab-complete on a board
shows firmware names.

Array methods
-------------

Array-wide methods appear on ``ca`` through
:class:`~pychfpga.common.array_methods.ArrayMethods` provider classes, not
mixins. Each motherboard advertises a set of providers. ``FPGAArray`` takes
the **intersection** (a method is array-wide only if every board advertises
that provider) and instantiates one provider bound to the array.

Firmware registers providers: ``chFPGAArrayMethods`` from the ``chFPGA``
class, then extra CT / 100G providers during ``open_async`` (BCT, UCT, CGE).
Duplicate method names across providers raise.

Ccoll fan-out
-------------

``ca.ib`` and ``ca.ic`` are :class:`~pychfpga.common.ccoll.Ccoll` collections.
Attribute access and calls fan out to every member, concurrently for
coroutines:

.. code-block:: python

    await ca.ib.set_fpga_bitstream_async(mode, force=True)  # same method, every board
    await ca.sync_async()                                   # one coordinated array method

``Ccoll`` is “call the same method on each object”. Array methods coordinate
the array (one sync time, one frequency map, crate-aware shuffle).

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - You write
     - Meaning
   * - ``ca.ib`` / ``ca.ic``
     - ``Ccoll`` of motherboards / crates
   * - ``ca.ib[0]`` / ``ca.get_iceboard(...)``
     - one motherboard
   * - ``ca.ib.set_fpga_bitstream_async(...)``
     - platform method on every board
   * - ``ca.ib.GPIO`` / ``ca.ib.chan``
     - firmware objects on every board
   * - ``ca.sync_async()``
     - array-wide coordinated sync
   * - ``ca.set_operational_mode_async('corr8')``
     - array-wide operational mode


State management
================

Firmware modules expose durable, semantic configuration through a shared
``State`` tree (dotted access, ``as_dict()``, snapshot-based
``ack()`` / ``has_changed()``). See :doc:`firmware_state`.
