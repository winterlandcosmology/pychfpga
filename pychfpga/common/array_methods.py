"""Shared utilities for :class:`pychfpga.fpga_array.FPGAArray`-wide methods.

An *array method* is a function marked with ``is_array_method = True`` that a
:class:`FPGAArray` can dispatch across every motherboard in the array. This
module provides:

- :func:`array_method` -- decorator that tags a function and wraps it as a
  :class:`staticmethod` so that ``getattr(mb, name)`` returns the plain
  function (allowing ``functools.partial(fn, array)`` binding on the array
  side without a double ``self`` argument).
- :func:`get_array_methods` -- scanner that returns the ``frozenset`` of
  array-method names currently exposed by any object.

The decorator and scanner live here (rather than on :class:`FPGAArray`) so
that modules defining array methods -- including subclasses of
:class:`~pychfpga.hardware.motherboard.Motherboard`, which cannot import
``fpga_array`` without creating a circular import -- can pull them in
without any coupling to ``fpga_array``.
"""


def array_method(fn):
    """Mark ``fn`` as an :class:`FPGAArray`-wide operation.

    Sets ``is_array_method = True`` on the underlying function object and
    returns a :class:`staticmethod` wrapping it. Using ``staticmethod``
    guarantees that ``getattr(mb, name)`` on a motherboard / firmware
    instance returns the *plain* function, so :class:`FPGAArray` can bind
    itself as the first positional argument via :func:`functools.partial`
    without the descriptor protocol inserting ``mb`` as the first argument.

    Usage::

        from pychfpga.common.array_methods import array_method

        class chFPGA(FPGAFirmware):
            @array_method
            def sync_array(array, delay=..., ...):
                ...
    """
    fn.is_array_method = True
    return staticmethod(fn)


def get_array_methods(obj):
    """Return a ``frozenset`` of attribute names on ``obj`` marked as array
    methods (i.e. carrying ``is_array_method = True``).

    The scan walks ``dir(obj)``; for a
    :class:`~pychfpga.hardware.motherboard.Motherboard` instance this
    already unions Motherboard and FPGA-firmware attributes via
    :meth:`Motherboard.__dir__`.
    """
    return frozenset(
        n for n in dir(obj)
        if getattr(getattr(obj, n, None), 'is_array_method', False)
    )
