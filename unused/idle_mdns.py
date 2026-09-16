"""Mimic the Jupyter cell-rerun pattern: single mdns_discover from cold state,
idle gap, repeat. This is the realistic pattern that exposes occasional
multicast packet loss vs. the back-to-back stress test pattern.
"""
import asyncio
import logging
import sys
import time

sys.modules.setdefault('freetype', type(sys)('freetype'))

from pychfpga.common.mdns_discovery import mdns_discover
from pychfpga.hardware.crs import CRS  # noqa: F401  registers 'crs' part number

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# Track retry warnings
retry_count = 0


class _RetryCounter(logging.Handler):
    def emit(self, record):
        global retry_count
        if 'retrying' in record.getMessage():
            retry_count += 1


logging.getLogger('pychfpga.common.mdns_discovery').addHandler(_RetryCounter())


async def main(n_iters, idle_gap):
    print(f"=== Cold-start pattern: {n_iters} iters, {idle_gap}s idle between ===")
    times = []
    fails = 0
    for i in range(n_iters):
        t0 = time.time()
        try:
            ibs, _ = await mdns_discover(
                boards=[('crs', '37')], crates=[],
                timeout=2.0, retries=2, inter_reply_timeout=0.5)
            ok = any(int(ib.serial) == 37 for ib in (ibs or []))
        except Exception as e:
            ok = False
        dt = time.time() - t0
        times.append(dt)
        if not ok:
            fails += 1
        marker = '  ' if dt < 0.3 else 'SLOW'
        print(f"  [{i+1:3d}/{n_iters}] {marker} {dt*1000:6.0f} ms  ok={ok}  retries_so_far={retry_count}")
        if i < n_iters - 1:
            await asyncio.sleep(idle_gap)
    print()
    print(f"=== Summary: {n_iters} attempts, {fails} failures, {retry_count} total retries ===")
    times.sort()
    p = lambda q: times[min(int(q * len(times)), len(times) - 1)]
    print(f"=== Per-call: min={times[0]*1000:.0f}ms  p50={p(0.5)*1000:.0f}ms  p90={p(0.9)*1000:.0f}ms  p99={p(0.99)*1000:.0f}ms  max={times[-1]*1000:.0f}ms")


if __name__ == '__main__':
    asyncio.run(main(n_iters=60, idle_gap=2.0))
