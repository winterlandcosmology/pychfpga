from pychfpga.fpga_array import FPGAArray
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
import logging
import pytest
from .utils import plot
import matplotlib.pyplot as plt
import numpy as np

# yaml constants.<name> -> live board attribute (or callable).
_PIN_TO_BOARD = {
    'ADC_BITS_PER_SAMPLE': 'ADC_BITS_PER_SAMPLE',
    'INPUT_WIDTH': 'ADC_BITS_PER_SAMPLE',
    'ADC_WIDTH': 'ADC_BITS_PER_SAMPLE',
    'NUM_CHANNELS': 'NUMBER_OF_CHANNELIZERS',
    'SAMPLES_PER_FRAME': 'ADC_SAMPLES_PER_FRAME',
    'NUM_SAMPLES': 'ADC_SAMPLES_PER_FRAME',
    'NUM_BINS': 'NUMBER_OF_FREQUENCY_BINS',
    'BINS_PER_WORD': 'FFT_BINS_PER_WORD',
    'CAPTURE_BUS_WIDTH': 'CAPTURE_BUS_WIDTH',
    'FFT_WIDTH': lambda board: board.FFT_INFO[board.FFT_TYPE]['bits_per_bin'] // 2,
    'PLATFORM': lambda board: board.mb.part_number,
}


