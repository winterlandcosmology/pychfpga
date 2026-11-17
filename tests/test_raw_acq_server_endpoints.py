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


class _RaisingOnceProcessor:
    def __init__(self):
        self.calls = 0

    def process_udp_packets(self):
        self.calls += 1
        if self.calls == 1:
            raise IndexError('index 8 is out of bounds for axis 2 with size 2')


def _receiver_with(processor):
    from pychfpga.raw_acq.receiver import RawAcqReceiver
    receiver = object.__new__(RawAcqReceiver)
    receiver.log = logging.getLogger('test')
    receiver.name = 'raw_acq0Recv'  # used by repr()
    receiver.processors = {"raw": processor}
    receiver.n_stored = 10
    receiver.received_packets = 0
    receiver.packet_max_processing_time = 0
    receiver.packet_current_processing_time = 0
    receiver.processing_errors = 0
    receiver.last_processing_error = None
    return receiver


def test_process_packets_drops_a_failing_batch_and_keeps_going(caplog):
    processor = _RaisingOnceProcessor()
    receiver = _receiver_with(processor)

    with caplog.at_level(logging.ERROR, logger='test'):
        receiver.process_packets()  # must not raise: that used to end the receive thread
    receiver.process_packets()

    assert processor.calls == 2
    assert receiver.processing_errors == 1
    assert 'IndexError' in receiver.last_processing_error
    assert caplog.records[0].exc_info is not None  # the full traceback is logged


def test_status_reports_the_receive_thread_and_errors():
    receiver = SimpleNamespace(
        is_running=lambda: True, is_receiving=lambda: False,
        processing_errors=3, last_processing_error="IndexError('x')")

    result = asyncio.run(_server(receiver).status())

    assert result == dict(started=True, thread_alive=False, processing_errors=3,
                          last_processing_error="IndexError('x')")
    assert asyncio.run(_server(None).status()) == dict(started=False)
