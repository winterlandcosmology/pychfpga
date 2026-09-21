'''Session-level hooks for the firmware test suite.

Holds two things that have to live in a conftest:

- a guard that stops ``wtl.pytest_xreport`` from destroying pytest's terminal
  summary and suppressing the HTML report (see :func:`pytest_sessionfinish`), and
- the crate temperature / control-plane telemetry logger (see
  :func:`crate_telemetry`).

Note that ``test_setup.py`` in this directory is dead code: pytest.ini's
``python_files`` never collects it and nothing imports it, so the fixtures it
defines are never registered. The real per-session connection cache is
``TestModule.board_conn`` (module_test.py), keyed on ``xr.data``.
'''

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session, exitstatus):
    '''Work around an xreport/pytest-html hook-ordering bug, two ways.

    ``wtl.pytest_xreport.xreport.pytest_sessionfinish`` moves the HTML report to
    its final folder with ``os.renames()``, but it declares no hookimpl
    priority, so it runs at *normal* priority - while pytest-html, which is what
    actually writes that file, declares ``trylast``. pluggy runs non-wrapper
    impls ``tryfirst -> normal -> trylast``, so xreport always renames a file
    that does not exist yet. This is deterministic, not a race: every run.

    Two separate things break as a result, and this hook fixes both.

    1. *The report is never written.* pluggy skips all remaining impls once one
       raises, so pytest-html's ``trylast`` hook never runs. It still prints the
       report path in its terminal summary, which is actively misleading.
       Creating the file before the inner impls run makes xreport's rename a
       successful no-op - source and destination are the same path in this
       configuration - so the chain continues and pytest-html overwrites the
       placeholder with the real report. ``os.renames`` then tries to
       ``removedirs`` the source parent, which fails harmlessly because the
       directory still holds the JUnit XML and the asset folders.

    2. *The failure summary is lost.* ``TerminalReporter``'s own
       ``pytest_sessionfinish`` is a hook wrapper that calls
       ``pytest_terminal_summary`` after its ``yield`` returns, so an escaping
       exception skips the summary entirely - no "short test summary info" and
       no tracebacks, turning every failing run into a mystery. (The JUnit XML
       is still written, which is the only reason the earlier shuffle256
       failures could be recovered at all.) Catching the error below restores
       the summary even if step 1 did not apply.

    ``trylast=True`` is load-bearing for the second half. Wrappers run
    outermost-first in reverse registration order, and a conftest registers
    *after* TerminalReporter, so a plain wrapper would sit outside
    TerminalReporter's and swallowing the error there would still lose the
    summary. ``trylast`` makes this the innermost wrapper, catching the error
    before it unwinds that far.

    Only FileNotFoundError is caught, so any other session-finish failure still
    propagates normally.
    '''
    # Runs before every non-wrapper sessionfinish impl, including xreport's.
    htmlpath = getattr(session.config.option, 'htmlpath', None)
    if htmlpath:
        path = Path(htmlpath)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except OSError as exc:
            print(f'conftest: could not pre-create {path}: {exc!r}')

    try:
        return (yield)
    except FileNotFoundError as exc:
        # Print rather than log: logging is already being torn down by now.
        print(f'\nconftest: suppressed a FileNotFoundError raised during '
              f'pytest_sessionfinish so the test summary below survives.\n'
              f'          This is the known wtl.pytest_xreport/pytest-html hook '
              f'ordering bug: {exc}')
        return None


# --------------------------------------------------------------------------
# Crate temperature / control-plane telemetry
# --------------------------------------------------------------------------

TELEMETRY_PERIOD_S = 5.0
TELEMETRY_DIR = Path(__file__).resolve().parent.parent / 'fw_test_results'

# Motherboard sensors, hottest-running first. MB_PHY is the closest sensor to
# the SFP cage and so the best available proxy for SFP module temperature -
# the modules themselves expose no I2C/DOM path on this board.
MB_SENSOR_NAMES = ('MB_FPGA_DIE', 'MB_FPGA', 'MB_ARM', 'MB_PHY', 'MB_POWER')
BP_SENSOR_NAMES = ('BP_SLOT1', 'BP_SLOT16')


