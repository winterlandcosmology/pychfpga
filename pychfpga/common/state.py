"""Tracked hierarchical state containers."""

import copy
from collections.abc import Mapping, Sequence

import numpy as np
from wtl.namespace import NameSpace


_MISSING = object()


class State(NameSpace):
    """A dotted-access state tree with snapshot-based change tracking.

    ``ack()`` stores a snapshot of this state node, or selected attributes, for
    a given context. ``is_changed()`` compares that snapshot to current values.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_baselines", {})

    def __repr__(self):
        return f"{self.__class__.__name__}({self._obj!r})"

    def add(self, key, **values):
        """Create, attach, and return a new child ``State``.

        Raises
        ------
        KeyError
            If a child already exists under ``key``.
        """
        if key in self:
            raise KeyError(f"State child {key!r} already exists")
        self[key] = child = State(**values)
        return child

    def ack(self, attrs=None, *, context=None):
        """Record that ``context`` has consumed this state node or attributes."""
        for attr_name in self._get_attrs(attrs):
            self._baselines[(attr_name, context)] = self._snapshot_value(self[attr_name])

    def is_changed(self, attrs=None, *, context=None, ack=False):
        """Return whether state changed since ``context`` last acknowledged it."""
        changed = False
        for attr_name in self._get_attrs(attrs):
            baseline = self._baselines.get((attr_name, context), _MISSING)
            value = self._snapshot_value(self[attr_name])
            if baseline is _MISSING or not self._values_equal(baseline, value):
                changed = True
                break
        if ack:
            self.ack(attrs, context=context)
        return changed

    has_changed = is_changed

    def update(self, **values):
        """Assign multiple state values."""
        for key, value in values.items():
            setattr(self, key, value)
        return self

    @classmethod
    def _values_equal(cls, left, right):
        if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
            try:
                return np.array_equal(left, right)
            except Exception:
                return False
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            if set(left) != set(right):
                return False
            return all(cls._values_equal(left[key], right[key]) for key in left)
        if cls._is_non_string_sequence(left) and cls._is_non_string_sequence(right):
            if len(left) != len(right):
                return False
            return all(cls._values_equal(lval, rval) for lval, rval in zip(left, right))
        try:
            equal = left == right
        except Exception:
            return False
        if isinstance(equal, np.ndarray):
            return bool(np.all(equal))
        return bool(equal)

    def _get_attrs(self, attrs):
        if isinstance(attrs, (str, int)):
            attrs = (attrs,)
        return self.keys() if attrs is None else attrs

    def keys(self):
        """Return mapping keys or sequence indices for the wrapped object."""
        if isinstance(self._obj, Mapping):
            return self._obj.keys()
        if self._is_non_string_sequence(self._obj):
            return range(len(self._obj))
        raise TypeError(f"{type(self._obj).__name__} object does not provide state keys")

    @staticmethod
    def _snapshot_value(value):
        if isinstance(value, NameSpace):
            value = value.as_dict()
        return copy.deepcopy(value)

    @staticmethod
    def _is_non_string_sequence(value):
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
