import pytest

from pychfpga.common.state import State


class Context:
    pass


def test_state_tracks_plain_attribute_assignments_by_context():
    state = State()
    context = Context()

    state.source = "adc"

    assert state.is_changed("source", context=context)
    state.ack("source", context=context)
    assert not state.is_changed("source", context=context)

    state.source = "noise"

    assert state.source == "noise"
    assert state.is_changed("source", context=context)
    assert state.is_changed(context=context)


def test_state_ack_true_updates_context_baseline_after_query():
    state = State()
    context = Context()
    state.value = 1

    assert state.is_changed("value", context=context, ack=True)
    assert not state.is_changed("value", context=context)

    state.value = 2

    assert state.is_changed("value", context=context)


def test_state_supports_subset_checks_and_whole_subtree_checks():
    state = State()
    context = Context()
    state.update(left_shift=0, right_shift=0, source="adc")
    state.ack(context=context)

    state.left_shift = 2

    assert state.is_changed(["left_shift", "source"], context=context)
    assert not state.is_changed(["right_shift", "source"], context=context)
    assert state.is_changed(context=context)


def test_state_does_not_track_in_place_mutation():
    state = State()
    context = Context()
    table = {"a": []}
    state.table = table
    state.ack("table", context=context)

    state.table["a"].append(1)

    assert not state.is_changed("table", context=context)

    state.table = {"a": [1]}
    assert state.is_changed("table", context=context)


def test_state_adds_named_and_indexed_children_and_exports_dicts():
    root = State()
    root.add("chan")
    chan0 = root.chan.add(0)
    funcgen = chan0.add("funcgen")
    funcgen.update(source="adc", left_shift=0, right_shift=0)

    assert root.chan[0] is chan0
    assert root.as_dict() == {
        "chan": {
            0: {
                "funcgen": {
                    "source": "adc",
                    "left_shift": 0,
                    "right_shift": 0,
                }
            }
        }
    }
    assert root.chan.as_dict() == {
        0: {
            "funcgen": {
                "source": "adc",
                "left_shift": 0,
                "right_shift": 0,
            }
        }
    }
    assert funcgen.as_dict() == {"source": "adc", "left_shift": 0, "right_shift": 0}


def test_state_rejects_duplicate_add():
    state = State()
    state.add("funcgen")

    with pytest.raises(KeyError):
        state.add("funcgen")


def test_string_contexts_are_supported():
    state = State()
    state.value = 1
    state.ack("value", context="raw_acq:north")

    state.value = 2

    assert state.is_changed("value", context="raw_acq:north")


def test_none_is_valid_default_context():
    state = State()
    state.value = 1

    assert state.is_changed("value")
    state.ack("value")
    assert not state.is_changed("value")

    state.value = 2

    assert state.is_changed("value")


def test_values_equal_compares_nested_state_by_content():
    left = State(
        fmt_bus_width=8,
        fmt_data_width=4,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        use_offset_encoding=True,
    )
    right = State(
        fmt_bus_width=8,
        fmt_data_width=4,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        use_offset_encoding=True,
    )
    different = State(
        fmt_bus_width=8,
        fmt_data_width=8,
        fmt_is_complex=True,
        fmt_interleave_factor=1,
        use_offset_encoding=True,
    )

    assert left is not right
    assert left != right  # NameSpace identity / no content equality
    assert State._values_equal(left, right)
    assert not State._values_equal(left, different)


def test_assignment_tracks_object_identity_not_array_equality():
    import numpy as np

    arr = np.arange(8)
    state = State(map=arr)
    state.ack("map", context="t")
    assert not state.has_changed("map", context="t")

    state.map = arr
    assert not state.has_changed("map", context="t")

    state.map = np.arange(8)
    assert state.has_changed("map", context="t")


def test_reassigning_the_same_object_does_not_count_as_a_change():
    payload = {"a": 1}
    state = State()
    state.table = payload
    state.ack("table", context="t")

    state.table = payload
    assert not state.has_changed("table", context="t")

    state.table = dict(payload)
    assert state.has_changed("table", context="t")


def test_update_skips_version_bump_for_equal_values():
    import numpy as np

    fmt0 = State(fmt_bus_width=8)
    fmt1 = State(fmt_bus_width=16)
    state = State()
    state.update(stream_ids=tuple([10, 11]), map=np.arange(8))
    state.capture_format = [fmt0, fmt1]
    state.ack(context="t")
    held_sids = state.stream_ids
    held_fmt = state.capture_format
    held_map = state.map
    held_fmt1 = state.capture_format[1]

    state.update(stream_ids=tuple([10, 11]), map=np.arange(8))
    assert state.stream_ids is held_sids
    assert state.capture_format is held_fmt
    assert state.capture_format[1] is held_fmt1
    assert state.map is held_map
    assert not state.has_changed(context="t")

    state.update(stream_ids=(10, 12))
    fmt1.update(fmt_bus_width=32)
    assert state.stream_ids == (10, 12)
    assert state.capture_format is held_fmt
    assert state.capture_format[1] is held_fmt1
    assert state.capture_format[1].fmt_bus_width == 32
    assert state.has_changed("stream_ids", context="t")
    assert state.capture_format[1].has_changed("fmt_bus_width", context="t")
    assert state.has_changed("capture_format", context="t")


def test_update_rejects_nested_values():
    state = State()
    with pytest.raises(TypeError, match="update_from"):
        state.update(raw={"mode": 0})
    with pytest.raises(TypeError, match="update_from"):
        state.update(raw=State(mode=0))


