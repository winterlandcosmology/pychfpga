nexus 3132q configuration
=========================

- hardware profile front portmode sfp-plus
- int e1/1
- speed 10000
- int e1/1/1-4
- no shut
- show int e1/1/1-4  # show all details of the connections, including average bit rate
- show int eth1/1/1-4 status  # nice compact table

- int e1/5
- speed 10000
- int 1/5/1-4
- no shut


- show int e1/1/2 transceiver

- reload # reloads the operating system (reboot)