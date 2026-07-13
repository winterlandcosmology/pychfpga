import logging
from pathlib import Path

import numpy as np
import pytest
from pychfpga.calculate_gains import DirectGainCalc
from pychfpga.fpga_array import FPGAArray
from wtl.namespace import NameSpace

from .casper_simulations.caspersimtools import InputSignal


platforms = {
    'ICE-4bit-c':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 35, 'READOUT_SHIFT': 4},
    'ICE-4bit-r':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 34, 'READOUT_SHIFT': 4},
    'ICE-8bit-c':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 8, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 35, 'READOUT_SHIFT': 0},
    'ICE-8bit-r':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 8, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 34, 'READOUT_SHIFT': 0},
    'CRS':
        {'INPUT_WIDTH': 14, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 32, 'SCALED_WIDTH': 48, 'READOUT_SHIFT': 12},
}


class TestGain:
    SIM_DATA_DIR = Path(__file__).resolve().parent / 'casper_simulations' / 'sim_data' / 'ice'
    NOISE_INPUT = SIM_DATA_DIR / 'inputs' / 'noise.bin'
    FFT_FULL_CAPTURE_MODE = 'fft_full'
    FFT_SHIFT = 1984
    CHANNEL = 0
    FULLRES_NCAP = 100
    SCALER_NCAP = 16
    ADC_NCAP = 4
    CAPTURE_PERIOD = 0.01
    FLOAT_GAIN_BITS = 16
    FOUR_BIT_OUTPUT_WIDTH = 4
    ADC_SIGNAL_MIN_FRACTION = 0.25
    RMS_ATOL = 0.35

    def get_logger(self):
        return self.logger

    def get_widths(self, test_config):
        platform = test_config.platform
        if platform not in platforms:
            raise ValueError(f'{platform} not recognised as a possible platform')
        widths = NameSpace(platforms[platform])
        widths.PLATFORM = platform[:3]
        widths.MIN_INPUT = -2 ** (widths.INPUT_WIDTH - 1)
        widths.MAX_INPUT = 2 ** (widths.INPUT_WIDTH - 1) - 1
        return widths

    def board_conn(self, xr):
        if 'board' in xr.data:
            return xr.data.board
        ca = FPGAArray(**self.connection_config)
        xr.data.board = ca.ib[0]
        return xr.data.board

    @pytest.fixture(scope='function', autouse=True)
    def setup(self, xr):
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        xr.data.update(self.get_widths(self.test_config))
        self.data = xr.data
        self.board = self.board_conn(xr)
        self.data.NUM_SAMPLES = int(self.board.ADC_SAMPLES_PER_FRAME)
        self.data.NUM_BINS = int(self.board.NUMBER_OF_FREQUENCY_BINS)
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name)
        self.logger.setLevel(xr.config.test_config.logleveltest)

        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        pre_flags = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"gain setup pre-check: chan_fifo_overflow={pre_flags}")
        assert all(v == 0 for v in pre_flags)
        yield
        post_flags_before = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"gain setup post-check before cleanup: chan_fifo_overflow={post_flags_before}")
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        post_flags_after = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"gain setup post-check after cleanup: chan_fifo_overflow={post_flags_after}")
        assert all(v == 0 for v in post_flags_after)

    @pytest.fixture(scope='function')
    def setup_gain(self, setup):
        if self.data.PLATFORM != 'ICE':
            pytest.skip("Direct gain firmware test currently targets ICE")
        if int(getattr(self.board, 'FFT_TYPE', 0)) == 0:
            pytest.skip("Firmware reports no FFT implementation")
        adc_bits = int(getattr(self.board, 'ADC_BITS_PER_SAMPLE', self.data.INPUT_WIDTH))
        n_inputs = int(getattr(self.board, 'SAMPLES_PER_WORD', 0))
        if adc_bits != 8:
            pytest.skip(f"Direct gain firmware test expects 8-bit ADC input, got {adc_bits}")
        if n_inputs != 4:
            pytest.skip(f"Direct gain firmware test expects 4 ADC lanes, got {n_inputs}")
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("DirectGainCalc writes floating point gains; firmware is using complex gains")

    def load_noise_input(self):
        if not self.NOISE_INPUT.exists():
            raise FileNotFoundError(self.NOISE_INPUT)

        adc_bits = int(getattr(self.board, 'ADC_BITS_PER_SAMPLE', self.data.INPUT_WIDTH))
        n_inputs = int(getattr(self.board, 'SAMPLES_PER_WORD', 0))
        signal = InputSignal.from_binary(self.NOISE_INPUT, adc_bits=adc_bits, n_inputs=n_inputs)
        if signal.data.size < self.data.NUM_SAMPLES:
            raise ValueError(
                f"{self.NOISE_INPUT} contains {signal.data.size} samples, expected at least one "
                f"{self.data.NUM_SAMPLES}-sample ADC frame")
        return signal.data.T.ravel()[:self.data.NUM_SAMPLES].astype(np.int64, copy=False)

    def fft_bins_per_word(self):
        fft_info = self.board.FFT_INFO[int(self.board.FFT_TYPE)]
        return int(fft_info.get('bins_per_word') or 1)

    def derive_post_gain_bits(self):
        fft_info = self.board.FFT_INFO[int(self.board.FFT_TYPE)]
        fft_component_bits = int(fft_info['bits_per_bin']) // 2
        return fft_component_bits + self.FLOAT_GAIN_BITS - self.FOUR_BIT_OUTPUT_WIDTH

    def configure_fullres_capture(self, ch, data_source=None):
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags(channels=[ch])
        self.board.set_channelizer(
            fft_bypass=False,
            scaler_bypass=False,
            data_source=data_source,
            funcgen_left_shift=0 if data_source is not None else None,
            funcgen_right_shift=0 if data_source is not None else None,
            reset_chan=data_source is not None,
            scaler_bypass_data_type=0,
            scaler_cap_data_type=self.FFT_FULL_CAPTURE_MODE,
            offset_binary_encoding=False,
            scaler_eight_bit=True,
            prober_user_flags=False,
            scaler_rounding_mode=0,
            symmetric_saturation=False,
            channels=[ch],
        )
        self.board.set_scaler_output_modes(
            bypass=False,
            cap_data_type=self.FFT_FULL_CAPTURE_MODE,
            channels=[ch],
        )
        self.board.chan[ch].FFT.FFT_SHIFT = self.FFT_SHIFT
        self.board.chan[ch].FFT.reset_fft_overflow_count()

    def capture_fullres_fft(self, funcgen_input, ch):
        self.board.sync()
        if funcgen_input is not None:
            self.board.set_funcgen(
                function='arb',
                channels=[ch],
                left_shift=0,
                right_shift=0,
                reset_chan=True,
                data=funcgen_input,
            )
        frames_per_burst = 2 * self.fft_bins_per_word()
        self.board.start_data_capture(
            period=self.CAPTURE_PERIOD,
            frames_per_burst=frames_per_burst,
            channels=[ch],
            source='scaler',
            data_type=self.FFT_FULL_CAPTURE_MODE,
        )
        self.board.sync()

        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, count = receiver.read_raw_frames(
            ncap=self.FULLRES_NCAP,
            source=1,
            chan_first=False,
        )
        spectra = np.asarray(data[:, 0, :], dtype=np.complex64)
        overflow_count = int(self.board.chan[ch].FFT.get_fft_overflow_count())
        self.logger.debug(
            f"capture_fullres_fft: ch={ch} frames_per_burst={frames_per_burst} "
            f"ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape} "
            f"spectra_shape={spectra.shape} overflow_count={overflow_count}")
        return spectra, overflow_count, data.shape

    def check_external_adc_input(self, ch):
        self.board.stop_data_capture()
        self.board.set_channelizer(
            fft_bypass=True,
            scaler_bypass=True,
            data_source='adc',
            funcgen_left_shift=0,
            funcgen_right_shift=0,
            reset_chan=True,
            channels=[ch],
        )
        self.board.start_data_capture(
            period=self.CAPTURE_PERIOD,
            frames_per_burst=1,
            channels=[ch],
            source='funcgen',
            mode=3,
        )
        self.board.sync()

        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, adc_data, count = receiver.read_raw_frames(
            ncap=self.ADC_NCAP,
            split=True,
            source=0,
            chan_first=False,
        )
        adc_data = np.asarray(adc_data)
        adc_max_ampl = max(abs(int(self.data.MIN_INPUT)), abs(int(self.data.MAX_INPUT)))
        threshold = adc_max_ampl * self.ADC_SIGNAL_MIN_FRACTION
        peak = float(np.max(np.abs(adc_data.astype(np.float64))))
        self.logger.debug(
            f"check_external_adc_input: ch={ch} ts_shape={ts.shape} data_shape={adc_data.shape} "
            f"count_shape={count.shape} peak={peak:.6g} threshold={threshold:.6g}")
        if peak < threshold:
            raise AssertionError(
                f"External ADC gain test requires external input on channel {ch}; "
                f"observed ADC peak {peak:.6g} is below {self.ADC_SIGNAL_MIN_FRACTION:.0%} "
                f"of ADC range ({threshold:.6g}).")
        return adc_data.shape, peak, threshold

    def calculate_direct_gains(self, fullres_spectra, ch):
        stream_id = int(self.board.get_stream_id(ch))
        channel_id = tuple(self.board.get_channel_ids([ch])[0])
        post_gain_bits = self.derive_post_gain_bits()
        rms = np.sqrt(np.mean(
            fullres_spectra.real.astype(np.float64)**2 + fullres_spectra.imag.astype(np.float64)**2,
            axis=0,
        ))
        calc = DirectGainCalc(
            channel_ids=[channel_id],
            stream_ids=[stream_id],
            post_gain_bits=post_gain_bits,
            n_bins=self.data.NUM_BINS,
        )
        calc.update_gains(np.array([stream_id]), rms[np.newaxis, :])
        filtered_gains, mask = calc.get_filtered_gains()
        gain_vec, glog = filtered_gains[channel_id]
        valid_window = np.isfinite(gain_vec) & ~np.asarray(mask[0], dtype=bool)
        valid_window[0] = False
        if not np.any(valid_window):
            raise AssertionError("Direct gain calculation produced no finite non-DC bins for RMS validation")
        inv_bin_map = self.board.chan[ch].FFT.get_bin_map(flatten=True, reverse=False)
        mapped_gain = gain_vec[inv_bin_map]
        self.board.set_gains({ch: (mapped_gain, glog)}, bank=0, when='now', channels=[ch])
        self.logger.debug(
            f"calculate_direct_gains: ch={ch} stream_id={stream_id} channel_id={channel_id} "
            f"target_rms={calc.target_rms} post_gain_bits={post_gain_bits} glog={glog} "
            f"rms_median={float(np.median(rms[valid_window])):.6g} "
            f"gain_min={float(np.min(gain_vec[valid_window])):.6g} "
            f"gain_median={float(np.median(gain_vec[valid_window])):.6g} "
            f"gain_max={float(np.max(gain_vec[valid_window])):.6g}")
        return calc.target_rms, valid_window, post_gain_bits

    def configure_four_bit_scaler_capture(self, ch, data_source=None):
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags(channels=[ch])
        self.board.set_channelizer(
            fft_bypass=False,
            scaler_bypass=False,
            data_source=data_source,
            funcgen_left_shift=0 if data_source is not None else None,
            funcgen_right_shift=0 if data_source is not None else None,
            reset_chan=data_source is not None,
            scaler_bypass_data_type=0,
            scaler_cap_data_type=0,
            offset_binary_encoding=False,
            scaler_eight_bit=False,
            prober_user_flags=False,
            scaler_rounding_mode=2,
            symmetric_saturation=False,
            channels=[ch],
        )
        self.board.set_scaler_output_modes(
            bypass=False,
            cap_data_type=0,
            channels=[ch],
        )
        self.board.chan[ch].FFT.FFT_SHIFT = self.FFT_SHIFT
        self.board.chan[ch].FFT.reset_fft_overflow_count()

    def capture_scaler_mode0(self, ch, ncap=None):
        if ncap is None:
            ncap = self.SCALER_NCAP
        self.board.start_data_capture(
            period=self.CAPTURE_PERIOD,
            channels=[ch],
            source='scaler',
            data_type=0,
        )
        self.board.sync()
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, count = receiver.read_raw_frames(
            ncap=ncap,
            source=1,
            chan_first=False,
        )
        spectra = np.asarray(data[:, 0, :], dtype=np.complex64)
        self.logger.debug(
            f"capture_scaler_mode0: ch={ch} ts_shape={ts.shape} data_shape={data.shape} "
            f"count_shape={count.shape} spectra_shape={spectra.shape}")
        return spectra, data.shape

    @pytest.mark.gain
    @pytest.mark.floating
    @pytest.mark.parametrize("ch", (0,))
    def test_direct_gain_noise_rms(self, ch, setup_gain):
        funcgen_input = self.load_noise_input()
        self.configure_fullres_capture(ch)
        fullres_spectra, fullres_overflows, fullres_shape = self.capture_fullres_fft(funcgen_input, ch)
        assert fullres_overflows == 0

        expected_rms, valid_window, post_gain_bits = self.calculate_direct_gains(fullres_spectra, ch)
        self.configure_four_bit_scaler_capture(ch)
        scaler_spectra, scaler_shape = self.capture_scaler_mode0(ch)
        scaler_overflows = int(self.board.chan[ch].FFT.get_fft_overflow_count())
        observed_rms = float(np.sqrt(np.mean(
            scaler_spectra[:, valid_window].real.astype(np.float64)**2
            + scaler_spectra[:, valid_window].imag.astype(np.float64)**2)))

        print("\nDirect gain noise RMS")
        print(f"input bin:          {self.NOISE_INPUT}")
        print(f"FFT_SHIFT:          {self.FFT_SHIFT}")
        print(f"post_gain_bits:     {post_gain_bits}")
        print(f"target RMS:         {expected_rms:.6f}")
        print(f"observed RMS:       {observed_rms:.6f}")
        print(f"valid bins:         {int(np.count_nonzero(valid_window))}/{valid_window.size}")
        print(f"full-res shape:     {fullres_shape}")
        print(f"scaler shape:       {scaler_shape}")
        print(f"FFT overflows:      fullres={fullres_overflows}, scaler={scaler_overflows}")

        assert scaler_overflows == 0
        rms_error = abs(observed_rms - expected_rms)
        assert rms_error <= self.RMS_ATOL, (
            f"observed_rms={observed_rms:.6f}, expected_rms={expected_rms:.6f}, "
            f"abs_error={rms_error:.6f}, atol={self.RMS_ATOL:.6f}")

    @pytest.mark.gain
    @pytest.mark.floating
    @pytest.mark.external
    @pytest.mark.parametrize("ch", (0,))
    def test_direct_gain_external_adc_rms(self, ch, setup_gain):
        adc_shape, adc_peak, adc_threshold = self.check_external_adc_input(ch)
        self.configure_fullres_capture(ch, data_source='adc')
        fullres_spectra, fullres_overflows, fullres_shape = self.capture_fullres_fft(None, ch)
        assert fullres_overflows == 0

        expected_rms, valid_window, post_gain_bits = self.calculate_direct_gains(fullres_spectra, ch)
        self.configure_four_bit_scaler_capture(ch, data_source='adc')
        scaler_spectra, scaler_shape = self.capture_scaler_mode0(ch)
        scaler_overflows = int(self.board.chan[ch].FFT.get_fft_overflow_count())
        observed_rms = float(np.sqrt(np.mean(
            scaler_spectra[:, valid_window].real.astype(np.float64)**2
            + scaler_spectra[:, valid_window].imag.astype(np.float64)**2)))

        print("\nDirect gain external ADC RMS")
        print(f"ADC channel:        {ch}")
        print(f"ADC shape:          {adc_shape}")
        print(f"ADC peak:           {adc_peak:.6f}")
        print(f"ADC threshold:      {adc_threshold:.6f}")
        print(f"FFT_SHIFT:          {self.FFT_SHIFT}")
        print(f"post_gain_bits:     {post_gain_bits}")
        print(f"target RMS:         {expected_rms:.6f}")
        print(f"observed RMS:       {observed_rms:.6f}")
        print(f"valid bins:         {int(np.count_nonzero(valid_window))}/{valid_window.size}")
        print(f"full-res shape:     {fullres_shape}")
        print(f"scaler shape:       {scaler_shape}")
        print(f"FFT overflows:      fullres={fullres_overflows}, scaler={scaler_overflows}")

        assert scaler_overflows == 0
        rms_error = abs(observed_rms - expected_rms)
        assert rms_error <= self.RMS_ATOL, (
            f"observed_rms={observed_rms:.6f}, expected_rms={expected_rms:.6f}, "
            f"abs_error={rms_error:.6f}, atol={self.RMS_ATOL:.6f}")
