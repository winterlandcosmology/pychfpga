"""Tests for :class:`pychfpga.fpga_array.FPGAArray` array-method scan and
``__getattr__`` dispatch.

The dispatch uses a version-tag cache: each motherboard holds a
``frozenset`` of exposed array-method names in ``_array_methods``, and
:class:`FPGAArray` rebuilds its intersection (also stored in
``_array_methods``) whenever the tuple of motherboard frozensets changes
identity. There is no dirty flag.

When the rebuild runs, every tagged array-method name must appear on every
motherboard *and* resolve to the same function object across them; any
mismatch raises :class:`AttributeError` listing the offending names.
"""

import functools

import pytest


def _fpga_array_or_skip():
    """``fpga_array`` imports matplotlib at load; skip if that's unavailable."""
    try:
        from pychfpga.fpga_array import FPGAArray
        return FPGAArray
    except ImportError as e:
        pytest.skip(f'Cannot import pychfpga.fpga_array: {e}')


class _FakeMB:
    """Minimal stand-in for a Motherboard used to drive FPGAArray.__getattr__.

    Holds an ``_array_methods`` frozenset and exposes the corresponding
    callables as regular instance attributes so ``getattr(mb, name)``
    returns them just like on a real motherboard.
    """

    def __init__(self, method_map=None):
        method_map = dict(method_map or {})
        self._array_methods = frozenset(method_map)
        for name, fn in method_map.items():
            setattr(self, name, fn)

    def set_array_methods(self, method_map):
        """Replace the set of exposed array methods (new frozenset object)."""
        for name in list(self._array_methods):
            try:
                delattr(self, name)
            except AttributeError:
                pass
        self._array_methods = frozenset(method_map)
        for name, fn in method_map.items():
            setattr(self, name, fn)


def _array_fn(name):
    """Return a plain function carrying the ``is_array_method`` marker."""
    def fn(array, *args, **kwargs):
        return (name, array, args, kwargs)
    fn.__name__ = name
    fn.is_array_method = True
    return fn


def _make_array(motherboards):
    FPGAArray = _fpga_array_or_skip()
    arr = object.__new__(FPGAArray)
    arr.ib = list(motherboards)
    return arr, FPGAArray


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------

def test_intersection_is_common_names_only():
    """When every motherboard exposes the same names with the same callables,
    the rebuild succeeds and ``_array_methods`` is the common set.
    """
    shared = _array_fn('b')
    arr, _ = _make_array([
        _FakeMB({'b': shared}),
        _FakeMB({'b': shared}),
    ])
    names = arr.__dir__()
    assert 'b' in names
    assert arr._array_methods == frozenset({'b'})


def test_getattr_returns_partial_bound_to_array():
    """On hit, ``__getattr__`` returns a ``functools.partial`` with the array as first arg."""
    mb = _FakeMB({'do_it': _array_fn('do_it')})
    arr, _ = _make_array([mb])

    bound = arr.do_it
    assert isinstance(bound, functools.partial)
    assert bound.args == (arr,)
    result = bound(1, x=2)
    assert result == ('do_it', arr, (1,), {'x': 2})


def test_mismatch_raises_on_partial_presence():
    """A tagged name exposed on some but not all motherboards raises."""
    a = _FakeMB({'partial_method': _array_fn('partial_method')})
    b = _FakeMB({})
    arr, _ = _make_array([a, b])

    with pytest.raises(AttributeError) as excinfo:
        arr.partial_method
    msg = str(excinfo.value)
    assert 'mismatch' in msg
    assert 'partial_method' in msg
    assert 'not defined on every motherboard' in msg


def test_mismatch_raises_on_divergent_ids():
    """Same name resolving to different function objects across motherboards raises."""
    arr, _ = _make_array([
        _FakeMB({'op': _array_fn('op')}),   # distinct function object
        _FakeMB({'op': _array_fn('op')}),   # distinct function object
    ])
    with pytest.raises(AttributeError) as excinfo:
        arr.op
    msg = str(excinfo.value)
    assert 'mismatch' in msg
    assert 'op' in msg
    assert 'different function objects' in msg


