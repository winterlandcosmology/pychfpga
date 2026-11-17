"""CLI entry point for the RawAcq REST server.

Logs go to stderr. With ``--log-file PATH`` they are also written to that file, including
warnings and the tracebacks of uncaught exceptions in any thread (e.g. the packet receive
thread). Example::

    pychfpga_raw_acq --port 54322 --log-file /storage/crate012/corr16/raw_acq_server.log
"""

import argparse
import logging
import logging.handlers
import os
import sys
import threading

from wtl import log
from wtl.rest import run_client

from .client import RawAcqAsyncRESTClient
from .server import RawAcqAsyncRESTServer

LOG_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s.%(funcName)s() %(filename)s:%(lineno)d>> %(message)s"


def setup_logging(log_level='DEBUG', log_file=None):
    """Log to stderr and optionally to `log_file`, and log uncaught exceptions in all threads."""
    log.setup_basic_logging(log_level)
    if log_file:
        # Add the file handler to the root logger. A second wtl dictConfig would disable the
        # loggers that already exist.
        handler = logging.handlers.WatchedFileHandler(os.path.expanduser(log_file))
        handler.setFormatter(logging.Formatter(LOG_FILE_FORMAT))
        handler.setLevel(log_level)
        logging.getLogger().addHandler(handler)
    logging.captureWarnings(True)

    logger = logging.getLogger('pychfpga.raw_acq')

    def log_thread_exception(args):
        thread_name = args.thread.name if args.thread else '?'
        logger.critical(f'Uncaught exception in thread {thread_name}',
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def log_exception(exc_type, exc_value, exc_traceback):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical('Uncaught exception', exc_info=(exc_type, exc_value, exc_traceback))
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    threading.excepthook = log_thread_exception
    sys.excepthook = log_exception


def main():
    parser = argparse.ArgumentParser(description='RawAcq', epilog='')
    parser.add_argument('--log-file', default=None,
                        help='Also write all logs to this file (appended).')
    parser.add_argument('--log-level', default='DEBUG', type=str.upper,
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='Logging level. Default is DEBUG.')
    # run_client() adds its own arguments to this parser and parses the command line itself
    opts, _ = parser.parse_known_args(sys.argv[1:])
    setup_logging(opts.log_level, opts.log_file)

    client, server = run_client(
        sys.argv[1:],
        RawAcqAsyncRESTServer,
        RawAcqAsyncRESTClient,
        object_name='RawAcq',
        server_config_path='raw_acq.servers',
        parser=parser)
    return client, server


if __name__ == '__main__':
    client, server = main()
