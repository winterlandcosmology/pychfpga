#!/usr/bin/python

"""
hmc7044: Interface to the Analog Device HMC7044 PLL.

"""

import time
import os
import logging

class hmc7044(object):
    """
    Implements the interface to the Analog Device HMC7044 PLL.
    """

    # RESET_REG = 0x0000
    # REQUEST_REG = 0x0001

    # regs = dict(
    # # field_name = (addr, high_bit, low_bit, default)
    # glbl_cfg1_swrst = (RESET_REG, 0, 0, 0x00),

    # glbl_cfg1_sleep = (REQUEST_REG, 0, 0,  0x0),
    # glbl_cfg1_restart = (REQUEST_REG, 1, 1,  0x0),
    # sysr_cfg1_pulsor_req = (REQUEST_REG, 2, 2,  0x0),
    # pll1_cfg1_forceholdover = (REQUEST_REG, 4, 4,  0x0),
    # glbl_cfg1_perf_pllvco = (REQUEST_REG, 5, 5,  0x0),
    # dist_cfg1_perf_floor = (REQUEST_REG, 6, 6,  0x1),
    # sysr_cfg1_reseed_req = (REQUEST_REG, 7, 7,  0x0),

    # sysr_cfg1_rev=(0x0002, 0, 0, 0x0),
    # sysr_cfg1_slipN_req=(0x0002, 1, 1, 0x0),
    # pll2_cfg1_autotune_trig=(0x0002, 2, 2, 0x0),

    # glbl_cfg1_ena_pll1=(0x0003, 0, 0, 0x1),
    # glbl_cfg1_ena_pll2=(0x0003, 1, 1, 0x1),
    # glbl_cfg1_ena_sysr=(0x0003, 2, 2, 0x0),
    # glbl_cfg2_ena_vcos=(0x0003, 4, 3, 0x1),
    # glbl_cfg1_ena_sysri=(0x0003, 5, 5, 0x0),

    # glbl_cfg7_ena_clkgr=(0x0004, 6, 0, 0x7F),

    # glbl_cfg4_ena_rpath=(0x0005, 3, 0, 0xF),
    # dist_cfg1_refbuf0_as_rfsync=(0x0005, 4, 4, 0x0),
    # dist_cfg1_refbuf1_as_extvco=(0x0005, 5, 5, 0x0),
    # pll2_cfg2_syncpin_modesel=(0x0005, 7, 6, 0x0),

    # glbl_cfg1_clear_alarms=(0x0006, 0, 0, 0x0),
    # )

    def __init__(self, spi_interface, spi_port=0, verbose=0):
        """
        """
        self.log = logging.getLogger(__name__)
        self.spi = spi_interface
        self.spi_port = spi_port
        self.regs = {}  # image of latest values written

    def init(self, fref=10e6, fosc=50e6, fvco=3200e6, fout={}, filename=None, check=1, input_sel=None, input_priorities=[2,1,0,3]):
        """Initializes the PLL.

        Parameters:

            fref (float): reference clock frequency, in Hz. The 50 MHz VCXO of the first PLL will be locked to this reference.

            fosc (float): Frequency of the VCXO, in Hz.

            fvco (float): Frequency of the PLL2's VCO, in Hz.

            fout (dict): Frequency of each of the the PLL2's outputs. These should be a submultiples of fvco.

            filename (str): file from which to read a PLL config file. If specified, all otherPLL  parameters are ignored.

            check (bool): if True, the registers will be read back after every write to confirm their values

        """

        # Reset the PLL
        # Note: resetting the PLL perturbs the frequency of the 50 MHz VCXO,
        # which can cause disruptions on the Ethernet link.
        # self.reset()

        if filename:
            regs = self.load_config_file(filename)
            self.write_regs(regs)

        self.check = check

        # Initialize basic registers not handled below
        self.init_registers()

        # print(f'reg[0x0001]=0x{self.read_reg(0x1):02X}')
        # Set PLL1 clock inputs
        enable_list = [input_sel == i or (input_sel is None and i in input_priorities) for i in range(4)]
        self.set_input('CLKIN0', enable=enable_list[0], term=True) # Ethernet recovered clock
        self.set_input('CLKIN1', enable=enable_list[1], term=True) # Backplane 10 MHz reference
        self.set_input('CLKIN2', enable=enable_list[2], term=True) # SMA 10 MHz reference
        self.set_input('CLKIN3', enable=enable_list[3], term=True) # Recovered clock from FPGA
        self.set_input('OSCIN', enable=True, term=True) # Oscillator
        # print(f'reg[0x0001]=0x{self.read_reg(0x1):02X}')

        # Program PLL2. Select the VCO range (high or low). Then
        # program the dividers (R2, N2, and reference doubler).
        self.set_pll2(f_in=fosc, f_out=fvco)
        self.set_oscout()

        # Program PLL1. Set the lock detect timer threshold based
        # on the PLL1 BW of the user system. Set the LCM, R1, and
        # N1 divider setpoints. Enable the reference and VCXO
        # input buffer terminations.
        self.set_pll1(f_in=fref, f_out=fosc, input_sel=input_sel, input_priorities=input_priorities)
        # print(f'reg[0x0001]=0x{self.read_reg(0x1):02X}')

        # Program the SYSREF timer. Set the divide ratio (a
        # submultiple of the lower output channel frequency). Set the
        # pulse generator mode configuration, for example, selecting
        # level sensitive option and the number of pulses desired.
        self.set_sysref_timer()

        # Program the output channels. Set the output buffer modes
        # (for example, LVPECL, CML, and LVDS). Set the divide
        # ratio, channel start-up mode, coarse/analog delays, and
        # performance modes.
        for output, freq in fout.items():
            self.set_output(output, f_vco=fvco, f_out=freq)

        # print(f'reg[0x0001]=0x{self.read_reg(0x1):02X}')

        # Wait until the VCO peak detector loop has stabilized, 10 ms after set_pll2
        time.sleep(0.01)

        # Issue a software restart to reset the system and initiate
        # calibration. Toggle the restart dividers/FSMs bit to 1 and
        # then back to 0.
        self.restart()
        # print(f'reg[0x0001]=0x{self.read_reg(0x1):02X}')

        # Wait for PLL2 to be locked (takes ~50 μs in typical configurations).

        # Confirm that PLL2 is locked by checking the PLL2 lock detect bit.

        # Send a sync request via the SPI (set the reseed request bit) to align the divider phases and send any initial pulse generator stream.

        # Wait 6 SYSREF periods (6 × SYSREF Timer[11:0]) to allow the outputs to phase appropriately (takes ~3 μs in typical configurations).

        # Confirm that the outputs have all reached their phases by checking that the clock outputs phases status bit = 1.

        # Wait for PLL1 to lock. This takes ~50 ms for a 100 Hz BW (from Step 11).

        # When all JESD204B slaves are powered and ready, send a pulse
        # generator request to send out a pulse generator chain on any SYSREF
        # channels programmed for pulse generator mode.

        # Return which input is used as a reference. Returns None if no input is active.
        return self.get_active_clkin()

    def write_reg(self, reg, val, mask = 0xFF, timeout=10):
        """ Writes the register `reg` with 8-bit value `val`

        Parameters:

            reg (int): 13-bit address of register to write

            val (int): 8-bit value to write to the register

            mask (int): bit mask, were bit position with a '0' are not modified
        """

        reg &= 0x1FFF  # limit register address to 13 bit

        if mask != 0xFF:
            if reg not in self.regs:
                raise RuntimeError('Cannot use masks if the register has not been initialized with a full 8-bit value')
            val = self.regs[reg] & (~mask) | (val & mask)
        self.regs[reg] = val
        spi_data = bytes([reg >> 8, reg & 0xFF, val]) # r/w=0, W1=0, W0=0
        # print(f'write_reg: sending {len(spi_data)} bytes')
        self.spi.write_read(self.spi_port, spi_data, read_length=0, timeout=timeout)
        if self.check:
            read_val = self.read_reg(reg, timeout=timeout)
            if read_val != val:
                self.log.warning(f'Warning: Readback on register 0x{reg:04x} is 0x{read_val:02x} instead of 0x{val:02x}')

    def write_regs(self, regs):
        """Write a list of (register, value) tuples to the PLL.

        Parameters:

            regs (list or dict): list of ``(register_address, value)`` tuples or
                dict of ``{register_address:value, ... }`` , where
                ``register_address`` and ``value`` are 13-bit and 8-bit values
                respectively.
        """
        if isinstance(regs, dict):
            regs = regs.items()

        for (reg, val) in regs:
            # print(f'Writing Reg {reg:04X} with 0x{val:02X}')
            self.write_reg(reg, val)
            # time.sleep(0.1)

    def read_reg(self, reg, timeout=None):
        """ Read the8-bit value from register `reg`.

        Parameters:

            reg (int): 13-bit address of register to read

        Returns:

            (int): 8-bit value that was read from the register
        """

        reg &= 0x1FFF  # limit register address to 13 bit
        spi_data = bytes([(reg >> 8) | 0x80, reg & 0xFF]) # r/w=0, W1=0, W0=0
        val = self.spi.write_read(self.spi_port, spi_data, read_length=1, timeout=timeout)
        return val[0]


    def reset(self):
        """ Reset the PLL. Clears all registers
        """
        self.write_reg(self.RESET_REG, 1)


    def restart(self):
        """ resatrt the dividers and FSMs (including VCO tuning). Does not affect register contents.
        """
        self.write_reg(0x0001, 1<<1, mask=1<<1)  # set restart bit
        self.write_reg(0x0001, 0<<1, mask=1<<1)  # clear restart bit

    def load_config_file(self, filename="../crs/CRS_CHORD_3000MHz.py"):
        """ Load HMC7044 configuration file as saved by the Analog Device HMC7044 Configuration GUI software.

        The configuration file is a python script with a series of write commands (e.g. ``dut.write(0x6, 0x0)``) for each register from the first to the last.
        The same register address is not repeated, and the writes are not made in a particular initialization sequence.
        Just writing the content of the registers to the PLL does not guarantee proper initialization.

        Returns:
            a list containing the (register_address, values) found in the file.
        """

        fullpath = os.path.join(os.path.dirname(__file__), filename)
        regs = []
        with open(fullpath, 'r') as f:
            for line in f.readlines():
                if line.startswith('dut.write'):
                    reg, val = eval(line[9:])
                    regs.append((reg, val))
        return regs


    def init_registers(self,
        sync_mode=1,
        clkin1_as_vco=0,
        clkin0_as_rfsync=0,
        input_enable=0b1111,
        disable_sync_at_lock=0,
        rf_reseeder_enable=1,
        vco_selection=1,
        sysref_timer_enable=1,
        pll1_enable=1,
        pll2_enable=1
       ):
        """ Initialize global registers and reserved control registers to the value recommended by Analog Device.


        The global control register are mainly written with default values to
        initialize the local cache and use masked writes. The registers that
        are fully written by the other subsystem initializations are not
        included here.

        The value of the reserved registers values are taken from the ADI HMC7044 Evaluation software, and
        matches the default reset values listed in the datasheet Rev C except
        for the listed exceptions.

        Parameters:

            sync_mode (int): Selects SYNC Pin mode config with respect to PLL2 (Reg 0x0005 [7:6]):

                - 0: Disabled
                - 1: Rising edge on SYNC is carried through PLL2. Useful for multichip synchronization
                - 2: Pulse generator: request a pulse generator stream from any channel configured for dynamic startup.
                - 3: Causes SYNC if alarm exists, otherwise causes pulse generator

            clkin1_as_vco (int): If 1, clkin1 is used for external VCO(Reg 0x0005 [5])

            clkin0_as_rfsync (int): If 1, clkin0 is used for external RF sync (Reg 0x0005 [4])

            input_enable (int): 4-bit PLL1 Reference path enables. Bits 0-3 enables the CLKIN0-3 input paths.

            disable_sync_at_lock (int): If 1, PLL2 will not send a sync event up to N2 when lock is acheived  (Reg 0x0009[0])

            rf_reseeder_enable (int): If 1, the output stages can reset the output dividers. Must be set fo SYNC to work.

            vco_selection (int): Select the VCO. 0: disabled (use external); 1: high freq (3.2 GHz), 2: low freq

            sysref_timer_enable (int): Enables SYSREF timer. Required for SYNC to work.

            pll1_enable (int): If 1, PLL1 is enabled

            pll2_enable (int): If 1, PLL2 is enabled


        """

        regs = [

            # Global registers
            # --------------
            # glbl_cfg1_sleep[0:0] = 0x0
            # glbl_cfg1_restart[1:1] = 0x0
            # sysr_cfg1_pulsor_req[2:2] = 0x0
            # pll1_cfg1_forceholdover[4:4] = 0x0
            # glbl_cfg1_perf_pllvco[5:5] = 0x0
            # dist_cfg1_perf_floor[6:6] = 0x1
            # sysr_cfg1_reseed_req[7:7] = 0x0
            (0x1, 0x40),
            # sysr_cfg1_rev[0:0] = 0x0
            # sysr_cfg1_slipN_req[1:1] = 0x0
            # pll2_cfg1_autotune_trig[2:2] = 0x0
            (0x2, 0x0),
            # glbl_cfg1_ena_pll1[0:0] = 0x1
            # glbl_cfg1_ena_pll2[1:1] = 0x1
            # glbl_cfg1_ena_sysr[2:2] = 0x0
            # glbl_cfg2_ena_vcos[4:3] = 0x1
            # glbl_cfg1_ena_sysri[5:5] = 0x0
            # Reg 0x0003 bits are also set by set_pll1(), set_pll2() and set_sysref_timer().
            (0x3, (rf_reseeder_enable << 1) | (vco_selection << 3) | (sysref_timer_enable << 2) | (pll2_enable << 1) | (pll1_enable << 0)),
            # glbl_cfg7_ena_clkgr[6:0] = 0x7F
            (0x4, 0x7F),
            # glbl_cfg4_ena_rpath[3:0] = 0xF
            # dist_cfg1_refbuf0_as_rfsync[4:4] = 0x0
            # dist_cfg1_refbuf1_as_extvco[5:5] = 0x0
            # pll2_cfg2_syncpin_modesel[7:6] = 0x0
            (0x5, (sync_mode << 6) | (clkin1_as_vco << 5) | (clkin0_as_rfsync << 4) | input_enable), # SYNC Pin mode = 01 (rising edge carried through PLL2); CLKIN0-3 input path enabled
            # glbl_cfg1_clear_alarms[0:0] = 0x0
            (0x6, 0x0),
            # glbl_reserved[0:0] = 0x0
            (0x7, 0x0),
            # glbl_cfg1_dis_pll2_syncatlock[0:0] = 0x0
            (0x9, disable_sync_at_lock),

            # GPI/GPOs/SDATA
            # --------------
            # glbl_cfg5_gpi1_en[0:0] = 0x0
            # glbl_cfg5_gpi1_sel[4:1] = 0x0
            (0x46, 0x0),
            # glbl_cfg5_gpi2_en[0:0] = 0x0
            # glbl_cfg5_gpi2_sel[4:1] = 0x0
            (0x47, 0x0),
            # glbl_cfg5_gpi3_en[0:0] = 0x0
            # glbl_cfg5_gpi3_sel[4:1] = 0x4
            (0x48, 0x0),
            # glbl_cfg5_gpi4_en[0:0] = 0x0
            # glbl_cfg5_gpi4_sel[4:1] = 0x8
            (0x49, 0x0),


            # glbl_cfg8_gpo1_en[0:0] = 0x1
            # glbl_cfg8_gpo1_mode[1:1] = 0x1
            # glbl_cfg8_gpo1_sel[7:2] = 0x7
            (0x50, (0b000111 << 2) | 0b11),
            # glbl_cfg8_gpo2_en[0:0] = 0x1
            # glbl_cfg8_gpo2_mode[1:1] = 0x1
            # glbl_cfg8_gpo2_sel[7:2] = 0xA
            (0x51, (0b001010 << 2) | 0b11),
            # glbl_cfg8_gpo3_en[0:0] = 0x1
            # glbl_cfg8_gpo3_mode[1:1] = 0x1
            # glbl_cfg8_gpo3_sel[7:2] = 0xD
            (0x52, (0b001110 << 2) | 0b11),
            # glbl_cfg8_gpo4_en[0:0] = 0x1
            # glbl_cfg8_gpo4_mode[1:1] = 0x1
            # glbl_cfg8_gpo4_sel[7:2] = 0x0
            (0x53, (0b001100 << 2) | 0b11),


            # glbl_cfg2_sdio_en[0:0] = 0x1
            # glbl_cfg2_sdio_mode[1:1] = 0x1
            (0x54, 0x3),

            # Clock distribution Network
            # --------------------------
            (0x64, 0x0),
            (0x65, 0x0),

            # Alarms
            # --------
            (0x70, 0x0), # PLL1 alarm control register
            (0x71, 0x10), # Alarm mask control: 4: sync req, 3:PLL1/2 lock detect, 2: clk out phase status, 1: sysref sync status, 0: pll2 lock detect

            # (0x7B, 0x1), # Alarm readback register (read only)
            # (0x7C, 0x1F), # PLL1 alarm readback (read only)
            # (0x7D, 0x13), # Alarm readback (read only)
            # (0x7E, 0x7F), # Latched alarm readback (read only)

            # Reserved values recommended by Analog Devices. See Table 74 of datasheet Rev C.
            (0x96, 0x0),
            (0x97, 0x0),
            (0x98, 0x0),
            (0x99, 0x0),
            (0x9A, 0x0),
            (0x9B, 0xAA),
            (0x9C, 0xAA),
            (0x9D, 0xAA),
            (0x9E, 0xAA),
            (0x9F, 0x4D),  # Clock output driver low power setting (for optimum performance, set to 0x4D instead of default value). reset default = 0x55
            (0xA0, 0xDF),  # Clock output driver high power setting (for optimum performance, set to 0xDF instead of default value). reset default = 0x56
            (0xA1, 0x97),
            (0xA2, 0x3),
            (0xA3, 0x0),
            (0xA4, 0x0),
            (0xA5, 0x6),  # Clock output driver high power setting (for optimum performance, set to 0xDF instead of default value). reset default = 0x00
            (0xA6, 0x1C),
            (0xA7, 0x0),
            (0xA8, 0x6),  # Clock output driver high power setting (for optimum performance, set to 0xDF instead of default value). reset default = 0x22
            (0xA9, 0x0),
            (0xAB, 0x0),
            (0xAC, 0x20),
            (0xAD, 0x0),
            (0xAE, 0x8),
            (0xAF, 0x50),
            (0xB0, 0x4),  # Clock output driver high power setting (for optimum performance, set to 0xDF instead of default value). Reset default = 0x09
            (0xB1, 0xD),
            (0xB2, 0x0),
            (0xB3, 0x0),
            (0xB5, 0x0),
            (0xB6, 0x0),
            (0xB7, 0x0),
            (0xB8, 0x0),
            ]
        self.write_regs(regs)


    PLL1_INPUT_NAMES = {
        'CLKIN0': 0,
        'CLKIN1': 1,
        'CLKIN2': 2,
        'CLKIN3': 3,
        'OSCIN': 4
    }

    def set_input(self, input_number, enable=1, term=1, ac=1, lvpecl=0, hi_z=0, prescaler=1):
        """ Set one of the PLL1 clock input (either CLKIN0,1,2,3 or OSCIN).

        Parameters:

            input number (int or str): input to configure.

                0: CLKIN0
                1: CLKIN1
                2: CLKIN2
                3: CLKIN3
                4: OSCIN

        """
        if input_number in self.PLL1_INPUT_NAMES:
            input_number = self.PLL1_INPUT_NAMES[input_number]

        regs = {}

        self.log.debug(f'{self!r}: Reference input {input_number} enable = {enable}')
        regs[0x000A + input_number] = (hi_z << 4) | (lvpecl << 3) | (ac << 2) | (term << 1) | enable
        regs[0x001C + input_number] = prescaler

        self.write_regs(regs)

    def set_pll1(self, enable=True, input_priorities=[2,1,0,3], los_validation=7, input_sel=None, f_in=10e6, f_out=50e6, CP_gain=5, PFD_pol=0, restart=False):
        """ Configure PLL1

        Parameters:

            enable (bool): PLL2 is enabled when True

            input_priorities (list): list of 4 input numbers to be autoselected in order of priority. Is ignored if `input_sel` is provided.

            input_sel (int): Clock reference input (0-3) to use for PLL1. Overrides autoselection.
                If None, autoselection is enabled using the `input_priorities` list.

            los_validation (int): Loss of Signal validation time:
                0: None
                1: 2 cycles
                2: 4 cycles
                3: 8 cycles
                4: 16 cycles
                5: 32 cycles
                6: 64 cycles
                7: 128 cycles

            f_in (float): PLL1's input reference frequency, in Hz.

            f_out (float): VCO output frequency. Must be between 2150-3550 MHz, but only 2400-3200 MHz is guaranteed.

            CP_gain (int): PLL1 charge pump current as an innteger between 0 and 15, which sets current in multiples of 160 uA.

            PFD_pol (int) : PFD polarity: 0=positive, 1 = negative

            restart (bool): if True, dividers & FSMs are restarted.


        This is a simplistic implementation, where R2 is fixed at 1, and the frequency doubler is disabled.

        """

        PFD_up_en = 1
        PFD_down_en = 1
        PFD_up_force = 0
        PFD_down_force = 0

        R1 = 1
        if R1 != int(R1):
            raise RuntimeError(f'PLL1 R1={R1} is not an integer')

        N1 = f_out / (f_in / R1)
        if N1 != int(N1):
            raise RuntimeError(f'PLL1 N1={N1} is not an integer')
        N1 = int(N1)


        regs = {}
        # pll1_cfg2_rprior1[1:0] = 0x2
        # pll1_cfg2_rprior2[3:2] = 0x1
        # pll1_cfg2_rprior3[5:4] = 0x3
        # pll1_cfg2_rprior4[7:6] = 0x0
        regs[0x0014] = sum(pri<<(2*i) for i,pri in enumerate(input_priorities))


        # pll1_cfg3_los_valtime_sel[2:0] = 0x3
        regs[0x0015] = los_validation

        # pll1_cfg2_holdover_exitcrit[1:0] = 0x0
        # pll1_cfg2_holdover_exitactn[3:2] = 0x1
        holdover_exit_action = 3 # 0: reset dividers, 1,2: Do nothing, 3: DAC assist
        holdover_exit_criteria = 1 # 0: Exit when LOS gone, 1:exit when phase error = 0, 2: Exit immediately
        regs[0x0016] = (holdover_exit_action << 2) | (holdover_exit_criteria)

        # pll1_cfg7_hodac_offsetval[6:0] = 0x0
        regs[0x17] = 0x0  # not parametrized yet

        # pll1_cfg2_hoadc_bw_reduction[1:0] = 0x0
        # pll1_cfg1_hodac_force_quickmode[2:2] = 0x1
        # pll1_cfg1_hodac_dis_avg_track[3:3] = 0x0
        regs[0x18] = 0x4 # not parametrized yet

        # pll1_cfg1_los_uses_vcxodiv[0:0] = 0x0
        # pll1_cfg1_los_bypass_lcmdiv[1:1] = 0x0
        regs[0x19] = 0x0 # not parametrized yet

        # pll1_cfg4_cpi[3:0] = 0x5
        regs[0x001A] = CP_gain

        # pll1_cfg1_pfd_invert[0:0] = 0x0
        # pll1_cfg1_cppulldn[1:1] = 0x0
        # pll1_cfg1_cppullup[2:2] = 0x0
        # pll1_cfg1_cpendn[3:3] = 0x1
        # pll1_cfg1_cpenup[4:4] = 0x1
        regs[0x001B]= (PFD_up_en << 4) | (PFD_down_en << 3) | (PFD_up_force << 2) | (PFD_down_force << 1) | PFD_pol

        # pll1_cfg8_los_div_setpt_r0[7:0] = 0x1
        regs[0x1C] = 0x1  # not parametrized yet

        # pll1_cfg8_los_div_setpt_r1[7:0] = 0x1
        regs[0x1D] = 0x1  # not parametrized yet

        # pll1_cfg8_los_div_setpt_r2[7:0] = 0x1
        regs[0x1E] = 0x1  # not parametrized yet

        # pll1_cfg8_los_div_setpt_r3[7:0] = 0x1
        regs[0x1F] = 0x1  # not parametrized yet

        # pll1_cfg8_los_div_setpt_vcxo[7:0] = 0x5
        regs[0x20] = 0x5  # not parametrized yet

        # pll1_cfg16_refdivrat_lsb[7:0] = 0x1
        regs[0x0021] = R1 & 0xFF

        # pll1_cfg16_refdivrat_msb[7:0] = 0x0
        regs[0x0022] = (R1 >> 8) & 0xFF

        # pll1_cfg16_fbdivrat_lsb[7:0] = 0x5
        regs[0x0026] = N1 & 0xFF

        # pll1_cfg16_fbdivrat_msb[7:0] = 0x0
        regs[0x0027] = (N1 >> 8) & 0xFF

        # pll1_cfg5_lkdtimersetpt[4:0] = 0xF
        # pll1_cfg1_use_slip_for_lkdrst[5:5] = 0x0
        regs[0x28] = 0xF  # not parametrized yet

        # pll1_cfg1_automode[0:0] = 0x1
        # pll1_cfg1_autorevertive[1:1] = 0x0
        # pll1_cfg1_holdover_uses_dac[2:2] = 0x1
        # pll1_cfg2_manclksel[4:3] = 0x0
        # pll1_cfg1_byp_debouncer[5:5] = 0x0
        # 0x0029 : PLL1 Reference switching control
        # 5: bypass debouncer
        # 4:3: Manual switching input number
        # 2: holdover uses DAC
        # 1: Autorevertive ref switching
        # 0: Automode reference switching
        bypass_debouncer = 0
        manual_ref_input = input_priorities[0] if input_sel is None else input_sel
        holdover_uses_dac = 1
        auto_ref_revert = 0
        auto_ref_switching = 1 if input_sel is None else 0
        regs[0x0029]= (
            (bypass_debouncer << 5) |
            (manual_ref_input << 3) |
            (holdover_uses_dac << 2) |
            (auto_ref_revert << 1) |
            (auto_ref_switching << 0))

        # pll1_hoff_timer_setpoint[7:0] = 0x0
        regs[0x2A] = 0x0  # not parametrized yet


        # Global enable control register
        self.write_reg(0x0003, enable, mask=0b00000001)  # enable PLL1

        self.write_regs(regs)

        if restart:
            # Start autotune
            self.restart()

    def set_pll2(self, enable=True, f_in=50e6, f_out=3000e6, CP_gain=8, PFD_pol=0, restart=False, vco_sel=None):
        """ Configures PLL2

        Parameters:

            enable (bool): PLL2 is enabled when True

            f_in (float): PLL2's input reference frequency, in Hz.

            f_out (float): VCO output frequency. Must be between 2150-3550 MHz, but only 2400-3200 MHz is guaranteed.

            CP_gain (int): PLL2 charge pump current as an innteger between 0 and 15, which sets current in multiples of 160 uA.

            PFD_pol (int) : PFD polarity: 0=positive, 1 = negative

            restart (bool): if True, dividers & FSMs are restarted. Needed if we need to immediatly start the VCO autoruning.

            vco_sel (int): Selects the VCO to use. If None, the proper internal VCO will be automatically selected based on f_out:

                0: external
                1: high (2650 - 3550 MHz)
                2: low (2150 - 2880 MHz)

        This is a simplistic implementation, where R2 is fixed at 1, and the frequency doubler is disabled.

        """

        if f_out < 2150e6:
            raise ValueError('VCO frequenct too low. Must be >2150 MHz')
        if f_out > 3550e6:
            raise ValueError('VCO frequenct too high. Must be <3550 MHz')
        if f_out < 2400e6 or f_out > 3200e6:
            self.log.warning('VCO frequency in datasheet range but is outside the guaranteed 2400-3200 MHz range')

        if vco_sel is None:
            vco_sel = 2 if f_out < 2800e6 else 1  # 0=external, 1= high, 2 = low

        R2 = 1

        if R2 != int(R2):
            raise RuntimeError(f'PLL2 R2={R2} is not an integer')
        if  1 > R2 > 65535:
            raise RuntimeError(f'PLL2 R2={R2} is out of range (1-65535)')

        doubler = 0

        N2 = f_out / (f_in / R2 / (2 if doubler else 1))
        if N2 != int(N2):
            raise RuntimeError(f'PLL2 N2={N2} is not an integer')
        N2 = int(N2)
        if  1 > N2 > 4095:
            raise RuntimeError(f'PLL2 N2={N2} is out of range (1-4095)')

        PFD_up_en = 1
        PFD_down_en = 1
        PFD_up_force = 0
        PFD_down_force = 0

        regs = {}
        # pll2_reserved[7:0] = 0x1
        regs[0x31] = 0x1

        # pll2_cfg1_rpath_x2_bypass[0:0] = 0x1
        regs[0x32]= 1 if not doubler else 0

        # pll2_rdiv_cfg12_divratio_lsb[7:0] = 0x1
        regs[0x33]= R2 & 0xff

        # pll2_rdiv_cfg12_divratio_msb[3:0] = 0x0
        regs[0x34]= (R2 >> 8) & 0x0F

        # pll2_vdiv_cfg16_divratio_lsb[7:0] = 0x3C
        regs[0x35]= N2 & 0xFF

        # pll2_vdiv_cfg16_divratio_msb[7:0] = 0x0
        regs[0x36]= (N2 >> 8) & 0xFF

        # pll2_cfg4_cp_gain[3:0] = 0x1
        regs[0x37]= CP_gain

        # pll2_pfd_cfg1_invert[0:0] = 0x0
        # pll2_pfd_cfg1_force_dn[1:1] = 0x0
        # pll2_pfd_cfg1_force_up[2:2] = 0x0
        # pll2_pfd_cfg1_dn_en[3:3] = 0x1
        # pll2_pfd_cfg1_up_en[4:4] = 0x1
        regs[0x38]= (PFD_up_en << 4) | (PFD_down_en << 3) | (PFD_up_force << 2) | (PFD_down_force << 1) | PFD_pol

        # pll2_reserved[7:0] = 0x1
        # regs[0x3C] = 0x0 # is not present in the software tool output

        # Global enable control register
        # Program VCO range and enable PLL2
        self.write_reg(0x0003, (vco_sel<<3) | (enable << 1), mask=0b00011010)

        # configure R2, N2 etc.
        self.write_regs(regs)


        # Global request and mode register
        # Set PLL2 VCO autotune
        # self.write_reg(0x0002, 0<<2, mask=1<<2)  # clear autotune trigger
        self.write_reg(0x0002, 1<<2, mask=1<<2)  # set autotune trigger


        if restart:
            # Start autotune
            self.restart()

    def set_output(self,
                   output_number,
                   enable=1,
                   hi_perf=1,
                   sync_enable=1,
                   slip_enable=1,
                   startup_mode=0,
                   multislip_enable=0,
                   divider=None,
                   f_out=None,
                   f_vco=None,
                   analog_delay=0,
                   digital_delay=0,
                   multislip_delay=0,
                   output_sel=0,
                   mute=0,
                   dynamic_driver=0,
                   driver_mode=2,
                   driver_impedance=1):
        """ Set one of the outputs (CLKOUT0-13).

        Parameters:

            output_number (int): output to configure (0 to 13).

            enable (int): Enables the channel

            hi_perf (int): High performance mode. 0 or 1. Adjusts the divider and
                buffer bias to improve swing/phase noise at the expense of
                power.

            sync_enable (int): Susceptible to SYNC event. 0 or 1.  The channel can process a
                SYNC event to reset its phase.

            slip_enable (int):


            multislip_enable (int):

            startup_mode (int): Configures the channel to normal mode with
                asynchronous startup, or to a pulse generator mode with
                dynamic start-up. Note that this must be set to asynchronous
                mode if the channel is unused.

                - 0: Asynchronous
                - 1: reserved
                - 2: reserved
                - 3: Dynamic

            divider (int or float): 12-bit channel divider setpoint LSB. The
                divider supports even divide ratios from 2 to 4094. The
                supported odd divide ratios are 1, 3, and 5. All even and odd
                divide ratios have 50.0% duty cycle. `divider` can be a float,
                but an arror will be raised if it has a fractional part. Set
                to `None` if `f_out` and `f_vco` are specified instead.

            f_out (float); desired output frequency in Hz. Used to compute
                `divide`. `divide` should not be specified. Requires `f_vco`
                to be specified.

            f_vco (float): frequency of the PLL2 VCO in Hz. Required only if `f_out` is specified.

            analog_delay (int): 24 fine delay steps. Step size = 25 ps. Values
                greater than 23 have no effect on analog delay.

            digital_delay (int): 17 coarse delay steps. Step size = 1/2 VCO
                cycle. This flip flop (FF)-based digital delay does not
                increase noise level at the expense of power. Values greater
                than 17 have no effect on coarse delay.

            multislip_delay (int): 12-bit multislip digital delay amount LSB.
                Step size = (delay amount: MSB + LSB) × VCO cycles. If
                multislip enable bit = 1, any slip events (caused by GPI, SPI,
                SYNC, or pulse generator events) repeat the number of times
                set by 12-Bit Multislip Digital Delay[11:0] to adjust the
                phase by step size.

            output_sel (int): Channel output mux selection. If None, it will be
               set to 0 for even and 1 for off channel numbers, i.e. CLK
               outputs use CLK divider and SCLK output uses SCLK dividers.

               - 0: Channel divider output.
               - 1: Analog delay output.
               - 2: Other channel of the clock group pair.
               - 3: Input VCO clock (fundamental). Fundamental can also
                   be generated with 12-Bit Channel Divider[11:0] = 1.

            mute (int): Idle at Logic 0 selection (pulse generator mode only). Force to Logic 0 or Vcm .

                - 0: Normal mode (selection for DCLK).
                - 1: Reserved.
                - 2: Force to Logic 0.
                - 3: Reserved.

            dynamic_driver (int): Dynamic driver enable (pulse generator mode
                only). Driver is enabled/disabled with channel enable bit

            driver_mode (int): Output driver mode selection.

                - 0: CML mode.
                - 1: LVPECL mode.
                - 2: LVDS mode.
                - 3: CMOS mode.

            driver_impedance (int): Output driver impedance selection for CML mode olny.

                - 0: Internal resistor disable.
                - 1: Internal 100 Ω resistor enable per output pin.
                - 2: Reserved.
                - 3: Internal 50 Ω resistor enable per output pin.


        """


        if f_out and divider:
            raise RuntimeError(f'f_out and divider cannot be specified at the same time')
        if f_out:
            if not f_vco:
                raise RuntimeError(f'f_vco must be specified if f_out is specified.')
            divider = f_vco / f_out
            if f_out > 1000e6:
                self.log.debug(f'{self!r}: Forcing output {output_number} mode to LVPECL because fout is high')
                driver_mode = 1
        if divider != int(divider):
            raise RuntimeError(f'Output {output_number} divider={divider} is not an integer')
        divider = int(divider)

        if  not 1 <= divider <= 4094:
            raise ValueError(f'Output divider={divider} is out of range (1-4094)')
        if divider > 5 and divider & 1:
            raise ValueError(f'Divider {divider} for output {output_number} is not supported. The only accepted odd output dividers are 1, 3 and 5')
        if analog_delay > 23:
            raise ValueError(f'Analog delay out of range (0 to 23)')
        if digital_delay > 17:
            raise ValueError(f'Digital delay out of range (0 to 17)')
        if multislip_delay >= 1<<12:
            raise ValueError(f'Multislip delay out of range (0 to 4095)')
        if output_sel is None:
            output_sel = output_number & 1

        regs = {}

        # clkgrp1_div1_cfg1_en[0:0] = 0x1
        # clkgrp1_div1_cfg1_phdelta_mslip[1:1] = 0x1
        # clkgrp1_div1_cfg2_startmode[3:2] = 0x0
        # clkgrp1_div1_cfg1_rev[4:4] = 0x1
        # clkgrp1_div1_cfg1_slipmask[5:5] = 0x1
        # clkgrp1_div1_cfg1_reseedmask[6:6] = 0x1
        # clkgrp1_div1_cfg1_hi_perf[7:7] = 0x1
        regs[0x00C8 + output_number * 10] = (hi_perf << 7) | (sync_enable << 6) | (slip_enable << 5) | (1 << 4) | (startup_mode << 2) | (multislip_enable <<1) | enable


        # clkgrp1_div1_cfg12_divrat_lsb[7:0] = 0x1
        regs[0x00C9 + output_number * 10] = divider & 0xFF

        # clkgrp1_div1_cfg12_divrat_msb[3:0] = 0x0
        regs[0x00CA + output_number * 10] = (divider >> 8) & 0xF

        # clkgrp1_div1_cfg5_fine_delay[4:0] = 0x0
        regs[0x00CB + output_number * 10] = analog_delay & 0x1F

        # clkgrp1_div1_cfg5_sel_coarse_delay[4:0] = 0x0
        regs[0x00CC + output_number * 10] = digital_delay & 0x1F

        # clkgrp1_div1_cfg12_mslip_lsb[7:0] = 0x0
        regs[0x00CD + output_number * 10] = multislip_delay & 0xFF

        # clkgrp1_div1_cfg12_mslip_msb[3:0] = 0x0
        regs[0x00CE + output_number * 10] = (multislip_delay >> 8) & 0xF

        # clkgrp1_div1_cfg2_sel_outmux[1:0] = 0x0
        # clkgrp1_div1_cfg1_drvr_sel_testclk[2:2] = 0x0
        regs[0x00CF + output_number * 10] = output_sel & 0x3

        # clkgrp1_div1_cfg5_drvr_res[1:0] = 0x0
        # clkgrp1_div1_cfg5_drvr_spare[2:2] = 0x0
        # clkgrp1_div1_cfg5_drvr_mode[4:3] = 0x2
        # clkgrp1_div1_cfg_outbuf_dyn[5:5] = 0x0
        # clkgrp1_div1_cfg2_mutesel[7:6] = 0x0

        regs[0x00D0 + output_number * 10] = ((mute & 3) << 6) | (dynamic_driver << 5) | ((driver_mode & 3) << 3) | ((driver_impedance & 3) << 0)

        self.write_regs(regs)

    def set_sysref_timer(self,
            pulse_generator_mode=1,
            sync_retime=0,
            sync_through_pll2=1,
            sync_pol=0,
            sysref_timer=1200,
            rf_seeder_enable=1,
            sysref_timer_enable=1
     ):
        """

        Parameters:

            pulse_generator_mode (int): SYSREF output enable with pulse generator
                - 0: level sensivive
                - 1: 1 pulse
                - 2: 2 pulses
                - 3: 4 pulses
                - 4: 8 pulses
                - 5: 16 pulses
                - 6: 16 pulses
                - 7: COntinuous mode (50% duty cycle)

            sync_retime (int): If 1, the external SYNC is retimed using Reference 0.

            sync_through_pll2 (int): When 1, allows a reseed event to be through PLL2

            sync_pol (int): 0=positive, 1=Negative. Must be 0 if not using CLKIN0 as input.

            sysref_timer (int): 12-bit SYSREF timer. This sets the internal beat frequency of the
                master timer, which controls synchronization and pulse generator events. Set the
                12-bit timer to a submultiple of te lowest SYSREF frequency, and program it to be no
                faster than 4 MHz.

            rf_seeder_enable (int): When 1, enables the RF seeder to the outputs. Must be set for SYNC to work

            sysref_timer_enable (int): When 1, the SYSREF timer is enabled. Must be set for SYNC to work


        Notes:

            - https://ez.analog.com/clock_and_timing/f/q-a/570739/hmc7044-rf-reseed-request-clarification/496287
        """
        if sysref_timer < 1 or sysref_timer>2**12-1:
            raise RuntimeError('Sysref timer value out of range')

        regs = [
            # PULSE/SYNC/SYSREF
            # sysr_cfg3_pulsor_mode[2:0] = 0x1
            (0x5A, pulse_generator_mode),
            # sysr_cfg1_synci_invpol[0:0] = 0x0
            # sysr_cfg1_pll2_carryup_sel[1:1] = 0x0
            # sysr_cfg1_ext_sync_retimemode[2:2] = 0x1
            (0x5B, (sync_retime << 2) | (sync_through_pll2 << 1) | (sync_pol)), # reserved; SYNC retime; SYNC through PLL2; SYNC pol
            # sysr_cfg16_divrat_lsb[7:0] = 0x0
            (0x5C, sysref_timer & 0xFF),
            # sysr_cfg16_divrat_msb[3:0] = 0x6
            (0x5D, sysref_timer >> 8),
            # 0x005E reserved - not programmed by GUI
            ]
        self.write_regs(regs)
        # Enable RF Reseeded and SYSREF timer, otherwise SYNC events won't work.
        self.write_reg(0x0003, (rf_seeder_enable << 5) | (sysref_timer_enable << 2), mask=0b00100100)


    def get_product_id(self):
        """ Return the HMC7044 product ID

        Returns:
          24-bit product ID
        """
        reg_78 = self.read_reg(0x0078)
        reg_79 = self.read_reg(0x0079)
        reg_7a = self.read_reg(0x007A)

        return (reg_7a << 16) | (reg_79 << 8) | reg_78

    def get_clkin_status(self):
        """ Returns the state (i.e. loss of signal (LOS)) if the reference clocks

        Returns:

            list of bool describing the state of each reference clock: True = active, False = Loss of signal
        """

        alarm = self.read_reg(0x7c)
        return [not (alarm & (1 << i)) for i in range(4)]

    def get_active_clkin(self):
        """ Return the currently active input reference number. Returns None if no input is active.

        Returns:

            int: currently active input reference number
        """
        input_states = self.get_clkin_status()
        if not any(input_states):
            return None

        reg_82 = self.read_reg(0x0082)
        active_input = (reg_82 >>3 ) & 0x3

        if not input_states[active_input]:
            self.logger.warn(f'The input selected by the PLL is not active')
        return active_input

    def get_pll1_status(self):
        """ Returns a dict describing the status of PLL1
        """


        reg_82 = self.read_reg(0x0082)
        reg_83 = self.read_reg(0x0083)
        reg_84 = self.read_reg(0x0084)
        reg_85 = self.read_reg(0x0085)
        reg_86 = self.read_reg(0x0086)
        reg_87 = self.read_reg(0x0087)

        status = dict(

            best_input = (reg_82 >>5) & 0x3,
            active_input = (reg_82 >>3) & 0x3,
            FSM_state = ('Reset', 'Acquisition', 'Locked', 'Invalid',
                              'holdover', 'DAC holdover exit', 'NA', 'NA')[(reg_82 >> 4) & 0x7],
            average_DAC_code = reg_83 & 0x7F,
            holdover_comparator_value = (reg_84 >> 7) & 1,
            current_DAC_code = reg_84 & 0x7F,
            active_CLKIN_LOS = (reg_85 >> 3) & 1,
            CLKIN_faster_than_VCXO = (reg_85 >> 2) & 1,
            holdover_ADC_not_acquiring = (reg_85 >> 1) & 1,
            holdover_ADC_out_of_range = (reg_85 >> 0) & 1,
            holdover_exit_phase = (reg_86 >> 3) & 0x3,
            )

        return status


    def get_pll2_status(self):
        """ Returns a dict describing the status of PLL2
        """

        reg_8c = self.read_reg(0x008C)
        reg_8d = self.read_reg(0x008d)
        reg_8e = self.read_reg(0x008e)
        reg_8f = self.read_reg(0x008f)

        status = dict(

            autotune_value = reg_8c,
            autotune_error_count = (reg_8d | ((reg_8e & 0x3f) << 8)) * (-1 if reg_8e & (1 << 6) else 1),
            autotune_busy = bool(reg_8e & (1 << 7)),
            autotune_state = ('Idle', 'Startup1', 'Startup2', 'Reset1',
                              'reset2', 'reset3', 'Measure', 'Wait1',
                              'Wait2', 'Update_loop', 'Round', 'Finish',
                              'NA', 'NA', 'NA', 'NA')[(reg_8f >> 4) & 0xF],
            sync_state = ('Idle', 'NA', 'NA', 'NA',
                          'Powerup A', 'NA', 'Powerup B', 'Send N2',
                          'NA', 'NA', 'NA', 'NA',
                          'Powerdown A', 'NA', 'Powerdown B', 'NA')[reg_8f & 0xF]
            )

        return status

    def get_sysref_status(self):
        """ Returns a dict describing the status of PLL2
        """

        reg_91 = self.read_reg(0x0091)

        status = dict(

            channel_output_FSM_busy = (reg_91 >> 4) & 1,
            FSM_state = ('Reset', 'NA', 'Done', 'NA',
                              'Get Ready1', 'Get Ready2', 'Get Ready3', 'NA',
                              'NA', 'NA', 'Running (pulse gen)', 'Start',
                              'Powerup1', 'Powerup2', 'Powerup3', 'NA')[(reg_91 >> 0) & 0xF],
            )

        return status



    def status(self):
        """ Print PLL status information
        """

        # subsystems = dict(
        #     pll1_status = self.get_pll1_status(),
        #     pll2_status = self.get_pll2_status(),
        #     sysref_status = self.get_sysref_status()
        #     )
        print(f'HMC7044 Status')
        pid = self.get_product_id()
        print(f'   Product ID = 0x{pid:06X}')
        alarm = self.read_reg(0x7c)
        print(f'PLL1 alarm readback: reg 0x7c = {alarm:08b}')
        print(f'   Near lock: {bool(alarm & 0x80)}')
        print(f'   Lock acquisition: {bool(alarm & 0x40)}')
        print(f'   Lock detect: {bool(alarm & 0x20)}')
        print(f'   Holdover status: {bool(alarm & 0x10)}')
        print(f'   CLKIN0 (ETH_REG_125MHz) LOS: {bool(alarm & 0x1)}')
        print(f'   CLKIN1 (BP_CLK_10MHZ) LOS: {bool(alarm & 0x2)}')
        print(f'   CLKIN2 (SMA_CLK_10MHZ) LOS: {bool(alarm & 0x4)}')
        print(f'   CLKIN3 (PL_RECCLK) LOS: {bool(alarm & 0x8)}')
        prio = self.read_reg(0x14)
        print(f'PLL1 ref priority: Reg 0x14 =  {prio:08b}')
        print(f'   1st priority clock: {(prio >>0) & 3}')
        print(f'   2nd priority clock: {(prio >>2) & 3}')
        print(f'   3rd priority clock: {(prio >>4) & 3}')
        print(f'   4th priority clock: {(prio >>6) & 3}')
        swc = self.read_reg(0x29)
        print(f'PLL1 ref switching control: Reg 0x29 = {swc:08b}')
        print(f'   Bypass debouncer: {(swc >> 5) & 1}')
        print(f'   Manual CLKIN source: {(swc >> 3) & 3}')
        print(f'   Holdover uses DAC: {(swc >> 2) & 1}')
        print(f'   Autorevertive manual switching: {(swc >> 1) & 1}')
        print(f'   Automode reference switching: {(swc >> 0) & 1}')
        print(f'PLL1 status: Regs 0x82-0x87')
        for k,v in self.get_pll1_status().items():
            print(f'    {k:27s}: {v}')
        print(f'PLL2 status: Regs 0x8C-0x8F')
        for k,v in self.get_pll2_status().items():
            print(f'    {k:27s}: {v}')
        print(f'SYSREF status: Regs 0x91')
        for k,v in self.get_sysref_status().items():
            print(f'    {k:27s}: {v}')
        print()

    def set_oscout(self, path_enable=1, path_divider=0, driver_mode=2, driver_impedance=0, driver_enable=1):
        """ Sets the OSCOUT1 path and outputs. OSCOUT0 is disabled as it shares a pin with CLKIN2, which is used as a clock input.

        OSCOUT is the output of PLL1's external VCXO, which can be divided and
        routed to the dedicated OSCOUT0 and OSCOUT1 pins.


        Parameters:

            output (int): OSCOUT output number to configure (0 or 1). OSCOUT0 connects to the same pin as CLKIN2.

            path_enable (bool): OSCOUT output path enabled if True

            path_divider (int): Oscillator output divider ratio:

                0: divide by 1
                1: Divide by 2
                2: divide by 4
                3: divide by 8

            driver_mode (int):

                0: CML
                1: LVPECL
                2: LVDS
                3: CMOS

            driver_impedance: oscillator output driver impedance selection for CML mode

                0: disable
                1: internal 100 ohms per pin
                2: reserved
                3: 50 ohms per pin

            driver_enable (bool): Enable output driver

        Note:
            - Both output drivers are set with the same parameters

        """

        regs = {}
        # pll2_cfg1_oscout_path_en[0:0] = 0x1
        # pll2_cfg2_oscout_divratio[2:1] = 0x0
        regs[0x39]= (path_divider << 1) | path_enable

        # pll2_cfg1_obuf0_drvr_en[0:0] = 0x1
        # pll2_cfg5_obuf0_drvr_res[2:1] = 0x0
        # pll2_cfg5_obuf0_drvr_mode[5:4] = 0x2
        regs[0x3A]= 0 # disable OSCOUT0
        regs[0x3B]= (driver_mode << 4) | (driver_impedance << 1) | driver_enable # set OSCOUT1

        self.write_regs(regs)

