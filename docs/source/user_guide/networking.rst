----------
Networking
----------

Host settings
=============

Jumbo frames
------------

Raw-data and correlator packets are UDP jumbo frames. Set the MTU of the interface that faces the
boards, and of every switch between them, to 9000:

.. code-block:: bash

    sudo ip link set <interface> mtu 9000

To make it permanent, set ``mtu: 9000`` for the interface in netplan, or the MTU field of the
connection in NetworkManager.

UDP receive buffers
-------------------

The default buffers are too small and packets get dropped:

.. code-block:: bash

    sudo sysctl -w net.core.rmem_max=26214400
    sudo sysctl -w net.core.rmem_default=26214400
    sudo sysctl -w net.ipv4.udp_mem='26214400 26214400 26214400'
    sudo sysctl -w net.ipv4.udp_rmem_min=26214400

To make it permanent, put the same settings (without ``sysctl -w``) in
``/etc/sysctl.d/90-pychfpga.conf``.

Firewall
--------

Accept UDP from the boards (commands, raw data, correlator data, discovery), and TCP on the
server ports ``54320-54329`` if clients on other machines connect to ``fpga_master``,
``raw_acq`` or the auxiliary servers.

Check incoming traffic with ``ip -s link show <interface>``.

IceBoard
========

Each IceBoard needs two 1G Ethernet connections.

- **ARM**: the lower of the two RJ45 ports. It gets its address from DHCP and runs at
  10/100/1000 Mbps. The MAC addresses start with ``84:7E:40`` (Texas Instruments).
- **FPGA**: the SFP+ cage with an SFP-to-RJ45 adapter. 1G only, no auto-negotiation, no DHCP.
  The FPGA address is derived from the ARM address by ``fpga_ip_addr_fn`` (default
  ``(a,b,3,d)``: ARM ``a.b.c.d`` gives FPGA ``a.b.3.d``).

The subnet must therefore be /22 or larger so that ``*.*.3.*`` is reachable, and the DHCP
server must not hand out ``*.*.3.*`` addresses.

mDNS discovery
--------------

The IceBoard ARM announces itself with mDNS and DNS-SD (hostname, IP address, serial number,
crate model and serial, slot). The host needs an mDNS client (Avahi on Linux) to find boards by
serial number. Check with:

.. code-block:: bash

    avahi-browse _tuber-jsonrpc._tcp --resolve

If the query times out, restart the daemon with ``sudo avahi-daemon -k``.

When traffic crosses several switches, they must forward multicast: either flood it, or use IGMP
snooping. Boards announce themselves on IPv4 and IPv6 at power-up; mDNS uses UDP port 5353.

CRS
===

CRS boards broadcast UDP discovery beacons (port 9875) from U-Boot and from the TCPipe firmware.
List the boards on the local network:

.. code-block:: bash

    crs_discovery            # live table
    crs_discovery --dump     # print once and exit

Discovery uses broadcast, so the host must be on the same layer-2 network as the boards.

Real-time offload
=================

In corner-turn modes the firmware streams corner-turned data as UDP packets over the 40G or 100G
QSFP ports, at rates above 75 Gbit/s per board. These links need dedicated cabling or switches
to the X-engine. They are not used when the on-board X-engine is enabled.