def test_getattr_unknown_attribute_plain_error():
    """A name that is not an array method on any motherboard raises a plain AttributeError."""
    arr, _ = _make_array([_FakeMB({'known': _array_fn('known')})])
    with pytest.raises(AttributeError) as excinfo:
        arr.completely_unknown
    assert 'completely_unknown' in str(excinfo.value)


def test_cache_rebuilds_when_motherboard_array_methods_change():
    """Replacing ``mb._array_methods`` invalidates the cache on the next access."""
    fn_a = _array_fn('a')
    mb = _FakeMB({'a': fn_a})
    arr, _ = _make_array([mb])

    _ = arr.a
    assert arr._array_methods == frozenset({'a'})

    mb.set_array_methods({'b': _array_fn('b')})
    _ = arr.__dir__()
    assert arr._array_methods == frozenset({'b'})

    with pytest.raises(AttributeError):
        arr.a
    bound = arr.b
    assert isinstance(bound, functools.partial)
    assert bound.args == (arr,)


def test_cache_stable_when_nothing_changes():
    """Repeated __getattr__ calls with unchanged motherboard sets must not rebuild."""
    mb = _FakeMB({'a': _array_fn('a')})
    arr, _ = _make_array([mb])

    _ = arr.a
    first_common = arr._array_methods
    first_seen = arr._array_methods_sets

    _ = arr.a
    _ = arr.a
    assert arr._array_methods is first_common
    assert arr._array_methods_sets is first_seen


# ---------------------------------------------------------------------------
# Scanner (now lives in pychfpga.common.array_methods)
# ---------------------------------------------------------------------------

def test_get_array_methods_finds_marked_attributes():
    """``get_array_methods`` picks up every ``is_array_method`` attribute in ``dir()``."""
    from pychfpga.common.array_methods import get_array_methods

    fn1 = _array_fn('fn1')
    fn2 = _array_fn('fn2')

    class Holder:
        a = fn1
        b = fn2
        c = 'not a function'

        def plain(self):
            pass

    assert get_array_methods(Holder()) == frozenset({'a', 'b'})


def test_array_method_decorator_marks_and_wraps_as_staticmethod():
    """``@array_method`` tags the function and returns it as a staticmethod."""
    from pychfpga.common.array_methods import array_method, get_array_methods

    class Holder:
        @array_method
        def foo(array, x):
            return (array, x)

    # Attribute resolution goes through the staticmethod descriptor and returns
    # the *plain* function, so dispatching via ``functools.partial(fn, array)``
    # on the array side doesn't double-inject ``self``.
    h = Holder()
    assert getattr(h, 'foo').is_array_method is True
    assert 'foo' in get_array_methods(h)
    assert getattr(h, 'foo')(object(), 7)[1] == 7


def test_get_array_methods_on_real_chfpga_sees_sync_array():
    """Integration sanity check: ``chFPGA.sync_array`` is decorated and thus discoverable."""
    _fpga_array_or_skip()
    from pychfpga.common.array_methods import get_array_methods
    try:
        from pychfpga.fpga_firmware.chfpga.chfpga import chFPGA
    except ImportError as e:
        pytest.skip(f'Cannot import chFPGA: {e}')

    names = get_array_methods(chFPGA)
    assert 'sync_array' in names


def test_motherboard_update_array_methods_populates_frozenset():
    """A real Motherboard instance refreshes its ``_array_methods`` via the shared scanner."""
    _fpga_array_or_skip()
    try:
        from pychfpga.hardware.motherboard import Motherboard
    except ImportError as e:
        pytest.skip(f'Cannot import Motherboard: {e}')

    mb = object.__new__(Motherboard)
    mb.fpga = None  # Motherboard.__dir__ tolerates a missing firmware.

    mb.custom_array_op = _array_fn('custom_array_op')
    result = mb.update_array_methods()
    assert isinstance(result, frozenset)
    assert 'custom_array_op' in result
    assert mb._array_methods is result
