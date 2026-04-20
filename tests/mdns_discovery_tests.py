"""Regression test for repeated calls to ``mdns_discover``.

The test patches the zeroconf transport classes used by
``pychfpga.mdns_discovery`` so that the function can be exercised offline.
A stub ``AsyncServiceBrowser`` synthesizes a burst of service announcements
each time it is constructed; the per-service handler is artificially slowed
down so that some of its tasks are still in flight when ``mdns_discover``
starts tearing the browser/Zeroconf instance down.

The regression we are guarding against: prior to the fix, each call leaked
the in-flight handler coroutines (created via ``asyncio.ensure_future``).
On a long-lived event loop the leaked tasks accumulated across calls and
eventually caused discovery to misbehave.

Run with::

    python -m pytest tests/mdns_discovery_tests.py
    # or, without pytest:
    python tests/mdns_discovery_tests.py
"""

# Standard library
import asyncio
import logging
import sys
import unittest
from unittest import mock


class _FakeAsyncZeroconf:
    """Minimal stand-in for ``zeroconf.asyncio.AsyncZeroconf``."""

    def __init__(self, *args, **kwargs):
        self.zeroconf = object()
        self.closed = False

    async def async_close(self):
        self.closed = True


class _FakeAsyncServiceInfo:
    """``AsyncServiceInfo`` stub whose ``async_request`` sleeps.

    Sleeping ensures the handler task is still active when the discovery
    loop hits its timeout and starts shutting down, exercising the
    drain-on-shutdown path.
    """

    sleep_time = 0.5
    no_ipv4 = True

    def __init__(self, service_type, name):
        self.service_type = service_type
        self.name = name
        self.properties = {}
        self.port = 0

    async def async_request(self, zeroconf, timeout_ms):
        await asyncio.sleep(self.sleep_time)
        return True

    def parsed_addresses(self, version=None):
        return [] if self.no_ipv4 else ['127.0.0.1']

    def addresses_by_version(self, version=None):
        return [] if self.no_ipv4 else [b'\x7f\x00\x00\x01']

    def __bool__(self):
        return True


class _FakeAsyncServiceInfoFast(_FakeAsyncServiceInfo):
    """Variant that returns immediately so handlers complete before drain."""

    sleep_time = 0
    no_ipv4 = False  # provide an IPv4 so the handler gets further along


class _FakeAsyncServiceInfoRaises(_FakeAsyncServiceInfoFast):
    """Variant that always raises during async_request."""

    async def async_request(self, zeroconf, timeout_ms):
        raise RuntimeError('simulated handler failure')


class _FakeAsyncServiceBrowser:
    """Browser stub that synthesizes a burst of ``Added`` events."""

    def __init__(self, zeroconf, service_types, handlers=None, delay=0):
        from zeroconf import ServiceStateChange

        self._cancelled = False
        # Fire a small burst of state-change events on the running loop so
        # the handlers register pending tasks before the discovery loop's
        # timeout expires.
        loop = asyncio.get_event_loop()
        for handler in handlers or []:
            for i in range(5):
                loop.call_soon(
                    handler,
                    zeroconf,
                    '_tuber-jsonrpc._tcp.local.',
                    f'fake-svc-{i}._tuber-jsonrpc._tcp.local.',
                    ServiceStateChange.Added,
                )

    async def async_cancel(self):
        self._cancelled = True


class _CountingAsyncServiceBrowser:
    """Browser stub used by the retry test.

    Tracks how many times a browser is constructed (i.e., how many discovery
    attempts ``mdns_discover`` makes). Fires a single ``Added`` event on the
    Nth construction so the test can verify retries happen until success.
    """

    construction_count = 0
    succeed_on_attempt = 2  # 1-based: succeed on the 2nd attempt

    def __init__(self, zeroconf, service_types, handlers=None, delay=0):
        from zeroconf import ServiceStateChange

        self._cancelled = False
        type(self).construction_count += 1
        if type(self).construction_count >= type(self).succeed_on_attempt:
            loop = asyncio.get_event_loop()
            for handler in handlers or []:
                loop.call_soon(
                    handler,
                    zeroconf,
                    '_tuber-jsonrpc._tcp.local.',
                    'crs-37._tuber-jsonrpc._tcp.local.',
                    ServiceStateChange.Added,
                )

    async def async_cancel(self):
        self._cancelled = True


class _FakeAsyncServiceInfoCRS37(_FakeAsyncServiceInfo):
    """ServiceInfo stub that returns a valid CRS-like board record."""

    sleep_time = 0
    no_ipv4 = False

    @property
    def properties(self):
        return {
            b'motherboard-serial': b'0037',
            b'motherboard-part': b'crs',
        }

    @properties.setter
    def properties(self, _):
        pass


def _patch_mdns(module, info_cls=_FakeAsyncServiceInfo):
    """Return a context manager that patches the zeroconf surface used by ``mdns_discover``."""
    return mock.patch.multiple(
        module,
        AsyncZeroconf=_FakeAsyncZeroconf,
        AsyncServiceBrowser=_FakeAsyncServiceBrowser,
        AsyncServiceInfo=info_cls,
    )


