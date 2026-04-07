#!/usr/bin/env python

from wtl.pytest_xreport import run_test_menu
import os

if __name__ == '__main__':
    """ Run the test in this file."""
    config_filename = os.path.realpath(os.path.join(os.path.dirname(__file__), 'qcengine_test_config.yaml'))
    v = run_test_menu([None, config_filename])
    locals().update(v)  # bring local variables from the test runner into the current namespace for easier debugging
