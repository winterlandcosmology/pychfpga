Arm Firmware
============

The arm firmware is stored in git LFS in the ``pychfpga`` repository, under the folder pychfpga/pychfpga/arm_firmware.

The file is a linux image compressed with bzip2. The file shall be about 100 Mbytes. If is just a few kilobytes, git probably didn't get the file itself, but rather its pointer text file. Make sure you have git LFS installed (it is already included in modern versions of git), make sure you are in the correct branch, and do a ``git lfs pull`` to replace the pointer with the actual data.


Create SD card on a mac:
------------------------

Uncompress the image file.


Find which drive is SD card::
    $ sudo diskutil list

(be sure you are correct, as this can wipe your system if used incorrectly)

Unmount it::
    $ sudo diskutil umountDisk /dev/disk3

Clone the image, then safely remove::

    $ sudo dd if=iceboard_chime28.img of=/dev/rdisk3 bs=10m
    $ sudo diskutil umountDisk /dev/disk3
    $ sudo diskutil eject /dev/disk3

Linux:
------

Find the drive::
    $ lsblk

(be sure you are correct, as this can wipe your system if used incorrectly)

Unmount it::
    $ sudo umount /dev/sdb1
    $ sudo umount /dev/sdb2
    ...

Clone the image, safely remove::
    $ sudo dd if=iceboard_chime28.img of=/dev/sdb bs=10M
    $ sudo umount /dev/sdb1
    $ sudo umount /dev/sdb2
    $ sudo eject /dev/sdb

(the eject command here may error, is important on some distros...)

Windows:
--------
Uncompress the file, using for example 7-zip. You will get an .img file.

Use an application to write the image to the SD card. Win32diskimager is free, minimalist, and works well.

It takes about 45 seconds to write the image using a fast SD card adapter.

Eject the card when you are finished to make sure there are no pending writes.






