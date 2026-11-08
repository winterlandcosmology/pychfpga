------------
Requirements
------------

Host computer
=============

- Linux (development and testing are done on Ubuntu)
- Python 3.12
- git and git-lfs (bitstreams and ARM firmware are stored with LFS)
- Build tools (``make``, a C compiler) and ``python3.12-dev``, needed to compile ``netifaces``


Optional requirements: 

  - `uv <https://docs.astral.sh/uv/>`_, used by the Makefile and recommended to manage
  the environment
  - avahi-daemon, for mDNS support (for IceBoard only)


Network
=======

- A 1G Ethernet switch between the host and the control port of every board
- Jumbo frames (MTU 9000) on the host interface and on every switch in the data path
- Enlarged UDP receive buffers on the host
- A host firewall that accepts UDP from the boards and TCP on the server ports
- IceBoard only: a /22 or larger subnet whose DHCP pool does not assign ``*.*.3.*``, and mDNS
  between the host and the boards
- Corner-turn modes: dedicated 40G/100G links or switches to the external X-engine

Details are in :doc:`../user_guide/networking`.

Clock and timing
================

Multi-board arrays that must be coherent need:

- a 10 MHz reference clock to synchronize sampling
- an IRIG-B time signal or a trigger to synchronize data framing

A GPS receiver can provide both. Crates can distribute them to every board through the backplane.

Platform hardware
=================

Board-specific requirements (power, cooling, cabling, firmware) are in :doc:`../platforms/index`.
