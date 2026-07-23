import asyncio

from pychfpga.raw_acq.client import RawAcqAsyncRESTClient
from pychfpga.raw_acq.server import RawAcqAsyncRESTServer


class _Log:
    def info(self, *args, **kwargs):
        pass


class _Receiver:
    def __init__(self):
        self.updates = []
        self.processors = {}

    def update_config(self, config):
        self.updates.append(config)


def test_raw_acq_client_envelopes_firmware_raw_section():
    client = object.__new__(RawAcqAsyncRESTClient)
    client.log = _Log()
    client.hostname = "localhost"
    client.port = 54322
    calls = []

    async def post(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {"ok": True}

    client.post = post
    config = {"raw": {"frames_per_burst": 4}, "corr": {"NCHAN": 16}}

    result = asyncio.run(client.update_config(config))

    assert result == {"ok": True}
    assert calls == [("update-config", {"config": config})]


def test_raw_acq_server_unwraps_firmware_config():
    server = object.__new__(RawAcqAsyncRESTServer)
    server.log = _Log()
    server.address = "localhost"
    server.port = 54322
    server.receiver = _Receiver()
    config = {"raw": {"frames_per_burst": 4}, "corr": {"NCHAN": 16}}

    asyncio.run(server.update_config(config=config))

    assert server.receiver.updates == [config]


def test_raw_acq_server_preserves_direct_section_patches():
    server = object.__new__(RawAcqAsyncRESTServer)
    server.log = _Log()
    server.address = "localhost"
    server.port = 54322
    server.receiver = _Receiver()
    raw = {"frames_per_burst": 4}

    asyncio.run(server.update_config(raw=raw))

    assert server.receiver.updates == [{"raw": raw}]


def test_raw_acq_server_prefers_enveloped_config():
    server = object.__new__(RawAcqAsyncRESTServer)
    server.log = _Log()
    server.address = "localhost"
    server.port = 54322
    server.receiver = _Receiver()
    config = {"raw": {"frames_per_burst": 4}}

    asyncio.run(server.update_config(config=config, corr={"NCHAN": 16}))

    assert server.receiver.updates == [config]
