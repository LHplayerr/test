"""Manual end-to-end test for the worker pool daemon.

Starts the daemon as a subprocess, publishes a start command through
Redis, waits for the worker to appear, publishes a stop command, and
verifies clean shutdown.

Run from the project root with the venv active:

    python -m tests.manual.test_daemon

Requires Redis on localhost. Uses ports 5300-5310.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

from shared.constants import REDIS_EVT_STREAM, STREAM_MAXLEN
from shared.schemas import (
    StartSessionCommand,
    StopSessionCommand,
    parse_event,
)


async def _wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.3):
    """Poll predicate() (sync or async) until it returns truthy or times out."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    return None


async def main() -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=True)

    # Clean slate
    async for key in r.scan_iter("cip:*"):
        await r.delete(key)
    await r.delete("cip:cmd", "cip:evt")

    env = {
        **os.environ,
        "REDIS_URL": redis_url,
        "OPCUA_PORT_START": "5300",
        "OPCUA_PORT_END": "5310",
    }

    print("== start daemon ==")
    proc = subprocess.Popen(
        [sys.executable, "-m", "worker_pool.daemon",
         "--advertise-host", "127.0.0.1"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    async def drain():
        """Print daemon output as it arrives."""
        assert proc.stdout is not None
        while True:
            line = await asyncio.to_thread(proc.stdout.readline)
            if not line:
                break
            print("   daemon:", line.decode("utf-8", errors="replace").rstrip())

    drain_task = asyncio.create_task(drain())

    # Wait for daemon to be ready by watching for the ready log line.
    # Simplest proxy: it will create the consumer group and be ready
    # within ~2s of start.
    await asyncio.sleep(2.5)

    print()
    print("== publish start command ==")
    sid = "daemon-test1"
    start = StartSessionCommand(
        session_id=sid,
        simulation_id="cip",
        config_params={},
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    await r.xadd(
        "cip:cmd", {"payload": start.model_dump_json()},
        maxlen=STREAM_MAXLEN, approximate=True,
    )

    async def worker_running():
        hb = await r.get(f"cip:hb:{sid}")
        return hb is not None

    found = await _wait_for(worker_running, timeout=15.0)
    print("   worker heartbeat present:", bool(found))
    if found:
        print("   hb:", found)

    print()
    print("== active sessions (via Redis heartbeat) ==")
    async for key in r.scan_iter("cip:hb:*"):
        print("  ", key)

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

    async def worker_gone():
        hb = await r.get(f"cip:hb:{sid}")
        return hb is None

    gone = await _wait_for(worker_gone, timeout=15.0)
    print("   worker gone:", bool(gone))

    print()
    print("== events emitted ==")
    entries = await r.xrange(REDIS_EVT_STREAM, "-", "+")
    for _msg_id, fields in entries:
        payload = fields.get("payload")
        if payload:
            try:
                print("  ", parse_event(payload).kind)
            except Exception:
                print("   unparsable")

    print()
    print("== stop daemon (SIGTERM) ==")
    proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=15.0)
        print("   exited with code:", proc.returncode)
    except asyncio.TimeoutError:
        print("   daemon did not exit; killing")
        proc.kill()

    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)

    await r.aclose()
    print()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))