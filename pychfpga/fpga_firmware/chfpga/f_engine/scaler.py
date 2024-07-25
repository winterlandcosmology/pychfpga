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
from ..mmi import MMI, BitField


class SCALER(MMI):
    """ Implements interface to the SCALER module within a procecessor pipeline"""
    # Create local variables for page numbers tomake the table more readable
    CONTROL = BitField.CONTROL
    STATUS = BitField.STATUS

    # Define Control registers
    RESET                 = BitField(CONTROL, 0x00, 7, doc="Reset the SCALER.")
    BYPASS                = BitField(CONTROL, 0x00, 6, doc="Bypass the SCALER")
    FOUR_BITS             = BitField(CONTROL, 0x00, 5, doc="Enables 4-bit operation")
    SHIFT_LEFT            = BitField(CONTROL, 0x00, 0, width=5, doc="Number of bits to shift left the incoming data")
    USE_OFFSET_BINARY     = BitField(CONTROL, 0x01, 6, doc="When '1', offset binary encoding is used.")
    # USE_GAIN_TABLE        = BitField(CONTROL, 0x01, 5, doc="When '1', the gain tables are used to apply a bin-by-bin complex gain. Otherwise, the fixed complex gain is used for all bins.")
    READ_COEFF_BANK       = BitField(CONTROL, 0x01, 4, doc="Target gain coefficients bank to be used by the scaler")
    WRITE_COEFF_BANK_A      = BitField(CONTROL, 0x01, 0, width=4, doc="Indicates in which data page the gain coefficients are being written to. Page 0-7 are coefficients fri bank0, Page 8-15 are for Bank 1 coefficients.")
    WRITE_COEFF_BANK_B      = BitField(CONTROL, 0x01, 5, doc="Bit 4 of the write page")
    WRITE_COEFF_BANK_C      = BitField(CONTROL, 0x01, 7, doc="Bit 5 of the write page")

    STATS_CAPTURE         = BitField(CONTROL, 0x02, 7, doc="When '1', New saturation/overflow stats are captured")
    SATURATE_ON_MINUS_7   = BitField(CONTROL, 0x02, 6, doc="When '1', Values will saturate at -7 instead of -8.")
    ZERO_ON_SATURATION    = BitField(CONTROL, 0x02, 5, doc="When '1', Both real and Imaginary parts are zeroed when either of them overflow.")
    SYNCHRONIZE_GAIN_BANK = BitField(CONTROL, 0x02, 4, doc="When '1', The target bank number will be enabled at the target frame number.")
    FORCE_GAIN_TO_ONE     = BitField(CONTROL, 0x02, 2, doc="Force gain to (1+0j) (complex gain) or 1 (real gain). Gain table is ignored, but post-scaler (SHIFT_LEFT) still applies. ")
    CHAN_FIFO_OVERFLOW_RESET = BitField(CONTROL, 0x02, 3, doc="Resets the Channel FIFO overflow flag")
    ROUNDING_MODE         = BitField(CONTROL, 0x02, 0, width=2, doc="Set rounding mode.  0: Truncate, 1: Round, 2: Convergent Rounding")
    STATS_FRAME_COUNT     = BitField(CONTROL, 0x05, 0, width=24, doc="Number of frames to inclue in stats results.")

    GAIN_BANK_SWITCH_FRAME_NUMBER = BitField(CONTROL, 0x09, 0, width=32, doc="Frame number at which the target gain bak is to be activated.")
    DATA_TYPE             = BitField(CONTROL, 0x0A, 0, width=2, doc=" Selects the output data in conjunction with Bypass.\n"
                                "   BYPASS=0, DATA_TYPE=0: Send normal scaled data in 4 or 8 bit mode\n"
                                "   BYPASS=0, DATA_TYPE=1: Send (1+0j) if there is a saturation on either Re or Im\n"
                                "   BYPASS=0, DATA_TYPE=2: Send (1+0j) if Re has a positive saturation and (0+1j) if it has a negative saturation\n"
                                "   BYPASS=0, DATA_TYPE=3: Send (1+0j) if Im has a positive saturation and (0+1j) if it has a negative saturation\n"
                                "   BYPASS=1, DATA_TYPE=0: Send the most significant bits of the FFT input to the scaler\n"
                                "   BYPASS=1, DATA_TYPE=1: Send the most significant bits of the 34-bit left-shifted gain product, saturated to the word limits\n"
                                "   BYPASS=1, DATA_TYPE=2-3: Unused (all zeros)\n"
                                )
    STATS_READY            = BitField(STATUS, 0x00, 7, doc="Indicates that new stats results are ready")
    CURRENT_GAIN_BANK      = BitField(STATUS, 0x00, 6, doc="Currently active gain bank.")
    EIGHT_BIT_SUPPORT      = BitField(STATUS, 0x00, 5, doc="'1' when the SCALER supports 8-bit output")

    CHAN_FIFO_OVERFLOW      = BitField(STATUS, 0x00, 4, doc="'Channel FIFO overflow flag, sticky")
    USE_COMPLEX_GAINS      = BitField(STATUS, 0x00, 3, doc="1 if (16+16) bits complex gains are used, otherwise 16-bits real gains are used.")

    STATS_SCALER_OVERFLOWS = BitField(STATUS, 0x02, 0, width=16, doc="Stats result: number of scaler overflows")
    STATS_ADC_OVERFLOWS    = BitField(STATUS, 0x04, 0, width=16, doc="Stats result: number of ADC overflows")
    FRAME_CTR              = BitField(STATUS, 0x05, 0, width=8, doc="Free running frame counter (last 8 bits)")
    DELAY_CTR              = BitField(STATUS, 0x07, 0, width=16, doc="Debug: Delay counter")


    ROUNDING_MODE_TRUNCATE         = 0b00
    ROUNDING_MODE_ROUND            = 0b01
    ROUNDING_MODE_CONVERGENT_ROUND = 0b10

    # Define Status registers

    def __init__(self, fpga_instance, base_address, instance_number):

        super().__init__(fpga_instance, base_address, instance_number)

        self.cached_gain_table = {}
        self.cached_gain_timestamp = {}

    def reset(self):
        """ Resets the SCALER module """
        self.pulse_bit('RESET')

    def init(self):
        """ Initialize the SCALER module"""
        # Bypass the scaler by default if the FFT is not present
        if self.instance_number in self.fpga.LIST_OF_ANTENNAS_WITH_FFT:
            self.BYPASS = 0
        else:
            self.BYPASS = 1
        # self.BYPASS = 1
        # self.SHIFT_LEFT = 10
        self.SHIFT_LEFT = 31
        # self.USE_GAIN_TABLE = 1
        self.USE_OFFSET_BINARY = 1
        # self.set_fixed_gain(1)
        self.set_gain_table(1)
        self.SATURATE_ON_MINUS_7 = 1
        self.STATS_CAPTURE = 1
        self.STATS_FRAME_COUNT = int(800e6 / 2048 * 30)

    # def set_fixed_gain(self, complex_gain):
    #     """
    #     Sets the scaler's fixed gain complex value.
    #     """

    #     if complex_gain.real<-32768 or complex_gain.real > 32767 or complex_gain.imag<-32768 or
    #                complex_gain.imag>32767:
    #         raise ValueError("Invalid fixed gain")

    #     self.FIXED_GAIN_REAL = np.int16(complex_gain.real)
    #     self.FIXED_GAIN_IMAG = np.int16(complex_gain.imag)

    #     # if use_gain_table is not None:
    #     #     self.USE_GAIN_TABLE = use_gain_table

    # def get_fixed_gain(self):
    #     """
    #     Returns the scaler's fixed gain complex value.
    #     """
    #     return np.int16(self.FIXED_GAIN_REAL) + 1j*np.int16(self.FIXED_GAIN_IMAG)

    def set_page(self, page):
        self.WRITE_COEFF_BANK_A = page & 0b1111
        self.WRITE_COEFF_BANK_B = (page >> 4) & 1
        self.WRITE_COEFF_BANK_C = (page >> 5) & 1

    def set_gain_table(self, gain_list, bank=0, gain_timestamp=None):
        """
        Sets the scaler's digital gain table for the specified bank.


        Parameters:

            gain_list: gains to set:

                - if `gain_list` is a 1024-element list or ndarray, the numeric gains therein are
                  applied to each bin.

                - if `gains_list is a scalar int, float or complex numbers, all bins are set to that
                  scalar value.

                - if `gain_list` is `None`, no gains are set.

            bank (int): The bank in which the gains are to be written. If ``bank`` is None or is -1,
                the currently inactive bank is used. The method does not set the active bank.

            gain_timestamp: unix timestamp when the gains were calculated. If not provided, defaults
                to current time.
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

            self.cached_gain_table[bank] = gains
            self.cached_gain_timestamp[bank] = time.time() if gain_timestamp is None else gain_timestamp

            gain_string = np.reshape(np.vstack((gains.imag, gains.real)).T, 2 * total_bins).astype('<i2').tobytes()

        else: # use real gains
            # print(f'Setting gains to {gain_list}')
            if np.isscalar(gain_list):
                gains = np.ones(total_bins) * gain_list
            else:
                gains = np.array(gain_list)

            # print(f'gains= {gains}')
            if any(gains < -32768) or any(gains > 32767) or any(gains != gains.astype('<i2')):
                raise ValueError('All gains must be integers between -32768 and 32767')

            if len(gains) != total_bins:
                raise ValueError(f'Either a scalar gain or a {total_bins} element gain vector must be provided')

            self.cached_gain_table[bank] = gains
            self.cached_gain_timestamp[bank] = time.time() if gain_timestamp is None else gain_timestamp

            gain_string = gains.astype('<i2').tobytes()

        # page_table = np.zeros(512, np.int8)
        n_pages = len(gain_string) // 512
        for page in range(n_pages):  # there are 8 pages of coefficients per bank
            self.set_page(page + bank*n_pages)
            # self.WRITE_COEFF_BANK = 8 * bank + page
            self.write_ram(0, gain_string[512 * page: 512 * (page + 1)])

        self.READ_COEFF_BANK = bank

    def get_gain_table(self, bank=0, use_cache=False):
        """Gets the scaler's complex gain table for the specified bank. Converts to numpy array.

        Args:

            bank (int): bank number for which the gain is requested

        Returns:

            Gain table, as a list of self.fpga.NUMBER_OF_FREQUENCY_BINS values. If self.USE_COMPLEX_GAINS == True, we
            have complex values, where the real and imaginary parts are 16 bit integers. If self.USE_COMPLEX_GAINS == False,
            we just have real gains.
        """
        if use_cache and bank in self.cached_gain_table:
            return self.cached_gain_table[bank]

        page_table = np.zeros(512, np.int8)
        gain_table = []   # np.zeros(self.fpga.NUMBER_OF_FREQUENCY_BINS, np.complex)

        if self.USE_COMPLEX_GAINS:
            pages_per_bank = 8
            for page in range(pages_per_bank):  # there are 8 pages of coefficients per bank
                self.WRITE_COEFF_BANK = pages_per_bank * bank + page  # Sets which page/bank being read? Not sure if will work...

                # TODO: Read_ram now requires specifying the type of data the following line will fail
                page_table = self.read_ram(0, length=512)
                for ix in range(128):  # there are 128 coefficients per page (4 byte per coeff = 512 bytes total per page)
                    g_imag, g_real = struct.unpack('<hh', page_table[4 * ix: 4 * ix + 4])
                    gain_table.append(g_real + 1j * g_imag)  # [bin] = g_real +1j*g_imag

        else: # if not self.USE_COMPLEX_GAINS
            pages_per_bank = 32
            for page in range(32):  # there are 32 pages of coefficients per bank

                abs_page = pages_per_bank * bank + page

                self.WRITE_COEFF_BANK_A = abs_page & 0b1111
                self.WRITE_COEFF_BANK_B = (abs_page >> 4) & 1
                self.WRITE_COEFF_BANK_C = (abs_page >> 5) & 1

                page_table = self.read_ram(0, length=512/2, type = '<i2')
                # for ix in range(256):  # there are 256 coefficients per page (2 bytes per coeff = 512 bytes total per page)
                #     g = struct.unpack('<hh', page_table[2 * ix: 2 * ix + 2])
                gain_table.append(page_table.tolist())

        return gain_table

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
        print('-------------- CHAN[%i].SCALER STATUS --------------' % self.instance_number)
        print(' SCALER Bypass: %s' % (bool(self.BYPASS)))
        print(' Shift left: %i' % self.SHIFT_LEFT)

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
            shift_left = self.SHIFT_LEFT
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
