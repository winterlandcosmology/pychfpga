#!/usr/bin/python

"""
MGT_PLL.py module
 Implements the MGT PLL interface
#
# History:
# 2011-07-13 JFC : Created from test code in ADC_PLL.py
# 2011-08-11 JFC : Complete cleanup. Made the PLL programming work. Changed the order of computations.
    Changed the PLL parameter selection algorithm to select the values that yield the lowest frequency error.
    Print frequency table at the end, wich computation of precision, errror and PPM
    2011-06-08 JFC: Added an Exception if there are no valid P0/P1/N combinations for target frequency
                Slightly changed the programming sequence. Now done in 2 phases only: 1) program registers (including outputs levels)  and 2) initiate VCO cal.
"""
import numpy as np
import time

class Struct(object):
    def __init__(self, **args):
        self.__dict__.update(args)

class MGT_PLL_base(object):
    """ Implements interface to the PLL providing the reference clock signal to the FPGA Multi Gigabit Transceivers """
    def __init__(self, mezz, verbose=2):
        self.mezz = mezz
        self.verbose = verbose

    def write(self, addr, data):
        """ Writes an 8-bit value to a PLL register at specified address."""
        self.mezz.spi_read_write(self.mezz.SPI_PLL2_ADDR, [0x00 + (addr >> 8) & 0x1F, addr & 0xFF, data]) # Write mode, W1:W0='00' = 1 byte write

    def read(self,addr):
        """ Reads an 8-bit value from the PLL register at specified address."""
        self.mezz.spi_read_write(self.mezz.SPI_PLL2_ADDR, [0xA0 + (addr >> 8) & 0x1F, addr & 0xFF, 0x00]) # Write mode, W1:W0='00' = 1 byte write

# ---------------------------------------------------------------------------------------------

    def init(self, fout=312.5, fref=10, sel=0, band=None, verbose=2, wait_for_lock=True,
             OUT2_SOURCE=0,
             CP_CURRENT=0x80,
             FORCE_VCO_TO_MIDPOINT=0,
             VCO_SUPPLY_BOOST=0,
            ):
        """
        Initializes the MGT PLL to provide an adequate clock to the
        Multigigabit transceivers.

        Parameters:
            - fout: MGT reference frequency in MHz.
            - fref: PLL reference frequency in MHZ (typically 10 or 25 MHz,
              depending on the board reference)

            - OUT2_SOURCE: 0 = Same as OUT1, 1 = REF clock
        NOTES:
            - Using frequencies that require fractional frequency
              multiplication factors will generate more noise. The PLL
              configuration calculated here has not been optimized to minimize
              fractional-N noise.
            - The source code is heavily commented. Consult it to get an idea
              of how the PLL is configured
        """

        self.verbose = verbose

        if self.verbose:
            print ' --- MGT PLL Set-up ---'
            print ' Using reference frequency of %.0f MHz' % fref
            print ' Target MGT refernece frequency: %.0f MHz' % fout

        # VCO frequency limits
        fvco_min = 3350  # MHz
        fvco_max = 4050  # MHz


        # Compute the output division ratio which is ODF=P0*P1, where P0=4-11
        # and P1=1-63. We want to find which combination of P0 and P1 will
        # allow the exact frequency to be generated with a integer
        # multiplication of the reference frequency

        # Establish the limits of the ODS factor based on the VCO frequency range
        ODF_max = np.floor(fvco_max / float(fout))
        ODF_min = np.ceil(fvco_min / float(fout))
        N_min = 64
        N_max = 255
        # List all possible values of P0 and P1
        P0_list = range(4, 11+1)
        P1_list = range(1, 63+1)
        # set reference frequency doubler to true if can't get freq in range
        if (fref*N_max < fvco_min ):
            REFERENCE_FREQUENCY_DOUBLER = 1
            fref = fref * 2
        else:
            REFERENCE_FREQUENCY_DOUBLER = 0

        valid_params = []  # initialize list of valid PLL tuning parameter values
        # Loop over all possible values of P0 and P1 to find valid combinations of parameters
        for p0 in P0_list:
            for p1 in P1_list:
                fvco = float(fout)*p0*p1  # VCO frequency that would be required with this P0 P1 combination
                N_real = fvco/fref  # Compute the value of N required to provide the target VCO frequency
                N_int = int(N_real)  # Integer frequency reference multiplication factor
                N_frac = N_real-N_int  # remaining reference multiplication factor fraction required to obtain the target VCO value
                if  (fvco_min <= fvco <= fvco_max) and (N_min <= N_int <= N_max): # if the VCO frequency and N factor are within the valid VCO range
                    # Compute the fractional multiplication coefficients for the PLL
                    MODULUS = 2**20-1  # 0-1,048,575. We use the highest value to give us the maximum resolution (but the spuriouses will be closer to the carrier. We might want to change that is we wanted to use this VCO in fractional mode, which is not normally the case)
                    FRAC = int(N_frac * MODULUS)  # Should be in the range 0-1,048,575
                    N_real = (N_int+float(FRAC)/MODULUS);  # we update the fractional N with the actual ratio that was acheived given the rounding in FRAC and MODULUS
                    fout_real = N_real*fref/(p0*p1)  # frequency that should be generated by the PLL with this confuguration
                    fout_err = abs(fout-fout_real)

                    valid_params.append(Struct(P0=p0,P1=p1, fout_err=fout_err,N_int=N_int,N_real=N_real,MODULUS=MODULUS,FRAC=FRAC,fvco=fvco,fout_real=fout_real) ) # store combination (whether or not N is integer or not. We'll use a non-integer N if we have to.)

        if not len(valid_params):
            raise SystemError("Cannot find valid combination of parameters to acheive target MGT PLL frequency")

        # Sort the list of ODF values, putting the items with integer N first (if any) so we will use it.
        valid_params = sorted(valid_params, key=lambda k: k.fout_err)  # put combinations that yield integer cooefficients first

        if self.verbose > 1:
            print ' Target Output Division Factor (ODF) is from %i to %i' % (ODF_min, ODF_max)
            print ' Possible P0/P1 combinations'
            for params in valid_params:
                print '   P0=%i, P1=%i, ODF=%i, N=%.3f, Freq err=%.9f' % (params.P0, params.P1, params.P0*params.P1, params.N_int, params.fout_err)

        # Select first entry in the list as our operating parameters.
        params = valid_params[sel]
        P0 = params.P0  # 4-11
        P1 = params.P1  # 1-63
        N_int = params.N_int
        N_real = params.N_real
        MODULUS = params.MODULUS
        FRAC = params.FRAC
        fvco = params.fvco
        fout_real = params.fout_real
