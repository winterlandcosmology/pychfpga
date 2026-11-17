import asyncio
from types import SimpleNamespace

import numpy as np

from pychfpga.fpga_master import FPGAMasterAsyncRESTServer


class _FakeArray:
    def __init__(self):
        self.calls = []

    def get_frequency_map(self, format='l:bb'):
        self.calls.append(('get_frequency_map', format))
        return {'format': format, 'bins': np.arange(3)}

    async def get_gains_async(self, bank=0):
        self.calls.append(('get_gains_async', bank))
        return [bank]

    def _private(self):
        self.calls.append(('_private',))


def _server(fpgas):
    server = object.__new__(FPGAMasterAsyncRESTServer)
    server.fpga_master = SimpleNamespace(fpgas=fpgas)
    return server


def test_calls_a_sync_method_with_kwargs_only():
    fpgas = _FakeArray()

    result = asyncio.run(_server(fpgas).call_fpga_array_method(
        'get_frequency_map', kwargs={'format': 'l:cscb'}))

    assert fpgas.calls == [('get_frequency_map', 'l:cscb')]  # method_name is not forwarded
    assert result == {'format': 'l:cscb', 'bins': [0, 1, 2]}


def test_awaits_an_async_method():
    fpgas = _FakeArray()

    result = asyncio.run(_server(fpgas).call_fpga_array_method('get_gains_async', kwargs={'bank': 1}))

    assert result == [1]
    assert fpgas.calls == [('get_gains_async', 1)]


def test_refuses_private_attributes():
    fpgas = _FakeArray()

    result = asyncio.run(_server(fpgas).call_fpga_array_method('_private'))

    assert 'error' in result
    assert fpgas.calls == []


def test_reports_a_missing_array():
    result = asyncio.run(_server(None).call_fpga_array_method('get_frequency_map'))

    assert result == dict(error='FPGA array is not created yet')
