"""Manual smoke test for RedisBridge.

Runs a manager and a bridge in-process, publishes commands to the
command stream, and verifies that workers are spawned and stopped.

Run from the project root with the venv active:

    python -m tests.manual.test_redis_bridge

Requires Redis on localhost. Uses ports 5200-5210.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

from shared.constants import REDIS_EVT_STREAM, STREAM_MAXLEN
from shared.schemas import (
    SessionStartedEvent,
    SessionStoppedEvent,
    StartSessionCommand,
    StopSessionCommand,
    parse_event,
)
from worker_pool.manager import WorkerPoolManager
from worker_pool.port_allocator import PortAllocator
from worker_pool.redis_bridge import RedisBridge


async def _drain_events(r: aioredis.Redis) -> list[str]:
    """Read everything currently in the events stream."""
    entries = await r.xrange(REDIS_EVT_STREAM, "-", "+")
    out: list[str] = []
    for _msg_id, fields in entries:
        payload = fields.get("payload")
        if payload:
            try:
                event = parse_event(payload)
                out.append(event.kind)
            except Exception:
                out.append("unparsable")
    return out


async def main() -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)

    # Clean slate
    await r.delete(REDIS_EVT_STREAM, "cip:cmd")
    async for key in r.scan_iter("cip:*"):
        await r.delete(key)

    allocator = PortAllocator(r, start=5200, end=5210)
    mgr = WorkerPoolManager(
        redis=r,
        port_allocator=allocator,
        advertise_host="127.0.0.1",
    )
    bridge = RedisBridge(
        redis=r, manager=mgr,
        consumer_name=f"smoke-{uuid.uuid4().hex[:6]}",
    )

    await bridge.ensure_group()

    stop = asyncio.Event()
    bridge_task = asyncio.create_task(bridge.run(stop))

    # Give the bridge a moment to enter xreadgroup.
    await asyncio.sleep(0.5)

    sid = "smoke1"
    print("== publish start command ==")
    start_cmd = StartSessionCommand(
        session_id=sid,
        simulation_id="cip",
        config_params={},
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    await r.xadd(
        "cip:cmd", {"payload": start_cmd.model_dump_json()},
        maxlen=STREAM_MAXLEN, approximate=True,
    )

    # Wait for worker to spawn
    for _ in range(20):
        await asyncio.sleep(0.5)
        if mgr.is_tracked(sid):
            break

    print("   tracked:", mgr.is_tracked(sid))
    print("   sessions:", mgr.active_sessions())

    print()
    print("== wait for heartbeat ==")
    await asyncio.sleep(2.0)
    hb = await r.get(f"cip:hb:{sid}")
    print(f"   cip:hb:{sid} = {hb}")

    print()
    print("== publish stop command ==")
    stop_cmd = StopSessionCommand(
        session_id=sid,
        reason="user_request",
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    await r.xadd(
        "cip:cmd", {"payload": stop_cmd.model_dump_json()},
        maxlen=STREAM_MAXLEN, approximate=True,
    )

    for _ in range(20):
        await asyncio.sleep(0.5)
        if not mgr.is_tracked(sid):
            break

    print("   tracked:", mgr.is_tracked(sid))
    print("   sessions:", mgr.active_sessions())
    print("   ports leased:", await allocator.snapshot())

    print()
    print("== events emitted ==")
    kinds = await _drain_events(r)
    for kind in kinds:
        print("  ", kind)

    # Shutdown
    stop.set()
    try:
        await asyncio.wait_for(bridge_task, timeout=5.0)
    except asyncio.TimeoutError:
        bridge_task.cancel()

    await mgr.shutdown_all(timeout=5.0)
    await r.aclose()
    print()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))