def test_update_from_merges_serialized_dicts_without_false_dirty():
    import numpy as np

    fmt = State(fmt_bus_width=8, fmt_data_width=8, fmt_is_complex=False)
    state = State()
    state.update(stream_ids=(10, 11), bin_map=np.arange(8))
    state.capture_format = [fmt]
    state.ack(context="t")
    held_fmt = state.capture_format[0]
    held_fmt.ack(context="t")
    held_map = state.bin_map

    state.update_from(state.as_dict())
    assert state.capture_format[0] is held_fmt
    assert state.bin_map is held_map
    assert not state.has_changed(context="t")
    assert not held_fmt.has_changed(context="t")

    payload = state.as_dict()
    payload["stream_ids"] = [10, 12]
    payload["capture_format"][0]["fmt_data_width"] = 4
    state.update_from(payload)
    assert state.has_changed("stream_ids", context="t")
    assert held_fmt.has_changed("fmt_data_width", context="t")
    assert not held_fmt.has_changed("fmt_bus_width", context="t")


def test_update_from_rejects_state_patch_values():
    src = State(mode=0)
    dst = State()
    with pytest.raises(TypeError, match="direct assignment"):
        dst.update_from({"raw": src})


def test_update_from_merges_into_existing_child():
    child = State(mode=0)
    parent = State()
    parent.raw = child
    parent.ack(context="t")
    parent_gen = parent._gen

    parent.update_from({"raw": {"mode": 0}})
    assert parent.raw is child
    assert parent._gen == parent_gen
    assert not parent.has_changed(context="t")

    parent.update_from({"raw": {"mode": 1}})
    assert parent.raw is child
    assert parent._gen == parent_gen
    assert parent.has_changed(context="t")
    assert child.has_changed("mode", context="t")


def test_getattr_default_for_missing_key():
    state = State()

    assert getattr(state, "bin_map", None) is None
    assert "bin_map" not in state
    assert not hasattr(state, "bin_map")
    with pytest.raises(AttributeError):
        _ = state.bin_map
    with pytest.raises(KeyError):
        _ = state["bin_map"]

    state.bin_map = [0, 1]
    assert getattr(state, "bin_map", None) == [0, 1]
    assert state.has_attr("bin_map")
    assert hasattr(state, "bin_map")


def test_has_attr_requires_all_names():
    state = State(bin_map=[0, 1])

    assert state.has_attr("bin_map")
    assert not state.has_attr("prod_map")
    assert not state.has_attr("bin_map", "prod_map")

    state.prod_map = [2, 3]
    assert state.has_attr("bin_map", "prod_map")

    with pytest.raises(TypeError):
        state.has_attr()


def test_has_changed_sees_in_place_child_updates():
    fmt = State(fmt_bus_width=8)
    state = State()
    state.update(stream_ids=(10, 11))
    state.capture_format = [fmt]
    state.ack(context="t")

    assert not state.has_changed(context="t")
    assert not state.has_changed("capture_format", context="t")

    state.capture_format[0].fmt_bus_width = 16

    assert state.has_changed("capture_format", context="t")
    assert state.has_changed(context="t")
    assert state.capture_format[0].has_changed("fmt_bus_width", context="t")

    state.ack(context="t")
    assert not state.has_changed(context="t")
    assert not state.has_changed("capture_format", context="t")


def test_has_changed_no_false_positive_on_equal_update():
    import numpy as np

    fmt = State(fmt_bus_width=8, fmt_data_width=8)
    root = State()
    root.update(target_mode=0, stream_ids=(0, 16), bin_map=np.arange(4))
    root.capture_format = [fmt]
    root.ack(context="arr")

    root.update_from(
        {
            "target_mode": 0,
            "stream_ids": (0, 16),
            "capture_format": [{"fmt_bus_width": 8, "fmt_data_width": 8}],
            "bin_map": np.arange(4),
        }
    )
    assert not root.has_changed(context="arr")

    root.update(target_mode=3)
    assert root.has_changed(context="arr")
    assert root.has_changed("target_mode", context="arr")
    assert not root.has_changed("stream_ids", context="arr")


def test_get_common_attr_match_returns_first_identity():
    fmt = State(bits=8)
    a = State(source=1, fmt=fmt)
    b = State(source=1, fmt=fmt)
    assert State.get_common_attr([a, b], "source") == 1
    assert State.get_common_attr([a, b], "fmt") is fmt


def test_get_common_attr_mismatch_raises():
    a = State(source=1)
    b = State(source=2)
    with pytest.raises(ValueError, match="source"):
        State.get_common_attr([a, b], "source")


def test_get_common_attr_empty_raises():
    with pytest.raises(ValueError, match="no states"):
        State.get_common_attr([], "source")


def test_get_common_attr_ndarray_match():
    import numpy as np

    arr = np.arange(4)
    a = State(bin_map=arr)
    b = State(bin_map=np.arange(4))
    result = State.get_common_attr([a, b], "bin_map")
    assert result is arr
    assert np.array_equal(result, np.arange(4))


def test_get_common_attr_ndarray_mismatch():
    import numpy as np

    a = State(bin_map=np.arange(4))
    b = State(bin_map=np.arange(4) + 1)
    with pytest.raises(ValueError, match="bin_map"):
        State.get_common_attr([a, b], "bin_map")
