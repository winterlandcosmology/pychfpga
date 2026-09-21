"""Tracked hierarchical state containers."""

import copy
from collections.abc import Mapping, Sequence

import numpy as np
from wtl.namespace import NameSpace


_MISSING = object()
_PRIVATE = ("_obj", "_ver", "_seen", "_gen", "_seen_gen")


class State(NameSpace):
    """A dotted-access state tree with version-based change tracking.

    Direct assignment (``state.x = value``) bumps a per-attribute version when
    the stored object identity changes. ``update(**kwargs)`` assigns flat
    values with equality checking. ``update_from(mapping)`` merges nested dict
    trees (e.g. JSON patches). In-place mutation is not tracked.

    Each node also keeps a summary ``_gen`` incremented on every local version
    bump. ``has_changed()`` uses that integer for this node and walks nested
    ``State`` values (and ``State`` items in lists/tuples) so in-place child
    updates are visible. ``ack()`` snapshots the same tree for a consumer
    ``context``; the two always match.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_ver", {})
        object.__setattr__(self, "_seen", {})
        object.__setattr__(self, "_gen", 0)
        object.__setattr__(self, "_seen_gen", {})

    def deepcopy(self):
        """Return a new ``State`` with a deep-copied mapping (fresh versions)."""
        return State(copy.deepcopy(self._obj))

    def __deepcopy__(self, memo):
        return State(copy.deepcopy(self._obj, memo))

    def __repr__(self):
        return f"{self.__class__.__name__}({self._obj!r})"

    def __getattr__(self, name):
        """Attribute access for missing keys raises ``AttributeError``.

        ``NameSpace`` falls through to ``__getitem__``, so a missing key
        raises ``KeyError``. Convert that so ``getattr(state, name, default)``
        and ``hasattr`` work as they do on normal objects. Item access
        (``state[name]``) is unchanged.
        """
        try:
            return super().__getattr__(name)
        except (KeyError, IndexError):
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            ) from None

    def __setattr__(self, name, value):
        if name in _PRIVATE:
            object.__setattr__(self, name, value)
            return
        self._assign(name, value)

    def __setitem__(self, name, value):
        self._assign(name, value)

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
        """Record that ``context`` has consumed this state node or attributes.

        Snapshots this node and nested ``State`` children (including ``State``
        items in lists/tuples). A whole-node ack (``attrs is None``) records
        ``_gen`` so a later check is not a false positive.
        """
        if attrs is None:
            self._seen_gen[context] = self._gen
            for attr_name in self._get_attrs(None):
                self._seen[(attr_name, context)] = self._ver.get(attr_name, 0)
            for child in self._iter_state_children():
                child.ack(context=context)
            return
        attr_names = self._get_attrs(attrs)
        for attr_name in attr_names:
            self._seen[(attr_name, context)] = self._ver.get(attr_name, 0)
        for attr_name in attr_names:
            for child in self._state_children_of(self._obj[attr_name]):
                child.ack(context=context)

    def has_changed(self, attrs=None, *, context=None, ack=False):
        """Return whether this node or nested ``State`` children changed.

        With ``attrs is None``, this node's summary ``_gen`` is tested, then
        nested children are walked. With explicit ``attrs``, those slots'
        versions are tested locally, then any ``State`` values among them are
        walked. ``ack()`` always snapshots the same tree.
        """
        changed = self._tree_changed(attrs, context)
        if ack:
            self.ack(attrs, context=context)
        return changed

    is_changed = has_changed

    def has_attr(self, *names):
        """Return True if all named attributes exist on this node."""
        if not names:
            raise TypeError("has_attr() requires at least one name")
        return all(name in self for name in names)

    @classmethod
    def get_common_attr(cls, states, attr):
        """Return ``attr`` from ``states``; all values must match (``_values_equal``).

        Returns the first value by identity so nested ``State`` objects stay
        shared. Empty ``states`` or a mismatch raises ``ValueError`` naming
        ``attr``.
        """
        if not states:
            raise ValueError(
                f"Cannot get common attribute {attr!r}: no states"
            )
        first = states[0][attr]
        for state in states[1:]:
            if not cls._values_equal(first, state[attr]):
                raise ValueError(
                    f"Attribute {attr!r} differs across states"
                )
        return first

    def update(self, **values):
        """Assign flat keyword values with equality checking.

        Nested mappings and ``State`` values are rejected; use
        :meth:`update_from` for tree merges.
        """
        for key, value in values.items():
            if isinstance(value, (Mapping, NameSpace, State)):
                raise TypeError(
                    f"State.update() does not accept nested values for {key!r}; "
                    "use update_from()"
                )
            self._assign(key, value, compare_values=True)
        return self

    def update_from(self, mapping):
        """Merge a mapping into this node (JSON / REST patches).

        Nested dicts merge into existing child ``State`` nodes or create new
        ones. ``State`` / ``NameSpace`` patch values are rejected.
        """
        if not isinstance(mapping, Mapping):
            raise TypeError(
                f"State.update_from() expects Mapping, got {type(mapping).__name__!r}"
            )
        for key, value in mapping.items():
            self._assign_from(key, value)
        return self

    @classmethod
    def _values_equal(cls, left, right):
        if left is right:
            return True
        if isinstance(left, NameSpace):
            left = left.as_dict()
        if isinstance(right, NameSpace):
            right = right.as_dict()
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

    @staticmethod
    def _reject_patch_value(value, key):
        if isinstance(value, (NameSpace, State)):
            raise TypeError(
                f"State.update_from() does not accept State values for {key!r}; "
                "use direct assignment to wire shared refs"
            )

    @staticmethod
    def _mapping_payload(value):
        if isinstance(value, Mapping):
            return value
        return None

    def _assign_from(self, name, value):
        self._reject_patch_value(value, name)
        try:
            old = self._obj[name]
        except (KeyError, IndexError, TypeError):
            old = _MISSING

        payload = self._mapping_payload(value)
        if payload is not None:
            if isinstance(old, State):
                old.update_from(payload)
                return
            child = State()
            child.update_from(payload)
            self._obj[name] = child
            self._bump(name)
            return

        if (
            isinstance(old, list)
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
            and len(old) == len(value)
        ):
            changed = False
            for i, item in enumerate(value):
                cur = old[i]
                item_payload = self._mapping_payload(item)
                if isinstance(cur, State) and item_payload is not None:
                    cur.update_from(item_payload)
                elif cur is item:
                    continue
                elif self._values_equal(cur, item):
                    continue
                else:
                    self._reject_patch_value(item, f"{name}[{i}]")
                    if item_payload is not None:
                        child = State()
                        child.update_from(item_payload)
                        old[i] = child
                    else:
                        old[i] = item
                    changed = True
            if changed:
                self._bump(name)
            return

        if old is not _MISSING and self._values_equal(old, value):
            return
        if old is value:
            return

        self._obj[name] = value
        self._bump(name)

    def _assign(self, name, value, *, compare_values=False):
        try:
            old = self._obj[name]
        except (KeyError, IndexError, TypeError):
            old = _MISSING
        if old is value:
            return
        if compare_values:
            if (
                isinstance(old, tuple)
                and self._is_non_string_sequence(value)
                and not isinstance(value, tuple)
            ):
                value = tuple(value)
                if old is value:
                    return
            if old is not _MISSING and self._values_equal(old, value):
                return
        self._obj[name] = value
        self._bump(name)

    def _bump(self, name):
        self._ver[name] = self._ver.get(name, 0) + 1
        object.__setattr__(self, "_gen", self._gen + 1)

    def _tree_changed(self, attrs, context):
        if attrs is None:
            if self._seen_gen.get(context, -1) != self._gen:
                return True
            return any(
                child._tree_changed(None, context)
                for child in self._iter_state_children()
            )
        for attr_name in self._get_attrs(attrs):
            if self._seen.get((attr_name, context), -1) != self._ver.get(attr_name, 0):
                return True
            if any(
                child._tree_changed(None, context)
                for child in self._state_children_of(self._obj[attr_name])
            ):
                return True
        return False

    def _iter_state_children(self):
        obj = self._obj
        if isinstance(obj, Mapping):
            values = obj.values()
        elif self._is_non_string_sequence(obj):
            values = obj
        else:
            return
        for value in values:
            yield from self._state_children_of(value)

    @classmethod
    def _state_children_of(cls, value):
        if isinstance(value, State):
            yield value
        elif cls._is_non_string_sequence(value):
            for item in value:
                if isinstance(item, State):
                    yield item

    def _get_attrs(self, attrs):
        if isinstance(attrs, (str, int)):
            attrs = (attrs,)
        if attrs is None:
            return self.keys()
        for attr_name in attrs:
            self[attr_name]
        return attrs

    def keys(self):
        """Return mapping keys or sequence indices for the wrapped object."""
        if isinstance(self._obj, Mapping):
            return self._obj.keys()
        if self._is_non_string_sequence(self._obj):
            return range(len(self._obj))
        raise TypeError(f"{type(self._obj).__name__} object does not provide state keys")

    @staticmethod
    def _is_non_string_sequence(value):
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
