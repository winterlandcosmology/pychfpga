Flashing TCPipe ARM firmware on the CRS
---------------------------------------

The CRS needs a generic uboot firmware image to boot. pychfpga uses uboot commands to dynamically uploads the TCPIPE ARM firmware to the boards when an array is created, so there is no need to flash TCPIPE manually.
Here are the instructions if uboot needs to be updated:

Case 1 - Upgrading a board with an existing uboot

Modern uboot builds allow over-the-wire reflashing. The utility ``fwupgrade`` uploads the uboot image to selected CRS boards on the network (you must list target boards on the command line; see ``fwupgrade.py --help``). Use ``--dry-run`` to verify target matching without programming flash.
Turn off or disconnect boards you do not want to upgrade, or omit them from the target list. Example::

   cd pychfpga/pychfpga/hardware/crs/arm_firmware  # go to the firmware folder

   uv run fwupgrade.py --help # show all options


   uv run fwupgrade.py 37 55  # upgrades CRS 0037 and 0055. Same as CRS 37 55
   uv run fwupgrade.py "*" --dry-run # Update ALL the CRS boards on the local network. Use --dry-run first to check if you select the right boards before upgrading, then omit for actual upgrade.
   uv run fwupgrade.py "CRC4 11"  # upgrades all boards on crate SN011
   uv run fwupgrade.py "CRC? *"  # upgrades all boards on any type of crate on the network
   uv run fwupgrade.py "* 1"  # upgrades all boards that have the board, crate or backplane serial number 1. Useful to get out of pickle a crate part number is not programmed
   uv run fwupgrade.py 37 55 --firmware uboot.bin  # specify a different firmware image


If you don't have "uv", you can manually activate a pychfpga virtual environment and run the command::
      
    source /path/to/pychfpga/venv/bin/activate
    ./fwupgrade.py 37 55


Case 2 - JTAG flashing

If the board has a very old uboot or no firmware at all, you can program it using the Vitis programming tool oved JTAG.

Note: The instructions below are for Ubuntu

Prerequisites

- Vitis must be installed
- The CRS board must be connected to the host computer using the micro-USB cable
- The CRS board must be powered on
- minicom must be installed


To program the flash, activate the Vitis environment, go to the CRS firmware image folder, and run the Vitis programming comamnd::

	source /tools/Xilinx/Vitis/2022.2/settings64.sh
	cd pychfpga/pychfpga/hardware/crs/arm_firmware
    program_flash -f uboot.bin -offset 0 -flash_type qspi-x8-dual_parallel -fsbl fsbl-t0-crs.elf -blank_check -verify


The ``-blank_check`` and ``-verify`` options can be omitted to accelerate the programming.

To check the programming:

1) Connect the ethernet port of the CRS to a network with a local DHCP sever.
2) Launch minicom on the serial TTY port::

	    minicom -D /dev/ttyUSB1

	 Press ``CTRL-A`` ``u`` to enable adding linefeeds to carriage returns

3) Power cycle the CRS
4) Check minicom ooutput. Should look like::

     	Xilinx Zynq MP First Stage Boot Loader
		Release 2022.1   Apr 29 2024  -  18:05:58
		PMU-FW is not running, certain applications may not be supported.
		...
		mDNS: Board crs_0026 registered successfully over mDNS at 10.10.10.203:7




Resetting the board
-------------------

xsct
rst -por


Notes
-----

 2024-10-23: Programming failed on CRS 011.

   - Launched Hardware manager on Vivado and could see ARM & FPGA, and monitor ARM temperatures. So cable & JTAG is good.
   - Launched Vitis. tested target: can see FPGA.
   - Compiled TCPIPE. Created boot.bin (design was for ZCU111, had to change FSBL etc.). programmed flash. had wrong flash type, fixed it. reprogrammed, worked, verify is OK but says programming failed on last line. Does ot boot properly on minicom.
   - Retry script with image in arm_firmware. Still error on final line, but otherwise programming seems ok now. Reboot. minicom shows normal boot. Can connect with pychfpga.
   - Not sure what fixed the problem: launching Hardware manager, or programming with Vitis.