class _CrateTelemetry:
    '''Poll board temperatures on the ARM path while tests run.

    Exists to answer whether the SFP link failures and the FPGA MMI
    control-plane timeouts track a thermal ramp. Three constraints shape the
    implementation, and violating any of them would corrupt the very
    measurement it is trying to make:

    1. It polls ONLY ``tuber_*`` calls, which go over HTTP to the board's ARM
       and are independent of the FPGA UDP MMI socket the tests use. In
       particular it must never reach ``clear_fpga_udp_errors()``, which fires
       once ``mmi.error_counter`` passes 40 and then resets the SFP and the
       FPGA UDP stack - a thermally marginal link is exactly what pushes that
       counter up, so polling FPGA metrics here would reset the hardware
       mid-test and destroy the evidence. For the same reason
       ``mmi.error_counter`` is read as a plain attribute rather than through
       ``get_fpga_udp_metrics_async()``, which performs an MMI read.

    2. It never touches QSFP module temperature. ``board.qsfp`` runs over the
       FPGA's I2C master on the MMI path and mutates shared GPIO/port-select
       state, so it is not safe to sample concurrently with anything.

    3. It runs in a thread with its own event loop rather than as an asyncio
       Task. The tests are synchronous and reach hardware through ``run_async``
       (nest_asyncio + ``asyncio.run``), so a Task would only advance while some
       unrelated call happened to be spinning the loop. The thread must touch
       only ARM calls: the FPGA MMI socket is shared per board and carries a
       sequence counter, so it is not thread-safe.
    '''

    def __init__(self, xr_session, logger, period=TELEMETRY_PERIOD_S):
        self.xr = xr_session
        self.logger = logger
        self.period = period
        self._stop = threading.Event()
        self._thread = None
        self._csv_path = None
        self._fh = None
        self._writer = None
        self._sensors = None
        self._ticks = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name='crate-telemetry', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.period + 10)
        if self._fh is not None:
            self._fh.close()
        if self._ticks:
            self.logger.info(f'crate telemetry: wrote {self._ticks} samples to {self._csv_path}')

    # -- internals ---------------------------------------------------------

    def _get_array(self):
        '''The crate is connected by the first test, not before, so wait for it.'''
        return getattr(self.xr.data, 'array', None) if self.xr is not None else None

    def _resolve_sensors(self, board):
        '''Confirm which sensor names this ARM firmware actually exposes.

        TEMPERATURE_SENSOR is a remote Tuber property, not a local enum, so the
        valid names have to be read off the board rather than hardcoded.
        '''
        try:
            available = set(vars(board.TEMPERATURE_SENSOR))
        except Exception:                                      # noqa: BLE001
            available = set(MB_SENSOR_NAMES) | set(BP_SENSOR_NAMES)
        mb = [n for n in MB_SENSOR_NAMES if n in available] or list(MB_SENSOR_NAMES)
        bp = [n for n in BP_SENSOR_NAMES if n in available]
        return mb, bp

    def _open_csv(self, mb_names, bp_names):
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self._csv_path = TELEMETRY_DIR / f'crate_telemetry_{stamp}.csv'
        self._fh = self._csv_path.open('w', newline='')
        self._writer = csv.writer(self._fh)
        self._writer.writerow(
            ['timestamp', 'slot', 'serial']
            + [n.lower() for n in mb_names]
            + [n.lower() for n in bp_names]
            + ['mmi_error_counter'])
        self._fh.flush()
        self.logger.info(f'crate telemetry: logging every {self.period:.0f}s to {self._csv_path}')

    def _run(self):
        import asyncio
        loop = asyncio.new_event_loop()          # one loop for the thread, so the
        asyncio.set_event_loop(loop)             # tuber aiohttp session is reused
        try:
            while not self._stop.is_set():
                try:
                    self._tick(loop)
                except Exception as exc:         # noqa: BLE001 - never kill the run
                    self.logger.debug(f'crate telemetry: sample failed: {exc!r}')
                self._stop.wait(self.period)
        finally:
            try:
                loop.close()
            except Exception:                    # noqa: BLE001
                pass

    def _tick(self, loop):
        import asyncio

        array = self._get_array()
        if array is None:
            return                                # crate not connected yet
        boards = sorted(array.ib, key=lambda b: getattr(b, 'slot', 0) or 0)
        if not boards:
            return

        if self._sensors is None:
            self._sensors = self._resolve_sensors(boards[0])
            self._open_csv(*self._sensors)
        mb_names, bp_names = self._sensors

        # One concurrent fan-out per sensor across all boards. return_exceptions
        # keeps a single unresponsive ARM from losing the whole sample.
        mb_temps = {}
        for name in mb_names:
            sensor = getattr(boards[0].TEMPERATURE_SENSOR, name, name)
            mb_temps[name] = loop.run_until_complete(asyncio.gather(
                *[b.tuber_get_motherboard_temperature_async(sensor) for b in boards],
                return_exceptions=True))

        bp_temps = {}
        for name in bp_names:
            sensor = getattr(boards[0].TEMPERATURE_SENSOR, name, name)
            try:
                bp_temps[name] = loop.run_until_complete(
                    boards[0].tuber_get_backplane_temperature_async(sensor))
            except Exception:                                  # noqa: BLE001
                bp_temps[name] = None

        def num(v):
            return None if isinstance(v, BaseException) or v is None else round(float(v), 2)

        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        for i, board in enumerate(boards):
            # Plain attribute read: going through get_fpga_udp_metrics_async()
            # would issue an MMI read from this thread.
            try:
                errs = int(board.fpga.mmi.error_counter)
            except Exception:                                  # noqa: BLE001
                errs = None
            self._writer.writerow(
                [now, getattr(board, 'slot', None), getattr(board, 'serial', None)]
                + [num(mb_temps[n][i]) for n in mb_names]
                + [num(bp_temps.get(n)) for n in bp_names]
                + [errs])
        self._fh.flush()
        self._ticks += len(boards)

        phy = [v for v in (num(x) for x in mb_temps.get('MB_PHY', [])) if v is not None]
        die = [v for v in (num(x) for x in mb_temps.get('MB_FPGA_DIE', [])) if v is not None]
        if phy or die:
            self.logger.info(
                'crate telemetry: '
                + (f'PHY {min(phy):.1f}-{max(phy):.1f}C ' if phy else '')
                + (f'FPGA die {min(die):.1f}-{max(die):.1f}C ' if die else '')
                + (f'BP {bp_temps.get("BP_SLOT1")}/{bp_temps.get("BP_SLOT16")}C'
                   if bp_temps else ''))


@pytest.fixture(scope='session', autouse=True)
def crate_telemetry(xr_session):
    '''Log board temperatures and MMI error counts for the whole session.

    Autouse and session-scoped so a full suite run produces one continuous
    trace that can be lined up against the point where tests start failing.
    Set PYCHFPGA_NO_TELEMETRY=1 to disable.
    '''
    if os.environ.get('PYCHFPGA_NO_TELEMETRY'):
        yield None
        return
    logger = logging.getLogger('crate_telemetry')
    telemetry = _CrateTelemetry(xr_session, logger)
    telemetry.start()
    try:
        yield telemetry
    finally:
        telemetry.stop()
