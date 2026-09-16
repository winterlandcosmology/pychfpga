#!/usr/bin/env python3
"""Deprecated name for ``watch_tcpipe.py``; arguments are forwarded unchanged."""

from __future__ import annotations

import sys
import time

print(
    f"{time.strftime('%H:%M:%S')} note: watch_tcpipe_elf.py is now watch_tcpipe.py",
    file=sys.stderr,
    flush=True,
)

import watch_tcpipe  # noqa: E402

if __name__ == "__main__":
    sys.exit(watch_tcpipe.main())
