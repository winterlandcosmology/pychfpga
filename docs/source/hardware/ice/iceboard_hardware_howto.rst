ICE hardware description
------------------------

See https://icecore-docs.readthedocs.io for documentation on the ICE hardware.

Some additional is provided here.


Boot process
------------

 1) The ROM in the ARM looks through the SPI flash and SD card for a first stage file bootloader called MLO, loads it and executes it. That bootloader must be small enough to fit in the internal memory of the ARM since the DDR memory is not activated yet.
 2) The MLO first stage bootloader sets up the DDR memory, networking etc, and loads and executes the second stage bootloader. This MLO bootloader is built using U-boot. It has enough smarts to enable the DDR and load the next bootloader in it, but there is no space in internal RAM for the code to properly load and launch the Linux image.
 3) The second-stage bootloader (uboot.bin) looks for the linux image , loads it, and executes it.
 4) The linux kernel image (image.bin) mounts the  linux filesystem in the second SD partition and finishes initializing the operating system with the services (mDNS, SSH, web server, tuber etc.).

Notes:

   - The SPI flash is only 32 MBytes.
   - The AM3871 ARM processor has 128 kBytes of internal RAM
   - The ARM processor is connected to 2 GBytes of external DDR3 RAM

   - The SD card should have the following structure:
       - FAT32 partition (30 MB)
          - MLO: first stage bootloader, about 77 kbytes
          - u-boot.bin: 2nd stage bootloader, about 179 kbytes
          - uImage: Linux kernel image, about 4 Mbytes
       - Linux ext partition (350 Mbytes)
       	  - full linux filesystem


Boot mode configuration
-----------------------

The IceBoard ARM processor can boot off multiple sources.

.. image: ARM_boot_mode_configurations.png

Note that the table lists the bits values from BTMODE[4] to BTMODE[0] from left to right. Read the its from right to left to get the bits in the more natural order of BTMODE[4] to BTMODE[0].

BTMODE 4:0 are set by the DIP switch SW1, from position 1 to 5. the "ON" position on the switch means a binary '1'.
The default mode is BTMODE[4:0] = 10110

BTMODE[0] = SW1.1 = 0 (OFF)
BTMODE[1] = SW1.2 = 1 (ON)
BTMODE[2] = SW1.3 = 1 (ON)
BTMODE[3] = SW1.4 = 0 (OFF)
BTMODE[4] = SW1.5 = 1 (ON)

The boot order is then SPI -> MMC/SD -> UART -> EMAC, meaning it will boot from the SPI flash first if a valid bootloader is present there.

If we want to make sure the SD card is used first (in case the SPI flash is programmed but we don't want to boot from it), we want the mode

BTMODE[0] = SW1.1 = 1 (ON)
BTMODE[1] = SW1.2 = 1 (ON)
BTMODE[2] = SW1.3 = 1 (ON)
BTMODE[3] = SW1.4 = 0 (OFF)
BTMODE[4] = SW1.5 = 1 (ON)

So just turn ON SW1.1 to boot from SD first, OFF to boot from the SPI first.

