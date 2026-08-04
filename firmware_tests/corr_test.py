from pychfpga.fpga_array import FPGAArray
from .module_test import TestModule
import pytest
import logging
from .utils import gen_data, plot
from wtl.namespace import NameSpace
import matplotlib.pyplot as plt
import numpy as np

class TestCorr(TestModule):

    def get_constants(_, board):
        constants = NameSpace()
        constants.NUM_SAMPLES = board.ADC_SAMPLES_PER_FRAME
        constants.NUM_BINS = board.NUMBER_OF_FREQUENCY_BINS
        constants.ADC_WIDTH = board.ADC_BITS_PER_SAMPLE
        constants.NUM_FRAMES = 32000 #means 4 doesn't saturate
        constants.NUM_CHANNELS = board.NUMBER_OF_CHANNELIZERS
        print(board.NUMBER_OF_CHANNELIZERS)
        constants.NUM_CORR_PRODUCTS = constants.NUM_CHANNELS * (constants.NUM_CHANNELS + 1) // 2
        return constants
    
    @pytest.fixture(scope='function', autouse=True)
    def setup(self, xr):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = self.board_conn(xr)
        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME
        self.data.NUM_BINS = self.board.NUMBER_OF_FREQUENCY_BINS
        self.data.ADC_WIDTH = 14
        self.data.NUM_FRAMES = 32000 #means 4 doesn't saturate
        self.data.NUM_CHANNELS = 8
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)
        # check that fifo overflow doesn't go high either before or after test
        self.board.stop_data_capture()
       
    def _inject_spectrum(self, func, cap_mode=0, postscaler=26, channels=None, **kwargs):
        spectrum = gen_data(func, samples=self.data.NUM_SAMPLES, **kwargs)
        spectrum_cmplx = spectrum[::2] + 1j*spectrum[1::2]
        self.board.sync()
        self.board.set_data_source(source='arb', data=spectrum, channels=channels)
        self.board.set_channelizer(
                        fft_bypass=True, 
                        scaler_bypass=False, 
                        scaler_bypass_data_type=0, 
                        scaler_cap_data_type=cap_mode,
                        postscaler=postscaler,
                        gains=1,
                        symmetric_saturation=False,
                        offset_binary_encoding=False
                    ) #other channel parameters do not change so does not need to be specific to selected channels
        return spectrum_cmplx

    def _read_spectrum(self):
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        self.board.start_data_capture(period=0.1, source='scaler')
        self.board.sync()
        _, data, _ = receiver.read_raw_frames(split=True)
        data = data[:, -1, :] #get last frame
        return data

    def _sacrificial_bins(self):
        """Absolute bins co-opted for UltraRAM readout (invalid correlation data).

        Each correlator steals one local bin address for the readout bank
        (``CONFIG_CORR_READOUT_BINS``). On CRS corr8 that is ``(0, 1, 2, 3)`` —
        local index equals correlator index — so absolute bins follow the current
        FFT/CT bin map. With FFT bypass that is ``0, 5, 10, 15``, not ``0..3``.
        """
        bin_map, _ = self.board.CORR.get_bin_map()
        ncorr = bin_map.shape[1]
        corrs = np.arange(ncorr)
        return np.unique(np.asarray(bin_map[:, corrs, corrs, 0], dtype=np.intp))

    def _drop_sacrificial_bins(self, data, axis=1):
        """Remove UltraRAM readout-steal bins along the frequency-bin axis."""
        keep = np.ones(data.shape[axis], dtype=bool)
        keep[self._sacrificial_bins()] = False
        return np.compress(keep, data, axis=axis)

    def _corr_product_index(self, i, j):
        """Index of the auto-/cross-correlation of channels ``i`` and ``j``.

        Products are stored in pper-triangular order including the diagonal:
        ``(0,0), (0,1), ..., (0,nchan-1), (1,1), (1,2), ..., (nchan-1, nchan-1)``.
        ``(i, j)`` and ``(j, i)`` map to the same product.
        """
        if i > j:
            i, j = j, i
        return i * (2 * self.data.NUM_CHANNELS - i - 1) // 2 + j

    def _compute_psd(self, spectrum, num_frames=32000, normalise=True):
        res = np.clip(self._drop_sacrificial_bins((spectrum * np.conjugate(spectrum) * num_frames).astype(np.complex128), axis=0), 0, 131071)
        if normalise:
            res /= num_frames
        return res
    
    def _read_corr_data(self, num_frames=32000, num_soft_frames=1, max_tries=5, all_channels_equal=True, no_accum=False, normalise=True, return_sat=False, autocorr_only=False):
        # Leave UCAP in reset (fixture already called stop_data_capture). Correlator
        # packets are merged independently via axis_merge; starting UCAP only adds
        # raw traffic that can starve / confuse the corr receiver.
        self.board.stop_correlator()
        # One integration is 4*2048=8192 packets; keep room for >1 integ in the UDP batch buffer.
        corr_receiver = self.board.get_corr_receiver(
            buf_size=8192 * (num_soft_frames + 1), verbose=self.test_config.loglevelrecv)
        self.board.start_correlator(integration_period=num_frames,
                                autocorr_only = autocorr_only,
                                no_accum=no_accum,
                                verbose=self.test_config.loglevelrecv
                                )
        # Sync after start so the correlator sees aligned frames from a clean edge.
        self.board.sync()
        corr_receiver.update_config(self.board.CORR.config)
        corr_receiver.update_config({"soft_integ_period": int(num_soft_frames)})
        expected_packets = int(corr_receiver.expected_packets)
        corr_receiver.flush()
        pkt_count = 0
        num_tries = 0
        while pkt_count < expected_packets and num_tries < max_tries:
            _integ_ts, d_corr, sat, counts = corr_receiver.read_corr_frames(
                soft_integ_period=num_soft_frames,
                data_timeout=(num_frames / self.board.FRAME_RATE + 0.5) * (num_soft_frames + 1),
            )
            pkt_count = int(np.min(counts))
            num_tries += 1
        self.board.stop_correlator()
        d_corr = self._drop_sacrificial_bins(d_corr, axis=1)
        sat = self._drop_sacrificial_bins(sat, axis=1)
        if normalise and not no_accum:
            d_corr /= num_frames
        if all_channels_equal: #assert all autocorrelations and cross-correlations are equal
            assert np.all(np.swapaxes(d_corr, 1, 2) == d_corr[0, :, 0])
        print(f"{pkt_count=}, {expected_packets=}")
        if pkt_count != expected_packets:
            raise ValueError(
                f"Got {pkt_count}/{expected_packets} packets from correlator "
                f"(soft_integ_period={num_soft_frames})"
            )
        if return_sat:
            return d_corr, sat
        return d_corr
    
    @pytest.mark.corr
    @pytest.mark.parametrize('value', (1,-1, 2))
    @pytest.mark.parametrize('cap_mode', (0, 4))
    def test_constant_spectrum_injection(self, value, cap_mode, xr):
        '''
        Tests that in the function generator can be used to simulate an FFT spectrum when the FFT is bypassed and its output is read back (aka "spectrum injection"). 
        This capability is critical for most of the correlator tests. 

        Parameters:
            value (int): value to be injected by the function generator
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('a', a=value, cap_mode=cap_mode)
        data = self._read_spectrum()
        plot([data[0], spectrum], split_complex=True, title=f"Constant spectrum of {value}")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('cap_mode', (0, 4))
    def test_complex_ramp_spectrum_injection(self, cap_mode, xr):
        '''
        Tests spectrum injection with  a complex ramp from -64-64j to 63+63j as data instead. 
        Since adjacent bins are different, ensures bin ordering is properly preserved as well.

        Parameters:
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-64, max=63, cap_mode=cap_mode)
        data = self._read_spectrum()
        plot([data[0], spectrum], split_complex=True, title=f"Complex ramp spectrum")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('cap_mode', (0, 4))
    def test_periodic_ramp_spectrum_injection(self, cap_mode, xr):
        '''
        Test spectrum injection with a repeating ramp from -8 to 7.

        Parameters:
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('periodic_ramp', min=-8, max=7, cap_mode=cap_mode)
        data = self._read_spectrum()
        plot([data[0], spectrum], split_complex=True, title=f"Periodic ramp spectrum")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('value', (1,-1, 2, 1+0j, 2+1j))
    def test_constant_autocorr(self, value, num_frames, xr):
        '''
        Tests autocorrelation by setting each channel to a specific value and ensuring the autocorrelation is the squared magnitude fo this value

        Parameters:
            value (complex): constant value to set channel
        '''
        if not isinstance(value, complex):
            value *= (1+1j)
        spectrum = self._inject_spectrum('ab', a=np.real(value), b=np.imag(value))
        data = self._read_corr_data(num_frames)
        ref_data = self._compute_psd(spectrum, num_frames)
        plot(np.swapaxes(data[0, :64, :1], 0, 1), split_complex=True, title=f"PSD of constant spectrum of {value}")
        xr.insert_plot()
        np.testing.assert_equal(data, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))
    
    @pytest.mark.corr
    @pytest.mark.parametrize('values', ((0, 1), (-1, 1), (1+0j, 0+1j), (2+0j, 1+1j)))
    def test_alternating_autocorr(self, values, num_frames, xr):
        '''
        Tests autocorrelation by setting each channel to alternate between two values and ensuring the autocorrelation is the squared magnitude in the correct bin for each value.

        Parameters:
            values (tuple of complex): pair of values to alternate between

        '''
        a, b = values
        if not isinstance(a, complex):
            a *= 1+1j
        if not isinstance(b, complex):
            b *= 1+1j
        spectrum = self._inject_spectrum('tile', pattern=[np.real(a), np.imag(a), np.real(b), np.imag(b)])
        ref_data = self._compute_psd(spectrum, num_frames)
        data  = self._read_corr_data(num_frames)
        plot([np.swapaxes(data[0, :64, :1], 0, 1)[0], ref_data[:64]], split_complex=True, title=f"PSD of spectrum alternating between {a} and {b}")
        xr.insert_plot()
        np.testing.assert_equal(data, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))
    
    @pytest.mark.corr
    @pytest.mark.parametrize('period', range(2, 23, 3))
    def test_periodic_autocorr(self, period, xr):
        '''
        Tests that autocorrelation preserves bin order by providing a pattern of 0s interspersed with an occasional 1 in each channel/

        Parameters:
            period (int): how often to insert a 1
        '''
        spectrum = np.tile([*np.repeat(0, period - 1), 1], self.data.NUM_SAMPLES//period)
        spectrum = np.append(spectrum, (np.repeat(0, self.data.NUM_SAMPLES - spectrum.size)))
        spectrum = self._inject_spectrum('arb', data=spectrum)
        ref_data = self._compute_psd(spectrum,)
        data  = self._read_corr_data(self.data.NUM_FRAMES)
        plot([np.swapaxes(data[0, :64, :1], 0, 1)[0], ref_data[:64]], split_complex=True, title=f"PSD of periodic ramp spectrum")
        xr.insert_plot()
        np.testing.assert_equal(data, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))

    @pytest.mark.corr
    def test_full_range_autocorr(self, num_frames, xr):
        '''
        Tests the full range of possible inputs to correlator as complex ramp and ensures they are probably autocorrelated and integrated.

        Parameters:
            num_frames (int): number of frames to integrate over 
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-8, max=7)
        ref_data = self._compute_psd(spectrum, 1)
        data = self._read_corr_data(num_frames, no_accum=True)
        plot([np.swapaxes(data[0, :256, :1], 0, 1)[0], ref_data[:256]], split_complex=True, title=f"PSD of full range of values with no accumulation")
        xr.insert_plot()
        np.testing.assert_equal(data, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))
    
    @pytest.mark.corr
    def test_zero_except_one_autocorr(self, channel0, xr):
        '''
        Tests that the autocorrelation products are mapped to the right index by setting only one channel to 1 and every other channel to 0.

        Parameters: 
            channel0 (int): channel to set to 1
        '''
        selected_channel = [channel0]
        other_channels  = [chan for chan in range(self.data.NUM_CHANNELS) if chan != channel0]
        self._inject_spectrum('a', a=1, channels=selected_channel) #1+1j -> magnitude squared is 2
        self._inject_spectrum('a', a=0, channels=other_channels)
        data = self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False)
        index = np.where(data[0, 0, :] != 0)[0][0]
        expected = self._corr_product_index(channel0, channel0)
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS)
        ref_data[expected] = 2
        plot([data[0, 0, :], ref_data], split_complex=False, title=f"Correlation products with channel {channel0} set to 1") #plot first bin from each of 36 correlation products
        xr.insert_plot()
        np.testing.assert_equal(data[0, 0, :], ref_data)
    
    @pytest.mark.corr
    @pytest.mark.parametrize('autocorr_only', (True, False))
    def test_zero_except_two_crosscorr(self, channel0, autocorr_only, channel1, xr):
        '''
        Tests that crosscorrelation and autocorrelation products are being mapped to the right index by setting two channels to 1 and the others to 0.

        Parameters:
            channel0, channel1 (int): channels to set to 1
            autocorr_only: tests this feature by ensuring the crosscorrelations do not appear
        '''
        if channel0 >= channel1:
            pytest.skip("Redundant channel combination")
        selected_channels = [channel0, channel1]
        other_channels  = [chan for chan in range(self.data.NUM_CHANNELS) if chan not in selected_channels]
        self._inject_spectrum('a', a=1, channels=selected_channels)
        self._inject_spectrum('a', a=0, channels=other_channels)
        data = self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False, autocorr_only=autocorr_only)
        indices = np.where(data[0, 0, :] != 0)[0]
        if autocorr_only:
            expected = sorted([
                self._corr_product_index(channel0, channel0),
                self._corr_product_index(channel1, channel1),
            ])
        else:
            expected = sorted([
                self._corr_product_index(channel0, channel0),
                self._corr_product_index(channel0, channel1),
                self._corr_product_index(channel1, channel1),
            ])
        print(expected, indices)
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS)
        ref_data[expected] = 2
        plot([data[0, 0, :], ref_data], split_complex=False, title=f"{"Autoc" if autocorr_only else "C"}orrelation products with channels {channel0} and {channel1} set to 1") 
        xr.insert_plot()
        np.testing.assert_equal(data[0, 0, :], ref_data)

    @pytest.mark.corr
    @pytest.mark.parametrize('channel0, channel1', ((0, 1), (2, 5), (3, 7)))
    def test_crosscorr_conjugation(self, channel0, channel1, xr):
        '''Tests complex conjugation by cross-correlating unequal complex spectra
        
        Parameters:
            channel0, channel1 (int): channels to set to 2 and 1+1j, respectively
        '''
        a = 2 + 0j
        b = 1 + 1j
        other_channels = [chan for chan in range(self.data.NUM_CHANNELS) if chan not in (channel0, channel1)]
        self._inject_spectrum('ab', a=np.real(a), b=np.imag(a), channels=[channel0])
        self._inject_spectrum('ab', a=np.real(b), b=np.imag(b), channels=[channel1])
        self._inject_spectrum('a', a=0, channels=other_channels)
        data = self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False)
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS, dtype=np.complex128)
        ref_data[self._corr_product_index(channel0, channel0)] = a * np.conjugate(a)  # 4
        ref_data[self._corr_product_index(channel1, channel1)] = b * np.conjugate(b)  # 2
        ref_data[self._corr_product_index(channel0, channel1)] = a * np.conjugate(b)  # 2-2j
        wrong_cross = a * b  # 2+2j — what a missing B-conjugate would produce
        cross_idx = self._corr_product_index(channel0, channel1)
        plot([data[0, 0, :], ref_data], split_complex=True,
             title=f"Correlation products with channel{channel0}={a} and channel{channel1}={b}")
        xr.insert_plot()
        assert data[0, 0, cross_idx] != wrong_cross, (
            f"Cross product matched the unconjugated value {wrong_cross}; "
            f"expected {a * np.conjugate(b)}"
        ) #more specfic test for conjugation
        np.testing.assert_equal(data[0, 0, :], ref_data)
        
    @pytest.mark.corr
    @pytest.mark.parametrize('num_frames', (32768,))
    def test_sat_detection(self, num_frames, xr):
        '''
        Tests saturation detection by intentionally saturating certain bins and ensuring saturation is set to 1 at these bins

        Parameters:
            num_frames (int): number of frames to integrate over
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-16, max=15)
        data, sat = self._read_corr_data(num_frames, normalise=False, return_sat=True)
        ref_data = np.asarray((spectrum * np.conjugate(spectrum)) * num_frames > 131071, dtype=np.complex128)
        ref_data = self._drop_sacrificial_bins(ref_data, axis=0)
        print(sat[0, :16, 0])
        plot([np.swapaxes(data[0, :, :1], 0, 1)[0], self._drop_sacrificial_bins(spectrum * spectrum.conjugate() * num_frames, axis=0)], split_complex=True)
        xr.insert_plot()
        plot([np.swapaxes(sat[0, :, :1], 0, 1)[0], ref_data], title=f"Overflowed bins with {num_frames} frames of integration")
        xr.insert_plot()
        np.testing.assert_equal(sat, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))

    @pytest.mark.corr
    @pytest.mark.parametrize('num_soft_frames', (1, 2, 4))
    def test_software_integration(self, num_soft_frames, xr):
        '''
        Tests software integration

        ParameterS:
            num_soft_frames (int): number of returned frames to integrate in software
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-5, max=5)
        data = self._read_corr_data(self.data.NUM_FRAMES, num_soft_frames=num_soft_frames, normalise=False)
        firmware_data = self._compute_psd(spectrum, self.data.NUM_FRAMES, normalise=False)
        ref_data = num_soft_frames * firmware_data
        #print(data[0, 2000:3000, 0])
        plot([np.swapaxes(data[0, 2500:3500, :1], 0, 1)[0], ref_data[2500:3500]], split_complex=True, title=f"Software accumulation with {num_soft_frames} soft frames")
        xr.insert_plot()
        np.testing.assert_equal(data, np.broadcast_to(ref_data[np.newaxis, :, np.newaxis], data.shape))