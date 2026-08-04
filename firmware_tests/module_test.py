from pychfpga.fpga_array import FPGAArray
from wtl.pytest_xreport import xr, get_xr_from
import logging
from .utils import plot
import matplotlib.pyplot as plt
import numpy as np
class TestModule:
    def board_conn(self, xr):
        if  'board' in xr.data: #check if board is cached to avoid unnecessary reconnection
            return xr.data.board
        ca = FPGAArray(**xr.config.connection_config)
        xr.data.board = ca.ib[0]
        return xr.data.board

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