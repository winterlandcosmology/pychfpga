import asyncio
import logging
from types import SimpleNamespace

from pychfpga.raw_acq.server import RawAcqAsyncRESTServer


class _FakeADC:
    async def get_adc_rms_async(self):
        return [((0, 8, 0), 12.5, 0.48), ((0, 8, 1), 12.5, 0.42)]


def _server(receiver):
    server = object.__new__(RawAcqAsyncRESTServer)
    server.receiver = receiver
    server.log = logging.getLogger('test')
    server.address, server.port = '', 54322  # used by repr()
    return server


def test_get_rms_reads_the_adc_processor_under_raw():
    receiver = SimpleNamespace(
        processors={"raw": SimpleNamespace(adc=_FakeADC()), "corr": object()},
        is_running=lambda: True)

    result = asyncio.run(_server(receiver).get_rms())

    assert result == dict(rms=[((0, 8, 0), 12.5, 0.48), ((0, 8, 1), 12.5, 0.42)])


def test_get_rms_is_empty_when_the_receiver_is_not_running():
    assert asyncio.run(_server(None).get_rms()) == dict(rms=[])
    stopped = SimpleNamespace(processors={}, is_running=lambda: False)
    assert asyncio.run(_server(stopped).get_rms()) == dict(rms=[])