class TestModule:
    @pytest.fixture(autouse=True)
    def close_figures(self):
        """Close pyplot figures left open by the test.

        ``insert_plot`` saves the current figure and leaves it active, so a
        test can keep drawing on it. Figures are discarded once the test returns.
        """
        yield
        plt.close('all')

    def _connection_mode(self, xr):
        conn = xr.config.connection_config
        return conn.mode if hasattr(conn, 'mode') else conn.get('mode')

    def _supported_modes(self, xr, request):
        """Modes this test module may run in, from test_config.supported_modes.

        Key is the test file stem (``request.path.stem``: ``scaler_test``,
        ``corr_test``, ...). ``None`` means the yaml does not restrict this module.
        """
        supported = xr.config.test_config.get('supported_modes')
        if supported is None:
            return None
        name = request.path.stem
        if name not in supported:
            return None
        modes = supported[name]
        if isinstance(modes, str):
            return [modes]
        return list(modes)

    def _skip_unsupported_mode(self, xr, request, mode):
        """Skip before connecting when ``mode`` is not in this module's list."""
        modes = self._supported_modes(xr, request)
        if modes is None:
            return
        if mode not in modes:
            pytest.skip(
                f'{request.path.stem} does not support FPGA mode {mode!r} '
                f'(supported: {modes})'
            )

    def _connection_kwargs(self, xr, mode):
        cfg = xr.config.connection_config
        keys = cfg.keys() if hasattr(cfg, 'keys') else cfg.as_dict().keys()
        kwargs = {key: cfg[key] for key in keys}
        kwargs['mode'] = mode
        return kwargs

    async def board_conn(self, xr, request):
        desired_mode = self._connection_mode(xr)
        # Skip before FPGAArray(): an unsupported mode would otherwise reprogram
        # the board and only then skip, which looks like a hang.
        self._skip_unsupported_mode(xr, request, desired_mode)
        cached = 'board' in xr.data and 'ca' in xr.data
        if cached and xr.data.ca.get_mode() == desired_mode:
            self.ca = xr.data.ca
            board = xr.data.board
        else:
            logging.getLogger(FPGAArray.__name__).setLevel(xr.config.test_config.loglevelconn)
            ca = await FPGAArray(**self._connection_kwargs(xr, desired_mode))
            xr.data.ca = ca
            xr.data.board = ca.ib[0]
            self.ca = ca
            board = xr.data.board
        live = self.get_constants(board)
        self.assert_yaml_constants(xr, board, live)
        xr.data.update(live.as_dict() if hasattr(live, 'as_dict') else live)
        return board

    async def get_data_receiver(self, **kwargs):
        """Return the array raw-data receiver (one-board FPGAArray)."""
        return await self.ca.get_data_receiver_async(**kwargs)

    async def get_corr_receiver(self, **kwargs):
        """Return the array correlator receiver (one-board FPGAArray)."""
        kwargs.pop('buf_size', None)  # UDP default is already 8192; not an array-receiver arg
        return await self.ca.get_corr_receiver_async(**kwargs)

    @staticmethod
    def collection_constants(test_config):
        """Yaml constants plus aliases and scaler derives for collection-time eval."""
        constants = test_config.get('constants')
        if constants is None:
            raise KeyError(
                'test_config.constants is required for collection '
                '(no hardware at collect time); add constants to the yaml'
            )
        constants_dict = constants.as_dict() if hasattr(constants, 'as_dict') else dict(constants)
        ns = NameSpace(dict(constants_dict))
        adc_bits = ns.ADC_BITS_PER_SAMPLE
        capture_width = ns.CAPTURE_WIDTH
        fft_width = ns.FFT_WIDTH
        ns.INPUT_WIDTH = adc_bits
        ns.ADC_WIDTH = adc_bits
        ns.NUM_SAMPLES = ns.SAMPLES_PER_FRAME
        ns.SCALED_WIDTH = fft_width + 16  # maximum gain
        ns.MIN_INPUT, ns.MAX_INPUT = -2 ** (adc_bits - 1), 2 ** (adc_bits - 1) - 1
        ns.MIN_OUTPUT, ns.MAX_OUTPUT = -2 ** (capture_width - 1), 2 ** (capture_width - 1) - 1
        ns.IMPLICIT_SHIFT = fft_width - adc_bits  # shift applied to funcgen data when bypassing fft
        ns.SCALER_SHIFT = ns.SCALED_WIDTH - capture_width  # scaler extracts the salient MSBs
        ns.IDENTITY_POSTSCALER = ns.SCALER_SHIFT - ns.IMPLICIT_SHIFT  # captured data equals input
        ns.MAPPING_POSTSCALER = ns.IDENTITY_POSTSCALER - (adc_bits - capture_width)  # input range maps to capture range
        ns.READOUT_SHIFT = ns.CAPTURE_BUS_WIDTH // (2 * ns.BINS_PER_WORD) - capture_width  # Re and Im components
        return ns

    def assert_yaml_constants(self, xr, board, live):
        """Fail setup if yaml constants disagree with GPIO or live get_constants."""
        test_config = xr.config.test_config
        constants = test_config.get('constants')
        if constants is None:
            raise KeyError(
                'test_config.constants is required; add constants to the yaml'
            )
        yaml_name = (
            getattr(xr, 'config_filename', None)
            or getattr(xr.config, 'filename', None)
            or 'test_config.constants'
        )
        constants_dict = constants.as_dict() if hasattr(constants, 'as_dict') else dict(constants)
        live_dict = live.as_dict() if hasattr(live, 'as_dict') else dict(live)

        def _fail(name, expected, actual, source):
            raise AssertionError(
                f'yaml {yaml_name} constants.{name}={expected!r} but {source}={actual!r}'
            )

        for name, expected in constants_dict.items():
            getter = _PIN_TO_BOARD.get(name)
            if getter is not None:
                actual = getter(board) if callable(getter) else getattr(board, getter)
                if actual != expected:
                    source = (
                        f'board.mb.part_number' if name == 'PLATFORM'
                        else f'board.FFT_INFO[FFT_TYPE].bits_per_bin // 2' if name == 'FFT_WIDTH'
                        else f'board.{getter if not callable(getter) else name}'
                    )
                    _fail(name, expected, actual, source)
            if name == 'CAPTURE_WIDTH' and 'CAPTURE_WIDTH' in live_dict:
                actual = live_dict['CAPTURE_WIDTH']
                if actual != expected:
                    _fail(name, expected, actual, 'get_constants().CAPTURE_WIDTH')

        derived = self.collection_constants(test_config)
        derived_dict = derived.as_dict()
        for key, live_val in live_dict.items():
            if key in derived_dict and derived_dict[key] != live_val:
                _fail(key, derived_dict[key], live_val, f'get_constants().{key}')

    def _explicit_parametrize_fixtures(self, metafunc):
        """Return fixture names already handled by @pytest.mark.parametrize."""
        fixtures = set()
        for mark in metafunc.definition.iter_markers(name='parametrize'):
            argnames = mark.args[0]
            if isinstance(argnames, str):
                fixtures.update(name.strip() for name in argnames.split(',') if name.strip())
            else:
                fixtures.update(argnames)
        return fixtures

    def _config_skips_test(self, test_config, metafunc, func_name):
        """True when ``test_config.skip_tests`` drops this test before parametrization.

        ``skip_tests`` is keyed by the test file stem. ``all`` skips every test
        in that file. ``except`` skips every test except the named functions.
        A file that is absent is not skipped.
        """
        skip = test_config.get('skip_tests')
        if not skip:
            return False
        module = metafunc.definition.path.stem
        if module not in skip:
            return False
        entry = skip[module]
        if entry == 'all':
            return True
        kept = entry.get('except') if hasattr(entry, 'get') else None
        if kept is None:
            return False
        return func_name not in list(kept)

    def pytest_generate_tests(self, metafunc):
        """ This pytest hook is called before each test to allow dynamic generation of test parameters.

        It parameterises each argument corresponding to a parameter in the config file with an arbitrary python expression from that file.
        A test named by ``test_config.skip_tests`` is marked skipped and is not parametrized, so a config that does not define its arguments can still collect it.
        """
        xr = get_xr_from(metafunc)
        test_config = xr.config.test_config
        func_name = metafunc.function.__name__
        if self._config_skips_test(test_config, metafunc, func_name):
            pytest.mark.skip(
                reason=f"{func_name} is listed in test_config.skip_tests"
            )(metafunc.function)
            return
        widths = self.collection_constants(test_config)
        test_name = func_name[5:] #get rid of test_ prefix
        explicit_fixtures = self._explicit_parametrize_fixtures(metafunc)
        for fixture in metafunc.fixturenames:
            if fixture in explicit_fixtures:
                continue
            # test-specific, then top-level, then unused Ice global_ prefix
            found_param = False
            test_params = test_config.get(test_name) if test_name in test_config else None
            if test_params is not None and not isinstance(test_params, str) and fixture in test_params:
                value = test_params[fixture]
                found_param = True
            elif fixture in test_config:
                value = test_config[fixture]
                found_param = True
            elif ('global_' + fixture) in test_config:
                value = test_config['global_' + fixture]
                found_param = True
            if found_param:
                if isinstance(value, str):
                    try:
                        value = eval(value, widths.as_dict()) #pass widths as globals so that user can acess them
                    except NameError as exc:
                        raise NameError(
                            f'{test_name}: yaml param {fixture!r} = {value!r} references '
                            f'an unknown constant ({exc}). Add it to test_config.constants'
                        ) from exc
                metafunc.parametrize(fixture, value)
    
    def plot_and_test(self, xr, data, ref_data, title, **kwargs):
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        test_name = title[:title.find('(')] #up to position of first bracket
        parameters  = title[title.find('('):]
        xr.header(test_name)
        #assert np.all(np.equal(data[0], data))
        print(f'Parameters: {parameters}')
        print(data.shape, data.dtype)
        if np.iscomplexobj(data) and not np.iscomplexobj(ref_data):
            # Receiver already emits complex scaler/FFT spectra; pair interleaved Re/Im refs.
            ref = np.asarray(ref_data)
            data_arr = np.asarray(data)
            if ref.shape[-1] == 2 * data_arr.shape[-1]:
                ref_data = (ref[..., ::2] + 1j * ref[..., 1::2]).astype(data_arr.dtype, copy=False)
        if np.iscomplexobj(data):
            kwargs.setdefault('split_complex', True)
        residuals = data - ref_data
        print("Creating plots")
        plot((data, ref_data), title=test_name, **kwargs)
        xr.insert_plot()
        plt.close()
        plot((residuals,), title = f"Residuals of {test_name.lower()}", **kwargs)
        xr.insert_plot()
        plt.close()
        print(f"Sum of residuals: {np.sum(np.abs(residuals))}")
        if not self.test_config.only_plot:
            assert np.equal(data, ref_data).all()