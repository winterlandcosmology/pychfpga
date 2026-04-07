#!/usr/bin/python

""" Implements the interface to the PFB/FFT FPGA firmware module

.. History:
    2011-07-12 : JFC : Created from test code in chFPGA.py
    2012-05-29 JFC: Extracted frm ANT.py
    2012-09-05 JFC: Fixed PIPELINE_DELAY property: was STATUS instead of CONTROL
    2012-09-20 JFC: Changed FFT_SHIFT init value: left at default. FW was updated with updated value.
        Added OVERFLOW_RESET, SOFT_RESET bitfields to match FW
"""
import time
import numpy as np
from ..mmi import MMI, BitField, CONTROL, STATUS


# Lookup table to provide information of the FFT implemented in the firmware. Is indexed using the FFT_TYPE value provided by the firmware.
FFT_INFO = {
    0: dict(name='NONE', latency=0, bins_per_word=0, unscrambled=False),
    1: dict(name='CHIME', latency=3230, samples_per_frame=2048, bits_per_sample=8, bits_per_bin=18+18, bins_per_word=2, unscrambled=True),
    2: dict(name='D3A', latency=12533, samples_per_frame=16384, bits_per_sample=14, bits_per_bin=18+18, bins_per_word=4, unscrambled=True),
    3: dict(name='CHORD', latency=10452, samples_per_frame=16384, bits_per_sample=14, bits_per_bin=32+32, bins_per_word=4, unscrambled=False),
    4: dict(name='HIRAX', latency=0, samples_per_frame=2048, bits_per_sample=18+18, bits_per_bins=29+29, bins_per_word=2, unscrambled=False)
}

