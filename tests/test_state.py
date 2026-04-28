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


def test_state_detects_in_place_mutable_changes_by_comparing_snapshots():
    state = State()
    context = Context()
    state.table = {"a": []}
    state.ack("table", context=context)

    state.table["a"].append(1)

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
