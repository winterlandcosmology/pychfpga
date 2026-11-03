Networking configuration
==========================

This HowTo describes how to setup the receiver Hut networking.



Each IceBoard requires two 1G ethernet connections: one to the ARM processor , and one to the FPGA.


The ARM has two Ethernet ports.  Only the lower port should be used. It operates a DHCP client and will autimatically get an address from the DHCP server on the network.
The MAC addresses of the ARM links start with the OUI code 84:7E:40 (Texas Instruments, the manufacturer of the ARM processor). The port operate at 1000/100/10 Mbps, both on IPv4 and IPv6. The ARM processors are on the network 10.10.0.0/16.


The FPGA can operate a direct connection to the network using its SFP+ port. A proper SFP+ to RJ45 adapter will have to be used to connected to a RJ45-wired network. The port supports ONLY 1 Gigabit Ethernet, without auto negotiation. The IP address of the FPGAa are fixed and are assigned by the control software  (i.e the FPGA does not support DHCP).


mDNS and DNS-SD
---------------

The ARM firmware implements a mDNS (multicast Domain Name Server) and DNS-SD (Service discovery) to announce its presence on the subnet (with its hostname, IP addess, serial number, backplane model and serial number, slot number).

A computer with a mDNS client (Avahi on Linux, Bonjour on Windows and Mac) can discover all the boards on the network without knowing their IP addresses.


If traffic between the computer and a host has to go through multiple switch, the switches must be configured to pass the mDNS multicast packets. Multicast operates by having every participant in a network to broadcast a IGMP (Internet Group messaging protocol) message indicating they want to receive multicast traffic for a specific service identified by a specific IP multicast address. The switch must therefore either systematically forward all multicast messages to the next router, or must listen to the IGMP traffic (a process often called IGMP snooping) to know who whats what multicast packets, and send the packets to the proper ports.

Note: Proper mDNS operation can be checked by capturing IGMP packets  and IP multicast UDP packets on port 5353 (with Wireshark or other tools). Upon power up, the board will announce their presence from both their IPv4 and IPv6 addresses. The board shall also respond to mDNS requests in the same manner.

Crate switch
---------------

model:  Cisco SG200-50 50 port Gigabit smart switch

Config:
	- Disable Spanning Tree protocol (STP). This confuses the ARM and makes them not boot properly sometimes
	- Set spash screen message:"user= cisco, password=as usual"
	- Set password to the usual
	- Enable jumbo frames (Port-management->port settings)

Optional:

	- Set-up link aggregation if needed. Aggregate links 1-4 and 24-27 in LAG1. Select Enable LACP *before* clicking ok, otherwise you cannot change it unless you remove all the ports.


Container switch
----------------


connections
-----------

ARM & FPGA to Cisco switch.
Port #, color.

Connect crate ports 1-4 & 24-27 to one of the container switch LAG group ports.

LAGs:
  1 - Crate 0 - Ports gi 1/0/1-8  ( gigabitethernet, switch 1, port group 0, ports 1-8)
  2 - Crate 1 - Ports gi 1/0/1-8
  3 - Crate 2 - Ports gi 1/0/1-8
  4 - Crate 3 - Ports gi 1/0/1-8
  5 - Uplink  - Ports te 1/1/1 - 4 (tengigabitethernet, switch 1, port group 1, ports 1-4)

# Enabling jumbo frames

	enable
	configure terminal
	system mtu 9000
	end
	copy running-config startup-config
	reload
	show system mtu

#set-up LAG
	configure terminal
	interface range gi 1/0/1 - 8
	channel-group 1 active

# Disable STP


All lag link leds should be green


Central switch (Nexus 3132Q)
----------------------------

32x40G—This is the default port mode. Only the first 24 QSFP ports are break-out capable. You cannot enter the speed 10000 command on ports 25 through 32.


configure
feature lacp
system jumbomtu 9000

interface eth 1/2
speed 10000

int eth 1/2/1-4
channel-group 1 mode active




--- other set of config

nexus 3132q

hardware profile front portmode sfp-plus
int e1/1
speed 10000
int e1/1/1-4
no shut
show int e1/1/1-4  # show all details of the connections, including average bit rate
show int eth1/1/1-4 status  # nice compact table

int e1/5
speed 10000
int 1/5/1-4
no shut


 show int e1/1/2 transceiver

reload # reloads the operating system (reboot)


.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
