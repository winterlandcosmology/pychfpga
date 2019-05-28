Arm Firmware
============

The arm firmware is stored using git annex, with the images stored on Microsoft onedrive.

Without Git-Annex:
------------------

If you've never used git annex before:

on a mac::

    $ brew install git-annex

on linux::
    $ sudo apt-get install git-annex

If you haven't used a onedrive remote, first follow
these instructions
(not necessary to have/use your own account or have onedrive client installed, this
accesses the onedrive api with python)::

http://git-annex.branchable.com/tips/skydriveannex/

reproduced here:

skydriveannex 0.2.1

Hook program for gitannex to use skydrive (previously Windows Live SkyDrive and Windows Live Folders) as backend
Requirements:

    python2
    python-yaml

Credit for the Skydrive api interface goes to https://github.com/mk-fg/python-skydrive
Install

Clone the git repository in your home folder.

    $git clone git://github.com/TobiasTheViking/skydriveannex.git 

This should make a ~/skydriveannex folder
Setup

Make the file executable, and link it into PATH

    $cd ~/skydriveannex; chmod +x git-annex-remote-skydrive; sudo ln -sf `pwd`/git-annex-remote-skydrive /usr/local/bin/git-annex-remote-skydrive

With Git-Annex:
---------------
Now that you have the prerequisites, cd back to your ch_acq folder and run::

    $ git annex enableremote skydrive

This will open a browser, enter chime.correlator@outlook.com as the user, and standard chime password (ask if you don't know),
click yes, then copy the URL in the browser.
then run::

    $ export OAUTH='[URL]'
    $ git annex enableremote skydrive

If successful should now be able to get firmware releases. for example:
from the ch_acq/pychfpga/firmware/arm/2015-02-13/ directory:
    $ git annex get iceboard_chime28.tar.gz

will now download to your machine from onedrive.

Untar the file to get the full image.

Create SD card on a mac:
---------

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








