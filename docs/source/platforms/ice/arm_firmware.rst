------------
ARM firmware
------------

The IceBoard ARM processor boots Linux from its SD card. The image contains the operating system,
the web server, and the IceBoard software that exposes the hardware over HTTP (Tuber).

The images are bzip2-compressed files in ``pychfpga/hardware/ice/arm_firmware/``
(``Chime_R11.3i.img.bz2`` and older releases), stored with git-lfs. A valid image is about
100 MB; if it is a few kilobytes, run ``git lfs pull``.

Writing an SD card
==================

Writing a card takes about 45 seconds with a fast reader.

Linux
-----

.. code-block:: bash

    bunzip2 -k Chime_R11.3i.img.bz2
    lsblk                                  # find the SD card, e.g. /dev/sdb
    sudo umount /dev/sdb1 /dev/sdb2        # unmount every partition of the card
    sudo dd if=Chime_R11.3i.img of=/dev/sdb bs=10M status=progress
    sync
    sudo eject /dev/sdb

**Be sure of the device name: ``dd`` overwrites it without confirmation.**

macOS
-----

.. code-block:: bash

    bunzip2 -k Chime_R11.3i.img.bz2
    diskutil list                          # find the SD card, e.g. /dev/disk3
    sudo diskutil unmountDisk /dev/disk3
    sudo dd if=Chime_R11.3i.img of=/dev/rdisk3 bs=10m
    sudo diskutil eject /dev/disk3

Windows
-------

Decompress the file (for example with 7-zip) and write the ``.img`` file with Win32 Disk Imager.
Eject the card before removing it.

Updating boards remotely
========================

All the SD cards of an array can be rewritten in parallel over the network. The board copies the
image to a RAM disk, writes it to the SD card while still running from it, and waits 120 seconds
for the writes to finish.

.. code-block:: python

    ca = await FPGAArray("bp16 12")    # no mode: connect to the ARMs only
    await ca.ib._update_arm_firmware("pychfpga/hardware/ice/arm_firmware/Chime_R11.3i.img.bz2")

The image must be bzip2-compressed. Power-cycle the boards afterwards.

.. warning::

    Do not interrupt the update: an interrupted write corrupts the SD card and the board no longer
    boots. Keep a spare card and an SD card writer at hand.
