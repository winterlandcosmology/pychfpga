import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from pychfpga.common.state import State

# Firmware package import pulls matplotlib (optional here). Load the module file
# so this helper can be tested without hardware or a working matplotlib stack.
sys.modules.setdefault("matplotlib", MagicMock())
sys.modules.setdefault("matplotlib.pyplot", MagicMock())

_ARRAY_METHODS = (
    Path(__file__).resolve().parents[1]
    / "pychfpga"
    / "fpga_firmware"
    / "chfpga"
    / "chfpga_array_methods.py"
)
_spec = importlib.util.spec_from_file_location(
    "chfpga_array_methods_standalone", _ARRAY_METHODS
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
chFPGAArrayMethods = _mod.chFPGAArrayMethods


def test_get_common_config_attr_match_returns_first_identity():
    fmt = State(bits=8)
    a = State(source=1, fmt=fmt)
    b = State(source=1, fmt=fmt)
    assert chFPGAArrayMethods.get_common_config_attr([a, b], "source") == 1
    assert chFPGAArrayMethods.get_common_config_attr([a, b], "fmt") is fmt


def test_get_common_config_attr_mismatch_raises():
    a = State(source=1)
    b = State(source=2)
    with pytest.raises(ValueError, match="source"):
        chFPGAArrayMethods.get_common_config_attr([a, b], "source")


def test_get_common_config_attr_empty_raises():
    with pytest.raises(ValueError, match="no configs"):
        chFPGAArrayMethods.get_common_config_attr([], "source")


def test_get_common_config_attr_ndarray_match():
    arr = np.arange(4)
    a = State(bin_map=arr)
    b = State(bin_map=np.arange(4))
    result = chFPGAArrayMethods.get_common_config_attr([a, b], "bin_map")
    assert result is arr
    assert np.array_equal(result, np.arange(4))


def test_get_common_config_attr_ndarray_mismatch():
    a = State(bin_map=np.arange(4))
    b = State(bin_map=np.arange(4) + 1)
    with pytest.raises(ValueError, match="bin_map"):
        chFPGAArrayMethods.get_common_config_attr([a, b], "bin_map")
