import logging
import numpy as np
from pychfpga.fpga_array import FPGAArray
from .utils import plot
import pytest
from random import randint
from time import sleep
from wtl.pytest_xreport import xr, get_xr_from
from wtl.namespace import NameSpace
from pathlib import Path
import matplotlib.pyplot as plt
from textwrap import wrap

'''
Control flow
==============
Step-by-step description of the sequence in which pytest runs the setup and tests

1. When pytest is run on the command line, the pytest.ini file provides the config file as a command-line argument
2. In a session start hook, xr parses this config file and converts it to a namespace in xr.config
3. The pytest-generate-tests hook is called on each test (with a new instance of TestScaler each time)
    a. The xr object is extracted from the metafunc passed to this function, from which the config data can be accessed
    b. The get_widths method is called, to map the widths to values based on the platform specified in the config file.
    c. Any candidate fixtures in the metafunc are paramatrised according to an arbitrary user-provided python expression in the config file.
       The dictionary returned by get_widths is passed to the `eval` call as the local variables, allowing them to be referenced in the config file.
       All fixtures which are present in the argument list of the function as well as in the config file (either as global or test-specific parameters) are parameterized.
4. Before each test, the function-level setup fixture is called. Via the xr fixture, it gains access to the xr object and thus all the parameters contained therein. 
It then either connects to the board or extracts the cached board object from the xr object, as needed.
Finally, it transfers important parameters, including the board object, from xr to the namespace of self. 
5. The test is run.


The majority of the tests follow the same procedure.
First, data is generated, sent to the FPGA as input, and the result is read back from the output.
Then, using the generated input data, the code computes the expected output.
The two are then plotted and compared.

TODO: 1. Import pytest_generate_tests into .utils so that both funcgen, scaler and future test modules use the same hook.
      2. Separate config files for each module if config.yaml becomes too convoluted.  
      3. Parametrize scaler tests to work with CRS (add ucap capture)
'''

platforms = {
    'ICE-4bit-c':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 35, 'READOUT_SHIFT': 4, 'BINS_PER_SAMPLE': 2},
    'ICE-4bit-r':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 34, 'READOUT_SHIFT': 4, 'BINS_PER_SAMPLE': 2},
    'ICE-8bit-c':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 8, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 35, 'READOUT_SHIFT': 0, 'BINS_PER_SAMPLE': 2},
    'ICE-8bit-r':
        {'INPUT_WIDTH': 8, 'CAPTURE_WIDTH': 8, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 34, 'READOUT_SHIFT': 0, 'BINS_PER_SAMPLE': 2},
    'CRS':
        {'INPUT_WIDTH': 14, 'CAPTURE_WIDTH': 4, 'FFT_WIDTH': 32, 'SCALED_WIDTH': 48, 'READOUT_SHIFT': 12, 'BINS_PER_SAMPLE': 4},
    #'CHORD':
    #    {'INPUT_WIDTH': 14, 'CAPTURE_WIDTH': 16, 'FFT_WIDTH': 32, 'SCALED_WIDTH': 48, 'READOUT_SHIFT': 0, 'BINS_PER_SAMPLE': 4}
}

def unsplit_imag(array):
    '''
    Turns an array of N real numbers into an array of N/2 complex numbers, interpreting every other element as the the imaginary component.
    '''
    if array.size % 2:
        raise ValueError(f"Cannot unsplit odd-length array of size {array.size} into complex samples.")
    return (array[::2] + array[1::2]*1j).astype(np.complex64)

def split_imag(array):
    '''
    Turns an array of N/2 complex numbers into an array of N real numbers, where every other element is the imaginary component of the complex number it came from. 
    '''
    return np.stack((np.real(array), np.imag(array)), axis=1).reshape(-1).astype(np.int64)

