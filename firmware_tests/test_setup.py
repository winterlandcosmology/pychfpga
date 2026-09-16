from pychfpga.fpga_array import FPGAArray
import logging
import pytest
from wtl.pytest_xreport import get_xr_from
from pathlib import Path
import os

PLOT_DIR = Path("test_results")


def _session_config(pytestconfig):
    return get_xr_from(pytestconfig).config


def _conn_scope(fixture_name, config):
    return _session_config(config).test_config["conn_scope"]

#Maps platform name to list of parameters for it

@pytest.fixture(scope=_conn_scope)
def delete_plots(request, pytestconfig):
    test_config = _session_config(pytestconfig).test_config
    logger = logging.getLogger(request.cls.__name__)
    if test_config['always_plot'] or test_config['plot_on_failure']:
        PLOT_DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.isdir(PLOT_DIR):
        logger.warning(f"{PLOT_DIR} does not exist")
        return
    if test_config['delete_plots_before_run']:
        logger.info('Deleting previous plots')
        for file in PLOT_DIR.rglob('*.png'):
            os.remove(file)
    for dirpath, dirnames, filenames in os.walk(PLOT_DIR, topdown=False):
        if not dirnames and not filenames:
            os.rmdir(dirpath)

@pytest.fixture(scope=_conn_scope)
def board_conn(request, delete_plots, pytestconfig):
    config = _session_config(pytestconfig)
    conn_config = config.connection_config
    test_config = config.test_config
    conn_logger_name = FPGAArray.__name__.rsplit('.', 1)[0] if '.' in __name__ else ''
    logging.getLogger(conn_logger_name).setLevel(test_config['loglevelconn'])
    logger = logging.getLogger(request.cls.__name__)
    logger.info(
        "Connecting with platform=%s mode=%s hwm=%s",
        test_config.get('platform'),
        conn_config.get('mode'),
        conn_config.get('hwm'),
    )
    ca = FPGAArray(**conn_config)
    request.cls.ca = ca
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
def setup_scaler(request, board_conn, pytestconfig):
    test_config = _session_config(pytestconfig).test_config
    logger = logging.getLogger(request.cls.__name__)
    request.cls.FG_NS = request.cls.board.chan[0].FUNCGEN.NS
    request.cls.PLATFORM = test_config.get('platform')[:3]
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
