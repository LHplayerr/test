"""Worker pool daemon.

Long-running service that spawns and tracks simulation workers on
command. It is the process-side counterpart to the Django control
plane; the two communicate only through Redis.

Responsibilities:

    - Load the port allocator and manager.
    - Reconcile against Redis heartbeats at startup, so workers that
      survived a daemon restart are adopted and stale state is dropped.
    - Consume StartSessionCommand and StopSessionCommand from the
      command stream via the Redis bridge.
    - Periodically reap dead workers and enforce idle timeouts.
    - Handle SIGTERM: stop accepting commands, shut down tracked
      workers gracefully, exit cleanly.

Run as a systemd service, or by hand for development:

    REDIS_URL=redis://localhost:6379/0 \\
    python -m worker_pool.daemon --advertise-host 10.0.0.5

Configuration is read from the environment (shared.constants).
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        f"Python 3.11+ required. Found Python "
        f"{sys.version_info.major}.{sys.version_info.minor}. Exiting.\n"
    )
    sys.exit(1)

import argparse
import asyncio
import os
import signal
import sys
import time

import redis.asyncio as aioredis

from shared.constants import (
    REDIS_HEARTBEAT_PREFIX,
    WORKER_IDLE_TIMEOUT,
)
from shared.logging import configure_logging, get_logger
from shared.schemas import SessionFailedEvent, SessionStoppedEvent
from worker_pool.manager import WorkerPoolManager
from worker_pool.port_allocator import PortAllocator
from worker_pool.redis_bridge import RedisBridge


log = get_logger(__name__)


# How often to scan for dead workers and idle sessions.
_REAP_INTERVAL_SEC = 5.0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CIP worker pool daemon")
    p.add_argument(
        "--advertise-host", required=True,
        help="Host or IP that OPC UA clients will use to reach workers. "
             "This is the address the server tells clients to connect to, "
             "not necessarily an interface on this machine.",
    )
    p.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Log level (DEBUG, INFO, WARNING, ERROR).",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _reaper_loop(
    *,
    manager: WorkerPoolManager,
    bridge: RedisBridge,
    stop: asyncio.Event,
) -> None:
    """Periodically detect dead workers and publish events for them."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_REAP_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            pass

        try:
            reaped = await manager.reap_dead_workers()
        except Exception:
            log.exception("reaper pass failed")
            continue

        for record in reaped:
            log.warning("worker died unexpectedly", extra={
                "session_id": record.session_id,
                "session_name": record.session_name,
            })
            await bridge.publish_event(SessionFailedEvent(
                session_id=record.session_id,
                session_name=record.session_name,
                error="worker process exited without a stop command",
            ))

async def _idle_loop(
    *,
    redis: aioredis.Redis,
    manager: WorkerPoolManager,
    bridge: RedisBridge,
    stop: asyncio.Event,
) -> None:
    """Stop workers whose OPC UA clients have been silent too long.

    The worker writes cip:idle:<sid> with the wall-clock timestamp of
    the last external OPC UA activity. If no client has connected for
    WORKER_IDLE_TIMEOUT seconds, the daemon stops the session.
    """
    # Check once per minute; the timeout itself is on the order of
    # half an hour, so precision is not important.
    interval = 60.0

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass

        now = time.time()
        for record in manager.active_sessions():
            session_id = record["session_id"]
            session_name = record["session_name"]
            try:
                idle_raw = await redis.get(f"cip:idle:{session_id}")
            except Exception:
                log.exception("idle check failed",
                              extra={"session_id": session_id})
                continue
            if idle_raw is None:
                # Heartbeat or idle key expired; the reaper will handle
                # it on the next pass. Skip here.
                continue
            try:
                last_activity = float(idle_raw)
            except (TypeError, ValueError):
                continue

            idle_secs = now - last_activity
            if idle_secs > WORKER_IDLE_TIMEOUT:
                log.info(
                    "stopping idle session",
                    extra={"session_id": session_id,
                           "session_name": session_name,
                           "idle_seconds": round(idle_secs, 1)},
                )
                stopped = await manager.stop_worker(
                    session_id, reason="idle_timeout",
                )
                if stopped:
                    await bridge.publish_event(SessionStoppedEvent(
                        session_id=session_id,
                        session_name=session_name,
                        reason="idle_timeout",
                    ))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    redis = aioredis.from_url(redis_url, decode_responses=True)

    # Verify Redis is reachable before doing anything else. A clear
    # error here beats a cascade of failures across the bridge.
    try:
        await redis.ping()
    except Exception as exc:
        log.error("cannot reach Redis", extra={"error": str(exc)})
        await redis.aclose()
        return 2

    allocator = PortAllocator(redis)
    manager = WorkerPoolManager(
        redis=redis,
        port_allocator=allocator,
        advertise_host=args.advertise_host,
    )
    bridge = RedisBridge(redis=redis, manager=manager)

    log.info("daemon starting", extra={
        "advertise_host": args.advertise_host,
        "pid": os.getpid(),
    })

    # Reconcile before consuming commands, so we know what is already
    # running and can drop stale port leases.
    await manager.reconcile_on_startup()
    active = manager.active_sessions()
    if active:
        log.info("adopted workers from previous run",
                 extra={"count": len(active)})

    await bridge.ensure_group()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    bridge_task = asyncio.create_task(
        bridge.run(stop), name="redis-bridge",
    )
    reaper_task = asyncio.create_task(
        _reaper_loop(manager=manager, bridge=bridge, stop=stop),
        name="reaper",
    )
    idle_task = asyncio.create_task(
        _idle_loop(redis=redis, manager=manager, bridge=bridge, stop=stop),
        name="idle-watch",
    )

    log.info("daemon ready")

    # Wait for the stop signal. The three background tasks keep running
    # until then.
    await stop.wait()

    log.info("daemon stopping")
    for task in (bridge_task, reaper_task, idle_task):
        task.cancel()
    await asyncio.gather(
        bridge_task, reaper_task, idle_task,
        return_exceptions=True,
    )

    # Stop all workers gracefully.
    await manager.shutdown_all(timeout=20.0)

    try:
        await redis.aclose()
    except Exception:
        log.exception("error closing Redis connection")

    log.info("daemon stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())