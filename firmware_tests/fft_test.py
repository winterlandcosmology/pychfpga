import logging
from pathlib import Path
from .module_test import TestModule
import matplotlib.pyplot as plt
import numpy as np
import pytest
from pychfpga.fpga_array import FPGAArray
from wtl.namespace import NameSpace

from .casper_simulations.caspersimtools import InputSignal, OutputSignal
from .utils import plot


class TestFFT(TestModule):
    SCALER_COMPLEX_GAIN = 1
    SCALER_COMPLEX_POSTSCALER = 20
    FFT_COMPLEX_CAPTURE_MODE = 'fft_2x'
    SIM_COMPARE_DATA_ROOT = Path(__file__).resolve().parent / 'casper_simulations' / 'sim_data'
    SIM_COMPARE_CONFIGS = {
        'ICE': {
            'instrument': 'ice',
            'capture_mode': 'fft_full',
            'adc_bits': 8,
            'n_inputs': 4,
            'fft_shift': 1984,
            'capture_right_shift': 5,
            'capture_period': 0.01,
            'output_dtype': np.int32,
            'scaler_eight_bit': True,
            'prober_user_flags': False,
            'frames_per_burst': 'full',
            'compare_atol': 0,
        },
        'CRS': {
            'instrument': 'crs',
            'capture_mode': 'fft_2x',
            'adc_bits': 12,
            'n_inputs': 8,
            'fft_shift': 0,
            'capture_right_shift': 0,
            'capture_period': 0.01,
            'output_dtype': np.int32,
            'scaler_eight_bit': False,
            'prober_user_flags': None,
            'frames_per_burst': 2,
            'compare_atol': 128,
        },
    }

    def get_logger(self):
        return self.logger

    def get_constants(_, board):
        constants = NameSpace()
        constants.PLATFORM = board.mb.part_number
        constants.INPUT_WIDTH = board.ADC_BITS_PER_SAMPLE
        constants.MIN_INPUT = -2 ** (constants.INPUT_WIDTH - 1)
        constants.MAX_INPUT = 2 ** (constants.INPUT_WIDTH - 1) - 1
        constants.NUM_SAMPLES = int(board.ADC_SAMPLES_PER_FRAME)
        constants.NUM_BINS = int(board.NUMBER_OF_FREQUENCY_BINS)
        return constants

    @pytest.fixture(scope='function', autouse=True)
    async def setup(self, xr, request):
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        #xr.data.update(self.get_widths(self.test_config))
        self.data = xr.data
        self.board = await self.board_conn(xr, request)
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name)
        self.logger.setLevel(xr.config.test_config.logleveltest)

        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        pre_flags = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"fft setup pre-check: chan_fifo_overflow={pre_flags}")
        assert all(v == 0 for v in pre_flags)
        yield
        post_flags_before = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"fft setup post-check before cleanup: chan_fifo_overflow={post_flags_before}")
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        post_flags_after = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"fft setup post-check after cleanup: chan_fifo_overflow={post_flags_after}")
        assert all(v == 0 for v in post_flags_after)

    @pytest.fixture(scope='function')
    async def setup_fft(self, setup):
        if int(getattr(self.board, 'FFT_TYPE', 0)) == 0:
            pytest.skip("Firmware reports no FFT implementation")

        channels = [0]
        # CAPTURE_WIDTH is a yaml test-path pin, not an FFT get_constants field.
        capture_width = int(self.test_config.constants.CAPTURE_WIDTH)
        scaler_eight_bit = capture_width > 4 and self.data.PLATFORM == 'ICE'
        self.board.set_channelizer(
            fft_bypass=False,
            scaler_bypass=False,
            data_source=None,
            scaler_bypass_data_type=0,
            scaler_cap_data_type=self.FFT_COMPLEX_CAPTURE_MODE,
            offset_binary_encoding=False,
            scaler_eight_bit=scaler_eight_bit,
            prober_user_flags=False if self.data.PLATFORM == 'ICE' else None,
            scaler_rounding_mode=0,
            symmetric_saturation=False,
            channels=channels,
        )
        self.board.set_gains(
            np.full(int(self.data.NUM_BINS), self.SCALER_COMPLEX_GAIN, dtype=np.float64),
            postscaler=self.SCALER_COMPLEX_POSTSCALER,
            channels=channels,
        )
        samples_per_frame = int(self.board.ADC_SAMPLES_PER_FRAME)
        if samples_per_frame <= 0 or samples_per_frame & (samples_per_frame - 1):
            raise ValueError(
                f"ADC_SAMPLES_PER_FRAME must be a positive power of two, got {samples_per_frame}"
            )
        fft_shift = samples_per_frame - 1
        for ch in channels:
            # Set the FFT stage shift schedule explicitly so the tests do not
            # depend on whichever bitshift was left by firmware init or a prior
            # hardware run. Derive the all-stage mask from the active firmware
            # frame length: 2047 for ICE, 16383 for CRS.
            chan = self.board.chan[ch]
            chan.FFT.FFT_SHIFT = fft_shift
            chan.FFT.reset_fft_overflow_count()
        fft_info = self.board.FFT_INFO.get(int(self.board.FFT_TYPE), {})
        self.logger.debug(
            "Setup channelizer for receiver-formatted complex FFT capture "
            f"(mode={self.FFT_COMPLEX_CAPTURE_MODE}, gain={self.SCALER_COMPLEX_GAIN}, "
            f"postscaler={self.SCALER_COMPLEX_POSTSCALER}, fft_shift={fft_shift}, "
            f"fft_type={int(self.board.FFT_TYPE)}, fft_info={fft_info}, "
            f"adc_lanes={int(getattr(self.board, 'SAMPLES_PER_WORD', 1) or 1)}, "
            f"scaler_eight_bit={scaler_eight_bit})"
        )

    def rtol(self):
        return float(self.test_config.rtol)

    def gen_waveform(self, kind, *, amplitude=16, bins=(), seed=0):
        samples = self.data.NUM_SAMPLES
        n = np.arange(samples, dtype=np.float64)
        if kind == 'dc':
            data = np.full(samples, amplitude, dtype=np.int64)
        elif kind == 'tone':
            (tone_bin,) = bins
            data = np.rint(amplitude * np.cos(2 * np.pi * tone_bin * n / samples)).astype(np.int64)
        elif kind == 'two_tone':
            tone_a, tone_b = bins
            data = np.rint(
                amplitude * np.cos(2 * np.pi * tone_a * n / samples)
                + amplitude * np.cos(2 * np.pi * tone_b * n / samples)
            ).astype(np.int64)
        elif kind == 'ramp':
            # Use a zero-mean periodic ramp to exercise a broad, deterministic
            # spectrum without letting a large DC term dominate the comparison.
            data = np.rint(np.linspace(-amplitude, amplitude, samples, endpoint=False)).astype(np.int64)
        elif kind == 'noise':
            # Seeded integer noise gives a repeatable full-band input while
            # keeping enough headroom for the fixed-point FFT path.
            rng = np.random.default_rng(seed)
            data = rng.integers(-amplitude, amplitude + 1, size=samples, dtype=np.int64)
            data = data - int(np.rint(np.mean(data)))
        else:
            raise ValueError(f"Unknown FFT waveform kind '{kind}'")
        return np.clip(data, self.data.MIN_INPUT, self.data.MAX_INPUT).astype(np.int64)

    def numpy_fft_reference(self, waveform):
        # The firmware emits positive-frequency bins only; drop NumPy's Nyquist
        # bin so the reference has the same length as the hardware FFT capture.
        return np.fft.rfft(waveform.astype(np.float64))[:-1]

    def pack_funcgen_adc_lanes(self, adc_lanes):
        """Pack parallel ADC lane streams into the FUNCGEN RAM layout.

        The arbitrary FUNCGEN API accepts a single one-dimensional buffer, but
        that buffer is not conceptually a serial scalar waveform when it feeds
        the channelizer. It is the RAM image consumed by the firmware's
        parallel ADC/F-engine input interface. One channelizer clock contains
        ``SAMPLES_PER_WORD`` ADC samples, and the host buffer must therefore be
        interleaved by lane:

            buffer[0::N] -> ADC/F-engine input lane 0
            buffer[1::N] -> ADC/F-engine input lane 1
            ...
            buffer[N-1::N] -> ADC/F-engine input lane N-1

        This is the same convention shown in ``fft_sim_test_example.py`` for
        replaying CASPER simulation inputs: take 512 samples from each of the
        four parallel ADC input signals and interleave them into one 2048-sample
        FUNCGEN buffer. Keeping that packing in a named helper is deliberate:
        FFT tests should construct the signal at the parallel input boundary
        and let this function create the write image. Writing a generated NumPy
        waveform directly to ``set_funcgen(data=...)`` hides that boundary and
        makes it easy to accidentally compare a scalar serial ordering against
        a four-lane firmware datapath.

        The helper only performs input packing. It does not rename or fold FFT
        output bins, and it does not compensate a failing result by changing the
        NumPy reference. If a tone still lands in a different captured bin after
        this packing, the mismatch is exposed in the test output and should be
        investigated as an FFT/capture convention issue rather than concealed in
        the reference spectrum.
        """
        adc_lanes = np.asarray(adc_lanes, dtype=np.int64)
        n_lanes = int(getattr(self.board, 'SAMPLES_PER_WORD', 1) or 1)
        if adc_lanes.ndim != 2 or adc_lanes.shape[0] != n_lanes:
            raise ValueError(f"adc_lanes must have shape ({n_lanes}, samples_per_lane)")
        if adc_lanes.size != self.data.NUM_SAMPLES:
            raise ValueError(
                f"adc_lanes must contain {self.data.NUM_SAMPLES} total samples, "
                f"got {adc_lanes.size}"
            )

        funcgen_input = np.empty(self.data.NUM_SAMPLES, dtype=np.int64)
        for lane in range(n_lanes):
            funcgen_input[lane::n_lanes] = adc_lanes[lane]
        return funcgen_input

    def pack_scalar_waveform_for_funcgen(self, waveform):
        """Convert a scalar time-series frame into FUNCGEN lane-interleaved RAM.

        Tests often start from a convenient scalar sequence because NumPy FFT
        references are naturally written that way. Before programming FUNCGEN,
        split that scalar sequence at the parallel ADC boundary and call
        ``pack_funcgen_adc_lanes``. For a correctly ordered scalar waveform this
        gives lane 0 samples ``0, N, 2N, ...``, lane 1 samples ``1, N+1, ...``,
        and so on, then writes the explicit lane-interleaved RAM image.
        """
        waveform = np.asarray(waveform, dtype=np.int64)
        if waveform.ndim != 1 or waveform.size != self.data.NUM_SAMPLES:
            raise ValueError(
                f"waveform must be a one-dimensional frame of {self.data.NUM_SAMPLES} samples"
            )
        n_lanes = int(getattr(self.board, 'SAMPLES_PER_WORD', 1) or 1)
        if waveform.size % n_lanes:
            raise ValueError(
                f"waveform size {waveform.size} is not divisible by adc lane count {n_lanes}"
            )
        adc_lanes = waveform.reshape(-1, n_lanes).T
        return self.pack_funcgen_adc_lanes(adc_lanes)

    async def capture_fft_complex(self, waveform, *, ch=0, ncap=4):
        logger = self.get_logger()
        funcgen_input = self.pack_scalar_waveform_for_funcgen(waveform)
        self.board.sync()
        # Reset FUNCGEN shifts explicitly so earlier tests cannot leave an
        # amplitude transform in front of the FFT path.
        self.board.set_funcgen(
            function='arb',
            channels=[ch],
            left_shift=0,
            right_shift=0,
            reset_chan=True,
            data=funcgen_input,
        )
        # Use the receiver's complex FFT formatter instead of manually pairing
        # bytes from the regular 8-bit scaler stream. Capture mode 4 is decoded
        # as complex data in natural bin order.
        self.board.set_scaler_output_modes(
            bypass=False,
            cap_data_type=self.FFT_COMPLEX_CAPTURE_MODE,
            channels=[ch],
        )

        frames_per_burst = 2
        capture_mode = 3 if getattr(self.board, 'CAPTURE_TYPE', None) == 'UCAP' else 0
        self.board.start_data_capture(
            period=0.01,
            frames_per_burst=frames_per_burst,
            mode=capture_mode,
            channels=[ch],
            source='scaler',
            data_type=self.FFT_COMPLEX_CAPTURE_MODE,
        )
        self.board.sync()
        receiver = await self.get_data_receiver(port_number=0, verbose=self.test_config.loglevelrecv)
        receiver.flush()
        # The PFB/FFT pipeline is continuously running. Read several completed
        # logical frames after changing FUNCGEN, then assert against the last one.
        ts, data, count = receiver.read_raw_frames(
            ncap=ncap,
            split=False,
            source=1,
            chan_first=False,
        )
        spectrum = data[-1, 0, :].astype(np.complex128)
        overflow_count = int(self.board.chan[ch].FFT.get_fft_overflow_count())
        logger.debug(
            f"capture_fft_complex: ch={ch} ncap={ncap} capture_mode={capture_mode} "
            f"frames_per_burst={frames_per_burst} "
            f"ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape} "
            f"spectrum_shape={spectrum.shape} spectrum_real_min={float(np.min(spectrum.real))} "
            f"spectrum_real_max={float(np.max(spectrum.real))} spectrum_imag_min={float(np.min(spectrum.imag))} "
            f"spectrum_imag_max={float(np.max(spectrum.imag))} "
            f"gain={self.SCALER_COMPLEX_GAIN} postscaler={self.SCALER_COMPLEX_POSTSCALER} "
            f"rtol={self.rtol()} "
            f"overflow_count={overflow_count} funcgen_left_shift={self.board.chan[ch].FUNCGEN.LEFT_SHIFT} "
            f"funcgen_right_shift={self.board.chan[ch].FUNCGEN.RIGHT_SHIFT} "
            f"funcgen_input_first16={funcgen_input[:16].tolist()}"
        )
        assert spectrum.size == self.data.NUM_BINS
        assert overflow_count == 0
        return spectrum

    def plot_spectrum_comparison(self, xr, hardware_fft, numpy_fft, title):
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        hw_mag = np.abs(hardware_fft)
        ref_mag = np.abs(numpy_fft)
        assert hw_mag.size == ref_mag.size
        assert np.max(hw_mag) > 0
        assert np.max(ref_mag) > 0

        # Plot normalized magnitudes because the firmware FFT is fixed-point and
        # PFB-scaled; peak structure is the useful visual comparison here.
        hw_norm = hw_mag / np.max(hw_mag)
        ref_norm = ref_mag / np.max(ref_mag)
        residuals = hw_norm - ref_norm

        xr.header(title)
        print("Creating FFT spectrum plots")
        plot((hw_norm, ref_norm), title=title)
        xr.insert_plot()
        plt.close()
        plot((residuals,), title=f"Residuals of {title.lower()}")
        xr.insert_plot()
        plt.close()
        print(f"Sum of normalized magnitude residuals: {np.sum(np.abs(residuals))}")
        return hw_norm, ref_norm

    def assert_spectrum_rtol(self, xr, hardware_fft, numpy_fft, *, title):
        logger = self.get_logger()
        hw_norm, ref_norm = self.plot_spectrum_comparison(xr, hardware_fft, numpy_fft, title)
        residuals = hw_norm - ref_norm
        data_scale = float(np.max(np.abs(ref_norm)))
        if data_scale <= 0:
            raise AssertionError("Cannot compute relative residual from a zero reference spectrum")
        max_residual = float(np.max(np.abs(residuals)))
        relative_residual = max_residual / data_scale
        allowed_residual = data_scale * self.rtol()
        largest_residual_bins = np.argsort(np.abs(residuals))[-8:][::-1]
        logger.debug(
            f"assert_spectrum_rtol: rtol={self.rtol()} "
            f"data_scale={data_scale} max_residual={max_residual} "
            f"allowed_residual={allowed_residual} relative_residual={relative_residual} "
            f"largest_residual_bins={largest_residual_bins.tolist()} "
            f"largest_residual_values={residuals[largest_residual_bins].tolist()}"
        )
        print(
            "Max normalized magnitude residual: "
            f"{max_residual:.6g}; allowed residual: {allowed_residual:.6g} "
            f"(relative {relative_residual:.6g} <= rtol {self.rtol():.6g}; "
            f"{100.0 * relative_residual:.3f}% of reference scale {data_scale:.6g})"
        )
        assert max_residual <= allowed_residual
        return hw_norm, ref_norm

    def assert_spectrum_peaks(self, xr, hardware_fft, numpy_fft, expected_bins, *, title):
        logger = self.get_logger()
        hw_norm, ref_norm = self.assert_spectrum_rtol(xr, hardware_fft, numpy_fft, title=title)
        expected_bins = tuple(int(b) for b in expected_bins)
        for bin_idx in expected_bins:
            assert 0 <= bin_idx < hw_norm.size

        top_count = max(len(expected_bins) * 2, 4)
        top_hw_bins = np.argsort(hw_norm)[-top_count:][::-1]
        top_ref_bins = np.argsort(ref_norm)[-top_count:][::-1]
        logger.debug(
            f"assert_spectrum_peaks: expected_bins={expected_bins} "
            f"top_hw_bins={top_hw_bins.tolist()} top_hw_values={hw_norm[top_hw_bins].tolist()} "
            f"top_ref_bins={top_ref_bins.tolist()} top_ref_values={ref_norm[top_ref_bins].tolist()}"
        )
        for bin_idx in expected_bins:
            assert bin_idx in top_hw_bins

    def assert_spectrum_shape(
        self,
        xr,
        hardware_fft,
        numpy_fft,
        *,
        title,
    ):
        logger = self.get_logger()
        hw_norm, ref_norm = self.assert_spectrum_rtol(xr, hardware_fft, numpy_fft, title=title)

        # Use raw per-bin residuals so bin swaps and local spectral errors stay
        # visible in both the plots and the assertion diagnostics.
        residuals = hw_norm - ref_norm
        bin_cosine = float(np.dot(hw_norm, ref_norm) / (np.linalg.norm(hw_norm) * np.linalg.norm(ref_norm)))
        bin_l1 = float(np.sum(np.abs(residuals)))
        bin_abs = float(np.max(np.abs(residuals)))
        largest_residual_bins = np.argsort(np.abs(residuals))[-8:][::-1]
        logger.debug(
            f"assert_spectrum_shape: bin_cosine={bin_cosine} bin_l1={bin_l1} bin_abs={bin_abs} "
            f"largest_residual_bins={largest_residual_bins.tolist()} "
            f"largest_residual_values={residuals[largest_residual_bins].tolist()}"
        )

    def setup_fft_simulation_compare(self, ch, sim_config):
        if int(getattr(self.board, 'FFT_TYPE', 0)) == 0:
            pytest.skip("Firmware reports no FFT implementation")

        adc_bits = int(getattr(self.board, 'ADC_BITS_PER_SAMPLE', self.data.INPUT_WIDTH))
        n_inputs = int(getattr(self.board, 'SAMPLES_PER_WORD', 0))
        if adc_bits < int(sim_config.adc_bits):
            pytest.skip(
                f"FFT simulation comparison input uses {sim_config.adc_bits}-bit samples, "
                f"but firmware reports {adc_bits}-bit ADC input"
            )
        if n_inputs != int(sim_config.n_inputs):
            pytest.skip(
                f"FFT simulation comparison for {sim_config.platform} expects "
                f"{sim_config.n_inputs} ADC lanes, got {n_inputs}"
            )

        self.board.set_channelizer(
            fft_bypass=False,
            scaler_bypass=False,
            data_source=None,
            scaler_bypass_data_type=0,
            scaler_cap_data_type=sim_config.capture_mode,
            offset_binary_encoding=False,
            scaler_eight_bit=sim_config.scaler_eight_bit,
            prober_user_flags=sim_config.prober_user_flags,
            scaler_rounding_mode=0,
            symmetric_saturation=False,
            channels=[ch],
        )
        self.board.set_scaler_output_modes(
            bypass=False,
            cap_data_type=sim_config.capture_mode,
            channels=[ch],
        )
        self.board.chan[ch].FFT.FFT_SHIFT = int(sim_config.fft_shift)
        self.board.chan[ch].FFT.reset_fft_overflow_count()

        fft_info = self.board.FFT_INFO.get(int(self.board.FFT_TYPE), {})
        self.logger.debug(
            "Setup channelizer for FFT simulation comparison "
            f"(platform={sim_config.platform}, instrument={sim_config.instrument}, "
            f"mode={sim_config.capture_mode}, fft_shift={sim_config.fft_shift}, "
            f"adc_bits={sim_config.adc_bits}, n_inputs={sim_config.n_inputs}, "
            f"fft_type={int(self.board.FFT_TYPE)}, fft_info={fft_info})"
        )

    def load_fft_simulation_case(self, stem, sim_config):
        input_path = sim_config.input_dir / f'{stem}.bin'
        output_path = sim_config.output_dir / f'{stem}_out.bin'
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        if not output_path.exists():
            raise FileNotFoundError(output_path)

        input_signal = InputSignal.from_binary(
            input_path,
            adc_bits=int(sim_config.adc_bits),
            n_inputs=int(sim_config.n_inputs),
        )
        board_adc_bits = int(getattr(self.board, 'ADC_BITS_PER_SAMPLE', self.data.INPUT_WIDTH))
        board_min_input = -(1 << (board_adc_bits - 1))
        board_max_input = (1 << (board_adc_bits - 1)) - 1
        input_min = int(np.min(input_signal.data))
        input_max = int(np.max(input_signal.data))
        if input_min < board_min_input or input_max > board_max_input:
            raise ValueError(
                f"{input_path} has samples outside firmware signed {board_adc_bits}-bit range: "
                f"min/max={input_min}/{input_max}, allowed={board_min_input}/{board_max_input}"
            )
        nsamp = int(self.board.ADC_SAMPLES_PER_FRAME)
        if input_signal.data.size < nsamp:
            raise ValueError(
                f"{input_path} contains {input_signal.data.size} samples, expected at least one "
                f"{nsamp}-sample ADC frame"
            )
        funcgen_input = input_signal.data.T.ravel()[:nsamp].astype(np.int64, copy=False)

        sim = OutputSignal.from_file(
            output_path,
            instrument=sim_config.instrument,
            dtype=sim_config.output_dtype,
        )
        sync_idx = np.flatnonzero(sim.sync_out == 1)
        if sync_idx.size != 1:
            raise AssertionError(f"Expected exactly one sync_out pulse in {output_path}, got {sync_idx.size}")
        sim_frame = sim.get_sync_frame()
        sim_real, sim_imag = sim_frame.get_real_imag(descramble=True)
        return {
            'input_path': input_path,
            'output_path': output_path,
            'funcgen_input': funcgen_input,
            'sync_idx': int(sync_idx[0]),
            'sim_frame_rows': int(sim_frame.sync_out.size),
            'sim_adc_bits': int(sim_config.adc_bits),
            'sim_n_inputs': int(sim_config.n_inputs),
            'input_min': input_min,
            'input_max': input_max,
            'sim_real': sim_real.astype(np.int64, copy=False),
            'sim_imag': sim_imag.astype(np.int64, copy=False),
        }

    def integer_parts(self, values, label):
        rounded = np.rint(values)
        fraction = values - rounded
        if np.any(fraction != 0):
            raise AssertionError(
                f"Captured {label} FFT output has non-integer values; "
                f"max discarded fraction={float(np.max(np.abs(fraction)))}"
            )
        return rounded.astype(np.int64)

    async def capture_fft_simulation_frame(self, funcgen_input, *, ch=0, ncap=4, sim_config):
        logger = self.get_logger()
        self.board.sync()
        self.board.set_funcgen(
            function='arb',
            channels=[ch],
            left_shift=0,
            right_shift=0,
            reset_chan=True,
            data=funcgen_input,
        )
        self.board.set_scaler_output_modes(
            bypass=False,
            cap_data_type=sim_config.capture_mode,
            channels=[ch],
        )

        if sim_config.frames_per_burst == 'full':
            fft_info = self.board.FFT_INFO[int(self.board.FFT_TYPE)]
            fft_bins_per_word = int(fft_info.get('bins_per_word') or 1)
            frames_per_burst = 2 * fft_bins_per_word
        else:
            frames_per_burst = int(sim_config.frames_per_burst)
        capture_mode = 3 if getattr(self.board, 'CAPTURE_TYPE', None) == 'UCAP' else 0
        self.board.start_data_capture(
            period=sim_config.capture_period,
            frames_per_burst=frames_per_burst,
            mode=capture_mode,
            channels=[ch],
            source='scaler',
            data_type=sim_config.capture_mode,
        )
        self.board.sync()

        receiver = await self.get_data_receiver(port_number=0, verbose=self.test_config.loglevelrecv)
        receiver.flush()
        ts, data, count = receiver.read_raw_frames(
            ncap=ncap,
            source=1,
            chan_first=False,
        )
        captured = data[-1, 0, :]
        captured_real = self.integer_parts(captured.real, 'real')
        captured_imag = self.integer_parts(captured.imag, 'imag')
        overflow_count = int(self.board.chan[ch].FFT.get_fft_overflow_count())
        logger.debug(
            f"capture_fft_simulation_frame: platform={sim_config.platform} ch={ch} ncap={ncap} "
            f"capture_mode={sim_config.capture_mode} ucap_mode={capture_mode} "
            f"frames_per_burst={frames_per_burst} "
            f"ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape} "
            f"captured_shape={captured.shape} overflow_count={overflow_count} "
            f"funcgen_min={int(np.min(funcgen_input))} funcgen_max={int(np.max(funcgen_input))} "
            f"funcgen_first16={funcgen_input[:16].tolist()}"
        )
        return {
            'real': captured_real >> int(sim_config.capture_right_shift),
            'imag': captured_imag >> int(sim_config.capture_right_shift),
            'overflow_count': overflow_count,
            'frames_per_burst': frames_per_burst,
            'ucap_mode': capture_mode,
            'capture_mode': sim_config.capture_mode,
            'capture_right_shift': int(sim_config.capture_right_shift),
            'fft_shift': int(sim_config.fft_shift),
            'timestamp': int(ts[-1]),
            'count': count[-1].tolist(),
            'raw_shape': data.shape,
            'raw_dtype': data.dtype,
        }

    def plot_simulation_comparison(
        self,
        xr,
        stem,
        captured_real,
        captured_imag,
        sim_real,
        sim_imag,
        real_residual,
        imag_residual,
        sim_config,
    ):
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        xr.header(f"FFT simulation comparison ({sim_config.platform}, {sim_config.capture_mode}): {stem}")
        fig, axes = plt.subplots(3, 1, figsize=(13, 9), sharex=True)
        axes[0].plot(sim_real, lw=1, label='simulation real')
        axes[0].plot(captured_real, lw=1, alpha=0.8, label='captured real')
        axes[0].set_ylabel('Real')
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        axes[1].plot(sim_imag, lw=1, label='simulation imag')
        axes[1].plot(captured_imag, lw=1, alpha=0.8, label='captured imag')
        axes[1].set_ylabel('Imag')
        axes[1].legend()
        axes[1].grid(True, alpha=0.3)

        axes[2].plot(real_residual, lw=1, label='real residual')
        axes[2].plot(imag_residual, lw=1, label='imag residual')
        axes[2].set_xlabel('FFT bin')
        axes[2].set_ylabel('Residual')
        axes[2].legend()
        axes[2].grid(True, alpha=0.3)

        fig.suptitle(f"{stem} {sim_config.platform} {sim_config.capture_mode} FFT capture vs simulation")
        fig.tight_layout()
        xr.insert_plot()
        plt.close(fig)

    async def assert_fft_matches_simulation(self, xr, stem, *, ch=0):
        platform = self.data.PLATFORM
        if platform not in self.SIM_COMPARE_CONFIGS:
            valid = ', '.join(sorted(self.SIM_COMPARE_CONFIGS))
            pytest.skip(f"FFT simulation comparison is defined only for: {valid}")
        config = dict(self.SIM_COMPARE_CONFIGS[platform])
        config['platform'] = platform
        config['data_dir'] = self.SIM_COMPARE_DATA_ROOT / config['instrument']
        config['input_dir'] = config['data_dir'] / 'inputs'
        config['output_dir'] = config['data_dir'] / 'outputs'
        sim_config = NameSpace(config)

        self.setup_fft_simulation_compare(ch, sim_config)
        sim_case = self.load_fft_simulation_case(stem, sim_config=sim_config)
        captured = await self.capture_fft_simulation_frame(sim_case['funcgen_input'], ch=ch, sim_config=sim_config)

        captured_real = captured['real']
        captured_imag = captured['imag']
        sim_real = sim_case['sim_real']
        sim_imag = sim_case['sim_imag']
        assert captured_real.size == sim_real.size
        assert captured_imag.size == sim_imag.size
        assert captured_real.size == int(self.data.NUM_BINS)

        real_residual = captured_real - sim_real
        imag_residual = captured_imag - sim_imag
        self.plot_simulation_comparison(
            xr,
            stem,
            captured_real,
            captured_imag,
            sim_real,
            sim_imag,
            real_residual,
            imag_residual,
            sim_config,
        )

        real_max_abs = int(np.max(np.abs(real_residual)))
        imag_max_abs = int(np.max(np.abs(imag_residual)))
        real_sum = int(np.sum(real_residual, dtype=np.int64))
        imag_sum = int(np.sum(imag_residual, dtype=np.int64))
        real_nonzero = int(np.count_nonzero(real_residual))
        imag_nonzero = int(np.count_nonzero(imag_residual))
        print(f"\nFFT simulation comparison ({stem}, ch={ch}, platform={sim_config.platform})")
        print(f"instrument:         {sim_config.instrument}")
        print(f"capture mode:       {captured['capture_mode']}")
        print(f"input bin:          {sim_case['input_path']}")
        print(f"simulation out bin: {sim_case['output_path']}")
        print(f"simulation sync:    index={sim_case['sync_idx']}, frame_rows={sim_case['sim_frame_rows']}")
        print(
            f"simulation input:   adc_bits={sim_case['sim_adc_bits']}, lanes={sim_case['sim_n_inputs']}, "
            f"min/max={sim_case['input_min']}/{sim_case['input_max']}"
        )
        print(
            f"funcgen frame:      {sim_case['funcgen_input'].size} samples, "
            f"min/max={int(np.min(sim_case['funcgen_input']))}/{int(np.max(sim_case['funcgen_input']))}"
        )
        print(f"captured shape:     {captured['raw_shape']}, dtype={captured['raw_dtype']}")
        print(f"capture count:      {captured['count']}, timestamp={captured['timestamp']}")
        print(f"frames_per_burst:   {captured['frames_per_burst']}")
        print(f"UCAP mode:          {captured['ucap_mode']}")
        print(f"FFT_SHIFT:          {captured['fft_shift']}")
        print(f"capture scale:      captured >> {captured['capture_right_shift']}")
        print(f"comparison atol:    {sim_config.compare_atol}")
        print(f"overflow_count:     {captured['overflow_count']}")
        print(
            "residuals:          "
            f"real max_abs={real_max_abs}, real sum={real_sum}, real nonzero={real_nonzero}; "
            f"imag max_abs={imag_max_abs}, imag sum={imag_sum}, imag nonzero={imag_nonzero}; "
            f"total sum={real_sum + imag_sum}"
        )

        assert captured['overflow_count'] == 0
        compare_atol = int(sim_config.compare_atol)
        if compare_atol == 0:
            np.testing.assert_array_equal(captured_real, sim_real)
            np.testing.assert_array_equal(captured_imag, sim_imag)
        else:
            assert real_max_abs <= compare_atol
            assert imag_max_abs <= compare_atol
            np.testing.assert_allclose(captured_real, sim_real, rtol=0, atol=compare_atol)
            np.testing.assert_allclose(captured_imag, sim_imag, rtol=0, atol=compare_atol)

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_fft_dc_peak(self, ch, setup_fft, xr):
        waveform = self.gen_waveform('dc', amplitude=16)
        hardware_fft = await self.capture_fft_complex(waveform, ch=ch)
        numpy_fft = self.numpy_fft_reference(waveform)
        self.assert_spectrum_peaks(
            xr,
            hardware_fft,
            numpy_fft,
            expected_bins=(0,),
            title=f"FFT DC peak (ch={ch})",
        )

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    @pytest.mark.parametrize("tone_bin", (8, 11, 37))
    async def test_fft_single_tone_peak(self, tone_bin, ch, setup_fft, xr):
        waveform = self.gen_waveform('tone', amplitude=16, bins=(tone_bin,))
        hardware_fft = await self.capture_fft_complex(waveform, ch=ch)
        numpy_fft = self.numpy_fft_reference(waveform)
        self.assert_spectrum_peaks(
            xr,
            hardware_fft,
            numpy_fft,
            expected_bins=(tone_bin,),
            title=f"FFT single tone peak (bin={tone_bin}, ch={ch})",
        )

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_fft_two_tone_peaks(self, ch, setup_fft, xr):
        tone_bins = (11, 53)
        waveform = self.gen_waveform('two_tone', amplitude=8, bins=tone_bins)
        hardware_fft = await self.capture_fft_complex(waveform, ch=ch)
        numpy_fft = self.numpy_fft_reference(waveform)
        self.assert_spectrum_peaks(
            xr,
            hardware_fft,
            numpy_fft,
            expected_bins=tone_bins,
            title=f"FFT two tone peaks (bins={tone_bins}, ch={ch})",
        )

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_fft_ramp_spectrum_shape(self, ch, setup_fft, xr):
        # The explicit all-stage FFT shift attenuates broadband spectra enough
        # that small inputs are dominated by 8-bit quantization. Use a high but
        # non-clipping ramp so this remains a shape/order test.
        waveform = self.gen_waveform('ramp', amplitude=96)
        hardware_fft = await self.capture_fft_complex(waveform, ch=ch)
        numpy_fft = self.numpy_fft_reference(waveform)
        self.assert_spectrum_shape(
            xr,
            hardware_fft,
            numpy_fft,
            title=f"FFT ramp spectrum shape (ch={ch})",
        )

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_fft_noise_spectrum_shape(self, ch, setup_fft, xr):
        # Seeded noise exercises many bins at once. With all FFT stages shifted,
        # low-amplitude noise is mostly quantization residue, so keep it high
        # enough for a stable spectral-shape comparison without input clipping.
        waveform = self.gen_waveform('noise', amplitude=96, seed=0x5EED)
        hardware_fft = await self.capture_fft_complex(waveform, ch=ch)
        numpy_fft = self.numpy_fft_reference(waveform)
        self.assert_spectrum_shape(
            xr,
            hardware_fft,
            numpy_fft,
            title=f"FFT seeded noise spectrum shape (ch={ch})",
        )

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_compare_to_simulation_ramp(self, ch, setup, xr):
        await self.assert_fft_matches_simulation(xr, 'ramp', ch=ch)

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_compare_to_simulation_two_tone(self, ch, setup, xr):
        await self.assert_fft_matches_simulation(xr, 'two_tone', ch=ch)

    @pytest.mark.fft
    @pytest.mark.parametrize("ch", (0,))
    async def test_compare_to_simulation_noise(self, ch, setup, xr):
        await self.assert_fft_matches_simulation(xr, 'noise', ch=ch)
