#!/usr/bin/python

"""
SCALER.py module
 Implements interface to the SCALER

 History:
        2012-07-13 JFC: Created
"""
# Python Standard library packages
import struct
import time
import numpy as np

# Local packages
from ..mmi import MMI, BitField, CONTROL, STATUS
from pychfpga.common.state import State


class SCALER(MMI):
    """ Implements interface to the SCALER module within a procecessor pipeline"""

    ADDRESS_WIDTH = 9

    # Define Control registers
    RESET                 = BitField(CONTROL, 0x00, 7, doc="Reset the SCALER.")
    BYPASS                = BitField(CONTROL, 0x00, 6, doc="Bypass the SCALER")
    FOUR_BITS             = BitField(CONTROL, 0x00, 5, doc="Enables 4-bit operation")
    SHIFT_LEFT_BANK0      = BitField(CONTROL, 0x00, 0, width=5, doc="Post-scaler left shift for gain bank 0. Use get_shift_left/set_shift_left.")
    USE_OFFSET_BINARY     = BitField(CONTROL, 0x01, 6, doc="When '1', offset binary encoding is used.")
    READ_COEFF_BANK       = BitField(CONTROL, 0x01, 4, doc="Target gain coefficients bank to be used by the scaler")
    WRITE_COEFF_BANK_A      = BitField(CONTROL, 0x01, 0, width=4, doc="Indicates in which data page the gain coefficients are being written to. Page 0-7 are coefficients fri bank0, Page 8-15 are for Bank 1 coefficients.")
    WRITE_COEFF_BANK_B      = BitField(CONTROL, 0x01, 5, doc="Bit 4 of the write page")
    WRITE_COEFF_BANK_C      = BitField(CONTROL, 0x01, 7, doc="Bit 5 of the write page")

    STATS_CAPTURE         = BitField(CONTROL, 0x02, 7, doc="When '1', New saturation/overflow stats are captured")
    SATURATE_ON_MINUS_7   = BitField(CONTROL, 0x02, 6, doc="When '1', Values will saturate at -7 instead of -8.")
    ZERO_ON_SATURATION    = BitField(CONTROL, 0x02, 5, doc="When '1', Both real and Imaginary parts are zeroed when either of them overflow.")
    SYNCHRONIZE_GAIN_BANK = BitField(CONTROL, 0x02, 4, doc="When '1', The target bank number will be enabled at the target frame number.")
    # FORCE_GAIN_TO_ONE     = BitField(CONTROL, 0x02, 2, doc="Force gain to (1+0j) (complex gain) or 1 (real gain). Gain table is ignored, but post-scaler (SHIFT_LEFT) still applies. ")
    CHAN_FIFO_OVERFLOW_RESET = BitField(CONTROL, 0x02, 3, doc="Resets the Channel FIFO overflow flag")
    STATS_SINGLE_BIN      = BitField(CONTROL, 0x02, 2, doc="When '1', overflow stats operate only on the bin in STATS_BIN_NUMBER.")
    ROUNDING_MODE         = BitField(CONTROL, 0x02, 0, width=2, doc="Set rounding mode.  0: Truncate, 1: Round, 2: Convergent Rounding")
    STATS_FRAME_COUNT     = BitField(CONTROL, 0x05, 0, width=24, doc="Number of frames to inclue in stats results.")

    GAIN_BANK_SWITCH_FRAME_NUMBER = BitField(CONTROL, 0x09, 0, width=32, doc="Frame number at which the target gain bak is to be activated.")
    USE_FLOAT_GAINS       = BitField(CONTROL, 0x0A, 7, doc="When '1', floating point gains are used. Works only if USE_COMPLEX_GAINS=0")
    MON_RESET_STATS       = BitField(CONTROL, 0x0A, 6, doc="When '1', MON stats are reset")
    MON_CTRL              = BitField(CONTROL, 10, 5, doc="When '1', Disable FIFO write")
    DATA_TYPE             = BitField(CONTROL, 0x0A, 3, width=2, doc="Main output when BYPASS=1 (bypass_data_type).\n"
                                "   0: input\n"
                                "   1: 1+0j if any saturation\n"
                                "   2: 1+0j or 0+1j if positive or negative saturation respectively\n"
                                )
    CAP_DATA_TYPE         = BitField(CONTROL, 0x0A, 0, width=3, doc="Capture data port source.\n"
                                "   0: main scaler output\n"
                                "   1: raw FFT MSBs\n"
                                "   2: post-gain MSBs\n"
                                "   3: 4+4 scaled\n"
                                "   4: dual-resolution raw FFT\n"
                                "   5: full-resolution raw FFT\n"
                                "   6: saturation flags\n"
                                "   7: reserved\n"
                                )
    STATS_BIN_NUMBER      = BitField(CONTROL, 0x0C, 0, width=16, doc="Bin number for overflow stats when STATS_SINGLE_BIN is set.")
    SHIFT_LEFT_BANK1      = BitField(CONTROL, 0x0D, 0, width=5, doc="Post-scaler left shift for gain bank 1. Use get_shift_left/set_shift_left.")

    STATS_READY            = BitField(STATUS, 0x00, 7, doc="Indicates that new stats results are ready")
    CURRENT_GAIN_BANK      = BitField(STATUS, 0x00, 6, doc="Currently active gain bank.")
    EIGHT_BIT_SUPPORT      = BitField(STATUS, 0x00, 5, doc="'1' when the SCALER supports 8-bit output")

    CHAN_FIFO_OVERFLOW      = BitField(STATUS, 0x00, 4, doc="'Channel FIFO overflow flag, sticky")
    USE_COMPLEX_GAINS      = BitField(STATUS, 0x00, 3, doc="1 if (16+16) bits complex gains are used, otherwise 16-bits real gains are used.")
    COEFF_PAGE_WIDTH       = BitField(STATUS, 0x00, 0, width=3, doc="Width of the page register for bytewise access to both gain banks.")

    STATS_SCALER_OVERFLOWS = BitField(STATUS, 0x02, 0, width=16, doc="Stats result: number of scaler overflows")
    STATS_ADC_OVERFLOWS    = BitField(STATUS, 0x04, 0, width=16, doc="Stats result: number of ADC overflows")
    FRAME_CTR              = BitField(STATUS, 0x05, 0, width=8, doc="Free running frame counter (last 8 bits)")
    DELAY_CTR              = BitField(STATUS, 0x07, 0, width=16, doc="Debug: Delay counter")
    MON_PACKET_LENGTH              = BitField(STATUS, 0x09, 0, width=16, doc="Debug")
    MON_PACKET_CTR              = BitField(STATUS, 10, 0, width=8, doc="Debug")
    MON_WORD_CTR              = BitField(STATUS, 12, 0, width=16, doc="Debug")
    CAP_FRAME_CTR              = BitField(STATUS, 13, 0, width=8, doc="Debug")
    MON_WORD              = BitField(STATUS, 15, 0, width=16, doc="Debug")
    MON_CLK_CTR           = BitField(STATUS, 16, 0, width=8, doc="Debug")
    MON_BIT_CTR           = BitField(STATUS, 17, 0, width=8, doc="Debug")


    ROUNDING_MODE_TRUNCATE         = 0b00
    ROUNDING_MODE_ROUND            = 0b01
    ROUNDING_MODE_CONVERGENT_ROUND = 0b10

    # Define Status registers

    def __init__(self, *, router, router_port, instance_number, config):
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config = config
        self.cached_gain_table = {}
        self.cached_gain_timestamp = {}

    def reset(self):
        """ Resets the SCALER module """
        self.pulse_bit('RESET')

    def init(self):
        """ Initialize the SCALER module"""
        # Bypass the scaler by default if the FFT is not present
        self.fft = self.fpga.chan[self.instance_number].FFT  # cache the FFT object for convenience

        if self.instance_number in self.fpga.LIST_OF_ANTENNAS_WITH_FFT:
            self.BYPASS = 0
        else:
            self.BYPASS = 1
        self.set_capture_data_type(0)
        self.set_shift_left(0, 31)
        self.set_shift_left(1, 31)
        self.set_offset_binary_encoding(1)
        self.set_gain_table(1)
        self.SATURATE_ON_MINUS_7 = 1
        self.STATS_CAPTURE = 1
        self.STATS_FRAME_COUNT = int(800e6 / 2048 * 30)

        self.config.update(
            bypass=bool(self.BYPASS),
            bypass_data_type=int(self.DATA_TYPE),
            eight_bit=not bool(self.FOUR_BITS),
            eight_bit_support=bool(self.EIGHT_BIT_SUPPORT),
            use_offset_binary=bool(self.USE_OFFSET_BINARY),
            cap_data_type=int(self.CAP_DATA_TYPE)
        )
        self.config.capture_format = State()
        self.update_config()

    def set_scaler_output(self, data_type, bypass):
        self.BYPASS = bypass
        self.DATA_TYPE = data_type
        self.config.bypass = bool(bypass)
        self.config.bypass_data_type = int(data_type)

    CAPTURE_DATA_TYPES = {
        'out': 0, # same as main data output
        'fft': 1, # MSBs of the raw FFT values\n"
        'fft_gain': 2, # MSBs of post-gain FFT value with saturation\n"
        'fft_4bit': 3, # 4+4 bit scaled values\n"
        'fft_2x': 4, # Dual resolution FFT
        'fft_full': 5, # Full resolution FFT
        'sat_flags': 6, # Post-gain saturation flags (+1/-1 per Re/Im field, VHDL mode "110")
    }

    def set_capture_data_type(self, data_type):
        if data_type in self.CAPTURE_DATA_TYPES:
            self.CAP_DATA_TYPE = self.CAPTURE_DATA_TYPES[data_type]
            self.config.cap_data_type = self.CAPTURE_DATA_TYPES[data_type]
        elif data_type in self.CAPTURE_DATA_TYPES.values():
            self.CAP_DATA_TYPE = data_type
            self.config.cap_data_type = int(data_type)
        else:
            valid_values = [f'{v}:{k}' for k,v in self.CAPTURE_DATA_TYPES.items()]
            raise RuntimeError(f"Invalid capture data type. Valid values are {', '.join(valid_values)}")

    def set_offset_binary_encoding(self, offset):
        self.USE_OFFSET_BINARY = offset
        self.config.use_offset_binary = bool(offset)

    def set_output_bit_width(self, width):
        """Set scaler output bit width.

        Parameters:
            width (int): ``4`` for (4+4)-bit output, ``8`` for (8+8)-bit output.

        Raises:
            ValueError: If ``width`` is not 4 or 8, or if 8-bit mode is requested
                but the firmware build does not support it.
        """
        if width == 4:
            eight_bit = False
        elif width == 8:
            if not self.config.eight_bit_support:
                raise ValueError(
                    '8-bit mode not supported in this build of the SCALER firmware')
            eight_bit = True
        else:
            raise ValueError(
                f'Number of bits {int(width)} is invalid for the scaler. '
                'Only 4 or 8 is allowed')
        self.FOUR_BITS = not eight_bit
        self.config.eight_bit = eight_bit

    def update_config(self):
        """ Update the capture_format field in the current scaler config and return that config.
        
        Returns:
            cfg (Config): the current scaler config with the updated capture_format field.
        """
        cfg = self.config
        board = self.fpga.config
        fft = self.fft  
        scaler_fmt_attrs = ('cap_data_type', 'eight_bit', 'use_offset_binary') # SCALER attributes that affect the format
        fft_fmt_attrs = ('bypass',) # FFT attributes that affect the format.'bypass' is a direct attribute, no need to update the FFT config
        ctx = ('SCALER.update_config', self)

        # If the SCALER or FFT configuration has not changed, do nothing
        if (not cfg.has_changed(scaler_fmt_attrs, context=ctx)
            and not fft.config.has_changed(fft_fmt_attrs, context=ctx)):
            return cfg

        # Dynamic parameters
        fft_bypass = bool(fft.config.bypass)
        cap_data_type = int(cfg.cap_data_type)
        eight_bit = bool(cfg.eight_bit)
        use_offset_encoding = bool(cfg.use_offset_binary)

        # Constants
        adc_bit_width = int(board.adc_bits_per_sample)
        fft_bins_per_word = int(board.fft_bins_per_word)
        fft_data_width = self.fpga.FFT_INFO[self.fpga.FFT_TYPE]['bits_per_bin'] // 2 # Width of the FFT Re or Im component
        capture_bus_width = board.capture_bus_width # total number of bits in the capture bus (capture word)

        # Compute the capture format parameters
        # integer_width: number of bits used un the capture bus to store each Re or Im element
        # data_width: actual number of bits of the Re or Im elements. The are MSB-aligned in the integer_width
        # interleave_factor: number of consecutive frames to read to get all the data
        if cap_data_type == 0: # Normal output, 4+4 bit or 8+8 bit
            integer_width = capture_bus_width // fft_bins_per_word // 2 # 16 for CRS, 8 for ICE
            data_width = 8 if eight_bit else 4
            interleave_factor = 1
        elif cap_data_type == 1: # MSBs of the raw FFT values
            integer_width = capture_bus_width // fft_bins_per_word // 2
            data_width = adc_bit_width if fft_bypass else min(fft_data_width, integer_width)
            interleave_factor = 1
        elif cap_data_type == 2: # MSBs of the post-gain FFT values
            integer_width = capture_bus_width // fft_bins_per_word // 2  
            data_width = integer_width
            interleave_factor = 1 
        elif cap_data_type == 3: # 4+4 bit values
            integer_width = capture_bus_width // fft_bins_per_word // 2 
            data_width = 8 if eight_bit else 4
            interleave_factor = 1 
        elif cap_data_type == 4: # Dual resolution FFT
            integer_width = capture_bus_width // fft_bins_per_word // 2 * 2 # each Re and Im is twice the standard width (sending half the bins on each frame) (32 for CRS, 16 for ICE)
            data_width = adc_bit_width if fft_bypass else min(fft_data_width, integer_width)
            interleave_factor = 2 # one frame is even bins, one is odd bins
        elif cap_data_type == 5:  # Full resolution FFT
            # CRS: 128-bit complex, ICE: 32-bit complex. TODO: have this parametrized by the firmware
            integer_width = capture_bus_width  # in full resolution, each Re or Im integer uses the full capture bus width
            data_width = adc_bit_width if fft_bypass else min(fft_data_width, integer_width)
            interleave_factor = 2 * fft_bins_per_word
        elif cap_data_type == 6:  # Post-gain saturation flags (0, +1, -1 MSB-aligned per Re/Im)
            integer_width = capture_bus_width // fft_bins_per_word // 2
            data_width = integer_width
            interleave_factor = 1
        else:
            raise ValueError(f'Invalid capture data type: {cap_data_type}')

        cfg.capture_format.update(
            fmt_bus_width=integer_width, # number of bits used to store the value on the on the capture bus (multiple of 8 bits)
            fmt_data_width=data_width,
            fmt_is_complex=True,
            fmt_interleave_factor=interleave_factor,
            use_offset_encoding=use_offset_encoding,
        )
        cfg.ack(scaler_fmt_attrs, context=ctx)
        fft.config.ack(fft_fmt_attrs, context=ctx)
        return cfg

    def get_bypass(self):
        return bool(self.config.bypass)

    def reset_overflow_flags(self):
        self.CHAN_FIFO_OVERFLOW_RESET = 1
        self.CHAN_FIFO_OVERFLOW_RESET = 0

    def set_page(self, page):
        self.WRITE_COEFF_BANK_A = page & 0b1111
        self.WRITE_COEFF_BANK_B = (page >> 4) & 1
        self.WRITE_COEFF_BANK_C = (page >> 5) & 1

    def get_shift_left(self, bank=0):
        """Return the post-scaler left shift programmed for ``bank``.

        Parameters:
            bank (int): Gain bank (0 or 1).

        Returns:
            int: Left-shift amount in 0..31.
        """
        bank = int(bank)
        if bank not in (0, 1):
            raise ValueError(f'bank must be 0 or 1, got {bank}')
        if bank == 0:
            return int(self.SHIFT_LEFT_BANK0)
        return int(self.SHIFT_LEFT_BANK1)

    def set_shift_left(self, bank, value):
        """Set the post-scaler left shift for ``bank``.

        The value is stored in that bank's register immediately. The datapath
        uses it only while that bank is active, aligned with the gain table
        after RAM latency.

        Parameters:
            bank (int): Gain bank (0 or 1).
            value (int): Left-shift amount in 0..31.
        """
        bank = int(bank)
        if bank not in (0, 1):
            raise ValueError(f'bank must be 0 or 1, got {bank}')
        value = int(value)
        if value < 0 or value > 31:
            raise ValueError(f'shift left must be in 0..31, got {value}')
        if bank == 0:
            self.SHIFT_LEFT_BANK0 = value
        else:
            self.SHIFT_LEFT_BANK1 = value

    def set_gain_table(self, gain_list, bank=0, gain_timestamp=None, log_gain=None):
        """
        Sets the scaler's digital gain table for the specified bank.

        Gains are provided in natural bin order (increasing frequency order). This method reorders the gains internally to match the native FFT bin order. 

        If USE_COMPLEX_GAINS is True, the gain_list is interpreted as a 16+16 bit complex number, with log_gain stored in that bank's left-shift register if not None.
        If USE_COMPLEX_GAINS is False and log_gain >=0, the gain_list is interpreted as a 16-bit integer real gain and the log_gain is stored in that bank's left-shift register.
        If USE_COMPLEX_GAINS is False and log_gain < 0 or log_gain is None, the gain_list is interpreted as a floating point real gain and a common log gain is computed automatically and stored in that bank's left-shift register.
    
        Parameters:

            gain_list: gains to set. Those are in natural bin order.

                - if `gain_list` is an NBIN-element list or ndarray, the numeric gains therein are
                  applied to each bin.

                - if `gains_list` is a scalar int, float or complex numbers, all bins are set to that
                  scalar value.

                - if `gain_list` is `None`, no gains are set.

            bank (int): The bank in which the gains and left-shift are written (0 or 1).
                Sets ``READ_COEFF_BANK`` to this bank (the target bank, which becomes
                active on the next frame TLAST if synchronized switching is off).

            gain_timestamp: unix timestamp when the gains were calculated. If not provided, defaults
                to current time.

            log_gain (int): binary left shift to apply after the linear gain has been applied.
                Stored in this bank's left-shift register via ``set_shift_left``. The datapath
                uses it when this bank is active. See above for when log_gain is interpreted. 

        Returns:
            actual_gains (np.array): the gains that were actually set, after quantization and normalization, in natural bin order. 
            
            Complex and integer real gains do not include the log gain and should match 'gain_list' exactly after expansion to NBIN elements unless the provided gains wer enot integers.
 
            However, floating point gains **do** include the automatically computed common log gain. The actual will roughly match the provided gains, plus or minus the quantization effects if the mantissa has more than 11 bits of precision.
        """

        if gain_list is None:
            return

        total_bins = self.fpga.NUMBER_OF_FREQUENCY_BINS
        use_complex_gains = self.USE_COMPLEX_GAINS



        if use_complex_gains:
            if np.isscalar(gain_list):
                gains = np.ones(total_bins, dtype=complex) * gain_list
            else:
                gains = np.array(gain_list, dtype=complex)

            if any(gains.real < -32768) or any(gains.real > 32767) or any(gains.real != gains.real.astype('<i2')) or \
               any(gains.imag < -32768) or any(gains.imag > 32767) or any(gains.imag != gains.imag.astype('<i2')):
                raise ValueError('All real or imaginary parts of the gains must be integers between -32768 and 32767')

            if len(gains) != total_bins:
                raise ValueError(f'Either a scalar gain or a {total_bins} element gain vector must be provided')

            # Set the postscaler value
            if log_gain is not None:
                self.set_shift_left(bank, int(log_gain))

            self.cached_gain_table[bank] = gains
            self.cached_gain_timestamp[bank] = time.time() if gain_timestamp is None else gain_timestamp

            actual_gains = gains  # excludes log gain

            gains_in_native_order = self.fft.natural_to_fft_order(gains) # reorder into the FPGA's internal bin order
            gain_string = np.reshape(np.vstack((gains_in_native_order.imag, gains_in_native_order.real)).T, 2 * total_bins).astype('<i2').tobytes()

        else: # use real or floating point gains
            # print(f'Setting gains to {gain_list}')
            if np.isscalar(gain_list):
                gains = np.ones(total_bins) * gain_list
            else:
                gains = np.array(gain_list)

            # print(f'gains= {gains}')
                            # Set the postscaler value
            if len(gains) != total_bins:
                raise ValueError(f'Either a scalar gain or a {total_bins} element gain vector must be provided')

            if log_gain is None or log_gain < 0: # use floating point gains
                self.USE_FLOAT_GAINS = 1
                glog = (np.floor(np.log2(gains))-10).astype(np.int32).clip(0,63)  #
                glog_common = int(min(glog).clip(0, 31))
                self.set_shift_left(bank, glog_common)
                glog -= glog_common
                gains /= 2.0**(glog + glog_common)
                if any(gains < 0) or any(gains >= 2**11):
                    raise ValueError('Floating point gain mantissa exceeds 2**11')
                if any(glog < 0) or any(glog >31):
                    raise ValueError(f'Floating point gain exponent of {max(glog)} exceeds 31 even after removal of the common exponent of {glog_common}')

                glin = gains.astype(np.int16)
                glog = np.int16(glog)
                gain_words = (glog.astype(np.uint16) << 11) | glin
                actual_gains = 2**(glog + glog_common) * gains  # includes auto-computed log gain
                

            else: # use linear + log gains
                self.USE_FLOAT_GAINS = 0
                if any(gains < 0) or any(gains > 65535) or any(gains != gains.astype('<u2')):
                    raise ValueError('All gains must be integers between 0 and 65535')
                        # Set the postscaler value

                log_gain = int(log_gain)
                self.set_shift_left(bank, log_gain)

                gain_words = gains.astype(np.int16)
                actual_gains = gain_words.astype(float) # excludes log gain

            self.cached_gain_table[bank] = actual_gains.astype(float)
            self.cached_gain_timestamp[bank] = time.time() if gain_timestamp is None else gain_timestamp

            gain_words_in_native_order = self.fft.natural_to_fft_order(gain_words) # reorder into the FPGA's internal bin order
            gain_string = gain_words_in_native_order.astype('<i2').tobytes()

        n_pages = len(gain_string) // 512
        for page in range(n_pages):  # there are 8 pages of coefficients per bank
            self.set_page(page + bank*n_pages)
            self.write_ram(0, gain_string[512 * page: 512 * (page + 1)])

        self.READ_COEFF_BANK = bank

        return actual_gains

    def get_gain_table(self, bank=0, use_cache=False):
        """Gets the scaler's complex gain table for the specified bank.

        Converts the FPGA table to a numpy array. Linear (mantissa) values
        are returned; the left-shift must be read separately except in
        floating-gain mode (see below).

        Parameters:
            bank (int): Bank number for which the gain is requested.
            use_cache (bool): If True, return the cached table when present
                without reading the FPGA. If False, always read the FPGA.

        Returns:
            numpy.ndarray: One gain per bin, in natural frequency order
            (``self.fpga.NUMBER_OF_FREQUENCY_BINS`` values).

            If ``self.USE_COMPLEX_GAINS`` is True, complex 16-bit integer
            Re/Im. The log gain is **not** included; read it with
            ``get_shift_left(bank)``.

            If complex gains are off and float gains are off, 16-bit
            integers. The log gain is **not** included; read it with
            ``get_shift_left(bank)``.

            If complex gains are off and float gains are on, floating-point
            values that include **both** the per-bin log gain and the common
            log gain from ``get_shift_left(bank)``.
        """
        if use_cache and bank in self.cached_gain_table:
            return self.cached_gain_table[bank]

        page_table = np.zeros(512, np.int8)
        gain_table = []   # np.zeros(self.fpga.NUMBER_OF_FREQUENCY_BINS, complex)

        if self.USE_COMPLEX_GAINS:
            pages_per_bank = 8
            for page in range(pages_per_bank):  # there are 8 pages of coefficients per bank
                self.WRITE_COEFF_BANK = pages_per_bank * bank + page  # Sets which page/bank being read? Not sure if will work...

                # TODO: Read_ram now requires specifying the type of data the following line will fail
                page_table = self.read_ram(0, length=512)
                for ix in range(128):  # there are 128 coefficients per page (4 byte per coeff = 512 bytes total per page)
                    g_imag, g_real = struct.unpack('<hh', page_table[4 * ix: 4 * ix + 4])
                    gain_table.append(g_real + 1j * g_imag)  # [bin] = g_real +1j*g_imag

            gain_table = np.array(gain_table)
            gain_table_in_natural_order = self.fft.fft_to_natural_order(gain_table) # reorder into natural order

        else: # if we don't have complex gains, it's 16-bit or floating point real gains.

            # First, read the 16-bit values for each bin in the bank.
            pages_per_bank = 32
            for page in range(32):  # there are 32 pages of coefficients per bank

                abs_page = pages_per_bank * bank + page

                self.WRITE_COEFF_BANK_A = abs_page & 0b1111
                self.WRITE_COEFF_BANK_B = (abs_page >> 4) & 1
                self.WRITE_COEFF_BANK_C = (abs_page >> 5) & 1

                page_table = self.read_ram(0, length=512//2, type = '<u2')
                # for ix in range(256):  # there are 256 coefficients per page (2 bytes per coeff = 512 bytes total per page)
                #     g = struct.unpack('<hh', page_table[2 * ix: 2 * ix + 2])
                gain_table.append(page_table.tolist())

            # Decode floating point values if needed.
            if self.USE_FLOAT_GAINS:
                # Additional processing to return floating point gains as a single value for each frequency bin
                gain_table = (np.array(gain_table) & 0xFFFF).flatten() # Full 16-bit value, meaningless without being processed
                gain_mant = (gain_table & 0x7FF).astype(float) # Lower 11 bits, linear (mantissa) component
                gain_exp = ((gain_table >> 11) + self.get_shift_left(bank)).astype(float) # Upper 5 bits, logarithmic (exponential) component, plus common log component
                gain_table = gain_mant * 2**gain_exp # Full gain can be expressed as a single value 

            # Reorder into natural order.
            gain_table_in_natural_order = self.fft.fft_to_natural_order(gain_table) # reorder into natural order

        return gain_table_in_natural_order

    def get_gains_timestamp(self, bank=0):
        """
        Gets the scaler's gain timestamp, which is the last time the gains were written to the FPGA.

        Parameters:

            bank (int): bank number for which the timestamp is requested

        Returns:

            a time.time() timestamp. None if the gain was never set.

        """
        return self.cached_gain_timestamp.get(bank, None)

    def status(self):
        """ Displays the status of the scaler module"""
        print(f'-------------- CHAN[{int(self.instance_number)}].SCALER STATUS --------------')
        print(f' SCALER Bypass: {bool(self.BYPASS)}')
        print(f' Shift left bank 0: {self.get_shift_left(0)}')
        print(f' Shift left bank 1: {self.get_shift_left(1)}')

    def get_sim_output(self, scaler_input):
        bypass = self.BYPASS

        four_bits = not self.EIGHT_BIT_SUPPORT and self.FOUR_BITS
        if not four_bits:
            raise RuntimeError('8-bit mode not supported by simulation model yet')

        word = [None] * 4
        (flags, word[0], word[1], word[2], word[3]) = scaler_input

        (number_of_frames, words_per_frame) = word.shape
        if bypass:
            word = np.array(word, dtype=int) & 0xff  # 8 bit values
            data = (word[0] << 24) | (word[1] << 16) | (word[2] << 8) | (word[3] << 0)
            return (flags, data)
        else:
            if any(word < -1 << 17) or any(word >= 1 << 17):
                raise ValueError('input values overflows a signed 18-bit word')
            data_real = np.reshape([word[0], word[2]], (number_of_frames, 2 * words_per_frame), order='F')
            data_imag = np.reshape([word[1], word[3]], (number_of_frames, 2 * words_per_frame), order='F')
            gains = self.get_gain_table()  # 16 bits
            shift_left = self.get_shift_left(self.CURRENT_GAIN_BANK)
            rounding_mode = self.ROUNDING_MODE
            zero_on_sat = self.ZERO_ON_SATURATION
            offset_binary = self.USE_OFFSET_BINARY
            (r_ovf, r_real, r_imag) = self.scale(
                (data_real, data_imag),
                gains=gains,
                shift_left=shift_left,
                rounding_mode=rounding_mode,
                zero_on_sat=zero_on_sat,
                offset_binary=offset_binary)

            r_data = (r_real[0::2] << 24) | (r_imag[0::2] << 16) | (r_real[1::2] << 8) | (r_imag[1::2] << 0)
            r_flags = (r_ovf[0::2] << 3) | (r_ovf[1::2] << 2) | flags

            return (r_flags, r_data)

    def scale(self,
              data,
              gains=None,
              shift_left=31,
              rounding_mode=ROUNDING_MODE_CONVERGENT_ROUND,
              zero_on_sat=False,
              offset_binary=False):

        """ Compute the thoretical output of the scaler.
        ``data`` a (real, imag) tuple of N-dimentional array of (18+18) bits complex values.
        """
        # Complex product = (a+bj)(c+dj) = (ac-bd) + j(bc+ad)
        #
        # Width: assume b,c,a,d are 8 bits and are -128. (bc+ad) =
        # 2*-128*-128 = +32768, need (8+8+1) bit to hold worst-case signed
        # product So, in our case, we need (18+16+1)=35 bits unsigned
        # value.
        (data_real, data_imag) = data
        (gains_real, gains_imag) = (gains.real.astype(int), gains.imag.astype(int))
        stage0_real = data_real * gains_real - data_imag * gains_imag  # (18+18) bits * (16+16) bits = (35+35) bits
        stage0_imag = data_real * gains_imag + data_imag * gains_real

        # word_var = np.array([even.real, even.imag, odd.real, odd.imag], dtype=np.int64)
        # stage1_sign = word_var & (1 << 34).astype(bool)  # Sign on bit 34

        # store real and imag part in an array so we can process them together.
        stage1_word = np.array([stage0_real, stage0_imag], int) << shift_left

        # stage1_overflow = (-1<<34) > stage1_word >= (1<<34)

        if rounding_mode == self.ROUNDING_MODE_ROUND:
            stage2_word = stage1_word + (1 << 30)
        elif rounding_mode == self.ROUNDING_MODE_CONVERGENT_ROUND:
            stage2_word = stage1_word + (1 << 30)
            stage2_word &= ~((stage2_word & ((1 << 30) - 1)).astype(bool) * (1 << 30))
        else:
            stage2_word = stage1_word

        stage3_word = stage2_word >> 31
        max_value = 7
        min_value = -8 if self.SATURATE_ON_MINUS_7 else -7
        stage3_overflow = (stage2_word < min_value) | (stage2_word > max_value)
        stage3_word = np.clip(stage3_word, min_value, max_value)
        data_overflow = (stage3_overflow[0] | stage3_overflow[1]).astype(bool)
        if zero_on_sat:
            stage3_word *= (data_overflow ^ 1)
        if self.USE_OFFSET_BINARY:
            stage3_word ^= 0x80
        stage3_word <<= 4
        return (data_overflow, stage3_word[0], stage3_word[1])
