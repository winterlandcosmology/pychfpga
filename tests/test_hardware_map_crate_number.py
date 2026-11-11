import pytest

from pychfpga.hardware import Crate, HardwareMap, Motherboard
from pychfpga.hardware.ice.icecrate import IceCrate_MGK7BP16
from pychfpga.hardware.ice.iceboard import IceBoard


@pytest.fixture(autouse=True)
def clean_hardware_map():
    HardwareMap.clear_hardware_map()
    yield
    HardwareMap.clear_hardware_map()


def test_explicit_crate_number_attaches_a_board_given_by_hostname():
    # Regression: update_instance() used to overwrite its crate_number argument
    # with the board's current (None) crate number, so the crate field of a
    # 'hostname:slot:crate' hardware map entry was silently dropped.
    crate = Crate.get_unique_instance(new_class=IceCrate_MGK7BP16, serial='012', crate_number=0)
    board = Motherboard.get_unique_instance(new_class=IceBoard, hostname='10.10.2.4', slot=5, crate_number=0)
    assert board.crate is crate
    assert board.get_crate_number() == 0
    assert crate.slot[5] is board


def test_explicit_crate_number_without_a_crate_creates_a_generic_one():
    board = Motherboard.get_unique_instance(new_class=IceBoard, hostname='10.10.2.4', slot=5, crate_number=1)
    assert board.get_crate_number() == 1
    assert board.crate.part_number is None  # resolved later by crate discovery


def test_board_without_crate_number_keeps_its_crate():
    crate = Crate.get_unique_instance(new_class=IceCrate_MGK7BP16, serial='012', crate_number=0)
    board = Motherboard.get_unique_instance(new_class=IceBoard, hostname='10.10.2.4', slot=5, crate_number=0)
    board.update_instance(serial='0513')
    assert board.crate is crate
    assert board.get_crate_number() == 0
