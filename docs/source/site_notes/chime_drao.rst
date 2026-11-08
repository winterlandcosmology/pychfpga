----------
CHIME/DRAO
----------

Notes specific to the CHIME installation at DRAO. They will move to a site repository.

The generic network requirements are in :doc:`../user_guide/networking`.

Network layout
==============

- ARM processors: DHCP on 10.10.0.0/16. Their MAC addresses start with 84:7E:40 (Texas
  Instruments).
- FPGAs: fixed addresses assigned by the control software (no DHCP).
- Crates connect to the container switch through link aggregation groups (LAGs). The container
  switches connect to the central switch over 10G.

Crate switch (Cisco SG200-50)
=============================

- Disable the Spanning Tree Protocol (STP): it confuses the ARMs, which sometimes do not boot.
- Enable jumbo frames (Port Management > Port Settings).
- Set the splash screen message to ``user= cisco, password=as usual`` and set the usual password.
- Optional link aggregation: put ports 1-4 and 24-27 in LAG1. Select *Enable LACP* before clicking
  OK; it cannot be changed afterwards unless all the ports are removed.

Container switch
================

Connect crate ports 1-4 and 24-27 to one of the container switch LAG port groups.

LAGs:

- 1: crate 0, ports gi 1/0/1-8 (gigabit ethernet, switch 1, port group 0, ports 1-8)
- 2: crate 1, ports gi 1/0/1-8
- 3: crate 2, ports gi 1/0/1-8
- 4: crate 3, ports gi 1/0/1-8
- 5: uplink, ports te 1/1/1-4 (10 gigabit ethernet, switch 1, port group 1, ports 1-4)

Jumbo frames:

.. code-block:: text

    enable
    configure terminal
    system mtu 9000
    end
    copy running-config startup-config
    reload
    show system mtu

LAG:

.. code-block:: text

    configure terminal
    interface range gi 1/0/1 - 8
    channel-group 1 active

Disable STP as well. All the LAG link LEDs should be green.

Central switch (Nexus 3132Q)
============================

The default port mode is 32 x 40G. Only the first 24 QSFP ports can be broken out into 4 x 10G;
``speed 10000`` is refused on ports 25 to 32.

.. code-block:: text

    configure
    feature lacp
    system jumbomtu 9000
    hardware profile front portmode sfp-plus

    int e1/1
    speed 10000
    int e1/1/1-4
    no shut
    channel-group 1 mode active

    show int e1/1/1-4             # details of the links, including the average bit rate
    show int eth1/1/1-4 status    # compact table
    show int e1/1/2 transceiver
    reload                        # reboot the switch

GPS serial adapters
===================

Startech NETRS232
-----------------

Check a unit by pinging it (e.g. ``ping 10.0.0.10``) or opening http://10.0.0.10. A unit that
stops working must be power-cycled. To check its open ports:

.. code-block:: bash

    nmap -sV -Pn 10.0.0.10    # -Pn: test the ports even if ping fails

Expected ports: 80 (web interface), 1001 (data), 6000 (control).

Configuration: set the network to DHCP with the manufacturer procedure (this may need a direct
connection, or the Windows utility). Then, in the web interface:

- System status: nickname ``gps-east`` or ``gps-west``
- TCP mode:

  - Telnet server/client: Server (otherwise even a plain TCP connection to the data port fails)
  - Reverse telnet: off
  - CLI mode: not enabled
  - Data port number: 1001 (the configuration files expect it)
  - Server mode protect timeout: 1 minute (default 60 minutes)

- UDP mode: disabled
- UART: RS232, 9600, 8, N, 1, no hardware flow control, no delimiter

Moxa NPort 5110
---------------

This procedure uses the Windows NPort Administration Suite, from the
`NPort 5100 resources page
<https://www.moxa.com/en/products/industrial-edge-connectivity/serial-device-servers/general-device-servers/nport-5100-series#resources>`_.

#. Connect the NPort to the network and power it. The Ready and Link LEDs turn green (Link may
   blink).
#. In the suite, under Function > Configuration, click Search to find the device.
#. Right-click the device, select Unlock and enter the password from the manual (usually
   ``moxa``).
#. Right-click > Configure > Network tab: in the IP configuration box, change Static to DHCP.
#. Under Function > COM Mapping, click Add, select the device (Search if needed) and click OK.
#. Right-click the device > COM Settings: choose a free COM port, and set the baud rate to 9600
   in the Serial Parameters tab.

DHCP and mDNS on ``carillon``
=============================

Static DHCP assignments are based on MAC addresses. Entries can be copied from the receiver hut
address table at https://bao.phas.ubc.ca/wiki/index.php/Receiver_Hut_Networking#IP_Address_map.

.. code-block:: bash

    sudo vim /etc/dhcp/dhcpd.conf       # general configuration
    sudo vim /etc/dhcp/fpga.network     # FPGA equipment
    sudo service dhcpd restart

Leases:

.. code-block:: bash

    cat /var/lib/dhcpd/dhcpd.leases                      # dynamic leases only
    cat /var/lib/dhcpd/dhcpd.leases | grep -B 7 '00:18'   # find a MAC address
    sudo cat /etc/dhcp/dhcpd.conf | grep '00:18'

New boards that are not in the static table get a dynamic address (10.0.1.x):

.. code-block:: bash

    sudo avahi-daemon -k
    avahi-browse _tuber-jsonrpc._tcp --resolve -t | grep -B2 -A2 '10.0.1.'

Active crates:

.. code-block:: bash

    sudo avahi-daemon -k
    avahi-browse _tuber-jsonrpc._tcp --resolve -t | grep -o 'backplane-serial=[0-9]*' | sort -u

Prometheus on ``hk-east``
=========================

As the ``chime`` user:

.. code-block:: bash

    sudo vim /etc/prometheus/prometheus.yml
    curl -X POST localhost:9090/-/reload
