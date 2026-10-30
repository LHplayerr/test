"""Worker pool manager.

Spawns, tracks, stops, and reconciles worker processes. This is the
process-lifecycle layer: it does not know about Redis streams, does not
publish events, and does not enforce idle timeouts. Those are the
daemon's job.

session_id is the UUID and keys the internal dict, port leases, and
PID files. session_name is the slug or UUID and is passed through to
the worker so the gateway can name the OPC UA folder. Both flow
unchanged through this module.

Design notes:

- Workers are spawned with start_new_session=True so they detach from
  the daemon's process group. If the daemon dies, workers keep running,
  and a restart can adopt them via Redis heartbeats.
- Graceful stop is SIGTERM, then WORKER_STOP_GRACE seconds, then
  SIGKILL. Port leases are released only after the process is gone.
- Reconciliation on startup uses Redis heartbeats as the source of
  truth. A worker is alive if its heartbeat key exists AND its PID
  responds to signal 0.
- All mutations of the worker dict take an asyncio.Lock so concurrent
  start_worker calls do not race on port allocation.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

from shared.constants import (
    REDIS_HEARTBEAT_PREFIX,
    WORKER_STOP_GRACE,
)
from shared.logging import get_logger
from worker_pool.port_allocator import PortAllocator


log = get_logger(__name__)


# Time to wait after spawning before checking whether the worker is
# still alive. Catches bad-arg and port-conflict failures quickly.
_STARTUP_LIVENESS_CHECK = 1.0


class WorkerManagerError(RuntimeError):
    """Base for manager errors."""


class AlreadyRunningError(WorkerManagerError):
    """A worker already exists for this session_id."""


class WorkerStartupError(WorkerManagerError):
    """The worker process exited during startup."""


@dataclass
class SpawnedWorker:
    """Bookkeeping for one running worker."""

    session_id: str          # UUID — identity
    session_name: str        # slug or UUID — display name
    simulation_id: str
    port: int
    pid: int
    process: asyncio.subprocess.Process | None   # None for adopted workers
    started_at: float
    config_params: dict[str, Any] = field(default_factory=dict)

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    @property
    def adopted(self) -> bool:
        """True if this record came from reconciliation, not this run."""
        return self.process is None


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists and is signalable.

    os.kill with signal 0 performs the existence and permission checks
    without actually sending a signal. ProcessLookupError means no such
    process exists. PermissionError means it exists but is owned by
    another user, which we treat as alive from our perspective.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WorkerPoolManager:
    """Spawns and tracks worker processes. See module docstring."""

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        port_allocator: PortAllocator,
        advertise_host: str,
        python_executable: str | None = None,
        worker_module: str = "worker_pool.worker.main",
        log_level: str = "INFO",
    ) -> None:
        self._redis = redis
        self._allocator = port_allocator
        self._advertise_host = advertise_host
        self._python = python_executable or sys.executable
        self._worker_module = worker_module
        self._log_level = log_level

        self._lock = asyncio.Lock()
        self._workers: dict[str, SpawnedWorker] = {}
        self._log_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start_worker(
        self,
        *,
        session_id: str,
        session_name: str,
        simulation_id: str,
        config_params: dict[str, Any] | None = None,
    ) -> SpawnedWorker:
        """Spawn a new worker process and track it.

        Raises AlreadyRunningError if session_id is already tracked.
        Raises WorkerStartupError if the process exits during startup.
        """
        async with self._lock:
            if session_id in self._workers:
                raise AlreadyRunningError(
                    f"worker for {session_id!r} is already tracked"
                )

        port = await self._allocator.acquire(session_id)

        try:
            proc = await self._spawn_process(
                session_id=session_id,
                session_name=session_name,
                simulation_id=simulation_id,
                port=port,
                config_params=config_params or {},
            )
        except Exception:
            await self._allocator.release(port, session_id)
            raise

        record = SpawnedWorker(
            session_id=session_id,
            session_name=session_name,
            simulation_id=simulation_id,
            port=port,
            pid=proc.pid,
            process=proc,
            started_at=time.time(),
            config_params=config_params or {},
        )

        # Fast-fail detection: give the worker a moment to crash on
        # bad args, missing plugin, or port conflict.
        await asyncio.sleep(_STARTUP_LIVENESS_CHECK)
        if proc.returncode is not None:
            await self._allocator.release(port, session_id)
            raise WorkerStartupError(
                f"worker for {session_id!r} exited during startup "
                f"with code {proc.returncode}"
            )

        async with self._lock:
            self._workers[session_id] = record
        self._log_tasks[session_id] = asyncio.create_task(
            self._pipe_logs(record),
            name=f"logs-{session_id}",
        )

        log.info("worker spawned", extra={
            "session_id": session_id,
            "session_name": session_name,
            "simulation_id": simulation_id,
            "port": port,
            "pid": proc.pid,
        })
        return record

    async def _spawn_process(
        self,
        *,
        session_id: str,
        session_name: str,
        simulation_id: str,
        port: int,
        config_params: dict[str, Any],
    ) -> asyncio.subprocess.Process:
        env = {
            **os.environ,
            "SIM_MANAGED": "1",
            "LOG_LEVEL": self._log_level,
        }
        cmd = [
            self._python, "-m", self._worker_module,
            "--session-id", session_id,
            "--session-name", session_name,
            "--simulation-id", simulation_id,
            "--opcua-port", str(port),
            "--advertise-host", self._advertise_host,
            "--config", json.dumps({"params": config_params}),
        ]
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    async def stop_worker(
        self, session_id: str, *, reason: str = "user_request"
    ) -> bool:
        """Stop a worker by session_id. Returns True if one was stopped.

        Sends SIGTERM, waits up to WORKER_STOP_GRACE seconds for exit,
        then SIGKILL. Releases the port only after the process is gone.
        """
        async with self._lock:
            record = self._workers.get(session_id)

        if record is None:
            return False

        await self._terminate(record)
        await self._allocator.release(record.port, session_id)

        async with self._lock:
            self._workers.pop(session_id, None)
        task = self._log_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

        log.info("worker stopped", extra={
            "session_id": session_id,
            "session_name": record.session_name,
            "reason": reason,
            "pid": record.pid,
        })
        return True

    async def _terminate(self, record: SpawnedWorker) -> None:
        """Terminate a worker gracefully, escalating to SIGKILL if needed."""
        if not _pid_alive(record.pid):
            return

        try:
            os.kill(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        deadline = time.monotonic() + WORKER_STOP_GRACE
        while time.monotonic() < deadline and _pid_alive(record.pid):
            await asyncio.sleep(0.2)

        if _pid_alive(record.pid):
            log.warning(
                "worker did not exit on SIGTERM; sending SIGKILL",
                extra={"session_id": record.session_id,
                       "session_name": record.session_name,
                       "pid": record.pid},
            )
            try:
                os.kill(record.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # If we have a handle, reap the zombie so the kernel frees the PID.
        if record.process is not None:
            try:
                await asyncio.wait_for(record.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Reap and reconcile
    # ------------------------------------------------------------------

    async def reap_dead_workers(self) -> list[SpawnedWorker]:
        """Detect and clean up workers whose processes have exited.

        Called periodically by the daemon. Returns the records that
        were reaped so the caller can publish events that carry both
        identifiers.
        """
        async with self._lock:
            candidates = list(self._workers.values())

        reaped: list[SpawnedWorker] = []
        for record in candidates:
            if _pid_alive(record.pid):
                continue
            log.info("reaping dead worker", extra={
                "session_id": record.session_id,
                "session_name": record.session_name,
                "pid": record.pid,
                "port": record.port,
            })
            await self._allocator.release(record.port, record.session_id)
            async with self._lock:
                self._workers.pop(record.session_id, None)
            task = self._log_tasks.pop(record.session_id, None)
            if task is not None:
                task.cancel()
            reaped.append(record)
        return reaped
    
    async def reconcile_on_startup(self) -> None:
        """Adopt workers that survived a daemon restart; drop dead ones.

        Uses Redis heartbeats as the source of truth. A worker is alive
        if its heartbeat key exists and its PID responds to signal 0.
        The heartbeat payload carries session_name so an adopted worker
        keeps its original display name.
        """
        keys = await self._redis.keys(f"{REDIS_HEARTBEAT_PREFIX}*")
        adopted: list[str] = []
        dropped: list[str] = []

        for key in keys:
            session_id = key.split(":", 2)[-1] if ":" in key else key
            raw = await self._redis.get(key)
            if not raw:
                continue

            try:
                hb = json.loads(raw)
                pid = int(hb["pid"])
                port = int(hb["port"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                await self._redis.delete(key)
                dropped.append(session_id)
                continue

            if not _pid_alive(pid):
                await self._redis.delete(key)
                dropped.append(session_id)
                continue

            record = SpawnedWorker(
                session_id=session_id,
                session_name=str(hb.get("session_name") or session_id),
                simulation_id=str(hb.get("simulation_id", "")),
                port=port,
                pid=pid,
                process=None,   # adopted; no Popen handle
                started_at=time.time() - float(hb.get("uptime", 0.0)),
            )
            async with self._lock:
                self._workers[session_id] = record
            adopted.append(session_id)

        # Drop stale port leases whose session no longer exists.
        await self._allocator.reconcile(set(adopted))

        log.info("startup reconciliation", extra={
            "adopted": len(adopted),
            "dropped": len(dropped),
        })

    # ------------------------------------------------------------------
    # Introspection and shutdown
    # ------------------------------------------------------------------

    def active_sessions(self) -> list[dict[str, Any]]:
        """Snapshot of currently tracked workers. Read-only."""
        return [
            {
                "session_id": r.session_id,
                "session_name": r.session_name,
                "simulation_id": r.simulation_id,
                "port": r.port,
                "pid": r.pid,
                "uptime": round(r.uptime, 1),
                "adopted": r.adopted,
            }
            for r in self._workers.values()
        ]

    def is_tracked(self, session_id: str) -> bool:
        return session_id in self._workers

    async def shutdown_all(self, *, timeout: float = 15.0) -> None:
        """Stop every tracked worker. Called on daemon shutdown."""
        async with self._lock:
            session_ids = list(self._workers.keys())
        if not session_ids:
            return

        log.info("shutting down all workers",
                 extra={"count": len(session_ids)})
        tasks = [
            asyncio.create_task(
                self.stop_worker(sid, reason="daemon_shutdown"),
                name=f"stop-{sid}",
            )
            for sid in session_ids
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("shutdown_all timed out; some workers may remain")

    # ------------------------------------------------------------------
    # Log piping
    # ------------------------------------------------------------------

    async def _pipe_logs(self, record: SpawnedWorker) -> None:
        """Forward the child's stdout into the daemon's structured logger.

        Workers emit JSON logs; we pass them through as the ``line``
        field so the daemon's own fields are stamped on every record
        without duplicating the worker's context.
        """
        proc = record.process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.info("worker", extra={
                        "session_id": record.session_id,
                        "session_name": record.session_name,
                        "line": text,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("log pipe failed",
                          extra={"session_id": record.session_id})