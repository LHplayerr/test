"""Gateway OPC UA server.

Owns the aggregate address space. Each active session appears under
Objects/Gateway/Sessions/<session_name>/. Mirrors are kept in a dict
keyed by session_id (UUID) and updated as session events arrive.

Mirrors are added two ways:

  1. reconcile_existing() — at startup, scan Redis heartbeats. This
     is the ground truth for "what workers are alive right now" and
     does not depend on having seen the SessionReadyEvent.
  2. run_event_loop() — consume the events stream for sessions that
     start or stop while the gateway is running.

Both paths converge on _add_mirror and _remove_mirror.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import redis.asyncio as aioredis
from asyncua import Server, ua

from gateway.mirror import SimulationRootNotFound, WorkerMirror
from shared.constants import (
    GATEWAY_EVT_CONSUMER_GROUP,
    REDIS_EVT_STREAM,
    REDIS_HEARTBEAT_PREFIX,
)
from shared.logging import get_logger
from shared.schemas import (
    SessionFailedEvent,
    SessionReadyEvent,
    SessionStoppedEvent,
    parse_event,
)


log = get_logger(__name__)


class Gateway:
    def __init__(
        self,
        *,
        advertise_host: str,
        port: int,
        redis: aioredis.Redis,
    ) -> None:
        self._advertise_host = advertise_host
        self._port = port
        self._redis = redis

        self._server = Server()
        self._namespace_idx: int = 0
        self._sessions_folder: ua.Node | None = None

        self._mirrors: dict[str, WorkerMirror] = {}
        self._mirrors_lock = asyncio.Lock()

        # Pending outbound writes to workers. The setter callback is
        # synchronous; enqueueing here decouples it from the async
        # write that actually reaches the worker.
        self._write_queue: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        self._write_task: asyncio.Task | None = None

        self._endpoint = f"opc.tcp://{advertise_host}:{port}/gateway/"

    # --- lifecycle ---

    async def start(self) -> None:
        await self._server.init()
        self._server.set_endpoint(self._endpoint)
        self._server.set_server_name("CIP Simulation Gateway")
        await self._server.set_application_uri("urn:cip-sim:gateway")
        self._server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self._server.set_identity_tokens([ua.AnonymousIdentityToken])

        self._namespace_idx = await self._server.register_namespace(
            "urn:cip-sim:gateway:1.0.0"
        )
        ns = self._namespace_idx

        root = await self._server.nodes.objects.add_object(
            ua.NodeId("Gateway", ns),
            ua.QualifiedName("Gateway", ns),
        )
        self._sessions_folder = await root.add_folder(
            ua.NodeId("Gateway.Sessions", ns),
            ua.QualifiedName("Sessions", ns),
        )

        self._write_task = asyncio.create_task(self._drain_writes())

        await self._server.start()
        log.info("gateway listening", extra={"endpoint": self._endpoint})

    async def stop(self) -> None:
        if self._write_task:
            self._write_task.cancel()
            try:
                await self._write_task
            except (asyncio.CancelledError, Exception):
                pass

        async with self._mirrors_lock:
            mirrors = list(self._mirrors.values())
            self._mirrors.clear()
        for m in mirrors:
            try:
                await m.disconnect()
            except Exception:
                log.exception("error disconnecting mirror",
                              extra={"session_id": m.session_id,
                                     "session_name": m.session_name})

        try:
            await self._server.stop()
        except Exception:
            log.exception("error stopping gateway server")

    # --- startup reconciliation ---

    async def reconcile_existing(self) -> None:
        """Scan Redis heartbeats and mirror every live worker.

        Called once at startup. Restart-safe: workers publish their
        simulation_id and session_name on every heartbeat, so a
        session created before the gateway started is still
        discoverable without having seen its SessionReadyEvent.
        """
        log.info("reconcile: starting")
        keys = await self._redis.keys(f"{REDIS_HEARTBEAT_PREFIX}*")
        log.info("reconcile: scanned keys", extra={"count": len(keys)})

        if not keys:
            log.info("reconcile: no live sessions")
            return

        seen: list[str] = []
        for key in keys:
            session_id = key.split(":", 2)[-1]
            hb_raw = await self._redis.get(key)
            if not hb_raw:
                continue
            try:
                hb = json.loads(hb_raw)
            except Exception:
                log.warning("reconcile: unparsable heartbeat",
                            extra={"session_id": session_id})
                continue

            port = hb.get("port")
            pid = hb.get("pid", 0)
            sim_id = hb.get("simulation_id")
            session_name = str(hb.get("session_name") or session_id)
            if port is None or not sim_id:
                log.warning(
                    "reconcile: heartbeat missing port or simulation_id",
                    extra={"session_id": session_id},
                )
                continue

            ev = SessionReadyEvent(
                session_id=session_id,
                session_name=session_name,
                simulation_id=str(sim_id),
                port=int(port),
                pid=int(pid),
            )
            await self._add_mirror(ev)
            seen.append(session_id)

        log.info("reconcile complete",
                 extra={"sessions": seen, "count": len(seen)})

    # --- event loop ---

    async def run_event_loop(self, stop: asyncio.Event) -> None:
        # id="0" so events that arrived before the gateway started are
        # not missed. Combined with reconcile_existing this covers both
        # the cold-start and restart cases.
        try:
            await self._redis.xgroup_create(
                REDIS_EVT_STREAM, GATEWAY_EVT_CONSUMER_GROUP,
                id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        consumer = f"gateway-{self._port}"

        while not stop.is_set():
            try:
                entries = await asyncio.wait_for(
                    self._redis.xreadgroup(
                        GATEWAY_EVT_CONSUMER_GROUP, consumer,
                        {REDIS_EVT_STREAM: ">"},
                        count=10, block=2000,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                continue
            except Exception:
                log.exception("xreadgroup failed")
                await asyncio.sleep(1.0)
                continue

            for _stream, messages in entries:
                for msg_id, fields in messages:
                    await self._handle_event(fields)
                    try:
                        await self._redis.xack(
                            REDIS_EVT_STREAM, GATEWAY_EVT_CONSUMER_GROUP,
                            msg_id,
                        )
                    except Exception:
                        log.exception("xack failed")

    async def _handle_event(self, fields: dict) -> None:
        payload = fields.get("payload")
        if not payload:
            return
        try:
            event = parse_event(payload)
        except Exception:
            log.exception("bad event payload")
            return

        if isinstance(event, SessionReadyEvent):
            await self._add_mirror(event)
        elif isinstance(event, (SessionStoppedEvent, SessionFailedEvent)):
            await self._remove_mirror(event.session_id)

    # --- mirrors ---

    async def _add_mirror(self, ev: SessionReadyEvent) -> None:
        async with self._mirrors_lock:
            if ev.session_id in self._mirrors:
                return

        # The gateway is on the same host as the workers, so it always
        # connects via loopback regardless of what the worker advertises.
        worker_endpoint = (
            f"opc.tcp://127.0.0.1:{ev.port}/{ev.simulation_id}/"
        )

        # Retry with backoff. The worker publishes SessionReadyEvent
        # only after the port is bound (see sim_runtime/opcua_adapter),
        # so the first attempt normally succeeds. The retry covers the
        # remaining cases: a gateway that reconciles a heartbeat a
        # split second before the worker finishes binding, a slow
        # asyncua handshake, transient network noise.
        #
        # A fresh WorkerMirror per attempt: a failed connect can leave
        # a half-open asyncua client, and reusing it tends to fail on
        # the second try with a confusing error.
        max_attempts = 5
        mirror: WorkerMirror | None = None

        for attempt in range(1, max_attempts + 1):
            candidate = WorkerMirror(
                session_id=ev.session_id,
                session_name=ev.session_name,
                simulation_id=ev.simulation_id,
                worker_endpoint=worker_endpoint,
                parent_folder=self._sessions_folder,
                namespace_idx=self._namespace_idx,
                server=self._server,
                enqueue_write=self._enqueue_write,
            )
            try:
                await candidate.connect_and_mirror()
                mirror = candidate
                break
            except SimulationRootNotFound as exc:
                # Not transient. The worker is up but its address space
                # does not have a discoverable root.
                log.error(
                    "worker has no discoverable simulation root",
                    extra={"session_id": ev.session_id,
                           "session_name": ev.session_name,
                           "error": str(exc)},
                )
                return
            except Exception as exc:
                if attempt >= max_attempts:
                    log.exception(
                        "failed to mirror worker after retries",
                        extra={"session_id": ev.session_id,
                               "session_name": ev.session_name,
                               "attempts": max_attempts},
                    )
                    return
                delay = min(0.3 * attempt, 1.5)
                log.info(
                    "mirror connect failed; retrying",
                    extra={"session_id": ev.session_id,
                           "attempt": attempt,
                           "delay_seconds": delay,
                           "error": type(exc).__name__},
                )
                await asyncio.sleep(delay)

        if mirror is None:
            return

        async with self._mirrors_lock:
            self._mirrors[ev.session_id] = mirror
        log.info("mirror added", extra={
            "session_id": ev.session_id,
            "session_name": ev.session_name,
        })

    async def _remove_mirror(self, session_id: str) -> None:
        async with self._mirrors_lock:
            mirror = self._mirrors.pop(session_id, None)
        if mirror is None:
            return
        try:
            await mirror.disconnect()
        except Exception:
            log.exception("error disconnecting mirror",
                          extra={"session_id": session_id,
                                 "session_name": mirror.session_name})
        log.info("mirror removed", extra={
            "session_id": session_id,
            "session_name": mirror.session_name,
        })

    # --- write forwarding ---

    def _enqueue_write(
        self, session_id: str, command_name: str, value: Any
    ) -> None:
        self._write_queue.put_nowait((session_id, command_name, value))

    async def _drain_writes(self) -> None:
        while True:
            session_id, command_name, value = await self._write_queue.get()
            mirror = self._mirrors.get(session_id)
            if mirror is None:
                continue
            try:
                await mirror.forward_command(command_name, value)
            except Exception:
                log.exception(
                    "command forward failed",
                    extra={"session_id": session_id,
                           "session_name": mirror.session_name,
                           "command": command_name},
                )