class FFT(MMI):
    """ Implements interface to the FR_DIST within a procecessor pipeline"""

    ADDRESS_WIDTH = 9

    # Control registers
    SOFT_RESET     = BitField(CONTROL, 0x00, 7, doc="Resets the module (also performs a DLY_RESET).")
    OVERFLOW_RESET = BitField(CONTROL, 0x00, 2, doc="Resets the overflow counter.")
    OVERFLOW_FORCE = BitField(CONTROL, 0x00, 3, doc="Force the overflow counter to count (for debugging).")
    # DLY_RESET      = BitField(CONTROL, 0x00, 1, doc="Reset the computation of the CASPER block pipelining delay. When released, the block will re-learn the block latency once a CASPER SYNC has passed through the block.")
    BYPASS         = BitField(CONTROL, 0x00, 0, doc="Bypass the FFT")
    FFT_SHIFT      = BitField(CONTROL, 0x02, 0, width=14, doc="FFT shift enable bit for each of the FFT stage")
    SYNC_PERIOD    = BitField(CONTROL, 0x04, 0, width=16, doc="Number of clock cycles between SYNC pulses. See CASPER documentation for minimum SYNC spacing.")
    PIPELINE_DELAY = BitField(CONTROL, 0x06, 0, width=16, doc="Latency (in number of clocks) of the CASPER PFB/FFT")

    # Status registers
    MEASURED_PIPELINE_DELAY = BitField(STATUS, 1, 0, width=16, doc="Latency (in numbe rof clocks) of the CASPER PFB/FFT")
    OVERFLOW_COUNT          = BitField(STATUS, 2, 0, width=8, doc="Number of FFT overflows since reset (rolls back)")
    FFT_TYPE                = BitField(STATUS, 3, 0, width=3, doc="Type of FFT implemented. Use to lookup FFT_INFO map.")
    FFT_DELAY_CTR_WIDTH     = BitField(STATUS, 3, 3, width=5, doc="Number of bits in the delay line counter")
    IMPLEMENT_FFT     = BitField(STATUS, 4, 7, doc="'1' if FFT in implemented")
    ROTATE_BINS       = BitField(STATUS, 4, 6, doc="'1' if FFT bin rotation is enabled")
    RESET_MON         = BitField(STATUS, 4, 5, doc="Monitors the reset line")
    def __init__(self, *, router, router_port, instance_number):
        super().__init__(router=router, router_port=router_port, instance_number=instance_number)
        self.config_id = 0  # Increases every time a configuration change is made

    def reset(self):
        self.pulse_bit('RESET')

    def init(self):
        """ Initialize the FFT module"""

        # Set the FFT Pipeline delay.
        self.PIPELINE_DELAY = self.fpga.FFT_LATENCY

        info = FFT_INFO[self.FFT_TYPE]
        self.n_lanes = info['bins_per_word']
        self.n_bins = info['samples_per_frame'] // 2
        self.n_bins_per_lane = self.n_bins // self.n_lanes
        self.is_scrambled = not info['unscrambled']
        self.is_rotated = self.ROTATE_BINS

        # For the CRS, the pipeline delay is not yet measured at this point, and is not measured even if we pulse SOFT_RESET.
        # So we can't check if it is right until the pipeline is running. This is why we disable the check below
        # if self.PIPELINE_DELAY != self.MEASURED_PIPELINE_DELAY:
        #    raise Exception('FFT pipeline delay is not set to the measured value!')

    def get_fft_overflow_count(self):
        return self.OVERFLOW_COUNT

    def reset_fft_overflow_count(self):
        self.OVERFLOW_RESET = 1
        self.OVERFLOW_RESET = 0

    def get_bin_map(self, flatten=False, reverse=False):
        """ Returns an array providing the bin number sent on every clock by every FFT output lane.

        Note that the map changes  during operation if BYPASS is changed.

        If BYPASS=1, the map is simply bin_number=real_sample_number//2 = complex_sample_number.


        Parameters:

            flatten (bool): If True, the map will be flatten, lane being the fast axis, then bin index.

            reverse (bool): If True, returns the list of index at which each bin can be found. `flatten` is ignored.

        Returns:

            (n_lanes, n_bins_per_lane) numpy array indicating the bin numbers emitted on each clock
                by each output lane of the FFT. Each column (bins=x[:,clk]) represents the bins
                emitted on one clock.
        """
        # Compute bin numbers at the output of the FFT, including bin rotation.
        # We split the data into 4 lanes since the FFT outputs 4 numbers per clock.
        # fft_bins = np.array([[n_bins_per_lane*((lane - (rotate*clk & 0b11)) & 0b11) + clk for clk in range(n_bins_per_lane)] for lane in range(n_lanes)])

        if self.BYPASS:
            bins = np.arange(self.n_bins).reshape((self.n_lanes, -1), order='F')
        else:
            # bins coming out straight out of the CASPER FFT
            bins = np.arange(self.n_bins).reshape((self.n_lanes, -1), order='C' if self.is_scrambled else 'F')
            if self.is_rotated:
                bins = (bins - np.arange(self.n_bins_per_lane) * self.n_bins_per_lane) % self.n_bins

        fbins = bins.flatten(order='F')

        if reverse:
            rbins = np.empty(self.n_bins, dtype=np.intp)
            rbins[fbins] = np.arange(self.n_bins)
            return rbins

        return fbins if flatten else bins

    def status(self):
        """ Displays the status of the data capture module"""
        print('-------------- CHAN[%i].FFT STATUS --------------' % self.instance_number)
        print(' FFT Bypass: %s' % (bool(self.BYPASS)))
        print(' FFT SHIFT schedule: 0x%X' % (self.FFT_SHIFT))
        print(' CASPER block pipeling delay: Measured=%i, set point=%i:  clocks' % (self.MEASURED_PIPELINE_DELAY, self.PIPELINE_DELAY))
        print(' Number of FFT overflows: %i' % (self.OVERFLOW_COUNT))

    def pfb_fft(self, data, N=4):
        """
        N = window size
        """
        # Compute window function
        frame_length = len(data[0])
        number_of_frames = len(data)
        sinc_window = np.sinc((np.arange(-frame_length * N // 2, frame_length * N // 2) + 0.5) / frame_length)
        hamming_window = np.hamming(frame_length * N)
        window = np.reshape((sinc_window * hamming_window * 512), (4, -1))

        windowed_data = [np.sum(data[n: n+4, :] * window, axis=0) for n in range(0, number_of_frames - 4 + 1)]
        fft = np.fft.rfft(windowed_data)
        return fft

    def get_sim_output(self, fft_input, bypass = None):
        """ Return the simulated output of the FFT module.

        Parameters:

            input:  is typically the data coming from the function generator. It is in the
                format (flags, data). Data is 32-bit, preferably stored in big endian format. sample 0
                is on the Most significant byte.

            bypass (bool):

        Returns:

            tuple: The output is a tuple of 512 element arrays containing ``(flags, even_real, even_imag, odd_real, odd_imag)`` where
                ``flags`` is a uint8, with bits as follows:

                - bit 3: ADC overflow (valid on the last element of the frame)
                - bit 2: EVEN bins scaler overflow (always zero for simulations)
                - bit 1: ODD bins scaler overflow (always zero for simulations)
                - bit 0: FFT overflow (always zero for simulations)
        """
        (flags, data) = fft_input
        data_bytes = data.astype('>u4', copy=False).view(np.int8)  # signed. astype won't do anything if input is already '>u4'
        (number_of_frames, frame_length) = data_bytes.shape

        if bypass is None:
            bypass = self.BYPASS or not self.fpga.NUMBER_OF_ANTENNAS_WITH_FFT

        if bypass:
            return (flags,
                    data_bytes[:, 0::4].astype('>i4'),
                    data_bytes[:, 1::4].astype('>i4'),
                    data_bytes[:, 2::4].astype('>i4'),
                    data_bytes[:, 3::4].astype('>i4'))

        fft_shift = self.FFT_SHIFT  # Read only once from the FPGA
        number_of_shifts = sum(bool(fft_shift & (1 << bit) for bit in range(11)))
        shift_factor = 2. ** number_of_shifts
        fft = self.pfb_fft(data, N=4) / shift_factor
        even = fft[0::2]
        odd = fft[1::2]
        return (flags,
                even.real().astype('>i4'),
                even.imag().astype('>i4'),
                odd.real().astype('>i4'),
                odd.imag().astype('>i4'))
