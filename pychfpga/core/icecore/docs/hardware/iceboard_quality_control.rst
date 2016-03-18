IceBoard Quality Control
========================

The IceBoard is a complex board with a large number of components, including fine pitch Ball Grid Arrays (BGA) chips such as the FPGA (901 pins), the ARM processor, the DDR RAM chips etc. Manufacturing errors therefore show up from time to time, resulting in a board that is not functional or has partial functionalities, even after working closely with the manufacturing house in order to optimize the manufacturing process. Since the IceBoards are meant to be used in large arrays (the CHIME radiotelescope uses 128 Iceboards), the probability that the array contains a bad board is not negligible at all unless every board has been subjected to a rigorous Quality Control (QC) process.

This section describes various aspects of the IceBoard's QC process. This process :

- Identifying all the probable failure modes that a new board might encounter and identify the corresponding measurable effects
- Design Quality control process that test for all the identified effects. This consists of visual inspection, manual tests and automated tests performed at the manufacturer's premises
- Store all QC information and other board's life-cycle information in a database to ensure traceability of every boards that is sent in the field.


Design Failure Modes and Effect Analysis
----------------------------------------

The Design Failure Modes and Effect Analysis (DFMEA) is a methodology that is
commonly used to assess all the possible ways a design or product can fail and
to identify the effects those failures can have on the item functionnality.

We used a simplified version of the DFMEA methodology to analyze the
IceBoard's possible modes of failures. The process consisted in:

	1)  Reviewing the schematics and for each component, identify how that component can fail to fulfill its function after being freshly received from manufacturing. The failures can include:

	     a. Bad soldering
	     b. Components placed with the wrong orientation
	     c. Wrong component value or part number
	     d. Broken connector pin
	     e. Missing component (or component that was installed when it shouldn't)

	2)  Assessing the effects of each failure. One failure can have multiple
        effects at the same time. When effects cascaded in other effects, we
        attempted to identify the effect that is the most easily visible in
        the chain.


We have not assigned a severity level or probability of occurence to each
failure, as we assume any manufacturing failure will be discovered if they
exist, and any failure will cause the board not to be rejected until all its
failures are resolved.

The result of this DFMEA analysis is to provide the information needed in
order to design a quality control process that will ensure a failure detection
coveraga that is as wise as possible.

Note that The goal of the analysis was *not* to  evaluate the failure rate or
the Mean Time Between Failures (MTBF) of the Iceboard in operation, but only
to assess if boards fresh out of production are performing as expected.

Also note that the IceBoards are meant to be used un non-life-critical
applications. The Quality Control made here is nowhere near adequate for such
applications. Standard disclaimers to this effect apply.


Quality control process
-----------------------


Boundary scan
+++++++++++++

A Boundary Scan consists in using the JTAG serial interface available on many
digital chips in order to electrically check the connectivity between
digital components equipped with such an interface. The scan in performed by sending
test vectors that set the digital outputs of chips to known values and by
reading back the levels that have been received by the pins of the destination
chip.

Although the FPGA, ARM processors and FMC Mezzanine connectors are equipped
with JTAG connectors, the manufacturing and QCing process of the ICEBoard does
not include Boundary Scans. The reason for this are:


	1) The main two chips on board, the FPGA and ARM, are pretty much
       independent and share only a few lines. Testing those lines do not
       offer a significant coverage to make it worth it the effort in
       designing a boundary scan  set-up.

	2) The ARM pins mainly connects to DDR Memory, PHY interface and other
       peripherals, which do not support JTAG.

	3) The FPGA pins mainly connect to the FMC Mezzanines (which has a JTAG
       connection), but the Mezzanine we use connect those lines to ADCs and
       DACs that do not support the JTAG Boundary scan.


Test scripts
++++++++++++




Current QC scripts
	- Visual inspection (Non-specific components & scratches, pins)
	- Record PCB and assembly serial number
	- power-off resistance
	- power-on voltages, plus main input current and voltage
	- PLL programming
	- ARM Boot
	- Ethernet communication with ARM (using 1 ethernet port)
	- Write ARM SPI flash with serial number
	- Program FPGA
	- Read on-board Voltages, current and temperatures using ARM
	- test IceBoard QSFP BER
	- test backplane PCB BER
	- FPGA communication through SFP+/Ethernet
	- Power, identify (via I2C EEPROM)  & configure CHIME MGADC08 Mezzanines (PLL) over SPI
	- Check test patterns generated by CHIME ADC mezzanine

