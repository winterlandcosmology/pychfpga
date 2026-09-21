from pychfpga.fpga_array import FPGAArray
from pychfpga.common import run_async
from wtl.pytest_xreport import xr, get_xr_from
import logging
from .utils import plot
import matplotlib.pyplot as plt
import numpy as np

class TestModule:
    def board_conn(self, xr):
        if 'board' in xr.data:  # check if board is cached to avoid unnecessary reconnection
            self.ca = xr.data.ca
            return xr.data.board
        ca = FPGAArray(**xr.config.connection_config)
        run_async(ca.init())
        xr.data.ca = ca
        xr.data.board = ca.ib[0]
        self.ca = ca
        return xr.data.board

    def get_data_receiver(self, **kwargs):
        """Return the array raw-data receiver (one-board FPGAArray)."""
        return run_async(self.ca.get_data_receiver_async(**kwargs))

    def get_corr_receiver(self, **kwargs):
        """Return the array correlator receiver (one-board FPGAArray)."""
        kwargs.pop('buf_size', None)  # UDP default is already 8192; not an array-receiver arg
        return run_async(self.ca.get_corr_receiver_async(**kwargs))

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
    
    def pytest_generate_tests(self, metafunc):
        """ This pytest hook is called before each test to allow dynamic generation of test parameters.

        It parameterises each argument corresponding to a parameter in the config file with an arbitrary python expression from that file
        """
        xr = get_xr_from(metafunc)
        test_config = xr.config.test_config
        board = self.board_conn(xr)
        widths = self.get_constants(board)
        xr.data.update(widths) #store all these parameters in xr
        test_name = metafunc.function.__name__[5:] #get rid of test_ prefix
        explicit_fixtures = self._explicit_parametrize_fixtures(metafunc)
        for fixture in metafunc.fixturenames:
            if fixture in explicit_fixtures:
                continue
            #find global default, if there is
            global_param = 'global_' + fixture
            found_param = False
            if fixture in test_config.keys():
                value = test_config[fixture]
                found_param = True
            if test_name in test_config.keys(): #not elif, so global will be overriden if applicable
                test_params = test_config[test_name]
                if fixture in test_params:
                    value = test_params[fixture]
                    found_param = True
            if found_param:
                if isinstance(value, str):
                    value = eval(value, widths.as_dict()) #pass widths as globals so that user can acess them
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