"""Tests for explicit :class:`pychfpga.fpga_array.FPGAArray` array methods."""

import logging

import pytest

from pychfpga.common.array_methods import ArrayMethods


def _fpga_array_or_skip():
    """``fpga_array`` imports matplotlib at load; skip if that's unavailable."""
    try:
        from pychfpga.fpga_array import FPGAArray
        return FPGAArray
    except ImportError as e:
        pytest.skip(f'Cannot import pychfpga.fpga_array: {e}')


class _ProviderA(ArrayMethods):
    def do_it(self, *args, **kwargs):
        return ('do_it', self, self.fpga_array, args, kwargs)


class _ProviderB(ArrayMethods):
    def other(self):
        return ('other', self, self.fpga_array)


class _ProviderDuplicate(ArrayMethods):
    def do_it(self):
        return ('duplicate', self, self.fpga_array)


class _FakeArray:
    def __init__(self):
        self.ib = []
        self.ic = []


class _FakeMB:
    def __init__(self, provider_classes=None):
        self.set_array_method_classes(provider_classes)

    def set_array_method_classes(self, provider_classes):
        if provider_classes is None:
            self._array_method_classes = frozenset()
        elif isinstance(provider_classes, frozenset):
            self._array_method_classes = provider_classes
        else:
            self._array_method_classes = frozenset((provider_classes,))


def _make_array(motherboards):
    FPGAArray = _fpga_array_or_skip()
    arr = object.__new__(FPGAArray)
    arr.ib = list(motherboards)
    arr.ic = []
    arr.logger = logging.getLogger(__name__)
    arr._array_methods = {}
    arr._array_method_class_sets = ()
    arr._array_method_instances = {}
    return arr, FPGAArray


def test_provider_method_discovery_exposes_public_callables_only():
    provider = _ProviderA(_FakeArray())

    methods = provider.get_array_methods()

    assert set(methods) == {'do_it'}
    assert methods['do_it'].__self__ is provider


def test_intersection_is_common_provider_classes_only():
    arr, _ = _make_array([
        _FakeMB(_ProviderA),
        _FakeMB(_ProviderA),
    ])

    names = arr.__dir__()

    assert 'do_it' in names
    assert 'other' not in names
    assert set(arr._array_methods) == {'do_it'}


def test_getattr_returns_provider_method_bound_to_array_context():
    arr, _ = _make_array([_FakeMB(_ProviderA)])

    bound = arr.do_it
    result = bound(1, x=2)

    assert result[0] == 'do_it'
    assert isinstance(result[1], _ProviderA)
    assert result[2] is arr
    assert result[3:] == ((1,), {'x': 2})


def test_getattr_unknown_attribute_plain_error():
    arr, _ = _make_array([_FakeMB(_ProviderA)])

    with pytest.raises(AttributeError) as excinfo:
        arr.completely_unknown

    assert 'completely_unknown' in str(excinfo.value)


def test_cache_rebuilds_when_motherboard_provider_classes_change():
    mb = _FakeMB(_ProviderA)
    arr, _ = _make_array([mb])

    _ = arr.do_it
    assert set(arr._array_methods) == {'do_it'}

    mb.set_array_method_classes(_ProviderB)
    _ = arr.__dir__()

    assert set(arr._array_methods) == {'other'}
    with pytest.raises(AttributeError):
        arr.do_it
    assert arr.other()[0] == 'other'


def test_provider_instances_are_reused_when_class_remains_common():
    mb1 = _FakeMB(frozenset({_ProviderA, _ProviderB}))
    mb2 = _FakeMB(frozenset({_ProviderA, _ProviderB}))
    arr, _ = _make_array([mb1, mb2])

    _ = arr.do_it
    first_provider = arr._array_method_instances[_ProviderA]

    mb1.set_array_method_classes(_ProviderA)
    _ = arr.do_it

    assert arr._array_method_instances[_ProviderA] is first_provider
    assert _ProviderB not in arr._array_method_instances


def test_cache_stable_when_nothing_changes():
    arr, _ = _make_array([_FakeMB(_ProviderA)])

    _ = arr.do_it
    first_methods = arr._array_methods
    first_seen = arr._array_method_class_sets
    first_instances = arr._array_method_instances

    _ = arr.do_it
    _ = arr.do_it

    assert arr._array_methods is first_methods
    assert arr._array_method_class_sets is first_seen
    assert arr._array_method_instances is first_instances


def test_duplicate_provider_method_names_raise():
    # A real motherboard can still expose two providers total: one from the
    # board class and one from the loaded firmware class.
    arr, _ = _make_array([_FakeMB(frozenset({_ProviderA, _ProviderDuplicate}))])

    with pytest.raises(AttributeError) as excinfo:
        arr.do_it

    assert 'do_it' in str(excinfo.value)
    assert 'more than one' in str(excinfo.value)


def test_motherboard_get_array_method_classes_combines_board_and_firmware():
    _fpga_array_or_skip()
    from pychfpga.hardware.motherboard import Motherboard

    class BoardProvider(ArrayMethods):
        def board_op(self):
            return self.fpga_array

    class FirmwareProvider(ArrayMethods):
        def firmware_op(self):
            return self.fpga_array

    class Board(Motherboard):
        array_method_class = BoardProvider

    class Firmware:
        array_method_class = FirmwareProvider

    mb = object.__new__(Board)
    mb.fpga = Firmware()

    assert mb.get_array_method_classes() == frozenset({BoardProvider, FirmwareProvider})
    assert mb.update_array_methods() == frozenset({BoardProvider, FirmwareProvider})
    assert mb._array_method_classes == frozenset({BoardProvider, FirmwareProvider})


def test_real_chfpga_advertises_array_method_provider():
    _fpga_array_or_skip()
    try:
        from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA, chFPGAArrayMethods
    except ImportError as e:
        pytest.skip(f'Cannot import chFPGA: {e}')

    assert chFPGA.array_method_class is chFPGAArrayMethods
    assert 'sync_array' in chFPGAArrayMethods(_FakeArray()).get_array_methods()
    assert 'get_corner_turn_bin_map' in chFPGAArrayMethods(_FakeArray()).get_array_methods()