#       fvco=fout*ODF  # VCO frequency required for this ODF
#       N=int(float(fvco)/fref) # integer frequency multiplication factor N. Should be in the range 64-255. We round down, and will add a fractional part if needed to get closer to the target frequency.

        if verbose > 1:
            print ' Choosing P0=%i, P1=%i, ODF=%i' % (P0, P1, P0*P1)
            print ' fvco=%.3f MHz (must be between %.0f and %.0f MHz)' % (fvco, fvco_min, fvco_max)
            print ' Integer multiplier N=%i (integer vco_freq=N*fref=%.0f MHz, integer fout=%.3f MHz, integer fout error=%.6f MHz)' % (N_int, N_int*fref, N_int*fref/(P0*P1), N_int*fref/(P0*P1)-fout)
            print ' Fractional multiplier FRAC=%i, MODULUS=%i (vco_freq=N*fref=%.0f MHz, fout=%.3f MHz)' % (FRAC, MODULUS, N_real*fref, fout_real)
            print

        # --- Define PLL parameters ---

        # VCO Cal
        # register 0x0E flags
        CAL_VCO = 0
        ENABLE_ALC = 1
        ALC_THRESHOLD = 6  # 0-7, default = 6
        ENABLE_SPI_VCO_CAL = 1  # Must be 1 so we can initiate VCO calibrations over SPI
        ENABLE_SPI_VCO_BAND = 0

        VCO_LEVEL = 0x20  # 0 - 0x3F
        VCO_BAND = 0x40  # 0-0x7F

        if band:
            VCO_BAND = band

        # Charge punp control
        ENABLE_SPI_CP_CURRENT = 1
        # CP_CURRENT = 0x80
        CP_MODE = 3
        ENABLE_CP_MODE = 0
        # FORCE_VCO_TO_MIDPOINT = 0

        # register 0x14 flags
        ENABLE_SPI_FREQ_CTRL = 1
        BYPASS_SDM = 0
        DISABLE_SDM = 0
        RESET_PLL = 0

        # register 0x19 flags
        ENABLE_SPI_OUT_DIV = 1

        # OUT1 Driver Control
        #   Register 0x32
        OUT1_DRIVE_STRENGTH = 1
        OUT1_POWER_DOWN = 0
        OUT1_MODE = 0  # 0 = CMOS (active,active), 1 = CMOS (active, Z), 2 = CMOS (z, active), 3 = CMOS (z,z), 4 = LVDS, 5 = LVPECL,
        OUT1_CMOS_POL = 0  # 0 = (+,-), 1 = (+,+), 2 = (-,-), 3 = (-,+)
        ENABLE_SPI_OUT1_CTRL = 1

        # OUT2 Driver Control
        #   Register 0x33
        # OUT2_SOURCE = 0  # 0 = Same as OUT1, 1 = REF clock
        #   Register 0x34
        OUT2_DRIVE_STRENGTH = 1
        OUT2_POWER_DOWN = 0
        OUT2_MODE = 0  # 0 = CMOS (active,active), 1 = CMOS (active, Z), 2 = CMOS (z, active), 3 = CMOS (z,z), 4 = LVDS, 5 = LVPECL,
        OUT2_CMOS_POL = 0  # 0 = (+,-), 1 = (+,+), 2 = (-,-), 3 = (-,+)
        ENABLE_SPI_OUT2_CTRL = 1




        # --- Reset PLL to a known state ---
        self.write(0x00, 0x3c)  # Soft reset
        self.write(0x05, 0x01)  # Update
        time.sleep(0.003)  # wait 3 ms for the VCO cal to complete (needed?)

        # --- Program the registers ---
        # Charge pump control
        self.write(0x0A, CP_CURRENT)
        self.write(0x0B, (ENABLE_SPI_CP_CURRENT << 7) | (CP_MODE << 4) | (ENABLE_CP_MODE << 3) | (FORCE_VCO_TO_MIDPOINT << 0))

        # VCO Control
        self.write(0x0E, (0 << 7) | (ENABLE_ALC << 6) | (ALC_THRESHOLD << 3) | (ENABLE_SPI_VCO_CAL << 2) | (VCO_SUPPLY_BOOST << 1) | (ENABLE_SPI_VCO_BAND << 0))
        self.write(0x0F, (VCO_LEVEL << 2))
        self.write(0x10, (VCO_BAND << 1))

        # PLL loop Control
        self.write(0x11, N_int)  # MOD
        self.write(0x12, (MODULUS >> 12) & 0xFF)  # MOD
        self.write(0x13, (MODULUS >> 4) & 0xFF)  # MOD
        self.write(0x14, ((MODULUS & 0x0F) << 4) | (ENABLE_SPI_FREQ_CTRL << 3) | (BYPASS_SDM << 2) | (DISABLE_SDM << 1) | (RESET_PLL << 0))  # MOD
        self.write(0x15, (FRAC >> 12) & 0xFF)  # MOD
        self.write(0x16, (FRAC >> 4) & 0xFF)  # MOD
        self.write(0x17, ((FRAC & 0x0F) << 4 | ((P1 >> 5) & 0x01)))
        self.write(0x18, ((P1 & 0x1F) << 3) | (P0 - 4))  # P1
        self.write(0x19, (ENABLE_SPI_OUT_DIV << 7))  #
        self.write(0x1d, (REFERENCE_FREQUENCY_DOUBLER << 2))

        # OUT1 Control
        self.write(0x32, (OUT1_DRIVE_STRENGTH << 7) | (OUT1_POWER_DOWN << 6) | (OUT1_MODE << 3) | (OUT1_CMOS_POL << 1) | (ENABLE_SPI_OUT1_CTRL << 0))

        # OUT2 Control
        self.write(0x33, (OUT2_SOURCE << 3))  #
        self.write(0x34, (OUT2_DRIVE_STRENGTH << 7) | (OUT2_POWER_DOWN << 6) | (OUT2_MODE << 3) | (OUT2_CMOS_POL << 1) | (ENABLE_SPI_OUT2_CTRL << 0))

        # Load register values
        self.write(0x05, 0x01)  # Tell the PLL to register the values sent so far

        # Initiate VCO calibration to allow locking with new parameters
        self.write(0x0E, (1 << 7) | (ENABLE_ALC << 6) | (ALC_THRESHOLD << 3) | (ENABLE_SPI_VCO_CAL << 2) | (VCO_SUPPLY_BOOST << 1) | (ENABLE_SPI_VCO_BAND << 0))
        self.write(0x05, 0x01)  # Force the PLL to register the values sent so far
        time.sleep(0.003)  # wait 3 ms for the VCO cal to complete

        if self.verbose > 0:
            fpga = self.mezz.motherboard
            gate_time = 0.1
            fout_meas0 = fpga.FreqCtr.read_frequency('FMC%s_MGT_PLL_REFCLK0' % ('A', 'B')[self.mezz.mezzanine-1], gate_time=gate_time)/1e6
            fout_meas1 = fpga.FreqCtr.read_frequency('FMC%s_MGT_PLL_REFCLK1' % ('A', 'B')[self.mezz.mezzanine-1], gate_time=gate_time)/1e6
            fout_meas_resolution = 2.0 / gate_time / 1e6
            print 'MGT refclk frequency:'
            print 'Requested:  %10.6f MHz' % (fout)
            print 'Configured: %10.6f MHz' % (fout_real)
            print 'Measured:  0: %10.6f MHz,  1: %10.6f MHz, Resolution = %.6f MHz' % (fout_meas0, fout_meas1, fout_meas_resolution)
            print 'Difference: %10.6f MHz (%.0f PPM)'  % (fout_meas0-fout_real, abs(fout_real-fout_meas0)/fout_real*1e6)
            print 'Locked:     ', self.mezz.IOExpander.PLL2_LOCK
            print 'MGT line frequency (fout*16): %.3f Mb/s (not measured)' % (fout_real*16)
            print 'MGT data clock (fout*16/40): %.3f MHz (not measured)' % (fout_real*16/40)

        if wait_for_lock:
            self.mezz.IOExpander.wait_for_bit('PLL2_LOCK', timeout=1)

        return self.is_locked()

    def is_locked(self):
        return self.mezz.IOExpander.PLL2_LOCK
