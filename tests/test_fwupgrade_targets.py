import pytest

from pychfpga.hardware.crs.arm_firmware.fwupgrade import (
    beacon_matches_targets,
    parse_fwupgrade_targets,
    tokenize_fwupgrade_argv,
)
from pychfpga.hardware.crs.crs_discovery import CRSBeacon


def test_tokenize_fwupgrade_argv_joins_and_splits():
    assert tokenize_fwupgrade_argv(['1', '2', '3']) == ['1', '2', '3']
    assert tokenize_fwupgrade_argv(['CRS  37   55']) == ['CRS', '37', '55']
    assert tokenize_fwupgrade_argv(['*']) == ['*']


def test_parse_fwupgrade_targets_default_board_part():
    targets = parse_fwupgrade_targets(['1', '2', '3'])
    assert targets == [
        ('CRS', '1'),
        ('CRS', '2'),
        ('CRS', '3'),
    ]


def test_parse_fwupgrade_targets_mixed_parts():
    tokens = ['1', '2', '3', 'CCR?', '10', '0012', '013', 'CBP', '69', 'CRS', '12*']
    targets = parse_fwupgrade_targets(tokens)
    assert ('CRS', '1') in targets
    assert ('CCR?', '10') in targets
    assert ('CCR?', '0012') in targets
    assert ('CBP', '69') in targets
    assert ('CRS', '12*') in targets


def test_beacon_matches_board_serial_without_leading_zeros():
    targets = parse_fwupgrade_targets(['37'])
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 4\n"
        b"Serial: 0037\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n\n")
    assert beacon_matches_targets(beacon, targets)


def test_beacon_matches_crate_or_backplane_identity():
    targets = parse_fwupgrade_targets(['CCR', '11', 'CBP', '69'])
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Manufacturer: t0\n"
        b"Product: crs\n"
        b"Revision: 4\n"
        b"Serial: 0146\n"
        b"Bootloader: u-boot\n"
        b"Bootloader Version: 0.1\n"
        b"Backplane-Part: CBP\n"
        b"Backplane-Serial: 069\n"
        b"Crate-Part: CCR\n"
        b"Crate-Serial: 011\n\n")
    assert beacon_matches_targets(beacon, targets)


def test_beacon_serial_wildcard_on_board_identity():
    targets = parse_fwupgrade_targets(['12*'])
    for serial in ('0120', '0122', '0123'):
        beacon = CRSBeacon(
            b"FWREQ/1\n"
            b"Manufacturer: t0\n"
            b"Product: crs\n"
            b"Revision: 4\n"
            b"Serial: %s\n"
            b"Bootloader: u-boot\n"
            b"Bootloader Version: 0.1\n\n" % serial.encode())
        assert beacon_matches_targets(beacon, targets)


def test_parse_requires_at_least_one_target():
    with pytest.raises(ValueError):
        parse_fwupgrade_targets([])


def test_parse_lone_wildcard_matches_all():
    assert parse_fwupgrade_targets(['*']) == [('CRS', '*')]


def test_parse_part_with_serial_wildcard():
    assert parse_fwupgrade_targets(['CCR', '*']) == [('CCR', '*')]


def test_beacon_matches_crc4_crate_case_insensitive_part():
    targets = parse_fwupgrade_targets(['crc4', '1'])
    beacon = CRSBeacon(
        b"TCPIPE/1\n"
        b"Product: crs\n"
        b"Serial: 0055\n"
        b"Crate Model: CRC4\n"
        b"Crate Serial: 001\n\n")
    assert beacon_matches_targets(beacon, targets)


def test_beacon_matches_crc4_crate_with_crc_question_mark():
    targets = parse_fwupgrade_targets(['CRC?', '1'])
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Product: crs\n"
        b"Serial: 0146\n"
        b"Crate Model: CRC4\n"
        b"Crate Serial: 001\n\n")
    assert beacon_matches_targets(beacon, targets)


def test_beacon_matches_chassis_only_crate_fields():
    targets = parse_fwupgrade_targets(['CRC?', '1'])
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Product: crs\n"
        b"Serial: 0146\n"
        b"Chassis Model: CRC4\n"
        b"Chassis Serial: 001\n\n")
    assert beacon_matches_targets(beacon, targets)


def test_lone_wildcard_matches_crs_board_serial():
    targets = parse_fwupgrade_targets(['*'])
    beacon = CRSBeacon(
        b"FWREQ/1\n"
        b"Product: crs\n"
        b"Serial: 9999\n\n")
    assert beacon_matches_targets(beacon, targets)
