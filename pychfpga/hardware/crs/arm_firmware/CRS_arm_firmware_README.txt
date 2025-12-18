Flashing TCPipe ARM firmware on the CRS
---------------------------------------

Note: The instructions below are for Ubuntu

Prerequisites

- Vitis must be installed
- The CRS board must be connected to the host computer using the micro-USB cable
- The CRS board must be powered on
- minicom must be installed


To program the flash, activate the Vitis environment, go to the CRS firmware image folder, and run the Vitis programming comamnd::

	source /tools/Xilinx/Vitis/2022.2/settings64.sh
	cd pychfpga/pychfpga/hardware/crs/arm_firmware
    program_flash -f BOOT.bin -offset 0 -flash_type qspi-x8-dual_parallel -fsbl fsbl.elf -blank_check -verify


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

		-----TCPipe server v1.0 ------
		Build time:Jul  3 2024 19:37:31
		GIT version:1.5-3-g571c24-dirty
		Processing system DNA = 400000000170CFA814A041C5
		PS IIC: initializing hardware
		PS IIC0: Starting IIC initialization
		PS IIC0: running self-test
		PS IIC0: IIC initialization successful
		PS IIC1: Starting IIC initialization
		PS IIC1: running self-test
		PS IIC1: IIC initialization successful
		PS SPI0: initializing hardware
		PS SPI0: SPI initialization successful
		PS SPI1: initializing hardware
		PS SPI1: SPI initialization successful
		Reading Board information from EEPROM
		Setting I2C switch
		Reading EEPROM
		Decoding EEPROM
		Platform is crs_SN0026
		Board Manufacturing Date: Mon Jan  1 00:00:00 1996

		Initializing networking stack
		Ethernet MAC is EMACPS (10/100/1000 Mbps) (Processing System) at base address 0xFF0E0000
		XEmacPs detect_phy: PHY detected at address 12 with ID 0x2000.
		Start TI PHY autonegotiation (PHY address 12)
		TI PHY IO_MUX_CFG register =0x0C4F
		Waiting for PHY to complete autonegotiation (iteration #1).
		Waiting for PHY to complete autonegotiation (iteration #2).
		Waiting for PHY to complete autonegotiation (iteration #3).
		Waiting for PHY to complete autonegotiation (iteration #4).
		Waiting for PHY to complete autonegotiation (iteration #5).
		Waiting for PHY to complete autonegotiation (iteration #6).
		Waiting for PHY to complete autonegotiation (iteration #7).
		Waiting for PHY to complete autonegotiation (iteration #8).
		Waiting for PHY to complete autonegotiation (iteration #9).
		Waiting for PHY to complete autonegotiation (iteration #10).
		autonegotiation complete
		link speed for phy address 12: 1000
		rxringptr: 0x002003A0
		txringptr: 0x00200330
		rx_bdspace: 800000
		tx_bdspace: 810000
		xemacpsif_mac_filter_update: Multicast MAC address successfully added.
		netif: added interface te IP addr 0.0.0.0 netmask 0.0.0.0 gw 0.0.0.0
		netif: setting default interface te
		Getting IP address from DHCP
		netif: netmask of interface te set to 255.255.0.0
		netif: GW address of interface te set to 10.10.10.1
		netif_set_ipaddr: netif address being changed
		Got an IP address
		Board IP: 10.10.10.203
		Netmask : 255.255.0.0
		Gateway : 10.10.10.1
		Starting mDNS
		xemacpsif_mac_filter_update: Multicast MAC address successfully added.
		TCPipe server started @ 10.10.10.203:7
		Starting main loop
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
