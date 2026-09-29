import pytest

from pychfpga.hardware import HardwareMap
from pychfpga.hardware.crs.crs import CRS
from pychfpga.hardware.interfaces.tcpipe import TCPipe


class _FakeTCPipe:
    IPMI_FIELD_WIRE_LEN = TCPipe.IPMI_FIELD_WIRE_LEN

    def __init__(self, backplane_info):
        self._backplane_info = backplane_info

    async def get_backplane_ipmi_info_async(self):
        return self._backplane_info

    async def get_motherboard_ipmi_info_async(self):
        return {
            'version': 1,
            'manufacturer': 't0',
            'product': 'crs',
            'revision': '5',
            'serial': '0037',
            'manufacturing_date': '2026-01-01',
        }


@pytest.fixture(autouse=True)
def clear_hardware_map():
    HardwareMap.clear_hardware_map()
    yield
    HardwareMap.clear_hardware_map()


@pytest.mark.asyncio
async def test_crs_discover_slot_from_tcpipe_backplane_ipmi():
    mb = CRS.get_unique_instance(hostname='192.0.2.1', serial='0037')
    mb.tcpipe = _FakeTCPipe({
        'version': 1,
        'crate_slot': 3,
        'flags': TCPipe.BACKPLANE_IPMI_FLAG_PRESENT,
        'backplane_present': True,
        'bp_manufacturer': 't0.technology',
        'bp_part': '4SBP',
        'bp_serial': '000',
        'bp_revision': '2',
        'crate_manufacturer': 't0.technology',
        'crate_model': 'CRC4',
        'crate_revision': '1',
        'crate_serial': '001',
        'chassis_part': 'CRC4',
        'chassis_serial': '001',
    })

    slot = await mb.discover_slot_async(update=True)

    assert slot == 3
    assert mb.slot == 3


@pytest.mark.asyncio
async def test_crs_discover_crate_from_tcpipe_backplane_ipmi():
    mb = CRS.get_unique_instance(hostname='192.0.2.2', serial='0038')
    mb.tcpipe = _FakeTCPipe({
        'version': 1,
        'crate_slot': 1,
        'flags': TCPipe.BACKPLANE_IPMI_FLAG_PRESENT,
        'backplane_present': True,
        'crate_model': 'CRC4',
        'crate_serial': '001',
        'chassis_part': '',
        'chassis_serial': '',
    })

    crate = await mb.discover_crate_async(update=True)

    assert crate is not None
    assert crate.part_number == 'CRC4'
    assert crate.serial == '001'
    assert mb.crate is crate
