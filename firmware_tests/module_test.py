from pychfpga.fpga_array import FPGAArray
from pychfpga.common import run_async
from wtl.pytest_xreport import xr, get_xr_from

class TestModule:
    def board_conn(self, xr):
        if 'board' in xr.data: #check if board is cached to avoid unnecessary reconnection
            self.ca = xr.data.ca
            return xr.data.board
        ca = FPGAArray(**self.connection_config)
        xr.data.ca = ca
        xr.data.board = ca.ib[0]
        self.ca = ca
        return xr.data.board

    def get_data_receiver(self, **kwargs):
        """Return the array raw-data receiver (one-board FPGAArray)."""
        return run_async(self.ca.get_data_receiver_async(**kwargs))
    
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