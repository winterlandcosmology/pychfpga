'''Session-level guard for the ICE-crate QC suite.

``wtl.pytest_xreport`` moves the per-test HTML report into the results folder
from its ``pytest_sessionfinish``, but ``pytest-html`` only writes that file in
a ``trylast`` ``pytest_sessionfinish`` that runs *after* xreport's, so xreport
always renames a file that does not exist yet. The escaping FileNotFoundError
skips pytest's own terminal summary (no "short test summary info", no
tracebacks) and makes the process exit non-zero even when every test passed.
The JUnit XML is written before the HTML step and is unaffected.

This is the same guard as ``firmware_tests/conftest.py`` (minus that file's
crate telemetry): pre-create the HTML placeholder so xreport's rename is a
successful no-op, and if a FileNotFoundError still escapes, suppress it so the
summary survives and the exit status reflects the tests, not the harness.
'''

from pathlib import Path

import pytest


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session, exitstatus):
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