class RepeatedMdnsDiscoverTests(unittest.TestCase):
    """Verify that repeated ``mdns_discover`` calls do not leak tasks."""

    def test_repeated_calls_do_not_leak_tasks(self):
        # Imported lazily so the patch targets the module's attributes.
        from pychfpga import mdns_discovery

        async def runner():
            baseline = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

            with _patch_mdns(mdns_discovery):
                for _ in range(5):
                    await mdns_discovery.mdns_discover(
                        iceboards=[('*', '*')],
                        icecrates=[],
                        timeout=0.3,
                    )

            # Allow any done callbacks to settle, then check task population.
            await asyncio.sleep(0)
            leftover = {
                t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and t not in baseline
            }
            self.assertEqual(
                leftover, set(),
                msg=f'mdns_discover leaked {len(leftover)} task(s) across repeated calls: {leftover}'
            )

        asyncio.run(runner())

    def test_handler_exceptions_are_retrieved(self):
        """Handler exceptions must be retrieved so they don't trigger the
        loop's "Task exception was never retrieved" handler. On Python 3.12
        with nest_asyncio (Jupyter Lab) we have observed that orphan
        handler-task exceptions from one ``mdns_discover`` call can interfere
        with subsequent calls' discovery. The drain logic in ``finally``
        retrieves the exception from every completed handler task so they
        are GC'd cleanly."""

        from pychfpga import mdns_discovery

        unhandled = []

        async def runner():
            loop = asyncio.get_running_loop()
            prev_handler = loop.get_exception_handler()

            def capture(loop, context):
                unhandled.append(context)

            loop.set_exception_handler(capture)
            try:
                with _patch_mdns(mdns_discovery, info_cls=_FakeAsyncServiceInfoRaises):
                    for _ in range(3):
                        await mdns_discovery.mdns_discover(
                            iceboards=[('*', '*')],
                            icecrates=[],
                            timeout=0.3,
                        )
                # Force a GC pass to flush any "Task exception was never
                # retrieved" warnings that would otherwise be emitted from
                # Task.__del__.
                import gc
                for _ in range(3):
                    await asyncio.sleep(0)
                    gc.collect()
            finally:
                loop.set_exception_handler(prev_handler)

        asyncio.run(runner())

        # No unretrieved task exceptions should have been routed through the
        # loop's exception handler.
        leaked = [
            ctx for ctx in unhandled
            if 'Task exception was never retrieved' in (ctx.get('message') or '')
            or 'never retrieved' in (ctx.get('message') or '')
        ]
        self.assertEqual(
            leaked, [],
            msg=f'mdns_discover left {len(leaked)} unretrieved handler-task exception(s): {leaked}'
        )

    def test_retries_when_expected_target_not_found(self):
        """If an explicitly-requested board doesn't reply on the first attempt,
        ``mdns_discover`` must retry (up to ``retries`` extra times) before
        giving up. This protects against transient board ARM / TCPIPE
        unresponsiveness."""

        from pychfpga import mdns_discovery

        # Stub Motherboard / Crate registry plumbing the handler needs.
        # Patch the bits we need so the handler's get_unique_instance /
        # get_class_by_ipmi_part_number calls don't fail.
        recorded = {'created': []}

        class _StubIB:
            part_number = 'CRS'
            NUMBER_OF_SLOTS = 1
            def __init__(self, serial=None, hostname=None):
                self.serial = serial
                self.hostname = hostname
                self.slot = None
                self.crate = None
                recorded['created'].append(self)
            def get_crate_number(self):
                return None
            def update_instance(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)
                return self

        class _StubIBCls:
            @staticmethod
            def get_unique_instance(serial=None, hostname=None, **kw):
                for ib in recorded['created']:
                    if ib.serial == serial:
                        ib.hostname = hostname
                        return ib
                return _StubIB(serial=serial, hostname=hostname)

        _CountingAsyncServiceBrowser.construction_count = 0
        _CountingAsyncServiceBrowser.succeed_on_attempt = 2

        async def runner():
            with mock.patch.multiple(
                mdns_discovery,
                AsyncZeroconf=_FakeAsyncZeroconf,
                AsyncServiceBrowser=_CountingAsyncServiceBrowser,
                AsyncServiceInfo=_FakeAsyncServiceInfoCRS37,
            ), mock.patch.object(
                mdns_discovery.Motherboard,
                'get_class_by_ipmi_part_number',
                staticmethod(lambda pn: _StubIBCls),
            ), mock.patch.object(
                mdns_discovery.Motherboard,
                'get_all_registered_instances',
                classmethod(lambda cls: list(recorded['created'])),
            ), mock.patch.object(
                mdns_discovery.Crate,
                'get_all_registered_instances',
                classmethod(lambda cls: []),
            ):
                ibs, _ = await mdns_discovery.mdns_discover(
                    iceboards=[('CRS', '0037')],
                    icecrates=[],
                    timeout=0.3,
                    retries=3,
                )
            return ibs

        ibs = asyncio.run(runner())

        self.assertEqual(
            _CountingAsyncServiceBrowser.construction_count, 2,
            msg='mdns_discover should have retried exactly once before succeeding'
        )
        self.assertEqual(len(ibs), 1)
        self.assertEqual(ibs[0].serial, '0037')

    def test_no_retry_for_pure_wildcard_search(self):
        """Wildcard-only searches have no completion criterion, so they should
        run exactly once even if ``retries > 0``."""

        from pychfpga import mdns_discovery

        _CountingAsyncServiceBrowser.construction_count = 0
        _CountingAsyncServiceBrowser.succeed_on_attempt = 999  # never satisfies

        async def runner():
            with mock.patch.multiple(
                mdns_discovery,
                AsyncZeroconf=_FakeAsyncZeroconf,
                AsyncServiceBrowser=_CountingAsyncServiceBrowser,
                AsyncServiceInfo=_FakeAsyncServiceInfo,
            ):
                await mdns_discovery.mdns_discover(
                    iceboards=[('*', '*')],
                    icecrates=[],
                    timeout=0.2,
                    retries=3,
                )

        asyncio.run(runner())

        self.assertEqual(
            _CountingAsyncServiceBrowser.construction_count, 1,
            msg='mdns_discover should not retry pure-wildcard searches'
        )


if __name__ == '__main__':
    unittest.main()
