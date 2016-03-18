Networking configuration
==========================

This HowTo describes how to setup the network to allow the operation of an annay of IceBoards.


Basics
--------------------------

The IceBoard requires a minimum of one network to its Ethernet port in order to send commands and get replies, as well as being able to ping the board and log into it.

The lower Ethernet port operates a DHCP client and will autimatically get an address from the DHCP server on the network. The upper port uses a fixed IP address (which is set in the SD card).

MAC addresses of the ICEBoard start with: 84:7E:40 (Texas Instruments, the manufacturer of the ARM processor).

Both ports operate at 1000/100/10 Mbps, both on IPv4 and IPv6.

mDNS and DNS-SD
---------------

The ARM firmware implements a mDNS (multicast Domain Name Server) and DNS-SD (Service discovery) to announce its presence on the subnet (with its hostname, IP addess, serial number, backplane model and serial number, slot number) and to indicate that it offers a ssh, sftp and a remote control service.

A computer with a mDNS client (Avahi on Linux, Bonjour on Windows and Mac) can access the board by its serial number without having to know what IP address was dynamically assigned to the board. There is therefore no need to configure the DHCP server (often implemented in the router) to serve fixed IP addresses.


If traffic between the computer and a host has to go through multiple switch, the switches must be configured to pass the mDNS multicast packets. Multicast operates by having every participant in a network to broadcast a IGMP (Internet Group messaging protocol) message indicating they want to receive multicast traffic for a specific service identified by a specific IP multicast address. The switch must therefore either systematically forward all multicast messages to the next router, or must listen to the IGMP traffic (a process often called IGMP snooping) to know who whats what multicast packets, and send the packets to the proper ports.

FPGA Networking
---------------

The FPGA can operate a direct connection to the network using its SFP+ port if its firmware supports that feature. A proper SFP+ to RJ45 adapter will have to be used to connected to a RJ45-wired network, and fiber connections are also possible. The port supports connections up to 10 Gbps, although capabilities depends on the firmware (CHIME firmware, for instance, supports only 1000Mbps Ethernet links (no 100/10 MBps) without autonegociation).

Jumpers or the ARM firmware must be set to connect the FPGA to the SFP+ port instead of connecting it to the ARM PCIe port. Connection to the SFP+ is the default at power up.

The MAC and IP address of the FPGA are firmware-dependent. For example, in CHIME, those are determined by the control computer and are programmed at startup through commands sent to the ARM processors, which sets the corresponding registers in the FPGA through the ARM-FPGA SPI link. The CHIME firmware can also receive broadcast packets to set-up the MAC and IP address of a specific FPGA if the broadcast packet contain the FPGA's unique serial number.

Notes
-----

   - MAC addresses of the ICEBoard start with: 84:7E:40 (Texas Instruments, the manufacturer of the ARM processor)

   - We noticed that IpV4 mDNS did not work well using a Netgear JGS524E ProSafe switch connected to the Cisco switches that connect to the array of iceboards. For this to work we had to setup the Netgear switch MultiCast option:

       + IGMP snooping status: Enable (default)
       + Validate IGMPv3 IP header: Disable (default)
       + Block Unknown Multicast Address: Disable (default)
       + IGMP Snooping Statis Router Port: ANY (This was *Not* the default)

   IPv6 multicast traffic operated properly with the default NetGear configuration, so IPv6-aware mDNS client (Avahi, Bonjour on Mac) operated properly. Only Windows Bonjour client, however, experienced long delays.

   - Proper mDNS operation can be checked by capturing IGMP packets  and IP multicast UDP packets on port 5353 (with Wireshark or other tools). Upon power up, the board will announce their presence from both their IPv4 and IPv6 addresses. The board shall also respond to mDNS requests in the same manner.

.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
