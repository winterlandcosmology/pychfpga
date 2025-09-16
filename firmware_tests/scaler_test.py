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
        {'INPUT_WIDTH': 14, 'CAPTURE_WIDTH': 16, 'FFT_WIDTH': 18, 'SCALED_WIDTH': 35, 'READOUT_SHIFT': 0, 'BINS_PER_SAMPLE': 4},
    'CHORD':
        {'INPUT_WIDTH': 14, 'CAPTURE_WIDTH': 16, 'FFT_WIDTH': 32, 'SCALED_WIDTH': 48, 'READOUT_SHIFT': 0, 'BINS_PER_SAMPLE': 4}
}

def unsplit_imag(array):
    '''
    Turns an array of N real numbers into an array of N/2 complex numbers, interpreting every other element as the the imaginary component.
    '''
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
            print(f'{platform} not recognised as a possible platform')
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
        self.data.NUM_CHANNELIZERS = len(self.board.chan) #currently unused
        conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
        self.logger = logging.getLogger(conn_logger_name) #store logger in xr for future use
        self.logger.setLevel(xr.config.test_config.loglevelconn)
        self.plot_dir = Path('plots/')
        #check that fifo overflow doesn't go high either before or after test
        for ch in self.board.chan:
            assert(ch.SCALER.CHAN_FIFO_OVERFLOW == 0)
        yield #test occurs here
        for ch in self.board.chan:
            assert(ch.SCALER.CHAN_FIFO_OVERFLOW == 0)


    @pytest.fixture(scope='function')
    def setup_scaler(self, xr):
        self.logger.debug(f"Attempting to setup scaler on instance {self}")
        self.board.set_channelizer(
            fft_bypass=True,
            scaler_bypass=False,
            scaler_out_data_type=0,
            scaler_cap_data_type=0,
            offset_binary_encoding=False,
            scaler_eight_bit=self.data.CAPTURE_WIDTH > 4 and self.data.PLATFORM == 'ICE', #TODO: generalise these for other platforms
            prober_user_flags=self.data.CAPTURE_WIDTH <= 4 and self.data.PLATFORM == 'ICE',
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

    def get_test_param(self, test_name, param_name, default):
        '''
        Get a parameter for a certain test, permitting both the test and the parameter to be omitted
        '''
        params = self.test_config.get(test_name, {}) #we have access to test_config since setup has been run 
        return params.get(param_name, default)

    def _set_capture_scaler(self, func, double=False, read=True, get_flags=False, **func_kwargs):
        '''
        Sets the function generator to output a specific set of values and then captures data from the scaler.
        '''
        source = self.gen_data(func, samples=self.data.NUM_SAMPLES, **func_kwargs) #setup has already been run, so self.data has been set
        self.board.set_data_source(source='arb', data=source)
        self.board.start_data_capture(period=0.01, source='scaler')
        if not read:
            return source, source
        receiver = self.board.get_data_receiver()
        if self.data.PLATFORM == 'ICE':
            if double:
                _, data, _ = receiver.read_raw_frames_double_resolution(verbose=False)
            else:
                _, data, _ = receiver.read_raw_frames(verbose=False)
            if get_flags:
                return source, data & (2**self.data.READOUT_SHIFT-1)
            return source, data // (2**self.data.READOUT_SHIFT)
        #TODO: add capture mode for ucap
        else:
            if double:
                pytest.skip("Double resolution not supported on UCAP yet")
            else:
                 _, data, _ = receiver.read_raw_frames(format='16', split = True, verbose=False)
            if get_flags:
                raise Exception("Flag feature not implemented on UCAP")
            return source, data // (2**self.data.READOUT_SHIFT)

    def plot_and_test(self, xr, data, ref_data, title, **kwargs):
        test_name = title[:title.find('(')] #up to position of first bracket
        parameters  = title[title.find('('):]
        xr.header(test_name)
        assert(np.equal(data[0], data).all(), "Not all channels are equal")
        print(f'Parameters: {parameters}')
        residuals = data[0] - ref_data
        print("Creating plots")
        plot((data[0], ref_data), title=test_name, **kwargs)
        xr.insert_plot()
        plot((residuals,), title = f"Residuals of {test_name.lower()}")
        xr.insert_plot()
        print(f"Sum of residuals: {np.sum(residuals)}")
        if not self.test_config.only_plot:
            assert(np.equal(data, ref_data).all())
    

    @pytest.mark.scaler
    def test_all_zeroes(self, setup_scaler, xr):
        '''
        Set all gains to 0 and tests if output is uniformly 0 when provided with a ramp as an input.
        '''
        self.board.set_gains(0)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = source * 0
        self.plot_and_test(xr, data, ref_data, 'All zeroes')


    @pytest.mark.scaler
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    @pytest.mark.parametrize('use_prerounding', (False, True))
    def test_all_ones(self, postscaler, symmetric_saturation, use_prerounding, setup_scaler, xr):
        '''
        Sets all gains to 1 and tests if the output is merely a shifted and truncated copy of the input, which is a ramp.

        Parameters:
            
            postscaler (int): postscaler(s) to employ with this test
            
            symmetric_saturation (bool): whether or not to employ symmetric saturation 

            use_prerounding (bool): if True, tests cap_data_type 2 (output is extracted directly after scaling) thereby bypassing symmetric saturation
        '''
        if use_prerounding:
            self.board.set_scaler_output_modes(cap_data_type=2) #capture data right after multiplication
        else:
            self.board.set_scaler_output_modes(cap_data_type=0)
        self.board.set_scaler_rounding_mode(0)
        self.board.set_gains(1, postscaler=postscaler)
        xr.data.board.set_symmetric_saturation(symmetric_saturation)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT + (symmetric_saturation and not use_prerounding), self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'All ones (postscaler={postscaler}, symmetric_saturation={symmetric_saturation}, use pre-rounding={use_prerounding})')

    @pytest.mark.scaler
    def test_alternating_gain(self, period, setup_scaler, xr):
        '''
        Tests that the gains are being applied to the correct bin by setting the gains uniformly to 0, save for each periodth gain, which is set to 1. This ensures that the gains are properly aligned with the bins.
        The input is a periodic ramp, so it covers the range multiple times per frame instead of just once. This ensures that adjacent inputs are never equal,
        
        Parameters:
            period (int): period in number of bins of the pattern
        '''
        gains = np.tile([*np.repeat(0, period - 1), 1], self.data.NUM_SAMPLES//(2 * period))
        gains = np.append(gains, (np.repeat(0, self.data.NUM_SAMPLES//2 - gains.size)))
        xr.data.board.set_gains(gains, self.data.MAPPING_POSTSCALER)
        source, data = self._set_capture_scaler('periodic_ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(source * np.repeat(gains, 2) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
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
        '''
        if force_float_gains:
            if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
                pytest.skip("Platform does not support floating point gains")
            self.board.set_gains(gain * 2**postscaler)
            assert(all(ch.SCALER.USE_FLOAT_GAINS == True for ch in self.board.chan))
        else:
            self.board.set_gains(gain, postscaler=postscaler)
            assert(all(ch.SCALER.USE_FLOAT_GAINS == False for ch in self.board.chan))
        self.board.set_symmetric_saturation(symmetric_saturation)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(gain * source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT + symmetric_saturation, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'Constant gain (postscaler={postscaler}, gain={gain}, symmetric saturation={symmetric_saturation}, force_float_gains={force_float_gains})')
        

    @pytest.mark.scaler
    def test_ramp_gains(self, setup_scaler, xr):
        '''
        Sets the gains to a periodic ramp betwen gain_min and gain_max and ensures that they are correctly applied to the input bins
        '''
        gains = self.gen_data('periodic_ramp', min=self.get_test_param('ramp_gains', 'gain_min', 0), max=self.get_test_param('ramp_gains', 'gain_max', 8), samples=512)
        gains = np.concatenate((gains, gains[::-1]))
        self.board.set_gains(gains, self.data.MAPPING_POSTSCALER)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data  = np.clip(np.repeat(gains, 2) * source * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'Ramp gains')
        

    @pytest.mark.scaler
    @pytest.mark.complex
    def test_alternating_complex_gains(self, setup_scaler, xr):
        '''
        Alternates the gains between 1+0j and 0+1j. The test ensures not only that the gains are aligned, but that complex multiplier is working and multiplies the correct coefficients together,
        '''

        if any(not chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support complex gains")
        #alternate between 1+0j and 0+1j
        gains = np.tile((1+0j, 0+1j), 1024//2)
        self.board.set_gains(gains, self.data.MAPPING_POSTSCALER)
        split_source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        source = unsplit_imag(split_source)
        ref_data = np.clip(split_imag(gains * source) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, 'Alternating complex gains', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.complex
    @pytest.mark.parametrize("use_4bit_cap_mode", (False, True))
    def test_periodic_complex_gains(self, use_4bit_cap_mode, setup_scaler, xr):
        '''
        Tests complex multiplier by setting the gains to a complex ramp (0+0j, 0+1j, ..., 1+32j, +0j, ..., 32+31j, 32+32j) and ensures these coefficients are correctly multiplied with the input.
        
        Parameters:
            use_4bit_cap_mode (bool): if True, tests cap_data_type 3, which produces the same data as the normal scaler output even if the science output is set to another mode
        '''
        if any(not chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support complex gains")
        if use_4bit_cap_mode:
            self.board.set_scaler_output_modes(bypass=True, out_data_type=2, cap_data_type=3) #should generate same data as normal scaler output, even when output is outputting something else
        else:
            self.board.set_scaler_output_modes(bypass=False, out_data_type=0, cap_data_type=0)
        split_gains = self.gen_data('complex_ramp', min=0, max=32, samples=2048)
        gains = unsplit_imag(split_gains) #collapse 2048 real numbers into 1024 complex ones to be sent to FPGA
        self.board.set_gains(gains, self.data.MAPPING_POSTSCALER)
        split_source, data = self._set_capture_scaler('ramp', min=self.data.MIN_OUTPUT, max=self.data.MAX_OUTPUT)
        source = unsplit_imag(split_source) #collapse back to complex numbers
        ref_data = np.clip(split_imag(gains * source) * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'Periodic complex gains (use 4bit capture mode={use_4bit_cap_mode})', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_uniform_common_log_gain(self, setup_scaler, xr):
        '''
        Ensures floating point gains are properly calculated by setting all gains to a uniform power of 2 and ensures this is all moved to the common log gain. It does this test via manually inspecting the common log gain as well as by inspecting the scaling of the input.
        '''
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        self.board.set_gains(np.ones(1024) * 2**self.data.MAPPING_POSTSCALER) #since postscaler=None, should automatically detect common factor
        logger = self.get_logger()
        for ch in range(len(self.board.chan)):
            if not self.board.chan[ch].SCALER.USE_FLOAT_GAINS:
                logger.error(f'Floating point gains not enabled in channel {ch}')
                assert(False)
            if not self.board.chan[ch].SCALER.SHIFT_LEFT == self.data.MAPPING_POSTSCALER - 10:
                logger.error(f'Common log gain incorrectly set to {self.board.chan[ch].SCALER.SHIFT_LEFT} in channel {ch}')
                assert(False)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(source *  2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, 'Uniform common log gain')
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_common_log_gain(self, setup_scaler, xr):
        '''
        Similar to the test above, except the gains are first initialised to a random value within their range and then multiply them by a common power of 2. Tis ensures that the common log gain can correctly be exracted from an arbitray set of gains and the two sources of the left shiftnae
        '''
        common_log_gain = self.test_config.get('common_log_gain', 10)
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        gains = np.random.randint(0, 2**11, 1024) * 2**np.random.randint(0, 2**5 - common_log_gain, 1024) #11 bits linear, 5 bits log
        gains = gains.astype(np.float64)
        gains *= 2**common_log_gain
        gains[0] = 1 #ensure there is no common log gain by chance
        self.board.set_gains(gains)
        logger = self.get_logger()
        for ch in range(len(self.board.chan)):
            if not self.board.chan[ch].SCALER.USE_FLOAT_GAINS:
                logger.error(f'Floating point gains not enabled in channel {ch}')
                assert(False)
            if not self.board.chan[ch].SCALER.SHIFT_LEFT == common_log_gain - 10:
                logger.error(f'Common log gain incorrectly set to {self.board.chan[ch].SCALER.SHIFT_LEFT} in channel {ch}')
                assert(False)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(np.repeat(gains, 2) * source // 2**self.data.IDENTITY_POSTSCALER, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, 'Periodic complex gains (use 4bit capture mode={use_4bit_cap_mode})')
        

    @pytest.mark.scaler
    @pytest.mark.floating
    def test_ramp_float_gains(self, setup_scaler, xr):
        '''
        Sets the gains to range over many possible linear gains for each of a variety of log gains. This ensures that the multiplication with the floating-point gain does not contain any bugs that surface only with specific vkalues and also provides a good visualisation of the floating-point format.
        '''
        if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
            pytest.skip("Platform does not support floating point gains")
        log_gains = self.gen_data('ramp', min=self.data.IDENTITY_POSTSCALER-11, max=self.data.IDENTITY_POSTSCALER+4, samples=1024)
        lin_gains = self.gen_data('periodic_ramp', min=0, max=2**6-1, samples=1024) * 2**4
        gains = lin_gains * 2**log_gains
        self.board.set_gains(gains)
        source, data = self._set_capture_scaler('a', a=1)
        ref_data = np.clip((source * np.repeat(gains, 2)) * 2**self.data.IMPLICIT_SHIFT // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
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
        '''
        if use_prerounding:
            self.board.set_scaler_output_modes(cap_data_type=2)
        else:
            self.board.set_scaler_output_modes(cap_data_type=0)
        max_val = 2**(self.data.INPUT_WIDTH - num_fractional_bits - 1) - 1
        min_val = -max_val - 1
        if value < min_val or value > max_val:
            pytest.skip(f"Cannot fit {value} into input bits with {num_fractional_bits} fractional bits")
        value = np.clip(value, min_val, max_val)
        self.board.set_gains((1, self.data.IDENTITY_POSTSCALER-num_fractional_bits))
        self.board.set_scaler_rounding_mode(rounding_mode)
        source, data = self._set_capture_scaler('ramp', min=(value * 2**num_fractional_bits), max=((value + 1) * 2**num_fractional_bits - 1))
        bottom = abs(source % (2**num_fractional_bits))
        if use_prerounding: #pre-rounding data is equivalent to truncated data
            round_up = np.zeros(source.size)
        elif rounding_mode == 1:
            round_up = bottom >= 2**(num_fractional_bits - 1)
        elif rounding_mode == 2:
            round_up = (bottom > 2**(num_fractional_bits - 1)) | ((bottom == 2**(num_fractional_bits - 1)) & (value % 2 == 1))
        else:
            self.get_logger().error(f"Unrecognised rounding mode {rounding_mode} selected")
        ref_data = np.clip(value + round_up, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'Rounding (value={value}, # of fractional bits={num_fractional_bits}, rounding mode={self.ROUNDING_MODES[rounding_mode]}, use pre-rounding data={use_prerounding}]')
        

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
        '''
        if use_4bit_cap_mode:
            self.board.set_scaler_output_modes(bypass=True, out_data_type=2, cap_data_type=3) #should generate same data as normal scaler output, even when output is outputting something else
        else:
            self.board.set_scaler_output_modes(bypass=False, out_data_type=0, cap_data_type=0)
        #ensure we can saturate with this number of fractional bits
        if self.data.INPUT_WIDTH - num_fractional_bits <= self.data.CAPTURE_WIDTH:
            pytest.skip(f"input is insufficently wide to allow saturation to occur while leaving {num_fractional_bits} fractional bits")
        self.board.set_scaler_rounding_mode(rounding_mode)
        self.board.set_symmetric_saturation(symmetric_saturation)
        #set postscaler #minimum value to get saturations even with no rounding
        if force_float_gains:
            if any(chan.SCALER.USE_COMPLEX_GAINS for chan in self.board.chan):
                pytest.skip("Platform does not support floating point gains")
            self.board.set_gains(2**(self.data.IDENTITY_POSTSCALER-num_fractional_bits))
            assert(all(ch.SCALER.USE_FLOAT_GAINS == True for ch in self.board.chan))
        else:
            self.board.set_gains(1, postscaler=self.data.IDENTITY_POSTSCALER-num_fractional_bits)
            assert(all(ch.SCALER.USE_FLOAT_GAINS == False for ch in self.board.chan))
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = source * 2**self.data.IMPLICIT_SHIFT * 2**(self.data.IDENTITY_POSTSCALER-num_fractional_bits)  // 2**self.data.SCALER_SHIFT
        bottom = abs(source % (2**num_fractional_bits))
        if rounding_mode == 1:
            round_up = bottom >= 2**(num_fractional_bits - 1)
        elif rounding_mode == 2:
            round_up = (bottom > 2**(num_fractional_bits - 1)) | ((bottom == 2**(num_fractional_bits - 1)) & (ref_data % 2 == 1))
        else:
            round_up = np.zeros(2048)
        ref_data += round_up
        ref_data = np.clip(ref_data, self.data.MIN_OUTPUT + symmetric_saturation, self.data.MAX_OUTPUT)
        self.plot_and_test(xr, data, ref_data, f'Post-rounding Saturation (# of fractional bits={num_fractional_bits}, symmetric saturation={symmetric_saturation}, rounding_mode={self.ROUNDING_MODES[rounding_mode]}, force float gains={force_float_gains}, use 4bit capture method={use_4bit_cap_mode})')
        

    @pytest.mark.scaler
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    def test_zero_on_sat(self, symmetric_saturation, setup_scaler, xr):
        '''
        Tests the zero on saturation functionality by ensuring that both the real and imaginary components are zeroed when either exceeds the bounds. The input is a complex ramp (see  test_periodic_complex_gains for details)
        
        Parameters:
            symmetric_saturation (bool): whether or not to employ symmetric saturation 
        '''
        self.board.set_gains(1, self.data.IDENTITY_POSTSCALER + 1)
        self.board.set_zero_on_sat(True)
        self.board.set_symmetric_saturation(symmetric_saturation)
        source, data = self._set_capture_scaler(func='complex_ramp', min=self.data.MIN_OUTPUT//2 - 1, max=self.data.MAX_OUTPUT//2 + 1)
        self.board.set_zero_on_sat(False)
        self.board.set_symmetric_saturation(False)
        mask = (source*2 < (self.data.MIN_OUTPUT + symmetric_saturation)) | (source*2 > self.data.MAX_OUTPUT)
        mask = np.repeat(mask[::2] | mask[1::2], 2) #spill from Re or Im into pair
        ref_data = source * 2
        ref_data[mask] = 0
        self.plot_and_test(xr, data, ref_data, f'Zero on saturation (symmetric saturation={bool(symmetric_saturation)})', split_complex=True)
        


    @pytest.mark.scaler
    def test_offset_encoding(self, postscaler, setup_scaler, xr):
        '''
        Similar to the all_ones test, but ensures that binary offset encoding is correctly applied to the output.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test
        '''
        self.board.set_gains(1, postscaler)
        self.board.set_offset_binary_encoding(True)
        source, data = self._set_capture_scaler(func='ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = np.clip(source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        sign = (ref_data >= 0) * 2 - 1
        ref_data -= sign * (self.data.MAX_OUTPUT + 1)
        self.plot_and_test(xr, data, ref_data, f'Offset encoding (postscaler={postscaler})')
        



    @pytest.mark.scaler
    @pytest.mark.parametrize('out_mode', (1, 2, 3))
    @pytest.mark.parametrize('symmetric_saturation', (False, True))
    def test_overflow_detection(self, postscaler, out_mode, symmetric_saturation, setup_scaler, xr):
        '''
        Ensures that the alternate output modes of the scaler work correctly by providing an input which overflows and underflows in the real and imaginary components and verifies that the correct pattern is prodcued given this and the output mode.
        
        Parameters:
            postscaler (int): postscaler(s) to employ with this test
            
            out_mode (int): Output mode to employ. Either 1, 2, or 3
        '''
        self.board.set_scaler_output_modes(bypass=True, out_data_type=out_mode)
        self.board.set_gains(1, postscaler)
        self.board.set_symmetric_saturation(symmetric_saturation)
        source, data = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        if self.data.CAPTURE_WIDTH > 4: #even in 8bit mode, the 1 is placed in the 4th bit, so need to correct for that
            data //= 16
        #only checks re saturation
        if out_mode == 1:
            out = source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT
            ref_data = (out < self.data.MIN_OUTPUT + symmetric_saturation) | (out > self.data.MAX_OUTPUT)
            ref_data[::2] = ref_data[1::2] #spread from imag to re
            ref_data[1::2] = 0  #zero out imag
        else:
            comp = (source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT)[out_mode-2::2] #either real or im depending on mode
            underflow = comp < self.data.MIN_OUTPUT + symmetric_saturation
            overflow = comp > self.data.MAX_OUTPUT
            ref_data = np.zeros(2048)
            ref_data[::2] = overflow
            ref_data[1::2] = underflow
        self.plot_and_test(xr, data, ref_data, f'Overflow detection (postscaler={postscaler}, mode={out_mode}, symmetric_saturation={symmetric_saturation})', split_complex=True)
        

    @pytest.mark.scaler
    @pytest.mark.parametrize("bypass_to_output", (False, True))
    def test_bypass(self, bypass_to_output, setup_scaler, xr):
        '''
        Tests whether scaler bypass works correctly by ensuring FFT data is correctly forwarded through the scaler.

        Parameters:
            bypass_to_output (bool): if True, FFT data is sent unchanged to output and then captured from there. Otherwise, the FFT data is captured directly and the science output remains normal
        '''
        if bypass_to_output:
            self.board.set_scaler_output_modes(bypass=True, out_data_type=0, cap_data_type=0) #send FFT to output and capture from output
        else:
            self.board.set_scaler_output_modes(bypass=False, out_data_type=0, cap_data_type=1) #send FFT to capture directly
        source, data = self._set_capture_scaler('periodic_ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        ref_data = source * 2**self.data.IMPLICIT_SHIFT // 2**(self.data.FFT_WIDTH - self.data.CAPTURE_WIDTH)
        self.plot_and_test(xr, data, ref_data, f'Scaler bypass (bypass_to_output={bypass_to_output})')
        
    
    @pytest.mark.scaler
    @pytest.mark.parametrize("cap_data_type", (6, 7))
    def test_double_resolution_capture(self, postscaler, cap_data_type, setup_scaler, xr):
        '''
        Tests that the double resolution capture mode extracts the right bits of the FFT data from the right bins

        Parameters:
            postscaler (int): postscaler(s) to employ with this test

            cap_data_type (int): If 6, captures even bins with double resolution. If 7, odd bins are captured instead
        '''
        if self.data.CAPTURE_WIDTH < 8:
            pytest.skip("Cannot use double resolution capture in 4-bit mode")
        self.board.set_scaler_output_modes(cap_data_type=cap_data_type) #send scaled even or odd bins
        self.board.set_gains(1, postscaler)
        source, data = self._set_capture_scaler('ramp', double=True, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        #combine adjacent bytes into 16 bit number
        ref_data = np.clip(source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**(self.data.SCALED_WIDTH - 16), -2**15, 2**15-1) #get top 16 bits
        ref_data = ref_data[(cap_data_type % 2)::2] #get odd or even bins, depending on cap_data_type
        #TODO: reparametrise for CRS
        self.plot_and_test(xr, data, ref_data, f'Double resolution capture (postscaler = {postscaler}, capture mode = {cap_data_type})', split_complex=False) 

    @pytest.mark.scaler
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ch", range(16))
    #@pytest.mark.parametrize('postscaler, symmetric_saturation, frame_cnt', product(range(TEST_CONFIG['overflow_stats'].get('postscaler_min', 0), TEST_CONFIG['overflow_stats'].get('postscaler_max', 31)), (False, True), TEST_CONFIG['overflow_stats'].get('frame_cnt', 1)))
    def test_scaler_overflow_stats(self, ch, frame_cnt, setup_scaler, xr):
        '''
        Tests the collection of scaler overflow statistics by setting the input to cause a precise number of scaler overflows per frame (2), and ensuring that the scaler counts a number of overflows equal to this number times the integration period in frames.

        Parameters:
            ch (int): ch to capture statistics from
            frame_cnt (int): the integration period in frames
        '''
        self.board.set_gains(1, self.data.IDENTITY_POSTSCALER+1)
        data = np.zeros(2048)
        #set 2 overflows per frame
        data[0] = self.data.MAX_OUTPUT/2 + 1
        data[-1] = self.data.MIN_OUTPUT/2 - 1
        _, _ = self._set_capture_scaler(read=False, func='arb', data=data)
        overflow_stats = self.board.get_channel_metrics(ch=ch, frame_cnt=frame_cnt)[0]
        assert(overflow_stats == 2*frame_cnt)

    @pytest.mark.scaler
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ch", range(16))
    @pytest.mark.parametrize("fft_bypass", [True, False])
    def test_adc_overflow_stats(self, ch, frame_cnt, fft_bypass, setup_scaler, xr):
        '''
        Tests the collection of adc overflow statistics by setting the adc overflow flag each frame and ensuring that the scaler counts a number of adc overflows equal to the number of frames integrated.

        f Parameters:
            ch (int): ch to capture statistics from
            frame_cnt (int): the integration period in frames
        '''
        injected_data = np.zeros(self.data.NUM_SAMPLES)
        injected_data[0] = 1 #inject 1 overflow per frame
        self.board.set_channelizer(data_source='frame8', fft_bypass=fft_bypass, scaler_bypass=0, overflow_buffer=injected_data)
        self.board.start_data_capture(period=0.01, source='scaler')
        overflow_stats = self.board.get_channel_metrics(ch=ch, frame_cnt=frame_cnt)[1]
        self.board.chan[0].FUNCGEN.STATS_PERIOD = frame_cnt - 1
        sleep(1)
        assert(self.board.chan[0].FUNCGEN.OVERFLOW_CTR == frame_cnt)
        assert(overflow_stats == frame_cnt)

    @pytest.mark.scaler
    def test_delay_counter(self, setup_scaler, xr):
        '''
        Tests that all channelisers all start recieving data at the same by ensuring that the delay counters, which measure clocks from sync until first data is recieved, are equal.
        '''
        for ch in self.board.chan:
            ch.SCALER.RESET = 1
        sleep(0.1)
        for ch in self.board.chan:
            ch.SCALER.RESET = 0
        _, _ = self._set_capture_scaler('a', a=1)
        delay_ctrs = [ch.SCALER.DELAY_CTR for ch in self.board.chan]
        logger = self.get_logger()
        logger.debug(delay_ctrs)
        assert(len(set(delay_ctrs)) == 1) #ensure all delay counters are the same

    @pytest.mark.scaler
    def test_counters(self, setup_scaler, xr):
        '''
        Tests the reset signal by ensure that various counters are always 0 as long as the reset signal is high, and that they are (mostly) non-zero when it is low.
        '''
        for chan in self.board.chan:
            chan.SCALER.RESET = 1
            chan.SCALER.MON_RESET_STATS = 1
        self.board.start_data_capture(source='scaler', period=0.1)
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
            chan.MON_RESET_STATS = 0
        for i in range(10): #aggregate counts during data capture
            sleep(0.1)
            for chan in self.board.chan:
                frame_counts.append(chan.SCALER.FRAME_CTR)
                mon_packet_lengths.append(chan.SCALER.MON_PACKET_LENGTH)
                mon_packet_counts.append(chan.SCALER.MON_PACKET_CTR)
                mon_word_counts.append(chan.SCALER.MON_WORD_CTR)
                cap_frame_counts.append(chan.SCALER.CAP_FRAME_CTR)
        #the only (plausible) way they could all be equal is that they are all 0 and did not start counting properly after reset went low
        assert(len(set(frame_counts)) > 0)
        assert(len(set(mon_packet_lengths)) > 0)
        assert(len(set(mon_word_counts)) > 0)
        assert(len(set(mon_word_counts)) > 0)
        assert(len(set(cap_frame_counts)) > 0)

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

            adc_overflow (bool): if True, forces adc overflow flag from funcgen hhigh
        '''
        if self.data.CAPTURE_WIDTH > 4: #no room for flags:
            pytest.skip("Output is too wide for flags")
        for chan in self.board.chan:
            chan.FUNCGEN.BYTE_A = adc_overflow 
        self.board.set_gains(1, postscaler=postscaler, bank=bank, when='now')
        source, flags = self._set_capture_scaler('ramp', min=self.data.MIN_INPUT, max=self.data.MAX_INPUT, get_flags=True)
        ref_data = source * 2**self.data.IMPLICIT_SHIFT * 2**postscaler // 2**self.data.SCALER_SHIFT
        overflow = (ref_data > self.data.MAX_OUTPUT) | (ref_data < self.data.MIN_OUTPUT)
        tlast_flags = np.zeros(2048)
        tlast_flags[-4:] = bank * 2 + adc_overflow
        ref_data = overflow * 12 + tlast_flags
        for chan in self.board.chan:
            chan.FUNCGEN.BYTE_A = 0
        self.plot_and_test(xr, flags, ref_data, f'Flags (postscaler={postscaler}, bank={bank}, adc overflow={adc_overflow}')
    
    @pytest.mark.scaler
    def test_gain_bank_switching(self, setup_scaler, xr, interval=int(1e6)): #10 000 frames
        '''
        Test that the gain bank switching works and happens at exactly the right moment. The test sets up both gain banks and programs a switch to occur at a precise interval, then collects frames in pairs of 2 with a period of 1/5th this interval. By inspecting the output data in each frame, it ensures that the switch occurs at some point and that the timestampes of the frame right before and after the switch occured are the interval and the interval+1.
        This ensures that the gain bank switch happens at precisely the scheduled moment. It also ensures that the gain bank status variable reflects the switch.
        
        Parameters:
            interval (int): How often (in frames) to capture data. Switch occurs after 5 intervals
        '''
        self.board.set_gains(2, self.data.MAPPING_POSTSCALER, bank=1)
        self.board.set_gains(1, self.data.MAPPING_POSTSCALER, bank=0, when='now')
        source = self.gen_data(func='ramp', samples=self.data.NUM_SAMPLES, min=self.data.MIN_INPUT, max=self.data.MAX_INPUT)
        self.board.set_data_source(source='arb', data=source)
        self.board.switch_gains(bank=1, when=5*interval)
        receiver = self.board.get_data_receiver()
        ref_before_frame = np.clip(source * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        ref_after_frame = np.clip(2 * source * 2**self.data.IMPLICIT_SHIFT * 2**self.data.MAPPING_POSTSCALER // 2**self.data.SCALER_SHIFT, self.data.MIN_OUTPUT, self.data.MAX_OUTPUT)
        assert(all(ch.SCALER.CURRENT_GAIN_BANK == 0 for ch in self.board.chan))
        self.board.start_data_capture(burst_period_in_frames=interval, source='scaler', frames_per_burst=2, verbose=0)
        assert(self.board.GPIO.ANT_RESET == 0)
        ts, data, _ = receiver.read_raw_frames(number_of_frames=10) #5 pairs of 2 frames
        data //= 2**self.data.READOUT_SHIFT
        self.get_logger().debug(data.shape)
        for i in range(1, data.shape[1]):
            if (data[:, i] != data[:, i-1]).any():
                before_frame = data[:, i-1]
                after_frame = data[:, i]
                before_ts = ts[i-1]
                after_ts = ts[i]
                break
        else:
            logger = self.get_logger()
            self.get_logger().error("No gain bank switch occured")
            assert(False)
        assert(all(ch.SCALER.CURRENT_GAIN_BANK == 1 for ch in self.board.chan))
        logger  = self.get_logger()
        logger.debug(f'Switch occured after {before_ts} and before {after_ts}')
        assert((before_frame == ref_before_frame).all() and (after_frame == ref_after_frame).all())
        assert(before_ts == interval*5 and after_ts == interval*5+1)

    def sat_percent(self, postscaler: int, symmetric_saturation: bool):
        '''
        Returns the percentage of inputs that are expected to saturate the scaler when the log gain is set to postscaler and symmetric saturation is set to symmetric_saturation, assuming a uniform distribution over all possible 8 bit inputs.
        '''
        count = 0
        for v in range(-128, 128):
            out = v * 2.0**(postscaler + self.data.IMPLICIT_SHIFT - self.data.SCALER_SHIFT)
            count += (out >= self.data.MAX_OUTPUT) or (out <= self.data.MIN_OUTPUT + symmetric_saturation)
        return count / 256

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
            
        '''
        FRAMES_PER_BURST = 3
        assert num_frames % FRAMES_PER_BURST == 0, f"{num_frames} frames cannot be divided into bursts of {FRAMES_PER_BURST} frames"
        self.board.set_data_source('noise')
        self.board.start_data_capture(source='scaler', period=0.01, frames_per_burst=FRAMES_PER_BURST)
        self.board.set_scaler_rounding_mode(rounding_mode)
        self.board.set_symmetric_saturation(symmetric_saturation)
        receiver = self.board.get_data_receiver()
        avgs = np.zeros(postscaler_range[1]  - postscaler_range[0])
        n = 2048 * 16 * num_frames
        for postscaler in range(postscaler_range[0], postscaler_range[1]): #for all possible postscalers
            self.board.set_gains(1, postscaler)
            for i in range(num_frames//FRAMES_PER_BURST):
                for ch in range(len(self.board.chan)):
                    self.board.chan[ch].FUNCGEN.BYTE_A = randint(0, 255)
                    self.board.chan[ch].FUNCGEN.BYTE_B = randint(0, 255)
                _, data, _ = receiver.read_raw_frames(number_of_frames=FRAMES_PER_BURST)
                data //= 2 ** self.data.READOUT_SHIFT
                avgs[postscaler-postscaler_range[0]] += np.sum(data)
        avgs /= n
        self.logger.debug(f'Average: {[s for s in avgs]}')
        if rounding_mode == 0 and not symmetric_saturation:
            ref_data = -0.5 * np.ones(postscaler_range[1] - postscaler_range[0])
        elif rounding_mode == 0 and symmetric_saturation:
            ref_data = -0.5 * (1 - np.vectorize(self.sat_percent)(np.arange(postscaler_range[0], postscaler_range[1]), symmetric_saturation=symmetric_saturation))
        elif rounding_mode > 0 and not symmetric_saturation:
            ref_data = -0.5 * np.vectorize(self.sat_percent)(np.arange(postscaler_range[0], postscaler_range[1]), symmetric_saturation=symmetric_saturation)
        elif rounding_mode > 0 and symmetric_saturation:
            ref_data = np.zeros(postscaler_range[1] - postscaler_range[0])
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