Updated QC script:
	- Visual Inspection

	    + ARM heat sink (and check for nearby components/tracks)
	    + Stiffner
	    + Check board flatness
	    + FPGA heat Sink (model, orientation, posts, shorts)
	    + SFP & QSFP cages
	    + PLL Heat sinks
	    + Obvious scratches & Missing components

	- Labeling
	    + record PCB and assembly serial number
	    + Check uniqueness of serial number
	- Power off tests:
		+ measure Resistances

	- Power supply check

	    + All specified LEDs turn ON (power LEDs) or OFF (fault LEDs).
	    + Measure input current and voltage
	    + Manually measure voltages on switchers (? Board is fried by now anyway)

	- PLL Programming

	    + program PLL1.
	    + Select Backplane clock. power cycle. Check LED
	    + Select Crystal clock. Power cycle. Check LED
	    + Connect SMA 10 MHz source. Select SMA. Power Cycle. Check LED
	    + Repeat for PLL2

	- ARM Boot & Networking

	    + Check Boot mode switches
	    + Power on. Check for specified Blinking pattern
	    + Find board on mDNS (i.e Linux has booted, RAM works, One ethernet port works)
	    + Ping ARM on Both interfaces
	    + Check that specified Ethernet LEDs are on or blinking


	- ARM Peripherals
	    + Connect to ARM with Tuber only (no FPGA)
	    +  Obtain Both Eth MAC and IP address
	    + Write and readback IPMI in SPI Flash & EEPROM
	    + Read all current, voltage & temperatures through the ARM (SMPS Bus). Check with limits.
	    + Write values to every IO Extenders (GPIO Bus). Have ARM pulse GPIO_RST. All IO Extenders should have returned to default values.
	    + Set GPIO LEDs via ARM (GPIO Bus). Check is specified pattern is obtained
	    + Set GPIO DIP Switches to pattern. Readback. Check if specified values are obtained.
	    + Read and check SFP PRSNT & FAULT (SFP Bus)
	    + Read SFP EEPROM via ARM (SFP Bus)
	    + Check QSFP GPIO Lines (GPIO Bus): PRSNT, RESET, SELECT.
	    + Read QSFP EEPROM via ARM (QSFP Bus). Set LP Mode readback over EEPROM. (Set INTERRUPT over I2C interface?)
	    + Set FPGA MMI register. Press ARM POR reset switch. ARM should reboot with specified sequence of LEDs. Check if FPGA values are reset to defaults.
	    + Press ARM MR (Master Reset). ARM should reboot with specified sequence of LEDs.
	    + Press MPROG (FPGA PROG) switch. DONE LED must turn OFF
	    + Press PD (power down) switch. All specified LEDs must turn OFF

	- FPGA Programming & Networking

	    + Program FPGA. Check that DONE LED is on and that FPGA LEDs are blinking.
	    + Initialize communication with FPGA over SFP+ link.

	- FPGA Peripherals

	    + Read all current, voltage & temperatures through the FPGA (SMPS Bus).
	    + Read SFP EEPROM via FPGA (SFP Bus)
	    + Read QSFP EEPROM via FPGA (QSFP Bus)
	    + Read/write GPIO via FPGA (GPIO Bus)
	    + Set FPGA to send known pattern on SMAs. Check for specified pattern with oscilloscope.
	    + Read FPGA JTAG info (temperature) via ARM. Read many times to confirm reliability.
	    + Read Frequency counter for all system frequencies (except Mezz) and check with targets.
		+ Test Motherboard QSFP BER


	- Mezzanine tests

		+ Read Mezzanine EEPROM via FPGA (FMC Bus)
		+ Read Mezzanine EEPROM via ARM (FMC Bus)
		+ Test Mezzanine Power on/off. Check LEDs.
		+ Test Mezzanine PRSNT, POWER GOOD
		+ Read Mezz reference frequencies (2x 10 MHz, GBTCLK)
	    + Check that SYNC stops clock
	    + Check SPI Communications with Mezzanine (read/write ADC)
	    + Check that ADC_RESET resets the ADC and IO Expanders
	    + Check if PLL_LOCK line is set/unset when PLL is lock/unlocked

	- Backplane tests (single-slot backplane)
		+ Test Backplane QSFP BER
		+ Test Backplane PCB BER
		+ Read backplane EEPROM via FPGA (BP Bus)
		+ Read backplane EEPROM via ARM (BP Bus)
		+ Backplane ARM MR test
		+ Backplane MPROG test
		+ Backplane PD test
		+
	- Not tested

		+ Full load test (test temperature, stresses power)
	    + Front panel UART port
	    + Back panel UART Port
	    + ARM JTAG port
	    + Full DDR Memory tests
	    + ARM USB Ports
	    + ARM SATA Ports
	    + ARM PCIe link to FPGA
	    + Mezzanine JTAG Port
	    + ARM IRQ Line
	    + GPIO IRQ Line
	    + FPGA JTAG Lines
	    + ARM extra backplane I2C links
	    +