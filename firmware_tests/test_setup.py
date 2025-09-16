from pychfpga.fpga_array import FPGAArray
import logging
import pytest
from wtl.config import load_yaml_config
from pathlib import Path
import os

root = os.path.split(__file__)[0]
config_pathname = os.path.join(root,'config.yaml')
print(f'root of {__file__} is {root}. config file is {config_pathname}')
CONN_CONFIG = load_yaml_config("connection_config", config_pathname)
TEST_CONFIG = load_yaml_config("test_config", config_pathname)
print(f'***test config is {TEST_CONFIG}')


PLOT_DIR = Path("test_results")


if TEST_CONFIG['always_plot'] or TEST_CONFIG['plot_on_failure']:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)

#Maps platform name to list of parameters for it

@pytest.fixture(scope=TEST_CONFIG['conn_scope'])
def delete_plots(request):
    logger = logging.getLogger(request.cls.__name__)
    if not os.path.isdir(PLOT_DIR):
        logger.warning(f"{PLOT_DIR} does not exist")
        return
    if TEST_CONFIG['delete_plots_before_run']:
        logger.info('Deleting previous plots')
        for file in PLOT_DIR.rglob('*.png'):
            os.remove(file)
    for dirpath, dirnames, filenames in os.walk(PLOT_DIR, topdown=False):
        if not dirnames and not filenames:
            os.rmdir(dirpath)

@pytest.fixture(scope=TEST_CONFIG['conn_scope'])
def board_conn(request, delete_plots):
    conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
    logging.getLogger(conn_logger_name).setLevel(TEST_CONFIG['loglevelconn'])
    logger = logging.getLogger(request.cls.__name__)
    logger.info("Connecting to the ICE board")
    ca = FPGAArray(**CONN_CONFIG)
    request.cls.board = ca.ib[0]
    logger.info(f"Firmware version: {ca.ib[0].get_version()}")

@pytest.fixture(scope='function')
def setup_funcgen(request, board_conn):
    logger = logging.getLogger(request.cls.__name__)
    request.cls.FG_NS = request.cls.board.ADC_SAMPLES_PER_FRAME
    request.cls.NUMBER_OF_CHANNELIZERS = request.cls.board.NUMBER_OF_CHANNELIZERS
    # Should throw an exception when ADC_BYTES_PER_FRAME not 1 or 2 but the connection does it itself
    if request.cls.board.ADC_BYTES_PER_SAMPLE == 1:
        request.cls.FG_DTYPE = 'u1'
        request.cls.FG_DTYPE_SIGNED = 'i1'
        request.cls.FG_LSHIFT = 8 - request.cls.board.ADC_BITS_PER_SAMPLE
    else:
        request.cls.FG_DTYPE = '>u2'
        request.cls.FG_DTYPE_SIGNED = 'i2'
        request.cls.FG_LSHIFT = 16 - request.cls.board.ADC_BITS_PER_SAMPLE
    logger.debug(f"Setting funcgen output dtype to {request.cls.FG_DTYPE}")

@pytest.fixture(scope='function')
def setup_scaler(request, board_conn):
    logger = logging.getLogger(request.cls.__name__)
    request.cls.FG_NS = request.cls.board.chan[0].FUNCGEN.NS
    request.cls.PLATFORM = TEST_CONFIG.get('platform')[:3]
    request.cls.NUM_CHANNELIZERS = len(request.cls.board.chan)
    request.cls.BINS_PER_SAMPLE = 4 if request.cls.PLATFORM == 'CRS' else 1
    request.cls.board.set_channelizer(
        fft_bypass=True,
        scaler_bypass=False,
        scaler_out_data_type=0,
        scaler_cap_data_type=0,
        offset_binary_encoding=False,
        scaler_eight_bit=request.cls.CAPTURE_WIDTH > 4 and request.cls.PLATFORM == 'ICE',
        prober_user_flags=False,# if request.cls.PLATFORM == 'ICE' else None,
        scaler_rounding_mode=0,
        symmetric_saturation=False)
    logger.debug("Setup channelizer for testing scaler")

'''@pytest.fixture(autouse=True)
def check_fifo_overflow(request): #check that fifo overflow flag never went high during test
    yield
    for ch in request.cls.board.chan:
        pass
        #assert(ch.SCALER.CHAN_FIFO_OVERFLOW == 0)
'''
