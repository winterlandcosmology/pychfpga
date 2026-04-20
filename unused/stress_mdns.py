"""Parallel mDNS stress test for CRS 37 to validate lwIP legacy_query fix.

Same scenarios as the previous run so results are directly comparable.
"""
import asyncio
import logging
import time
from collections import Counter

import sys
sys.modules.setdefault('freetype', type(sys)('freetype'))  # stub missing optional dep
from pychfpga.mdns_discovery import mdns_discover
from pychfpga.hardware.crs import CRS  # registers 'crs' part number  # noqa: F401

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')


async def one(idx, *, timeout, retries):
    t0 = time.time()
    try:
        ibs, _ = await mdns_discover(
            iceboards=[('crs', '37')], icecrates=[],
            timeout=timeout, retries=retries)
        ok = any(int(ib.serial) == 37 for ib in (ibs or []))
        res = [ib.serial for ib in (ibs or [])]
    except Exception as e:
        ok = False
        res = repr(e)
    dt = time.time() - t0
    return idx, ok, dt, res


async def run_test(label, n_calls, concurrency, timeout, retries):
    print(f"\n=== {label}: n={n_calls} conc={concurrency} timeout={timeout}s retries={retries} ===")
    sem = asyncio.Semaphore(concurrency)

    async def gated(i):
        async with sem:
            return await one(i, timeout=timeout, retries=retries)

    t0 = time.time()
    results = await asyncio.gather(*(gated(i) for i in range(n_calls)))
    total_dt = time.time() - t0

    succ = sum(1 for _, ok, _, _ in results if ok)
    times = sorted(dt for _, _, dt, _ in results)
    buckets = Counter()
    for dt in times:
        if dt < 0.3:
            buckets['<0.3s'] += 1
        elif dt < 1.0:
            buckets['0.3-1s'] += 1
        elif dt < 2.0:
            buckets['1-2s'] += 1
        elif dt < 4.0:
            buckets['2-4s'] += 1
        else:
            buckets['>=4s'] += 1
    print(f"  success: {succ}/{n_calls}  wallclock: {total_dt:.1f}s")
    p = lambda q: times[min(int(q * len(times)), len(times) - 1)]
    print(f"  per-call dt:  min={times[0]:.2f}  p50={p(0.5):.2f}  p90={p(0.9):.2f}  p99={p(0.99):.2f}  max={times[-1]:.2f}")
    print(f"  buckets: {dict(buckets)}")
    return succ, n_calls


async def main():
    # Test 1: 50 calls, 10 concurrent, default settings
    await run_test("Test 1 (paced)", n_calls=50, concurrency=10, timeout=2.0, retries=2)
    # Test 2: 50 calls, all simultaneous
    await run_test("Test 2 (simultaneous)", n_calls=50, concurrency=50, timeout=2.0, retries=2)
    # Test 3: 100 calls, 20 concurrent, aggressive
    await run_test("Test 3 (aggressive)", n_calls=100, concurrency=20, timeout=1.0, retries=4)


if __name__ == '__main__':
    asyncio.run(main())
