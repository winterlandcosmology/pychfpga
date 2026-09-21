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


def test_provider_method_discovery_skips_instance_callables_and_class_data():
    class _Callable:
        def __call__(self):
            return 'called'

    class _Provider(ArrayMethods):
        FLAG = ('not', 'a', 'method')

        def op(self):
            return 'op'

    arr = _FakeArray()
    arr.ib = _Callable()
    arr.ic = _Callable()
    provider = _Provider(arr)

    methods = provider.get_array_methods()

    assert set(methods) == {'op'}
    assert 'ib' not in methods
    assert 'ic' not in methods
    assert 'FLAG' not in methods


def test_uct_array_methods_operational_stubs():
    try:
        from pychfpga.fpga_firmware.chfpga.ct_engine.uct_array_methods import UCTArrayMethods
    except ImportError as e:
        pytest.skip(f'Cannot import UCTArrayMethods: {e}')

    provider = UCTArrayMethods(_FakeArray())
    methods = provider.get_array_methods()

    assert methods['get_frequency_map']() == {}
    assert methods['reset_crossbar_stats']() is None
    assert methods['reset_bp_shuffle_stats']() is None
    assert 'set_tx_power' not in methods
    assert 'get_chan_identity_map' not in methods
    assert 'detect_backplane_links' not in methods


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


def test_two_providers_do_not_conflict_on_callable_ib():
    class _Callable:
        def __call__(self):
            return 'ib'

    class _EmptyA(ArrayMethods):
        def a_op(self):
            return 'a'

    class _EmptyB(ArrayMethods):
        def b_op(self):
            return 'b'

    arr, _ = _make_array([_FakeMB(frozenset({_EmptyA, _EmptyB}))])
    arr.ib = _Callable()

    names = set(arr.get_array_methods())

    assert names == {'a_op', 'b_op'}


def test_motherboard_get_array_method_classes_combines_board_and_firmware():
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
        _array_method_classes = {'fw': FirmwareProvider}

    mb = object.__new__(Board)
    mb.fpga = Firmware()

    assert mb.update_array_method_classes() == frozenset({BoardProvider, FirmwareProvider})
    assert mb.get_array_method_classes() == frozenset({BoardProvider, FirmwareProvider})
    assert mb.update_array_method_classes() == frozenset({BoardProvider, FirmwareProvider})
    assert mb._array_method_classes == frozenset({BoardProvider, FirmwareProvider})


def test_real_chfpga_advertises_array_method_provider():
    _fpga_array_or_skip()
    try:
        from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA, chFPGAArrayMethods
    except ImportError as e:
        pytest.skip(f'Cannot import chFPGA: {e}')

    assert chFPGA._array_method_classes['chFPGA'] is chFPGAArrayMethods
    ch_methods = chFPGAArrayMethods(_FakeArray()).get_array_methods()
    assert 'sync_array' in ch_methods
    assert 'get_frequency_map' not in ch_methods
    assert 'set_tx_power' not in ch_methods
    assert 'detect_backplane_links' not in ch_methods

    try:
        from pychfpga.fpga_firmware.chfpga.ct_engine.bct_array_methods import BCTArrayMethods
        from pychfpga.fpga_firmware.chfpga.ct_engine.uct_array_methods import UCTArrayMethods
    except ImportError as e:
        pytest.skip(f'Cannot import CT array methods: {e}')

    bct_methods = BCTArrayMethods(_FakeArray()).get_array_methods()
    assert 'get_frequency_map' in bct_methods
    assert 'set_tx_power' in bct_methods
    assert 'reset_crossbar_stats' in bct_methods
    assert 'reset_bp_shuffle_stats' in bct_methods
    assert 'detect_backplane_links' in bct_methods
    assert 'print_shuffle_status_async' in bct_methods

    uct_methods = UCTArrayMethods(_FakeArray()).get_array_methods()
    assert set(uct_methods) >= {
        'get_frequency_map', 'reset_crossbar_stats', 'reset_bp_shuffle_stats'}
    assert 'set_tx_power' not in uct_methods
    assert 'detect_backplane_links' not in uct_methods


def _make_firmware_class(providers=None):
    from pychfpga.fpga_firmware.fpga_firmware import FPGAFirmware

    providers = dict(providers or {})

    class _TestFirmware(FPGAFirmware):
        PLATFORM_SUPPORT = (
            dict(platform='TEST', modes=('test',), firmware_url='test.bit'),
        )
        _array_method_classes = providers

    return _TestFirmware


def test_firmware_init_subclass_requires_array_method_classes_dict():
    from pychfpga.fpga_firmware.fpga_firmware import FPGAFirmware

    with pytest.raises(RuntimeError, match='_array_method_classes'):
        class _MissingDict(FPGAFirmware):
            PLATFORM_SUPPORT = (
                dict(platform='TEST', modes=('test',), firmware_url='test.bit'),
            )


def test_register_array_methods_keyed_dict_and_motherboard_cache():
    from pychfpga.hardware.motherboard import Motherboard

    class CTProvider(ArrayMethods):
        def ct_op(self):
            return self.fpga_array

    class OtherCTProvider(ArrayMethods):
        def other_ct_op(self):
            return self.fpga_array

    fw_cls = _make_firmware_class({'chFPGA': _ProviderA})
    original = dict(fw_cls._array_method_classes)
    try:
        mb = object.__new__(Motherboard)
        mb.array_method_class = None
        fw = object.__new__(fw_cls)
        fw.mb = mb
        mb.fpga = fw

        mb.update_array_method_classes()
        assert mb.get_array_method_classes() == frozenset({_ProviderA})

        fw.register_array_methods(CTProvider, key='CT')
        assert fw_cls._array_method_classes['CT'] is CTProvider
        assert mb.get_array_method_classes() == frozenset({_ProviderA, CTProvider})

        mb2 = object.__new__(Motherboard)
        mb2.array_method_class = None
        fw2 = object.__new__(fw_cls)
        fw2.mb = mb2
        mb2.fpga = fw2
        assert fw2._array_method_classes is fw_cls._array_method_classes

        fw2.register_array_methods(CTProvider, key='CT')
        assert mb2.get_array_method_classes() == frozenset({_ProviderA, CTProvider})

        with pytest.raises(RuntimeError, match="key 'CT'"):
            fw.register_array_methods(OtherCTProvider, key='CT')
    finally:
        fw_cls._array_method_classes.clear()
        fw_cls._array_method_classes.update(original)


def test_fpga_array_rebuilds_after_ct_register():
    _fpga_array_or_skip()
    from pychfpga.hardware.motherboard import Motherboard

    class CTProvider(ArrayMethods):
        def ct_op(self):
            return 'ct'

    fw_cls = _make_firmware_class({'chFPGA': _ProviderA})
    original = dict(fw_cls._array_method_classes)
    try:
        mb = object.__new__(Motherboard)
        mb.array_method_class = None
        fw = object.__new__(fw_cls)
        fw.mb = mb
        mb.fpga = fw
        mb.update_array_method_classes()

        arr, _ = _make_array([mb])
        assert 'do_it' in arr.get_array_methods()
        assert 'ct_op' not in arr.get_array_methods()
        cached = arr._array_methods
        assert arr.get_array_methods() is cached

        fw.register_array_methods(CTProvider, key='CT')
        methods = arr.get_array_methods()
        assert 'ct_op' in methods
        assert methods is not cached
        assert methods['ct_op']() == 'ct'
    finally:
        fw_cls._array_method_classes.clear()
        fw_cls._array_method_classes.update(original)
