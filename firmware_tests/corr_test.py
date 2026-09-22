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
        constants.NUM_CORR_PRODUCTS = constants.NUM_CHANNELS * (constants.NUM_CHANNELS + 1) // 2
        return constants
    
    @pytest.fixture(scope='function', autouse=True)
    async def setup(self, xr, request):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = await self.board_conn(xr, request)
        self.data_receiver = await self.ca.get_data_receiver_async(verbose=self.test_config.loglevelrecv)
        self.corr_receiver = await self.ca.get_corr_receiver_async(verbose=self.test_config.loglevelrecv)

        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME
        self.data.NUM_BINS = self.board.NUMBER_OF_FREQUENCY_BINS
        self.data.NUM_FRAMES = 32000 #means 4 doesn't saturate
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)
        # Do not call stop_data_capture() here. On UCAP it asserts USER_RESET and
        # GLOBAL_TRIG=0; with those held, the board sends no correlator packets
        # (Wireshark: beacons only). Spectrum tests start UCAP themselves.

    def _inject_spectrum(self, func, postscaler=26, channels=None, **kwargs):
        spectrum = gen_data(func, samples=self.data.NUM_SAMPLES, **kwargs)
        spectrum_cmplx = spectrum[::2] + 1j*spectrum[1::2]
        # self.board.set_data_source(source='arb', data=spectrum, channels=channels)
        self.board.set_channelizer(
                        data_source='buffer',
                        buffer=spectrum,
                        channels=channels,
                        fft_bypass=True, 
                        scaler_bypass=False, 
                        scaler_bypass_data_type=0, 
                        postscaler=postscaler,
                        gains=1,
                        symmetric_saturation=False,
                        offset_binary_encoding=False
                    ) #other channel parameters do not change so does not need to be specific to selected channels
        self.board.sync()
        return spectrum_cmplx

    async def _read_spectrum(self, cap_mode=0):
        receiver = self.data_receiver
        receiver.flush()
        self.board.start_data_capture(period=0.1, source='scaler', data_type=cap_mode)
        self.board.sync()
        _, data, _ = receiver.read_raw_frames(split=True, source=1)
        data = data[:, -1, :] #get last frame
        return data

    async def _good_bins_map(self):
        """Boolean mask over FFT bins from ``get_good_bin_mask``.

        False for sacrificial readout bins and for bins the corner-turn drops.
        """
        return self.corr_receiver.get_good_bin_mask()

    def _first_good_bin(self, keep):
        """FFT index of the first bin the correlator treats as valid data."""
        good = np.flatnonzero(np.asarray(keep, dtype=bool))
        if good.size == 0:
            raise RuntimeError("get_good_bin_mask() has no valid FFT bins")
        return int(good[0])

    def _corr_product_index(self, i, j):
        """Index of the auto-/cross-correlation of channels ``i`` and ``j``.

        Products are stored in pper-triangular order including the diagonal:
        ``(0,0), (0,1), ..., (0,nchan-1), (1,1), (1,2), ..., (nchan-1, nchan-1)``.
        ``(i, j)`` and ``(j, i)`` map to the same product.
        """
        if i > j:
            i, j = j, i
        return i * (2 * self.data.NUM_CHANNELS - i - 1) // 2 + j

    async def _compute_psd(self, spectrum, num_frames=32000):
        """Integer accumulator reference on the full FFT-bin axis.

        ``clip(|s|^2 * num_frames, 0, 131071)``. Values stay whole numbers
        (complex128 storage). Sacrificial bins are left in the array;
        ``_assert_corr_psd`` skips them.
        """
        psd = (spectrum * np.conjugate(spectrum) * num_frames).astype(np.complex128)
        return np.clip(psd, 0, 131071)

    async def _assert_corr_psd(self, data, ref_1d, num_frames, context=""):
        """Fail with a short summary of how correlator output differs from the injected PSD.

        Compares real and imaginary parts as integers (the accumulator is stored
        in complex64, but the values are whole numbers up to 131071). Bins where
        ``get_good_bin_mask()`` is false are not compared. Reported bin indices
        are FFT bin numbers on the full axis.
        """
        prefix = f"{context}: " if context else ""
        ref_1d = np.asarray(ref_1d)
        expected = np.broadcast_to(ref_1d[np.newaxis, :, np.newaxis], np.shape(data))
        actual = np.asarray(data)
        if actual.shape != expected.shape:
            raise AssertionError(
                f"{prefix}shape mismatch: got {actual.shape}, expected {expected.shape}"
            )

        keep = await self._good_bins_map()
        n_bins = actual.shape[1]
        if keep.shape[0] < n_bins:
            raise AssertionError(
                f"{prefix}good-bin mask length {keep.shape[0]} is shorter than "
                f"correlator bin axis {n_bins}"
            )
        keep = keep[:n_bins]
        compared = np.zeros(actual.shape, dtype=bool)
        compared[:, keep, :] = True

        act_re = np.rint(actual.real).astype(np.int64)
        act_im = np.rint(actual.imag).astype(np.int64)
        exp_re = np.rint(np.asarray(expected.real)).astype(np.int64)
        exp_im = np.rint(np.asarray(expected.imag)).astype(np.int64)
        mismatch = (act_re != exp_re) | (act_im != exp_im)
        bad = mismatch & compared
        n_bad = int(np.count_nonzero(bad))
        n_total = int(np.count_nonzero(compared))
        if n_bad == 0:
            return

        diff_re = act_re - exp_re
        diff_im = act_im - exp_im
        max_abs = int(max(np.max(np.abs(diff_re[compared])), np.max(np.abs(diff_im[compared]))))
        ref_scale = int(max(np.max(np.abs(exp_re[compared])), np.max(np.abs(exp_im[compared])))) or 1
        lines = [
            f"{prefix}correlator PSD mismatch: {n_bad}/{n_total} cells "
            f"({100.0 * n_bad / n_total:.4f}%)",
            f"  integration_period={num_frames}  correlator dtype={actual.dtype}  "
            f"reference dtype={ref_1d.dtype}",
            f"  max |diff|={max_abs}  (max |ref|={ref_scale})",
        ]

        if actual.ndim == 3:
            bad_bins = np.flatnonzero(bad[0].any(axis=1))
            if bad_bins.size:
                show = bad_bins[:16].tolist()
                tail = f" … (+{bad_bins.size - 16} more)" if bad_bins.size > 16 else ""
                lines.append(f"  FFT bin indices with any mismatch: {show}{tail}")

        per_frame = np.rint(exp_re.astype(np.float64) / float(num_frames)).astype(np.int64)
        bump_mask = bad & (exp_re != 0) & (diff_re == per_frame) & (diff_im == 0)
        n_bump = int(np.count_nonzero(bump_mask))
        if n_bump:
            lines.append(
                f"  {n_bump} mismatches match +1 extra integration frame "
                f"(Δreal = round(ref/{num_frames}))"
            )

        flat_ix = np.flatnonzero(bad.ravel())[:5]
        lines.append("  Examples (index, actual, expected):")
        for fi in flat_ix:
            idx = np.unravel_index(fi, bad.shape)
            lines.append(
                f"    {idx}: {act_re[idx]:+d}{act_im[idx]:+d}j  vs  "
                f"{exp_re[idx]:+d}{exp_im[idx]:+d}j"
            )
        raise AssertionError("\n".join(lines))
    
    async def _read_corr_data(self, num_frames=32000, num_soft_frames=1, max_tries=5, all_channels_equal=True, no_accum=False, return_sat=False, autocorr_only=False):
        self.board.stop_correlator()
        corr_receiver = self.corr_receiver 
        sock = getattr(getattr(corr_receiver, "pr", None), "sock", None)
        if sock is not None:
            ip, port = sock.getsockname()[:2]
            dest = await self.board.get_local_data_port_number_async()
            print(f"corr_test data socket {ip}:{port}  FPGA dest {dest}")
        self.board.start_correlator(integration_period=num_frames,
                                autocorr_only = autocorr_only,
                                no_accum=no_accum,
                                verbose=self.test_config.loglevelrecv
                                )
        # Sync after start so the correlator sees aligned frames from a clean edge.
        self.board.sync()
        num_tries = 0
        while True:
            _integ_ts, d_corr, counts, sat = corr_receiver.read_corr_frames(
                flush = True,
                soft_integ_period=num_soft_frames,
                data_timeout=(num_frames / self.board.FRAME_RATE + 0.5) * (num_soft_frames + 1),
            )

            num_tries += 1
            pkt_count = int(np.min(counts))
            if pkt_count == corr_receiver.expected_packets:
                break
            print(f"Missing packets: {pkt_count=}, {corr_receiver.expected_packets=}")
            if num_tries >= max_tries:  
                raise RuntimeError(f"Unable to capture correlator data without missing packets after {num_tries} tries")
        self.board.stop_correlator()
        if all_channels_equal: #assert all autocorrelations and cross-correlations are equal
            keep = await self._good_bins_map()
            good = d_corr[:, keep[:d_corr.shape[1]], :]
            assert np.all(np.swapaxes(good, 1, 2) == good[0, :, 0])
        # expected_packets = int(corr_receiver.expected_packets)
        # print(f"{pkt_count=}, {expected_packets=}")
        # if pkt_count != expected_packets:
        #     raise ValueError(
        #         f"Got {pkt_count}/{expected_packets} packets from correlator "
        #         f"(soft_integ_period={num_soft_frames})"
        #     )
        if return_sat:
            return d_corr, sat
        return d_corr
    
    @pytest.mark.corr
    @pytest.mark.parametrize('value', (1,-1, 2))
    @pytest.mark.parametrize('cap_mode', (0, 4))
    async def test_constant_spectrum_injection(self, value, cap_mode, xr):
        '''
        Tests that in the function generator can be used to simulate an FFT spectrum when the FFT is bypassed and its output is read back (aka "spectrum injection"). 
        This capability is critical for most of the correlator tests. 

        Parameters:
            value (int): value to be injected by the function generator
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('a', a=value)
        data = await self._read_spectrum(cap_mode=cap_mode)
        plot([data[0], spectrum], split_complex=True, title=f"Constant spectrum of {value}")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('cap_mode', (0, 4))
    async def test_complex_ramp_spectrum_injection(self, cap_mode, xr):
        '''
        Tests spectrum injection with  a complex ramp from -64-64j to 63+63j as data instead. 
        Since adjacent bins are different, ensures bin ordering is properly preserved as well.

        Parameters:
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-8, max=7)
        data = await self._read_spectrum(cap_mode=cap_mode)
        #if cap_mode == 0:
        #    spectrum = np.clip(spectrum, -8, 7)
        plot([data[0], spectrum], split_complex=True, title=f"Complex ramp spectrum")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('cap_mode', (0, 4))
    async def test_periodic_ramp_spectrum_injection(self, cap_mode, xr):
        '''
        Test spectrum injection with a repeating ramp from -8 to 7.

        Parameters:
            cap_mode (int): capture mode to read back data in
        '''
        spectrum = self._inject_spectrum('periodic_ramp', min=-8, max=7)
        data = await self._read_spectrum(cap_mode=cap_mode)
        plot([data[0], spectrum], split_complex=True, title=f"Periodic ramp spectrum")
        xr.insert_plot()
        np.testing.assert_equal(data[0], spectrum) #no shifting should be required
        

    @pytest.mark.corr
    @pytest.mark.parametrize('value', (1,-1, 2, 1+0j, 2+1j))
    async def test_constant_autocorr(self, value, num_frames, xr):
        '''
        Tests autocorrelation by setting each channel to a specific value and ensuring the autocorrelation is the squared magnitude fo this value

        Parameters:
            value (complex): constant value to set channel
        '''
        if not isinstance(value, complex):
            value *= (1+1j)
        spectrum = self._inject_spectrum('ab', a=np.real(value), b=np.imag(value))
        import time
        time.sleep(1)
        data = await self._read_corr_data(num_frames)
        ref_data = await self._compute_psd(spectrum, num_frames)
        plot(np.swapaxes(data[0, :64, :1], 0, 1), split_complex=True, title=f"PSD of constant spectrum of {value}")
        xr.insert_plot()
        await self._assert_corr_psd(data, ref_data, num_frames, context="constant_autocorr")
    
    @pytest.mark.corr
    @pytest.mark.parametrize('values', ((0, 1), (-1, 1), (1+0j, 0+1j), (2+0j, 1+1j)))
    async def test_alternating_autocorr(self, values, num_frames, xr):
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
        ref_data = await self._compute_psd(spectrum, num_frames)
        data  = await self._read_corr_data(num_frames)
        plot([np.swapaxes(data[0, :64, :1], 0, 1)[0], ref_data[:64]], split_complex=True, title=f"PSD of spectrum alternating between {a} and {b}")
        xr.insert_plot()
        await self._assert_corr_psd(data, ref_data, num_frames, context="alternating_autocorr")
    
    @pytest.mark.corr
    @pytest.mark.parametrize('period', range(2, 23, 3))
    async def test_periodic_autocorr(self, period, xr):
        '''
        Tests that autocorrelation preserves bin order by providing a pattern of 0s interspersed with an occasional 1 in each channel/

        Parameters:
            period (int): how often to insert a 1
        '''
        spectrum = np.tile([*np.repeat(0, period - 1), 1], self.data.NUM_SAMPLES//period)
        spectrum = np.append(spectrum, (np.repeat(0, self.data.NUM_SAMPLES - spectrum.size)))
        spectrum = self._inject_spectrum('arb', data=spectrum)
        num_frames = self.data.NUM_FRAMES
        ref_data = await self._compute_psd(spectrum, num_frames)
        data = await self._read_corr_data(num_frames)
        plot([np.swapaxes(data[0, :64, :1], 0, 1)[0], ref_data[:64]], split_complex=True, title=f"PSD of alternating spectrum (period={period})")
        xr.insert_plot()
        await self._assert_corr_psd(data, ref_data, num_frames, context=f"periodic_autocorr period={period}")

    @pytest.mark.corr
    async def test_full_range_autocorr(self, num_frames, xr):
        '''
        Tests the full range of possible inputs to correlator as complex ramp and ensures they are probably autocorrelated and integrated.

        Parameters:
            num_frames (int): number of frames to integrate over 
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-8, max=7)
        ref_data = await self._compute_psd(spectrum, 1)
        data = await self._read_corr_data(num_frames, no_accum=True)
        plot([np.swapaxes(data[0, :256, :1], 0, 1)[0], ref_data[:256]], split_complex=True, title=f"PSD of full range of values with no accumulation")
        xr.insert_plot()
        await self._assert_corr_psd(data, ref_data, num_frames, context="full_range_autocorr")
    
    @pytest.mark.corr
    async def test_zero_except_one_autocorr(self, channel0, xr):
        '''
        Tests that the autocorrelation products are mapped to the right index by setting only one channel to 1 and every other channel to 0.

        Parameters: 
            channel0 (int): channel to set to 1
        '''
        selected_channel = [channel0]
        other_channels  = [chan for chan in range(self.data.NUM_CHANNELS) if chan != channel0]
        self._inject_spectrum('a', a=1, channels=selected_channel) #1+1j -> magnitude squared is 2
        self._inject_spectrum('a', a=0, channels=other_channels)
        data = await self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False)
        good_bin = self._first_good_bin(await self._good_bins_map())
        products = data[0, good_bin, :]
        index = np.where(products != 0)[0][0]
        expected = self._corr_product_index(channel0, channel0)
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS)
        ref_data[expected] = 2 * self.data.NUM_FRAMES
        np.testing.assert_equal(products.imag, 0)
        plot(
            [products.real, ref_data],
            split_complex=False,
            title=f"Correlation products with channel {channel0} set to 1",
        )  # plot first good bin from each of 36 correlation products
        xr.insert_plot()
        np.testing.assert_equal(products.real, ref_data)
    
    @pytest.mark.corr
    @pytest.mark.parametrize('autocorr_only', (False, True))
    async def test_zero_except_two_crosscorr(self, channel0, autocorr_only, channel1, xr):
        '''
        Tests that crosscorrelation and autocorrelation products are being mapped to the right index by setting two channels to 1 and the others to 0.

        Parameters:
            channel0, channel1 (int): channels to set to 1
            autocorr_only: tests this feature by ensuring the crosscorrelations do not appear
        '''
        if channel0 >= channel1:
            # pytest.skip("Redundant channel combination")
            print(f"Redundant channel combination ({channel0}, {channel1}). Passing the test.")
            return
        selected_channels = [channel0, channel1]
        other_channels  = [chan for chan in range(self.data.NUM_CHANNELS) if chan not in selected_channels]
        self._inject_spectrum('a', a=1, channels=selected_channels)
        self._inject_spectrum('a', a=0, channels=other_channels)
        data = await self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False, autocorr_only=autocorr_only)
        good_bin = self._first_good_bin(await self._good_bins_map())
        products = data[0, good_bin, :]
        indices = np.where(products != 0)[0]
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
        np.testing.assert_equal(products.imag, 0)
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS)
        ref_data[expected] = 2 * self.data.NUM_FRAMES
        kind = "Autoc" if autocorr_only else "C"
        plot(
            [products.real, ref_data],
            split_complex=False,
            title=f"{kind}orrelation products with channels {channel0} and {channel1} set to 1",
        )
        xr.insert_plot()
        np.testing.assert_equal(products.real, ref_data)

    @pytest.mark.corr
    @pytest.mark.parametrize('channel0, channel1', ((0, 1), (2, 5), (3, 7)))
    async def test_crosscorr_conjugation(self, channel0, channel1, xr):
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
        data = await self._read_corr_data(self.data.NUM_FRAMES, all_channels_equal=False)
        good_bin = self._first_good_bin(await self._good_bins_map())
        products = data[0, good_bin, :]
        n = self.data.NUM_FRAMES
        ref_data = np.zeros(self.data.NUM_CORR_PRODUCTS, dtype=np.complex128)
        ref_data[self._corr_product_index(channel0, channel0)] = a * np.conjugate(a) * n  # 4*N
        ref_data[self._corr_product_index(channel1, channel1)] = b * np.conjugate(b) * n  # 2*N
        ref_data[self._corr_product_index(channel0, channel1)] = a * np.conjugate(b) * n  # (2-2j)*N
        wrong_cross = a * b * n  # (2+2j)*N — what a missing B-conjugate would produce
        cross_idx = self._corr_product_index(channel0, channel1)
        plot([products, ref_data], split_complex=True,
             title=f"Correlation products with channel{channel0}={a} and channel{channel1}={b}")
        xr.insert_plot()
        assert products[cross_idx] != wrong_cross, (
            f"Cross product matched the unconjugated value {wrong_cross}; "
            f"expected {a * np.conjugate(b)}"
        ) #more specfic test for conjugation
        np.testing.assert_equal(products, ref_data)
        
    @pytest.mark.corr
    @pytest.mark.parametrize('num_frames', (32768,))
    async def test_sat_detection(self, num_frames, xr):
        '''
        Tests saturation detection by intentionally saturating certain bins and ensuring saturation is set to 1 at these bins

        Parameters:
            num_frames (int): number of frames to integrate over
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-16, max=15)
        data, sat = await self._read_corr_data(num_frames, return_sat=True)
        # Scaler clips to the capture width before the correlator; 131071 is the
        # 18-bit accumulator max. UCORR sat is a count, not a 0/1 Ice-style flag.
        capture_width = int(self.test_config.constants.CAPTURE_WIDTH)
        half = 1 << (capture_width - 1)
        clipped = (
            np.clip(spectrum.real, -half, half - 1)
            + 1j * np.clip(spectrum.imag, -half, half - 1)
        )
        overflow = ((clipped * np.conjugate(clipped)).real * num_frames) > 131071
        keep = await self._good_bins_map()
        keep = keep[:sat.shape[1]]
        ref_flag = overflow.astype(np.uint8)
        sat_flag = ((sat.real != 0) | (sat.imag != 0)).astype(np.uint8)
        expected = np.broadcast_to(ref_flag[np.newaxis, :, np.newaxis], sat_flag.shape)
        print(sat[0, :16, 0])
        plot([np.swapaxes(data[0, keep, :1], 0, 1)[0], (clipped * clipped.conjugate() * num_frames)[keep]], split_complex=True)
        xr.insert_plot()
        plot([np.swapaxes(sat_flag[0, keep, :1], 0, 1)[0], ref_flag[keep]], title=f"Overflowed bins with {num_frames} frames of integration")
        xr.insert_plot()
        # if not np.any(sat_flag[:, keep, :]) and np.any(expected[:, keep, :]):
        #     print("UCORR saturation flags are not set in captured packets")
        np.testing.assert_equal(sat_flag[:, keep, :], expected[:, keep, :])

    @pytest.mark.corr
    @pytest.mark.parametrize('num_soft_frames', (1, 2, 4))
    async def test_software_integration(self, num_soft_frames, xr):
        '''
        Tests software integration

        ParameterS:
            num_soft_frames (int): number of returned frames to integrate in software
        '''
        spectrum = self._inject_spectrum('complex_ramp', min=-5, max=5)
        data = await self._read_corr_data(self.data.NUM_FRAMES, num_soft_frames=num_soft_frames)
        firmware_data = await self._compute_psd(spectrum, self.data.NUM_FRAMES)
        ref_data = num_soft_frames * firmware_data
        #print(data[0, 2000:3000, 0])
        plot([np.swapaxes(data[0, :, :1], 0, 1)[0], ref_data], split_complex=True, title=f"Software accumulation with {num_soft_frames} soft frames")
        xr.insert_plot()
        await self._assert_corr_psd(
            data, ref_data, self.data.NUM_FRAMES, context="software_integration"
        )