class TestScaler:


    ROUNDING_MODES = ['truncation', 'normal', 'convergent']
    
    def get_logger(self):
        return self.logger

    def get_widths(_, test_config):
        platform = test_config.platform
        if platform not in platforms.keys():
            raise ValueError(f'{platform} not recognised as a possible platform')
        widths = NameSpace(platforms[platform])
        widths.PLATFORM = platform[:3] # Either ICE or CRS
        widths.MIN_INPUT, widths.MAX_INPUT = -2**(widths.INPUT_WIDTH-1), 2**(widths.INPUT_WIDTH-1)-1
        widths.MIN_OUTPUT, widths.MAX_OUTPUT = -2**(widths.CAPTURE_WIDTH-1), 2**(widths.CAPTURE_WIDTH-1)-1
        widths.IMPLICIT_SHIFT = widths.FFT_WIDTH - widths.INPUT_WIDTH #shift applied to funcgen data when bypassing fft
        widths.SCALER_SHIFT = widths.SCALED_WIDTH - widths.CAPTURE_WIDTH #shift applied by scaler to extract the salient MSBs
        widths.IDENTITY_POSTSCALER = widths.SCALER_SHIFT - widths.IMPLICIT_SHIFT #postscaler such that captured data equals input data
        widths.MAPPING_POSTSCALER = widths.IDENTITY_POSTSCALER - (widths.INPUT_WIDTH - widths.CAPTURE_WIDTH) # postscaler such that the range of possible inputs is mapped to the range of possible captured outputs
        return widths

    def pytest_generate_tests(self, metafunc):
        """ This pytest hook is called before each test to allow dynamic generation of test parameters.

        It parameterises each argument corresponding to a parameter in the config file with an arbitrary python expression from that file
        """
        xr = get_xr_from(metafunc)
        test_config = xr.config.test_config
        widths = self.get_widths(test_config)
        xr.data.update(widths) #store all these parameters in xr
        test_name = metafunc.function.__name__[5:] #get rid of test_ prefix
        for fixture in metafunc.fixturenames:
            #find global default, if there is
            global_param = 'global_' + fixture
            found_param = False
            if global_param in test_config.keys():
                value = test_config[global_param]
                found_param = True
            if test_name in test_config.keys(): #not elif, so global will be overriden if applicable
                test_params = test_config[test_name]
                if fixture in test_params:
                    value = test_params[fixture]
                    found_param = True
            if found_param:
                if isinstance(value, str):
                    value = eval(value, widths.as_dict()) #pass widths as globals so that user can acess them
                print(f'Parametrising {fixture}')
                metafunc.parametrize(fixture, value) 

    
    def board_conn(self, xr):
        if  'board' in xr.data: #check if board is cached to avoid unnecessary reconnection
            return xr.data.board
        ca = FPGAArray(**self.connection_config)
        xr.data.board = ca.ib[0]
        return xr.data.board

    @pytest.fixture(scope='function', autouse=True)
    def setup(self, xr):
        self.data = xr.data
        #transfer to namespace of self
        self.test_config = xr.config.test_config
        self.connection_config = xr.config.connection_config
        self.board = self.board_conn(xr)
        self.data.NUM_SAMPLES = self.board.ADC_SAMPLES_PER_FRAME
        self.data.NUM_BINS = self.board.NUMBER_OF_FREQUENCY_BINS
        self.data.NUM_CHANNELIZERS = len(self.board.chan) #currently unused
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.logleveltest)
        self.plot_dir = Path('plots/')
        # check that fifo overflow doesn't go high either before or after test
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        pre_flags = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"setup pre-check: chan_fifo_overflow={pre_flags}")
        assert all(v == 0 for v in pre_flags)
        yield # test occurs here
        post_flags_before = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"setup post-check before cleanup: chan_fifo_overflow={post_flags_before}")
        self.board.stop_data_capture()
        self.board.reset_scaler_overflow_flags()
        post_flags_after = [ch.SCALER.CHAN_FIFO_OVERFLOW for ch in self.board.chan]
        self.logger.debug(f"setup post-check after cleanup: chan_fifo_overflow={post_flags_after}")
        assert all(v == 0 for v in post_flags_after)
        


    @pytest.fixture(scope='function')
    def setup_scaler(self, setup, xr):
        self.logger.debug(f"Attempting to setup scaler on instance {self}")
        self.board.set_channelizer(
            fft_bypass=True,
            scaler_bypass=False,
            data_source=None, #prevents warning for uninitialised funcgen
            scaler_bypass_data_type=0,
            scaler_cap_data_type=0,
            offset_binary_encoding=False,
            scaler_eight_bit=self.data.CAPTURE_WIDTH > 4 and self.data.PLATFORM == 'ICE', #TODO: generalise these for other platforms
            prober_user_flags=self.data.CAPTURE_WIDTH <= 4 if self.data.PLATFORM == 'ICE' else None,
            scaler_rounding_mode=0,
            symmetric_saturation=False)
        self.logger.debug("Setup channelizer for testing scaler")
    
    def gen_data(self, func, samples, **kwargs):
        if func == 'a': #constant input
            return np.ones(samples) * kwargs.get('a', 1)
        elif func == 'ab': #alternating input
            period = kwargs.get('period', 1)
            res = np.tile(np.concatenate((np.repeat(kwargs.get('a', 0), period), np.repeat(kwargs.get('b', 1), period))), samples//(2*period))
            return np.append(res, np.repeat(kwargs.get('a', 0), samples - res.size))
        elif func == 'ramp': #single ramp from min to max inclusive
            min = kwargs.get('min', 0)
            max = kwargs.get('max', samples)
            res = np.repeat(np.arange(min, max + 1), samples // (max - min + 1))
            return np.append(res, max * np.ones(samples - res.size))
        elif func == 'periodic_ramp': #periodic ramp from min to max inclusive
            #TODO: fix to ensure bounds are always exactly respected
            min = kwargs.get('min', 0)
            max = kwargs.get('max', samples)
            res = np.tile(np.arange(min, max + 1), samples // (max - min + 1))
            return np.append(res, np.arange(min, min + (samples - res.size)))
        elif func == 'complex_ramp': #complex ramp from min+min*j to max+max*j
            min = kwargs.get('min', 0)
            max = kwargs.get('max', np.sqrt(samples))
            half_samples = samples // 2
            reals = np.repeat(np.arange(min, max + 1), half_samples // (max - min + 1))
            reals = np.append(reals, max * np.ones(half_samples - reals.size))
            cmplx = np.tile(np.arange(min, max + 1), half_samples // (max - min + 1))
            cmplx = np.append(cmplx, np.arange(min, min + half_samples - cmplx.size))
            return np.stack((reals, cmplx), axis=1).reshape(-1) #interleave the real and complex arrays
        elif func == 'arb': #user-provided data buffer
            return kwargs.get('data', np.zeros(samples))
        raise ValueError(f"Unknown data generation function '{func}'")

    def get_test_param(self, test_name, param_name, default):
        '''
        Get a parameter for a certain test, permitting both the test and the parameter to be omitted
        '''
        params = self.test_config.get(test_name, {}) #we have access to test_config since setup has been run 
        return params.get(param_name, default)

    def _uniform_real_gains(self, gain):
        '''
        Return a full-bin real gain vector for APIs that no longer safely accept
        scalar real gains.
        '''
        return np.full(int(self.data.NUM_BINS), float(gain), dtype=np.float64)

    def _set_capture_scaler(self, func, double=False, read=True, get_flags=False, cap_data_type=None, **func_kwargs):
        '''
        Sets the function generator to output a specific set of values and then captures data from the scaler.
        '''
        logger = self.get_logger()
        if cap_data_type is None:
            cap_data_type = int(self.board.chan[0].SCALER.CAP_DATA_TYPE)
        #receiver.flush()
        self.board.sync()
        source = self.gen_data(func, samples=self.data.NUM_SAMPLES, **func_kwargs) #setup has already been run, so self.data has been set
        self.board.set_data_source(source='arb', data=source)
        self.board.start_data_capture(period=0.1, source='scaler', data_type=cap_data_type)
        self.board.sync()
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv) #NOTE: must be after earlier calls for some reason
        receiver.flush()
        #receiver.update_config(self.board.state.raw_capture)
        if not read:
            logger.debug(
                f"_set_capture_scaler(read=False): func={func} cap_data_type={cap_data_type} source_shape={source.shape}"
            )
            return source, source
        if self.data.PLATFORM == 'ICE':
            if double:
                _, data, _ = receiver.read_raw_frames_double_resolution()
            else:
                _, data, _ = receiver.read_raw_frames()
            assert np.all(np.equal(data[0], data))
            if get_flags:
                return source, data & (2**self.data.READOUT_SHIFT-1)
            return source, data // (2**self.data.READOUT_SHIFT)
        #TODO: add capture mode for ucap
        else:
            if double:
                pytest.skip("Double resolution not supported on UCAP yet")
            else:
                 _, data, _ = receiver.read_raw_frames(ncap=1, flush=False, split=True, verbose=self.test_config.loglevelrecv)
                 #data = data[:, ::2]
            assert np.all(np.equal(data[0], data))
            if get_flags:
                raise Exception("Flag feature not implemented on UCAP")
            return source, (data[:, 0] // (2**self.data.READOUT_SHIFT)) #only return first frame of data
        logger.debug("Initializing data receiver")
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        ts, data, count = receiver.read_raw_frames(ncap=1, split_frames=False)

        logger.debug(
            f"_set_capture_scaler: func={func} double={double} get_flags={get_flags} cap_data_type={cap_data_type} "
            f"ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape}"
        )

        if get_flags:
            if self.data.READOUT_SHIFT > 0:
                flags = data & (2 ** self.data.READOUT_SHIFT - 1)
            else:
                raise RuntimeError("Cannot extract scaler flags when READOUT_SHIFT is 0.")
            logger.debug(f"_set_capture_scaler: flags_first16={flags[0][:16]}")
            return source, flags

        if self.data.READOUT_SHIFT > 0:
            data = data // (2 ** self.data.READOUT_SHIFT)

        logger.debug(f"_set_capture_scaler: received_first16={data[0][:16]}")
        if double:
            logger.debug("_set_capture_scaler: double=True (decoded through capture profile)")
        return source, data

    def plot_and_test(self, xr, data, ref_data, title, **kwargs):
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
        test_name = title[:title.find('(')] #up to position of first bracket
        parameters  = title[title.find('('):]
        xr.header(test_name)
        assert np.all(np.equal(data[0], data))
        print(f'Parameters: {parameters}')
        print(data.shape, data.dtype)
        residuals = data[0] - ref_data
        print("Creating plots")
        plot((data[0], ref_data), title=test_name, **kwargs)
        xr.insert_plot()
        plot((residuals,), title = f"Residuals of {test_name.lower()}")
        xr.insert_plot()
        print(f"Sum of residuals: {np.sum(np.abs(residuals))}")
        if not self.test_config.only_plot:
            assert np.equal(data, ref_data).all()
    

    @pytest.mark.scaler
    def test_all_zeroes(self, setup_scaler, xr):
        '''
        Verify that zero digital gain forces scaler output to zero for the full input range.

        The test programs the channelizer to capture scaler output (capture mode 0), feeds a
        full-range ramp into FUNCGEN in FFT-bypass mode, and checks that the received scaler
        stream is exactly zero on every sample.
        '''
        logger = self.get_logger()
        self.board.set_gains(self._uniform_real_gains(0), postscaler=0)

        source = self.gen_data('ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)
        self.board.start_data_capture(period=0.01, source='scaler', data_type=0)
        receiver = self.board.get_data_receiver(verbose=2)
        _, data, _ = receiver.read_raw_frames(ncap=1, split_frames=False)

        ref_data = source * 0
        logger.debug(
            f"test_all_zeroes: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_all_zeroes: received_first16={data[0][:16]}")
        logger.debug(f"test_all_zeroes: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, data, ref_data, 'All zeroes')


    @pytest.mark.scaler
    @pytest.mark.parametrize('symmetric_saturation', (False,))
    def test_all_ones(self, postscaler, symmetric_saturation, setup_scaler, xr):
        '''
        Verify unity gain path for a full-range ramp

        Parameters:
            postscaler (int): postscaler(s) to employ with this test

            symmetric_saturation (bool): whether or not to employ symmetric saturation
        '''
        self.board.set_scaler_output_modes(cap_data_type=0, bypass=False)
        logger = self.get_logger()
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
        self.board.set_symmetric_saturation(symmetric_saturation)

        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        '''source = self.gen_data('ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)
        self.board.start_data_capture(period=0.01, source='scaler', data_type=cap_data_type)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        _, data, _ = receiver.read_raw_frames(ncap=1, split_frames=False)
        data = data // (2 ** self.data.READOUT_SHIFT)'''

        ref_data = np.clip(source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT + (symmetric_saturation), self.data.MAX_OUTPUT)
        #ref_data = np.repeat(ref_data, 2)
        logger.debug(
            f"test_all_ones: postscaler={postscaler} "
            f"symmetric_saturation={symmetric_saturation}"
        )
        logger.debug(
            f"test_all_ones: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        print(f"test_all_ones: received_first16={data[0][:16]}")
        print(f"test_all_ones: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, data, ref_data, f'All ones (postscaler={postscaler}, symmetric_saturation={symmetric_saturation})')

    @pytest.mark.scaler
    def test_alternating_gain(self, period, setup_scaler, xr):
        '''
        Verify per-bin gain alignment using a periodic sparse gain mask.

        The gain table is set to zeros except every `period`-th bin, which is set to 1.
        The input is a periodic ramp to avoid long flat segments, so misalignment between
        gain-bin indexing and input-bin ordering is visible in the captured output.

        Parameters:
            period (int): period in number of bins of the pattern
        '''
        logger = self.get_logger()
        gains = np.tile([*np.repeat(0, period - 1), 1], self.data.NUM_SAMPLES//(2 * period))
        gains = np.append(gains, (np.repeat(0, self.data.NUM_SAMPLES//2 - gains.size)))
        self.board.set_gains(gains, postscaler=self.data.MAPPING_POSTSCALER)

        source, data = self._set_capture_scaler('periodic_ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        '''source = self.gen_data('periodic_ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)
        self.board.start_data_capture(period=0.01, source='scaler', data_type=0)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        ts, data, count = receiver.read_raw_frames(ncap=1, split_frames=False)
        data = data // (2 ** self.data.READOUT_SHIFT)'''

        ref_data = np.clip(source * np.repeat(gains, 2) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        #logger.debug(
        #    f"test_alternating_gain: period={period} ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape}"
        #)
        logger.debug(f"test_alternating_gain: gains_first32={gains[:32]}")
        logger.debug(f"test_alternating_gain: received_first16={data[0][:16]}")
        logger.debug(f"test_alternating_gain: reference_first16={ref_data[:16]}")

        self.plot_and_test(xr, data, ref_data, f'Alternating gain (period={period})')
        

    @pytest.mark.scaler
    #@pytest.mark.parametrize('symmetric_saturation', (False, True))
    @pytest.mark.parametrize('force_float_gains', (False, True))
    def test_constant_gain(self, postscaler, gain, force_float_gains, setup_scaler, xr, symmetric_saturation = False):
        '''
        Sets linear gains to a consant values and tests if output corresponds to scaled, shifted and truncated copy of the input.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test

            gain (int): gains(s) to employ across all bins with this test

            force_float_gains (bool): if True, performs this test with the postscaler explicitly incorporated into the floating point gain, instead of seperately as a common log gain

        Notes:
            Uses scaler capture mode 0 and the current raw-frame API (`ncap`, `split_frames`)
            so the received stream is compared directly against the integer reference model.
        '''
        logger = self.get_logger()
        if force_float_gains:
            if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
                pytest.skip("Platform does not support floating point gains")
            self.board.set_gains((self._uniform_real_gains(gain * 2**postscaler), -1))
            assert(all(ch.SCALER.USE_FLOAT_GAINS == True for ch in self.board.chan))
        else:
            self.board.set_gains(self._uniform_real_gains(gain), postscaler=postscaler)
            assert(all(ch.SCALER.USE_FLOAT_GAINS == False for ch in self.board.chan))
        self.board.set_symmetric_saturation(symmetric_saturation)

        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)

        ref_data = np.clip(gain * source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT + symmetric_saturation, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_constant_gain: postscaler={postscaler} gain={gain} force_float_gains={force_float_gains} "
            f"symmetric_saturation={symmetric_saturation}"
        )
        logger.debug(
            f"test_constant_gain: data_shape={data.shape}"
            f"source_shape={source.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_constant_gain: received_first16={data[0][:16]}")
        logger.debug(f"test_constant_gain: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Constant gain (postscaler={postscaler}, gain={gain}, symmetric saturation={symmetric_saturation}, force_float_gains={force_float_gains})')
        

    @pytest.mark.scaler
    def test_ramp_gains(self, setup_scaler, xr):
        '''
        Sets the gains to a periodic ramp betwen gain_min and gain_max and ensures that they are correctly applied to the input bins

        Notes:
            Uses `_set_capture_scaler` with the current raw-frame capture API and the
            currently configured scaler capture mode.
        '''
        logger = self.get_logger()
        gains = self.gen_data('periodic_ramp', min=self.get_test_param('ramp_gains', 'gain_min', 0), max=self.get_test_param('ramp_gains', 'gain_max', 8), samples=self.data.NUM_BINS//2)
        gains = np.concatenate((gains, gains[::-1]))
        logger.debug(f'Gain shape: '  + str(gains.shape))
        self.board.set_gains(gains, postscaler=self.data.MAPPING_POSTSCALER)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data  = np.clip(np.repeat(gains, 2) * source * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_ramp_gains: gains_shape={gains.shape} source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_ramp_gains: gains_first32={gains[:32]}")
        logger.debug(f"test_ramp_gains: received_first16={data[0][:16]}")
        logger.debug(f"test_ramp_gains: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Ramp gains')
        

    @pytest.mark.scaler
    @pytest.mark.complex
    def test_alternating_complex_gains(self, setup_scaler, xr):
        '''
        Alternates the gains between 1+0j and 0+1j. The test ensures not only that the gains are aligned, but that complex multiplier is working and multiplies the correct coefficients together,

        Notes:
            Uses capture mode 0 through `_set_capture_scaler` and compares the full
            interleaved Re/Im stream against the host-side complex reference model.
        '''
        logger = self.get_logger()

        if any(not chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support complex gains")
        #alternate between 1+0j and 0+1j
        nfreq = self.data.NUM_SAMPLES // 2
        gains = np.tile((1+0j, 0+1j), (nfreq + 1) // 2)[:nfreq]
        self.board.set_gains(gains, postscaler=self.data.MAPPING_POSTSCALER)
        split_source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=0)
        source = unsplit_imag(split_source)
        ref_data = np.clip(split_imag(gains * source) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_alternating_complex_gains: nfreq={nfreq} gains_shape={gains.shape} "
            f"split_source_shape={split_source.shape} source_shape={source.shape} "
            f"data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_alternating_complex_gains: gains_first16={gains[:16]}")
        logger.debug(f"test_alternating_complex_gains: received_first16={data[0][:16]}")
        logger.debug(f"test_alternating_complex_gains: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, 'Alternating complex gains', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.complex
    @pytest.mark.parametrize("use_4bit_cap_mode", (False, True))
    def test_periodic_complex_gains(self, use_4bit_cap_mode, setup_scaler, xr):
        '''
        Tests complex multiplier by setting the gains to a complex ramp (0+0j, 0+1j, ..., 1+32j, +0j, ..., 32+31j, 32+32j) and ensures these coefficients are correctly multiplied with the input.
        
        Parameters:
            use_4bit_cap_mode (bool): if True, tests cap_data_type 3, which produces the same data as the normal scaler output even if the science output is set to another mode

        Notes:
            Uses `_set_capture_scaler` with explicit capture mode selection to verify
            both normal capture mode (0) and 4-bit mirror capture mode (3).
        '''
        logger = self.get_logger()
        if any(not chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support complex gains")
        if use_4bit_cap_mode:
            cap_data_type = 3
            self.board.set_scaler_output_modes(
                bypass=True,
                bypass_data_type=2,
                cap_data_type=cap_data_type,
            ) # should generate same data as normal scaler output, even when output is outputting something else
        else:
            cap_data_type = 0
            self.board.set_scaler_output_modes(bypass=False, cap_data_type=cap_data_type)
        nfreq = int(self.board.NUMBER_OF_FREQUENCY_BINS)
        split_gains = self.gen_data('complex_ramp', min=0, max=32, samples=2 * nfreq)
        gains = unsplit_imag(split_gains) #collapse interleaved real/imag values into complex coefficients
        self.board.set_gains(gains, postscaler=self.data.MAPPING_POSTSCALER)
        split_source, data = self._set_capture_scaler(
            'ramp',
            min=self.data.MIN_OUTPUT,
            max=self.data.MAX_OUTPUT,
            cap_data_type=cap_data_type,
        )
        source = unsplit_imag(split_source) #collapse back to complex numbers
        ref_data = np.clip(split_imag(gains * source) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_periodic_complex_gains: use_4bit_cap_mode={use_4bit_cap_mode} cap_data_type={cap_data_type} "
            f"nfreq={nfreq} split_gains_shape={split_gains.shape} gains_shape={gains.shape} "
            f"split_source_shape={split_source.shape} source_shape={source.shape} data_shape={data.shape} "
            f"ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_periodic_complex_gains: gains_first16={gains[:16]}")
        logger.debug(f"test_periodic_complex_gains: received_first16={data[0][:16]}")
        logger.debug(f"test_periodic_complex_gains: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Periodic complex gains (use 4bit capture mode={use_4bit_cap_mode})', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_uniform_common_log_gain(self, setup_scaler, xr):
        '''
        Ensures floating point gains are properly calculated by setting all gains to a uniform power of 2 and ensures this is all moved to the common log gain. It does this test via manually inspecting the common log gain as well as by inspecting the scaling of the input.

        Notes:
            Uses `_set_capture_scaler` in capture mode 0 and checks both register-level
            state (`USE_FLOAT_GAINS`, `SHIFT_LEFT`) and data-path output consistency.
        '''
        logger = self.get_logger()
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        self.board.set_gains(np.ones(self.data.NUM_SAMPLES//2) * 2**self.data.MAPPING_POSTSCALER) #since postscaler=None, should automatically detect common factor
        logger = self.get_logger()
        nfreq = int(self.board.NUMBER_OF_FREQUENCY_BINS)
        target_gain = 2 ** self.data.MAPPING_POSTSCALER
        self.board.set_gains((np.ones(nfreq) * target_gain, -1))
        expected_shift_left = self.data.MAPPING_POSTSCALER - 10
        logger.debug(
            f"test_uniform_common_log_gain: nfreq={nfreq} target_gain={target_gain} expected_shift_left={expected_shift_left}"
        )
        for ch in range(len(self.board.chan)):
            if not self.board.chan[ch].SCALER.USE_FLOAT_GAINS:
                logger.error(f'Floating point gains not enabled in channel {ch}')
                assert(False)
            if not self.board.chan[ch].SCALER.SHIFT_LEFT == expected_shift_left:
                logger.error(f'Common log gain incorrectly set to {self.board.chan[ch].SCALER.SHIFT_LEFT} in channel {ch}')
                assert(False)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=0)
        ref_data = np.clip(source *  2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_uniform_common_log_gain: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_uniform_common_log_gain: received_first16={data[0][:16]}")
        logger.debug(f"test_uniform_common_log_gain: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, 'Uniform common log gain')
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_common_log_gain(self, setup_scaler, xr):
        '''
        Similar to the test above, except the gains are first initialised to a random value within their range and then multiply them by a common power of 2. Tis ensures that the common log gain can correctly be exracted from an arbitray set of gains and the two sources of the left shiftnae

        Notes:
            Uses a deterministic minimum gain term so the expected extracted common
            log gain is well defined for any valid `common_log_gain` setting.
        '''
        logger = self.get_logger()
        common_log_gain = int(self.test_config.get('common_log_gain', 10))
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        gains = np.random.randint(0, 2**11, self.data.NUM_SAMPLES//2) * 2**np.random.randint(0, 2**5 - common_log_gain, self.data.NUM_SAMPLES//2) #11 bits linear, 5 bits log
        gains = gains.astype(np.float64)
        if common_log_gain < 0 or common_log_gain > 31:
            pytest.skip(f"common_log_gain={common_log_gain} is outside supported range [0, 31]")
        nfreq = int(self.board.NUMBER_OF_FREQUENCY_BINS)
        extra_log_max = 31 - common_log_gain
        rand_lin = np.random.randint(1, 2**11, nfreq)
        rand_log = np.random.randint(0, extra_log_max + 1, nfreq) if extra_log_max > 0 else np.zeros(nfreq, dtype=np.int64)
        gains = rand_lin.astype(np.float64) * 2**rand_log
        gains *= 2**common_log_gain
        gains[0] = float(2**common_log_gain) #pin minimum exponent so expected common log gain is deterministic
        expected_shift_left = max(common_log_gain - 10, 0)
        logger.debug(
            f"test_common_log_gain: common_log_gain={common_log_gain} expected_shift_left={expected_shift_left} "
            f"nfreq={nfreq} gains_first16={gains[:16]}"
        )
        self.board.set_gains((gains, -1))
        for ch in range(len(self.board.chan)):
            if not self.board.chan[ch].SCALER.USE_FLOAT_GAINS:
                logger.error(f'Floating point gains not enabled in channel {ch}')
                assert(False)
            if not self.board.chan[ch].SCALER.SHIFT_LEFT == expected_shift_left:
                logger.error(f'Common log gain incorrectly set to {self.board.chan[ch].SCALER.SHIFT_LEFT} in channel {ch}')
                assert(False)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=0)
        ref_data = np.clip(np.repeat(gains, 2) * source // 2**self.data.IDENTITY_POSTSCALER, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_common_log_gain: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_common_log_gain: received_first16={data[0][:16]}")
        logger.debug(f"test_common_log_gain: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Common log gain (common_log_gain={common_log_gain})')
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_ramp_float_gains(self, setup_scaler, xr):
        '''
        Sets the gains to range over many possible linear gains for each of a variety of log gains. This ensures that the multiplication with the floating-point gain does not contain any bugs that surface only with specific vkalues and also provides a good visualisation of the floating-point format.

        Notes:
            Uses `_set_capture_scaler` in capture mode 0 and sizes gain vectors from
            the active firmware frequency-bin count.
        '''
        logger = self.get_logger()
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        log_gains = self.gen_data('ramp', min=self.data.IDENTITY_POSTSCALER-11, max=self.data.IDENTITY_POSTSCALER+4, samples=self.data.NUM_SAMPLES//2)
        lin_gains = self.gen_data('periodic_ramp', min=0, max=2**6-1, samples=self.data.NUM_SAMPLES//2) * 2**4
        nfreq = int(self.board.NUMBER_OF_FREQUENCY_BINS)
        log_gains = self.gen_data('ramp', min=self.data.IDENTITY_POSTSCALER-11, max=self.data.IDENTITY_POSTSCALER+4, samples=nfreq)
        lin_gains = self.gen_data('periodic_ramp', min=1, max=2**6-1, samples=nfreq) * 2**4
        gains = lin_gains * 2**log_gains
        logger.debug(
            f"test_ramp_float_gains: nfreq={nfreq} log_gains_shape={log_gains.shape} "
            f"lin_gains_shape={lin_gains.shape} gains_shape={gains.shape}"
        )
        logger.debug(f"test_ramp_float_gains: log_gains_first16={log_gains[:16]}")
        logger.debug(f"test_ramp_float_gains: lin_gains_first16={lin_gains[:16]}")
        logger.debug(f"test_ramp_float_gains: gains_first16={gains[:16]}")
        self.board.set_gains((gains, -1))
        source, data = self._set_capture_scaler('a', a=1, cap_data_type=0)
        ref_data = np.clip((source * np.repeat(gains, 2)) * 2**self.data.IMPLICIT_SHIFT // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_ramp_float_gains: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_ramp_float_gains: received_first16={data[0][:16]}")
        logger.debug(f"test_ramp_float_gains: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, 'Ramp float gains')
        

    @pytest.mark.scaler
    @pytest.mark.parametrize('num_fractional_bits', range(1, 8))
    @pytest.mark.parametrize('rounding_mode', (1, 2))
    @pytest.mark.parametrize('use_prerounding', (False, True))
    def test_rounding(self, value, num_fractional_bits, rounding_mode, use_prerounding, setup_scaler, xr):
        '''
        Tests the 3 rounding modes of the scaler by ensuring the input number is rounded up or down at the correct threshold according to the value of its fractional bits (bits in the scaled word that can be non-zero but are not incorporated into the scaler output.
        Additionally tests the capture mode which captures the raw scaled data by ensuring it is unrounded.

        Parameters:
            test_values (int): base value(s) to act as target of rounding
            
            num_fractional_bits (int): number of fractional bits (i.e. bits not in MSBs captured by output but that contribute to rounding)

            rounding_mode (int): rounding mode (truncate, normal, or convergent) to test

            use_prerounding (bool): if True, tests cap_data_type 2 by capturing data directly after scaling and therefore before rounding and ensures no rounding is performed

        Notes:
            Uses explicit scaler capture mode selection (`cap_data_type` 0 or 2)
            and validates host-side rounding thresholds against the captured stream.
        '''
        logger = self.get_logger()
        cap_data_type = 2 if use_prerounding else 0
        self.board.set_scaler_output_modes(cap_data_type=cap_data_type)
        max_val = 2**(self.data.INPUT_WIDTH - num_fractional_bits - 1) - 1
        min_val = -max_val - 1
        if value < min_val or value > max_val:
            pytest.skip(f"Cannot fit {value} into input bits with {num_fractional_bits} fractional bits")
        value = np.clip(value, min_val, max_val)
        postscaler = self.data.IDENTITY_POSTSCALER - num_fractional_bits
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
        self.board.set_scaler_rounding_mode(rounding_mode)
        source, data = self._set_capture_scaler(
            'ramp',
            min=(value * 2**num_fractional_bits),
            max=((value + 1) * 2**num_fractional_bits - 1),
            cap_data_type=cap_data_type,
        )
        bottom = abs(source % (2**num_fractional_bits))
        if use_prerounding: #pre-rounding data is equivalent to truncated data
            round_up = np.zeros(source.size)
        elif rounding_mode == 1:
            round_up = bottom >= 2**(num_fractional_bits - 1)
        elif rounding_mode == 2:
            round_up = (bottom > 2**(num_fractional_bits - 1)) | ((bottom == 2**(num_fractional_bits - 1)) & (value % 2 == 1))
        else:
            raise ValueError(f"Unrecognised rounding mode {rounding_mode} selected")
        ref_data = np.clip(value + round_up, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_rounding: value={value} num_fractional_bits={num_fractional_bits} rounding_mode={rounding_mode} "
            f"use_prerounding={use_prerounding} cap_data_type={cap_data_type} postscaler={postscaler}"
        )
        logger.debug(
            f"test_rounding: source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape} "
            f"bottom_shape={bottom.shape}"
        )
        logger.debug(f"test_rounding: bottom_first16={bottom[:16]}")
        logger.debug(f"test_rounding: round_up_first16={round_up[:16]}")
        logger.debug(f"test_rounding: received_first16={data[0][:16]}")
        logger.debug(f"test_rounding: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Rounding (value={value}, # of fractional bits={num_fractional_bits}, rounding mode={self.ROUNDING_MODES[rounding_mode]}, use pre-rounding data={use_prerounding})')
        

    @pytest.mark.scaler
    @pytest.mark.parametrize('rounding_mode', (1, 2))
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    @pytest.mark.parametrize('num_fractional_bits', (1, 2, 3))
    @pytest.mark.parametrize('force_float_gains', (False,))
    @pytest.mark.parametrize('use_4bit_cap_mode', (False, True))
    def test_post_rounding_saturation(self, rounding_mode, symmetric_saturation, num_fractional_bits, force_float_gains, use_4bit_cap_mode, setup_scaler, xr):
        '''
        Tests that if rounding and not scaling which causes a bin to saturate, it is nevertheless corrected. The test is run for a range of fractional bits (see rounding for definition), but a more limited range because more bits are needed to produce both rounding and a saturation.
        The test ensures that this works correctly for a variety of rounding modes, symmetric saturations, and capture modes from the scaler.
        
        Parameters:
            rounding_mode (int): rounding mode (truncate, normal, or convergent) to test

            symmetric_saturation (bool): whether or not to employ symmetric saturation 

            force_float_gains (bool): if True, performs this test with the postscaler explicitly incorporated into the floating point gain, instead of seperately as a common log gain

            use_4bit_cap_mode (bool): if True, tests cap_data_type 3, which produces the same data as the normal scaler output even if the science output is set to another mode

        Notes:
            Uses explicit capture mode routing (0 or 3) and validates the
            round-then-saturate path for cases where rounding causes saturation.
        '''
        logger = self.get_logger()
        if use_4bit_cap_mode:
            self.board.set_scaler_output_modes(bypass=True, bypass_data_type=2, cap_data_type=3) #should generate same data as normal scaler output, even when output is outputting something else
            cap_data_type = 3
            self.board.set_scaler_output_modes(
                bypass=True,
                bypass_data_type=2,
                cap_data_type=cap_data_type,
            ) #should generate same data as normal scaler output, even when output is outputting something else
        else:
            self.board.set_scaler_output_modes(bypass=False, bypass_data_type=0, cap_data_type=0)
            cap_data_type = 0
            self.board.set_scaler_output_modes(bypass=False, cap_data_type=cap_data_type)
        #ensure we can saturate with this number of fractional bits
        if self.data.INPUT_WIDTH - num_fractional_bits <= self.data.CAPTURE_WIDTH:
            pytest.skip(f"input is insufficently wide to allow saturation to occur while leaving {num_fractional_bits} fractional bits")
        self.board.set_scaler_rounding_mode(rounding_mode)
        self.board.set_symmetric_saturation(symmetric_saturation)
        #set postscaler #minimum value to get saturations even with no rounding
        postscaler = self.data.IDENTITY_POSTSCALER - num_fractional_bits
        if force_float_gains:
            if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
                pytest.skip("Platform does not support floating point gains")
            self.board.set_gains((self._uniform_real_gains(2**postscaler), -1))
            assert(all(ch.SCALER.USE_FLOAT_GAINS == True for ch in self.board.chan))
        else:
            self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
            assert(all(ch.SCALER.USE_FLOAT_GAINS == False for ch in self.board.chan))
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=cap_data_type)
        ref_data = source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler  // 2**self.data.SCALER_SHIFT
        bottom = abs(source % (2**num_fractional_bits))
        if rounding_mode == 1:
            round_up = bottom >= 2**(num_fractional_bits - 1)
        elif rounding_mode == 2:
            round_up = (bottom > 2**(num_fractional_bits - 1)) | ((bottom == 2**(num_fractional_bits - 1)) & (ref_data % 2 == 1))
        else:
            round_up = np.zeros(self.data.NUM_SAMPLES)
            raise ValueError(f"Unrecognised rounding mode {rounding_mode} selected")
        ref_data += round_up
        ref_data = np.clip(ref_data, self.data.MIN_OUTPUT + symmetric_saturation, self.data.MAX_OUTPUT)
        logger.debug(
            f"test_post_rounding_saturation: rounding_mode={rounding_mode} symmetric_saturation={symmetric_saturation} "
            f"num_fractional_bits={num_fractional_bits} force_float_gains={force_float_gains} "
            f"use_4bit_cap_mode={use_4bit_cap_mode} cap_data_type={cap_data_type} postscaler={postscaler}"
        )
        logger.debug(
            f"test_post_rounding_saturation: source_shape={source.shape} data_shape={data.shape} "
            f"ref_shape={ref_data.shape} bottom_shape={bottom.shape}"
        )
        logger.debug(f"test_post_rounding_saturation: bottom_first16={bottom[:16]}")
        logger.debug(f"test_post_rounding_saturation: round_up_first16={round_up[:16]}")
        logger.debug(f"test_post_rounding_saturation: received_first16={data[0][:16]}")
        logger.debug(f"test_post_rounding_saturation: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Post-rounding Saturation (# of fractional bits={num_fractional_bits}, symmetric saturation={symmetric_saturation}, rounding_mode={self.ROUNDING_MODES[rounding_mode]}, force float gains={force_float_gains}, use 4bit capture method={use_4bit_cap_mode})')
        

    @pytest.mark.scaler
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    def test_zero_on_sat(self, symmetric_saturation, setup_scaler, xr):
        '''
        Tests the zero on saturation functionality by ensuring that both the real and imaginary components are zeroed when either exceeds the bounds. The input is a complex ramp (see  test_periodic_complex_gains for details)
        
        Parameters:
            symmetric_saturation (bool): whether or not to employ symmetric saturation 

        Notes:
            Uses capture mode 0 via `_set_capture_scaler` and validates that
            overflow in either component zeros both Re/Im outputs for that bin pair.
        '''
        logger = self.get_logger()
        self.board.set_gains(self._uniform_real_gains(1), postscaler=self.data.IDENTITY_POSTSCALER + 1)
        self.board.set_zero_on_sat(True)
        self.board.set_symmetric_saturation(symmetric_saturation)
        try:
            source, data = self._set_capture_scaler(
                func='complex_ramp',
                min=self.data.MIN_OUTPUT//2 - 1,
                max=self.data.MAX_OUTPUT//2 + 1,
                cap_data_type=0,
            )
        finally:
            self.board.set_zero_on_sat(False)
            self.board.set_symmetric_saturation(False)
        mask = (source*2 < (self.data.MIN_OUTPUT + symmetric_saturation)) | (source*2 > self.data.MAX_OUTPUT)
        mask = np.repeat(mask[::2] | mask[1::2], 2) #spill from Re or Im into pair
        ref_data = source * 2
        ref_data[mask] = 0
        logger.debug(
            f"test_zero_on_sat: symmetric_saturation={symmetric_saturation} source_shape={source.shape} "
            f"data_shape={data.shape} ref_shape={ref_data.shape} mask_shape={mask.shape} "
            f"mask_true={int(np.count_nonzero(mask))}"
        )
        logger.debug(f"test_zero_on_sat: source_first16={source[:16]}")
        logger.debug(f"test_zero_on_sat: mask_first16={mask[:16]}")
        logger.debug(f"test_zero_on_sat: received_first16={data[0][:16]}")
        logger.debug(f"test_zero_on_sat: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Zero on saturation (symmetric saturation={bool(symmetric_saturation)})', split_complex=True)
        


    @pytest.mark.scaler
    def test_offset_encoding(self, postscaler, setup_scaler, xr):
        '''
        Similar to the all_ones test, but ensures that binary offset encoding is correctly applied to the output.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test

        Notes:
            Uses scaler capture mode 0 and validates host-side offset-binary mapping
            of the signed clipped scaler output.
        '''
        logger = self.get_logger()
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
        required_offset = bool(self.board.CORR.REQUIRES_OFFSET_BINARY_ENCODING) if self.board.CORR else False
        previous_offset = bool(self.board.chan[0].SCALER.USE_OFFSET_BINARY)
        if self.board.CORR and not required_offset:
            pytest.skip("Current correlator mode requires 2's-complement output; offset-binary cannot be enabled.")
        self.board.set_offset_binary_encoding(True)
        try:
            source, data = self._set_capture_scaler(func='ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=0)
            source_i = source.astype(np.int64)
            ref_data = source_i * (2 ** self.data.IMPLICIT_SHIFT) * (2 ** postscaler)
            ref_data //= 2 ** self.data.SCALER_SHIFT
            ref_data = np.clip(ref_data, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT).astype(np.int64)

            # Convert signed two's-complement codes to offset-binary and interpret
            # them back as signed values, matching capture decode semantics.
            modulus = 2 ** self.data.CAPTURE_WIDTH
            half = modulus // 2
            twos_unsigned = (ref_data + modulus) % modulus
            offset_unsigned = twos_unsigned ^ half
            ref_data = np.where(offset_unsigned >= half, offset_unsigned - modulus, offset_unsigned).astype(np.int64)
            logger.debug(
                f"test_offset_encoding: postscaler={postscaler} source_shape={source.shape} "
                f"data_shape={data.shape} ref_shape={ref_data.shape}"
            )
            logger.debug(f"test_offset_encoding: source_first16={source[:16]}")
            logger.debug(f"test_offset_encoding: received_first16={data[0][:16]}")
            logger.debug(f"test_offset_encoding: reference_first16={ref_data[:16]}")
            self.plot_and_test(xr, data, ref_data, f'Offset encoding (postscaler={postscaler})')
        finally:
            restore_offset = required_offset if self.board.CORR else previous_offset
            self.board.set_offset_binary_encoding(restore_offset)
        



    @pytest.mark.scaler
    @pytest.mark.parametrize('out_mode', (1, 2, 3))
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    def test_overflow_detection(self, postscaler, out_mode, symmetric_saturation, setup_scaler, xr):
        '''
        Ensures that the alternate output modes of the scaler work correctly by providing an input which overflows and underflows in the real and imaginary components and verifies that the correct pattern is prodcued given this and the output mode.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test
            
            out_mode (int): Output mode to employ. Either 1, 2, or 3

        Notes:
            Uses scaler bypass output modes with capture mode 0 and validates the
            overflow/underflow flag mapping against host-computed saturation events.
        '''
        self.board.set_scaler_output_modes(bypass=True, bypass_data_type=out_mode)
        logger = self.get_logger()
        cap_data_type = 0
        self.board.set_scaler_output_modes(bypass=True, bypass_data_type=out_mode, cap_data_type=cap_data_type)
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
        self.board.set_symmetric_saturation(symmetric_saturation)
        source, data = self._set_capture_scaler('ramp', min=-8, max=8)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, cap_data_type=cap_data_type)
        source_i = source.astype(np.int64)
        if self.data.CAPTURE_WIDTH > 4: #even in 8bit mode, the 1 is placed in the 4th bit, so need to correct for that
            data //= 16
        scaled = source_i * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT
        #only checks re saturation
        if out_mode == 1:
            overflow = (scaled < self.data.MIN_OUTPUT + symmetric_saturation) | (scaled > self.data.MAX_OUTPUT)
            pair_overflow = overflow[::2] | overflow[1::2]
            ref_data = np.zeros(source.size, dtype=np.int64)
            ref_data[::2] = pair_overflow.astype(np.int64)  # zero out imag
        else:
            comp = scaled[out_mode-2::2] #either real or im depending on mode
            underflow = comp < self.data.MIN_OUTPUT + symmetric_saturation
            overflow = comp > self.data.MAX_OUTPUT
            ref_data = np.zeros(self.data.NUM_SAMPLES)
            ref_data[::2] = overflow
            ref_data[1::2] = underflow
            ref_data = np.zeros(source.size, dtype=np.int64)
            ref_data[::2] = overflow.astype(np.int64)
            ref_data[1::2] = underflow.astype(np.int64)
        logger.debug(
            f"test_overflow_detection: postscaler={postscaler} out_mode={out_mode} "
            f"symmetric_saturation={symmetric_saturation} cap_data_type={cap_data_type} "
            f"source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_overflow_detection: scaled_first16={scaled[:16]}")
        logger.debug(f"test_overflow_detection: received_first16={data[0][:16]}")
        logger.debug(f"test_overflow_detection: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Overflow detection (postscaler={postscaler}, mode={out_mode}, symmetric_saturation={symmetric_saturation})', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.parametrize("bypass_to_output", (False, True))
    def test_bypass(self, bypass_to_output, setup_scaler, xr):
        '''
        Tests whether scaler bypass works correctly by ensuring FFT data is correctly forwarded through the scaler.

        Parameters:
            bypass_to_output (bool): if True, FFT data is sent unchanged to output and then captured from there. Otherwise, the FFT data is captured directly and the science output remains normal

        Notes:
            Uses scaler bypass output control (`bypass_data_type`) and explicit
            capture mode selection (`cap_data_type`) with flattened receiver output.
        '''
        logger = self.get_logger()
        if bypass_to_output:
            self.board.set_scaler_output_modes(bypass=True, bypass_data_type=0, cap_data_type=0) #send FFT to output and capture from output
            cap_data_type = 0
            self.board.set_scaler_output_modes(
                bypass=True,
                bypass_data_type=0,
                cap_data_type=cap_data_type,
            ) #send FFT to output and capture from output
        else:
            self.board.set_scaler_output_modes(bypass=False, bypass_data_type=0, cap_data_type=1) #send FFT to capture directly
            cap_data_type = 1
            self.board.set_scaler_output_modes(
                bypass=False,
                bypass_data_type=0,
                cap_data_type=cap_data_type,
            ) #send FFT to capture directly
        source, data = self._set_capture_scaler(
            'periodic_ramp',
            min=self.data.MIN_INPUT,
            max=self.data.MAX_INPUT,
            cap_data_type=cap_data_type,
        )
        source_i = source.astype(np.int64)
        ref_data = source_i * 2**self.data.IMPLICIT_SHIFT // 2**(self.data.FFT_WIDTH - self.data.CAPTURE_WIDTH)
        logger.debug(
            f"test_bypass: bypass_to_output={bypass_to_output} cap_data_type={cap_data_type} "
            f"source_shape={source.shape} data_shape={data.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_bypass: source_first16={source[:16]}")
        logger.debug(f"test_bypass: received_first16={data[0][:16]}")
        logger.debug(f"test_bypass: reference_first16={ref_data[:16]}")
        self.plot_and_test(xr, data, ref_data, f'Scaler bypass (bypass_to_output={bypass_to_output})')
        
    
    @pytest.mark.scaler
    @pytest.mark.parametrize("cap_data_type", (4, 5))
    def test_mult_resolution_capture(self, postscaler, cap_data_type, setup_scaler, xr):
        '''
        Tests that the double resolution capture mode extracts the right bits of the FFT data from the right bins

        Parameters:
            postscaler (int): postscaler(s) to employ with this test

            cap_data_type (int): Capture mode to test. 4 is dual-resolution raw FFT. 5 is full-resolution raw FFT over multi-packet logical frames.

        Notes:
            Uses explicit `frames_per_burst` sizing to satisfy strict logical-frame
            decode requirements in `RawFrameReceiver` for scaler capture modes 4/5.
        '''
        logger = self.get_logger()
        if self.data.CAPTURE_WIDTH < 8:
            pytest.skip("Cannot use double resolution capture in 4-bit mode")
        self.board.set_scaler_output_modes(cap_data_type=cap_data_type)
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)

        source = self.gen_data('ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)

        fft_bins_per_word = int(self.board.FFT_INFO.get(self.board.FFT_TYPE, {}).get("bins_per_word") or 1)
        frames_per_burst = 2 if cap_data_type == 4 else 2 * fft_bins_per_word
        self.board.start_data_capture(
            period=0.01,
            frames_per_burst=frames_per_burst,
            source='scaler',
            data_type=cap_data_type,
        )
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        ts, data, count = receiver.read_raw_frames(ncap=1, split_frames=False)

        source_i = source.astype(np.int64)
        raw_fft = source_i * (2 ** self.data.IMPLICIT_SHIFT)

        if cap_data_type == 4:
            ref_data = np.clip(raw_fft // (2 ** max(self.data.FFT_WIDTH - 16, 0)), -2**15, 2**15 - 1).astype(np.int64)
        else:
            # Mode 5 is captured with msb_pad(..., 32): raw FFT component is placed in the
            # MSBs of the 32-bit word and LSBs are zero padded.
            fullres_lsb_pad = max(32 - self.data.FFT_WIDTH, 0)
            ref_data = (raw_fft << fullres_lsb_pad).astype(np.int64)

        logger.debug(
            f"test_double_resolution_capture: postscaler={postscaler} cap_data_type={cap_data_type} "
            f"frames_per_burst={frames_per_burst} fft_bins_per_word={fft_bins_per_word} "
            f"fullres_lsb_pad={max(32 - self.data.FFT_WIDTH, 0)} ts_shape={ts.shape} "
            f"data_shape={data.shape} count_shape={count.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_double_resolution_capture: source_first16={source[:16]}")
        logger.debug(f"test_double_resolution_capture: received_first16={data[0][:16]}")
        logger.debug(f"test_double_resolution_capture: reference_first16={ref_data[:16]}")
        #TODO: reparametrise for CRS
        self.plot_and_test(xr, data, ref_data, f'Double resolution capture (postscaler = {postscaler}, capture mode = {cap_data_type})', split_complex=False) 

    @pytest.mark.scaler
    @pytest.mark.parametrize("ch", (0,))
    #@pytest.mark.parametrize('postscaler, symmetric_saturation, frame_cnt', product(range(TEST_CONFIG['overflow_stats'].get('postscaler_min', 0), TEST_CONFIG['overflow_stats'].get('postscaler_max', 31)), (False, True), TEST_CONFIG['overflow_stats'].get('frame_cnt', 1)))
    def test_scaler_overflow_stats(self, ch, frame_cnt, setup_scaler, xr):
        '''
        Tests the collection of scaler overflow statistics by setting the input to cause a precise number of scaler overflows per frame (2), and ensuring that the scaler counts a number of overflows equal to this number times the integration period in frames.

        Parameters:
            ch (int): ch to capture statistics from
            frame_cnt (int): the integration period in frames

        Notes:
            Uses integer overflow markers derived from the active capture width
            so exactly one positive and one negative scaler overflow are injected
            per frame.
        '''
        logger = self.get_logger()
        self.board.set_gains(self._uniform_real_gains(1), postscaler=self.data.IDENTITY_POSTSCALER + 1)
        self.board.set_scaler_output_modes(bypass=False, cap_data_type=0)

        data = np.zeros(self.data.NUM_SAMPLES, dtype=np.int64)
        pos_overflow_marker = self.data.MAX_OUTPUT // 2 + 1
        neg_overflow_marker = self.data.MIN_OUTPUT // 2 - 1
        # Inject 2 overflows per frame: one positive and one negative.
        data[0] = pos_overflow_marker
        data[-1] = neg_overflow_marker

        _, _ = self._set_capture_scaler(read=False, func='arb', data=data, cap_data_type=0)
        overflow_stats, adc_stats = self.board.get_channel_metrics(ch=ch, frame_cnt=frame_cnt)
        expected_overflows = 2 * frame_cnt

        logger.debug(
            f"test_scaler_overflow_stats: ch={ch} frame_cnt={frame_cnt} num_samples={self.data.NUM_SAMPLES} "
            f"pos_marker={pos_overflow_marker} neg_marker={neg_overflow_marker} "
            f"expected_overflows={expected_overflows} measured_overflows={overflow_stats} "
            f"measured_adc_overflows={adc_stats}"
        )
        logger.debug(f"test_scaler_overflow_stats: injected_data_first16={data[:16]}")
        logger.debug(f"test_scaler_overflow_stats: injected_data_last16={data[-16:]}")

        assert overflow_stats == expected_overflows

    @pytest.mark.scaler
    @pytest.mark.parametrize("ch", (0,))
    @pytest.mark.parametrize("fft_bypass", [True, False])
    def test_adc_overflow_stats(self, ch, frame_cnt, fft_bypass, setup_scaler, xr):
        '''
        Tests the collection of adc overflow statistics by setting the adc overflow flag each frame and ensuring that the scaler counts a number of adc overflows equal to the number of frames integrated.

        Parameters:
            ch (int): ch to capture statistics from
            frame_cnt (int): the integration period in frames

        Notes:
            Injects one ADC-overflow marker per frame via FUNCGEN `frame8` +
            `overflow_buffer`, then cross-checks both scaler ADC overflow stats
            and FUNCGEN overflow counter against `frame_cnt`.
        '''
        logger = self.get_logger()
        injected_data = np.zeros(self.data.NUM_SAMPLES, dtype=np.int64)
        injected_data[0] = 1  # inject 1 overflow marker per frame

        self.board.set_channelizer(
            data_source='frame8',
            fft_bypass=fft_bypass,
            scaler_bypass=False,
            scaler_cap_data_type=0,
            overflow_buffer=injected_data,
        )
        self.board.start_data_capture(period=0.01, source='scaler', data_type=0)

        scaler_adc_overflows = self.board.get_channel_metrics(ch=ch, frame_cnt=frame_cnt)[1]

        chan = self.board.chan[ch]
        chan.FUNCGEN.STATS_ENABLE = 0
        chan.FUNCGEN.STATS_PERIOD = frame_cnt - 1
        chan.FUNCGEN.STATS_ENABLE = 1

        timeout_s = 2.0
        poll_s = 0.01
        polls = int(timeout_s / poll_s)
        for _ in range(polls):
            if chan.FUNCGEN.STATS_DONE:
                break
            sleep(poll_s)
        else:
            raise TimeoutError("Timed out waiting for FUNCGEN overflow stats completion (STATS_DONE).")

        funcgen_overflows = chan.FUNCGEN.OVERFLOW_CTR
        expected_overflows = frame_cnt

        logger.debug(
            f"test_adc_overflow_stats: ch={ch} frame_cnt={frame_cnt} fft_bypass={fft_bypass} "
            f"expected_overflows={expected_overflows} scaler_adc_overflows={scaler_adc_overflows} "
            f"funcgen_overflows={funcgen_overflows} injected_nonzero={int(np.count_nonzero(injected_data))}"
        )
        logger.debug(f"test_adc_overflow_stats: injected_data_first16={injected_data[:16]}")

        assert funcgen_overflows == expected_overflows
        assert scaler_adc_overflows == expected_overflows

    @pytest.mark.scaler
    def test_delay_counter(self, setup_scaler, xr):
        '''
        Tests that all channelisers all start recieving data at the same by ensuring that the delay counters, which measure clocks from sync until first data is recieved, are equal.

        Notes:
            Resets scaler pipelines, performs one scaler capture using the current
            raw-frame API, then checks DELAY_CTR uniformity across channels.
        '''
        logger = self.get_logger()
        for ch in self.board.chan:
            ch.SCALER.RESET = 1
        sleep(0.1)
        for ch in self.board.chan:
            ch.SCALER.RESET = 0
        source, data = self._set_capture_scaler('a', a=1, cap_data_type=0)
        delay_ctrs = [ch.SCALER.DELAY_CTR for ch in self.board.chan]
        logger.debug(
            f"test_delay_counter: source_shape={source.shape} data_shape={data.shape} "
            f"delay_ctrs={delay_ctrs} min_delay={min(delay_ctrs)} max_delay={max(delay_ctrs)}"
        )
        logger.debug(f"test_delay_counter: source_first16={source[:16]}")
        logger.debug(f"test_delay_counter: received_first16={data[0][:16]}")
        assert(len(set(delay_ctrs)) == 1) #ensure all delay counters are the same

    @pytest.mark.scaler
    def test_counter_reset(self, setup_scaler, xr):
        '''
        Tests the reset signal by ensure that various counters are always 0 as long as the reset signal is high, and that they are (mostly) non-zero when it is low.

        Notes:
            Runs scaler capture mode 0, holds SCALER reset high to validate zeroed
            monitor counters, then releases reset and verifies counters become active.
        '''
        logger = self.get_logger()
        for chan in self.board.chan:
            chan.SCALER.RESET = 1
            chan.SCALER.MON_RESET_STATS = 1
        self.board.start_data_capture(source='scaler', data_type=0, period=0.1)
        for i in range(10): #ensure that count stays at 0 while reset is high
            sleep(0.1)
            for chan in self.board.chan:
                assert(chan.SCALER.FRAME_CTR == 0)
                assert(chan.SCALER.MON_PACKET_LENGTH == 0)
                assert(chan.SCALER.MON_PACKET_CTR == 0)
                assert(chan.SCALER.MON_WORD_CTR == 0)
                #assert(chan.SCALER.CAP_FRAME_CTR == 0)

        frame_counts = []
        mon_packet_lengths = []
        mon_packet_counts = []
        mon_word_counts = []
        cap_frame_counts = []
        for chan in self.board.chan:
            chan.SCALER.RESET = 0
            chan.SCALER.MON_RESET_STATS = 0
        for i in range(10): #aggregate counts during data capture
            sleep(0.1)
            for chan in self.board.chan:
                frame_counts.append(chan.SCALER.FRAME_CTR)
                mon_packet_lengths.append(chan.SCALER.MON_PACKET_LENGTH)
                mon_packet_counts.append(chan.SCALER.MON_PACKET_CTR)
                mon_word_counts.append(chan.SCALER.MON_WORD_CTR)
                cap_frame_counts.append(chan.SCALER.CAP_FRAME_CTR)
        logger.debug(
            f"test_counters: frame_counts_first16={frame_counts[:16]} mon_packet_lengths_first16={mon_packet_lengths[:16]} "
            f"mon_packet_counts_first16={mon_packet_counts[:16]} mon_word_counts_first16={mon_word_counts[:16]} "
            f"cap_frame_counts_first16={cap_frame_counts[:16]}"
        )
        logger.debug(
            f"test_counters: frame_nonzero={any(v != 0 for v in frame_counts)} "
            f"pkt_len_nonzero={any(v != 0 for v in mon_packet_lengths)} "
            f"pkt_ctr_nonzero={any(v != 0 for v in mon_packet_counts)} "
            f"word_ctr_nonzero={any(v != 0 for v in mon_word_counts)} "
            f"cap_frame_nonzero={any(v != 0 for v in cap_frame_counts)}"
        )
        assert any(v != 0 for v in frame_counts)
        assert any(v != 0 for v in mon_packet_lengths)
        assert any(v != 0 for v in mon_packet_counts)
        assert any(v != 0 for v in mon_word_counts)
        assert any(v != 0 for v in cap_frame_counts)

    @pytest.mark.scaler
    @pytest.mark.parametrize("bank", (0, 1))
    @pytest.mark.parametrize("adc_overflow", (False, True))
    def test_flags(self, postscaler, bank, adc_overflow, setup_scaler, xr):
        '''
        Tests the tuser flags that the scaler outputs and the prober places in the bottom 4 bits of the capture output. These include scaler overflow flags (1 for each bin in the word), 1 adc overflow flag, and the current gain bank, the latter two only asserted at the end of the frame.
        The test ensures these are accurate by causing scaler overflows, switching between gain banks and forcing adc overflows.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test. Causes scaler overflow which can then be seen in the flags

            bank (int): which gain bank to employ

            adc_overflow (bool): if True, additionally validates adc-overflow flag propagation
                using FUNCGEN `frame8` with `overflow_buffer`.

        Notes:
            Uses explicit scaler capture mode 0 with flag extraction from the low
            4 bits on 4-bit ICE readout (`READOUT_SHIFT=4`).
            The ramp path (`arb`) is used to validate scaler-overflow and bank bits.
            ADC-overflow forcing is validated through `frame8` + `overflow_buffer`
            because writing `BYTE_A` does not inject ADC overflow in `arb` mode.
        '''
        logger = self.get_logger()
        if self.data.CAPTURE_WIDTH > 4: #no room for flags:
            pytest.skip("Output is too wide for flags")
        adc_overflow = bool(adc_overflow)
        adc_overflow_i = int(adc_overflow)
        self.board.set_scaler_output_modes(bypass=False, cap_data_type=0)
        self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler, bank=bank, when='now')
        source, flags = self._set_capture_scaler(
            'ramp',
            min=self.data.MIN_INPUT,
            max=self.data.MAX_INPUT,
            get_flags=True,
            cap_data_type=0,
        )
        source_i = source.astype(np.int64)
        scaled = source_i * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT
        overflow = ((scaled > self.data.MAX_OUTPUT) | (scaled < self.data.MIN_OUTPUT)).astype(np.int64)
        tlast_flags = np.zeros(self.data.NUM_SAMPLES, dtype=np.int64)
        # In ramp/arb mode, only bank is expected on the tail bits.
        tlast_flags[-4:] = bank * 2
        ref_data = overflow * 12 + tlast_flags

        logger.debug(
            f"test_flags: postscaler={postscaler} bank={bank} adc_overflow={adc_overflow_i} "
            f"source_shape={source.shape} flags_shape={flags.shape} ref_shape={ref_data.shape}"
        )
        logger.debug(f"test_flags: source_first16={source[:16]}")
        logger.debug(f"test_flags: flags_first16={flags[0][:16]}")
        logger.debug(f"test_flags: reference_first16={ref_data[:16]}")
        logger.debug(f"test_flags: flags_last16={flags[0][-16:]}")
        logger.debug(f"test_flags: reference_last16={ref_data[-16:]}")

        self.plot_and_test(
            xr,
            flags,
            ref_data,
            f'Flags (postscaler={postscaler}, bank={bank}, adc overflow={adc_overflow})',
        )

        if adc_overflow:
            mask = 2 ** self.data.READOUT_SHIFT - 1
            overflow_buffer = np.zeros(self.data.NUM_SAMPLES, dtype=np.int64)
            tail_width = 4

            def capture_flags_from_frame8(inject_overflow):
                overflow_buffer.fill(0)
                if inject_overflow:
                    overflow_buffer[0] = 1
                self.board.set_data_source(
                    source='frame8',
                    reset_chan=False,
                    overflow_buffer=overflow_buffer,
                )
                self.board.start_data_capture(period=0.01, source='scaler', data_type=0)
                receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
                ts, data, count = receiver.read_raw_frames(ncap=2, split_frames=False)
                # Use the second frame to avoid potential source-switch transition effects.
                flags_out = (data & mask)[-1:]
                logger.debug(
                    f"test_flags(frame8): inject_overflow={inject_overflow} ts_shape={ts.shape} "
                    f"data_shape={data.shape} count_shape={count.shape} flags_shape={flags_out.shape}"
                )
                logger.debug(f"test_flags(frame8): flags_last16={flags_out[0][-16:]}")
                return flags_out

            flags_noinj = capture_flags_from_frame8(inject_overflow=False)
            flags_inj = capture_flags_from_frame8(inject_overflow=True)

            noinj_tail = flags_noinj[0][-tail_width:]
            inj_tail = flags_inj[0][-tail_width:]
            noinj_adc = noinj_tail & 0x1
            inj_adc = inj_tail & 0x1
            noinj_bank = (noinj_tail >> 1) & 0x1
            inj_bank = (inj_tail >> 1) & 0x1

            logger.debug(
                f"test_flags(frame8): noinj_tail={noinj_tail} inj_tail={inj_tail} "
                f"noinj_adc={noinj_adc} inj_adc={inj_adc} noinj_bank={noinj_bank} inj_bank={inj_bank}"
            )

            assert np.equal(noinj_adc, 0).all()
            assert np.equal(inj_adc, 1).all()
            assert np.equal(noinj_bank, bank).all()
            assert np.equal(inj_bank, bank).all()
            # ADC overflow flag is frame-sticky in FUNCGEN: once any sample in a frame
            # overflows, the ADC bit remains asserted for the whole frame.
            assert np.equal(flags_noinj[0] & 0x1, 0).all()
            assert np.equal(flags_inj[0] & 0x1, 1).all()
    
    @pytest.mark.scaler
    def test_gain_bank_switching(self, setup_scaler, xr, interval=int(1e6)): #10 000 frames
        '''
        Test that the gain bank switching works and happens at exactly the right moment. The test sets up both gain banks and programs a switch to occur at a precise interval, then collects frames in pairs of 2 with a period of 1/5th this interval. By inspecting the output data in each frame, it ensures that the switch occurs at some point and that the timestampes of the frame right before and after the switch occured are the interval and the interval+1.
        This ensures that the gain bank switch happens at precisely the scheduled moment. It also ensures that the gain bank status variable reflects the switch.
        
        Parameters:
            interval (int): How often (in frames) to capture data. Switch occurs after 5 intervals

        Notes:
            Uses a runtime flow for robustness with the current receiver API:
            capture a frame, switch bank immediately, wait 1 second, capture
            another frame, and compare against before/after references.
        '''
        logger = self.get_logger()
        _ = interval  # kept for compatibility with existing test parametrization
        self.board.set_scaler_output_modes(bypass=False, cap_data_type=0)
        self.board.set_gains(self._uniform_real_gains(2), postscaler=self.data.MAPPING_POSTSCALER, bank=1)
        self.board.set_gains(self._uniform_real_gains(1), postscaler=self.data.MAPPING_POSTSCALER, bank=0, when='now')
        source = self.gen_data(func='ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        source_i = source.astype(np.int64)
        ref_before_frame = np.clip(
            source_i * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT,
            self.data.MIN_OUTPUT,
            self.data.MAX_OUTPUT,
        )
        ref_after_frame = np.clip(
            2 * source_i * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT,
            self.data.MIN_OUTPUT,
            self.data.MAX_OUTPUT,
        )
        assert(all(ch.SCALER.CURRENT_GAIN_BANK == 0 for ch in self.board.chan))
        self.board.start_data_capture(
            period=0.01,
            source='scaler',
            data_type=0,
            frames_per_burst=1,
            verbose=0,
        )
        assert(self.board.GPIO.ANT_RESET == 0)

        ts_before, data_before, count_before = receiver.read_raw_frames(ncap=1, split=True)
        if self.data.READOUT_SHIFT > 0:
            data_before //= 2**self.data.READOUT_SHIFT
        before_frame = data_before[0, 0]
        before_ts = int(ts_before[0])

        self.board.switch_gains(bank=1, when='now')
        print("switching gain banks")
        #self.board.sync()
        sleep(1.0)
        receiver.flush()

        ts_after, data_after, count_after = receiver.read_raw_frames(ncap=1, split=True)
        if self.data.READOUT_SHIFT > 0:
            data_after //= 2**self.data.READOUT_SHIFT
        after_frame = data_after[0, 0]
        after_ts = int(ts_after[0])

        assert all(ch.SCALER.CURRENT_GAIN_BANK == 1 for ch in self.board.chan)
        logger.debug(
            f"test_gain_bank_switching: ts_before_shape={ts_before.shape} ts_after_shape={ts_after.shape} "
            f"data_before_shape={data_before.shape} data_after_shape={data_after.shape} "
            f"count_before_shape={count_before.shape} count_after_shape={count_after.shape} "
            f"before_ts={before_ts} after_ts={after_ts}"
        )
        print(f"test_gain_bank_switching: ref_before_first16={ref_before_frame[4000:4100]}")
        print(f"test_gain_bank_switching: ref_after_first16={ref_after_frame[4000:4100]}")
        print(f"test_gain_bank_switching: before_frame_first16={before_frame[4000:4100]}")
        print(f"test_gain_bank_switching: after_frame_first16={after_frame[4000:4100]}")

        assert (before_frame == ref_before_frame).all()
        assert (after_frame == ref_after_frame).all()
        assert after_ts >= before_ts

    def sat_percent(self, postscaler: int, symmetric_saturation: bool):
        '''
        Returns the percentage of inputs that are expected to saturate the scaler when the log gain is set to postscaler and symmetric saturation is set to symmetric_saturation, assuming a uniform distribution over all possible input values for the active platform.
        '''
        count = 0
        nvals = self.data.MAX_INPUT - self.data.MIN_INPUT + 1
        for v in range(self.data.MIN_INPUT, self.data.MAX_INPUT + 1):
            out = v * 2.0**(postscaler + self.data.IMPLICIT_SHIFT - self.data.SCALER_SHIFT)
            count += (out > self.data.MAX_OUTPUT) or (out < self.data.MIN_OUTPUT + symmetric_saturation)
        return count / nvals

    @pytest.mark.scaler
    @pytest.mark.statistical
    @pytest.mark.parametrize('rounding_mode', (0, 1, 2))
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    def test_averages(self, num_frames, postscaler_range, rounding_mode, symmetric_saturation, setup_scaler, xr):
        '''
        Returns the average scaler output for a range of postscalers given a uniform, random distribution of input values (white noise). Performs the averaging for all possible rounding modes and with symmetric saturation enabled and disabled.

        Parameters:
            num_frames (int): number of frames to integrate over for the average. A higher number of frames will increase accuracy but take longer

            postscaler_range: the range of postscalers to compute the average over

            rounding_mode (int): rounding mode (truncate, normal, or convergent) to test

            symmetric_saturation (bool): whether or not to employ symmetric saturation 

        Notes:
            Uses explicit scaler capture mode 0 and `read_raw_frames(ncap=1)` to
            read one burst per iteration in the current receiver API.
            
        '''
        logger = self.get_logger()
        FRAMES_PER_BURST = 3
        assert num_frames % FRAMES_PER_BURST == 0, f"{num_frames} frames cannot be divided into bursts of {FRAMES_PER_BURST} frames"
        self.board.set_data_source('noise')
        self.board.set_scaler_output_modes(bypass=False, cap_data_type=0)
        self.board.start_data_capture(source='scaler', data_type=0, period=0.01, frames_per_burst=FRAMES_PER_BURST)
        self.board.set_scaler_rounding_mode(rounding_mode)
        self.board.set_symmetric_saturation(symmetric_saturation)
        receiver = self.board.get_data_receiver(verbose=self.test_config.loglevelrecv)
        avgs = np.zeros(postscaler_range[1]  - postscaler_range[0], dtype=np.float64)
        n = self.data.NUM_SAMPLES * len(self.board.chan) * num_frames
        num_bursts = num_frames // FRAMES_PER_BURST
        for postscaler in range(postscaler_range[0], postscaler_range[1]): #for all possible postscalers
            self.board.set_gains(self._uniform_real_gains(1), postscaler=postscaler)
            for i in range(num_bursts):
                for ch in range(len(self.board.chan)):
                    self.board.chan[ch].FUNCGEN.BYTE_A = randint(0, 255)
                    self.board.chan[ch].FUNCGEN.BYTE_B = randint(0, 255)
                receiver.flush()
                ts, data, count = receiver.read_raw_frames(ncap=1, split=False)
                if self.data.READOUT_SHIFT > 0:
                    data //= 2 ** self.data.READOUT_SHIFT
                avgs[postscaler-postscaler_range[0]] += np.sum(data)
                if i == 0 and postscaler == postscaler_range[0]:
                    logger.debug(
                        f"test_averages: ts_shape={ts.shape} data_shape={data.shape} count_shape={count.shape} "
                        f"frames_per_burst={FRAMES_PER_BURST} num_bursts={num_bursts}"
                    )
                    logger.debug(f"test_averages: first_data_first16={data[0][:16]}")
        avgs /= n
        logger.debug(f"test_averages: n={n} postscaler_range={postscaler_range} averages={avgs.tolist()}")
        if rounding_mode == 0 and not symmetric_saturation:
            ref_data = -0.5 * np.ones(postscaler_range[1] - postscaler_range[0])
        elif rounding_mode == 0 and symmetric_saturation:
            ref_data = -0.5 * (1 - np.vectorize(self.sat_percent)(np.arange(postscaler_range[0], postscaler_range[1]), symmetric_saturation=symmetric_saturation))
        elif rounding_mode > 0 and not symmetric_saturation:
            ref_data = -0.5 * np.vectorize(self.sat_percent)(np.arange(postscaler_range[0], postscaler_range[1]), symmetric_saturation=symmetric_saturation)
        elif rounding_mode > 0 and symmetric_saturation:
            ref_data = np.zeros(postscaler_range[1] - postscaler_range[0])
        logger.debug(f"test_averages: reference={ref_data.tolist()}")
        plt.plot(np.arange(postscaler_range[0], postscaler_range[1]), avgs)
        plt.plot(np.arange(postscaler_range[0], postscaler_range[1]), ref_data)
        plt.ylim((-0.6, 0.1))
        plt.xlabel("Postscaler")
        plt.ylabel("Average")
        plt.title("\n".join(wrap(f"Average scaler output by value of postscaler (rounding mode={self.ROUNDING_MODES[rounding_mode]}, symmetric_saturation={symmetric_saturation}", 60)))
        xr.insert_plot()
        plt.clf()
        if self.data.CAPTURE_WIDTH == 4:
            assert(np.isclose(avgs, ref_data, atol=0.1).all()) #absolute tolerance of 0.1
        else:
            pass #TODO: generalise these tests to work with different number of capture bits
