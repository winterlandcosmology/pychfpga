iceboard_software_howto
-----------------------



SSH into an Iceboard
--------------------

The default linux operating system running on the IceBoard's ARM processor provides a SSH server to allow a remote shell session into the board.

You should log in as root. No password is required (the IceBoards are assumed to be operating in an isolated subnet that is protected from attackers).

Note that the Linux system on the ICEBoard is pretty old and operated using deprecated key exchanged algorithms. We have to tell modern systems to use the older key exchange algorithms.

To log in:

ssh -oKexAlgorithms=+diffie-hellman-group1-sha1 root@<host_or_ip>

To find the hostname 
   - iceboardNNNN.local, where NNNN is the serial number (e.g. iceboard0181.local). This works only if mDNS is working on both the IceBoard and the computer you log from.
   - use avahi-discover (on Ubuntu) to see the IP address of the boards that annource themselves over mDNS as they boot
   - do a nmap scan
   - Check DHCP lease table on the DHDP server


See https://www.openssh.com/legacy.html or other ways to deal with old key exchange algorithm (for example, setting the option in the ~/.ssh/config file)

See content of SPI flash
------------------------

SSH into a board and execute:

	hexdump -C -vn 512 ./mtd0

mtd0 is where we store the SPI first stage bootloader (if we use one, like the netboot bootloader).

mtd4 seems to store IPMI board information.


Program SPI flash bootloader
----------------------------

The IceBoard can boot off the on-board SPI flash, but the space is limited. This is used by DfMux applications to load a bootloader than then performs a netboot.

As an indication, here is how a SPI flash bootloader is programmed.

	/usr/bin/flashcp /tmp/u-boot.min.spieth.spi /dev/mtd0

For more info, see mcgillcosmology.com/pydfmux/master/firmware/release_notes.html page or pydfmux/docs/firmware/firmware.rst repo.

