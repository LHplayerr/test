"""Manual smoke test for WorkerPoolManager.

Run from the project root with the venv active:

    python -m tests.manual.test_manager

Requires Redis running on localhost. Uses ports 5100-5110 so it does
not collide with anything else you have running.
"""

from __future__ import annotations

import asyncio
import os

import redis.asyncio as aioredis

from worker_pool.manager import WorkerPoolManager
from worker_pool.port_allocator import PortAllocator


async def main() -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)

    allocator = PortAllocator(r, start=5100, end=5110)
    mgr = WorkerPoolManager(
        redis=r,
        port_allocator=allocator,
        advertise_host="127.0.0.1",
    )

    print("== reconcile on startup ==")
    await mgr.reconcile_on_startup()
    print("   tracked:", mgr.active_sessions())

    print()
    print("== spawn test1 (cip) ==")
    record = await mgr.start_worker(
        session_id="test1",
        simulation_id="cip",
        config_params={},
    )
    print(f"   pid={record.pid} port={record.port}")

    print()
    print("== wait 3s for heartbeat ==")
    await asyncio.sleep(3.0)
    hb = await r.get("cip:hb:test1")
    print("   cip:hb:test1 =", hb)

    print()
    print("== active sessions ==")
    for s in mgr.active_sessions():
        print("  ", s)

    print()
    print("== stop test1 ==")
    ok = await mgr.stop_worker("test1", reason="smoke_test")
    print("   stopped:", ok)

    print()
    print("== verify cleanup ==")
    print("   tracked:", mgr.active_sessions())
    print("   heartbeat:", await r.get("cip:hb:test1"))
    print("   ports leased:", await allocator.snapshot())

    await r.aclose()
    print()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))