------------------
Boot and ARM shell
------------------

Boot process
============

1. The ARM boot ROM looks for the first-stage bootloader (``MLO``) in the SPI flash and on the SD
   card, then loads and runs it from internal memory (the DDR memory is not active yet).
2. ``MLO`` (built with U-Boot) enables the DDR memory and networking, then loads the second-stage
   bootloader into DDR.
3. ``u-boot.bin`` loads and starts the Linux kernel.
4. The kernel (``uImage``) mounts the root filesystem from the second SD partition and starts the
   services: mDNS, SSH, web server and Tuber.

Memory and SD card layout:

- SPI flash: 32 MB
- AM3871 ARM internal RAM: 128 kB; external DDR3: 2 GB
- SD card:

  - FAT32 partition (30 MB): ``MLO`` (~77 kB), ``u-boot.bin`` (~179 kB), ``uImage`` (~4 MB)
  - ext partition (350 MB): Linux root filesystem

Boot mode switches
==================

The ARM boot source is set by DIP switch SW1, positions 1 to 5, which drive BTMODE[0] to
BTMODE[4]. "ON" is a binary 1.

.. image:: ../../images/ARM_boot_mode_configurations.png
    :width: 80%

The table lists the bits from BTMODE[4] to BTMODE[0], left to right.

The default is BTMODE[4:0] = ``10110`` (SW1.1 OFF, SW1.2 ON, SW1.3 ON, SW1.4 OFF, SW1.5 ON). The
boot order is then SPI, MMC/SD, UART, EMAC: a valid bootloader in SPI flash takes precedence.

To boot from the SD card first even when the SPI flash is programmed, use BTMODE[4:0] = ``10111``,
i.e. turn SW1.1 ON.

Logging in to the ARM
=====================

The ARM runs an SSH server. Log in as ``root``; no password is required, as the boards are
expected to be on an isolated subnet. The ARM Linux only offers an old key-exchange algorithm,
which must be enabled explicitly:

.. code-block:: bash

    ssh -oKexAlgorithms=+diffie-hellman-group1-sha1 root@<host_or_ip>

The option can also be set for these hosts in ``~/.ssh/config``. See
https://www.openssh.com/legacy.html.

To find a board's address:

- ``iceboardNNNN.local``, where NNNN is the serial number (e.g. ``iceboard0181.local``), if mDNS
  works on the host
- ``avahi-browse _tuber-jsonrpc._tcp --resolve`` or ``avahi-discover``
- an ``nmap`` scan of the subnet
- the DHCP server lease table

SPI flash
=========

To inspect the SPI flash from the ARM shell:

.. code-block:: bash

    hexdump -C -vn 512 /dev/mtd0

``mtd0`` holds the SPI first-stage bootloader, if one is used (e.g. a network-boot bootloader).
``mtd4`` appears to hold IPMI board information.

The SPI flash is small. It can hold a bootloader that then boots from the network. Example of
programming one:

.. code-block:: bash

    /usr/bin/flashcp /tmp/u-boot.min.spieth.spi /dev/mtd0
