"""Redis Streams bridge for the worker pool daemon.

Consumes StartSessionCommand and StopSessionCommand from the command
stream, dispatches them to the manager, and publishes lifecycle events
back on the event stream. The daemon also publishes events it generates
itself — worker crashes, reconciliation actions, shutdown notices.

Streams, not pub/sub: a daemon restart must not lose commands that were
published while it was down. Unacked messages stay in the consumer
group's pending list and are delivered again on reconnect. Pub/sub
would silently drop them.

At-least-once delivery means command handlers must be idempotent.
Starting an already-tracked session raises AlreadyRunningError; the
bridge logs and ignores it rather than retrying. Stopping an untracked
session returns False; the bridge skips the stopped event.

Every command and event carries both session_id (UUID) and
session_name (slug or UUID). The bridge forwards both unchanged.

The consumer name includes the daemon PID, so a restart reads as a
fresh consumer. Messages left pending by the previous incarnation are
reclaimed via XAUTOCLAIM on startup.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from shared.constants import (
    CMD_CONSUMER_GROUP,
    REDIS_CMD_STREAM,
    REDIS_EVT_STREAM,
    STREAM_MAXLEN,
)
from shared.logging import get_logger
from shared.schemas import (
    Command,
    Event,
    PingCommand,
    SessionFailedEvent,
    SessionStartedEvent,
    SessionStoppedEvent,
    StartSessionCommand,
    StopSessionCommand,
    parse_command,
)
from worker_pool.manager import (
    AlreadyRunningError,
    WorkerManagerError,
    WorkerPoolManager,
    WorkerStartupError,
)


log = get_logger(__name__)


_READ_BLOCK_MS = 2000
_CLAIM_INTERVAL_SEC = 60.0
_CLAIM_MIN_IDLE_MS = 120_000


class RedisBridge:
    """Bridge between the Redis command/event streams and the manager."""

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        manager: WorkerPoolManager,
        consumer_name: str | None = None,
    ) -> None:
        self._redis = redis
        self._manager = manager
        self._consumer = consumer_name or f"worker-pool-{os.getpid()}"
        self._log = log
        # None = unknown; True/False once we've tried once.
        self._xautoclaim_supported: bool | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """Create the consumer group if it does not exist.

        id="0" starts delivery from the beginning of the stream, so a
        daemon that restarts after being down still sees any unacked
        commands. MKSTREAM creates the stream if nothing has published
        to it yet.
        """
        try:
            await self._redis.xgroup_create(
                REDIS_CMD_STREAM,
                CMD_CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
            self._log.info("consumer group created",
                           extra={"group": CMD_CONSUMER_GROUP})
        except RedisError as exc:
            if "BUSYGROUP" in str(exc):
                return
            raise

    # ------------------------------------------------------------------
    # Publish (used by daemon and by this bridge)
    # ------------------------------------------------------------------

    async def publish_event(self, event: Event) -> None:
        """Write one event to the events stream. Non-fatal on failure."""
        try:
            await self._redis.xadd(
                REDIS_EVT_STREAM,
                {"payload": event.model_dump_json()},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        except RedisError:
            self._log.exception(
                "failed to publish event",
                extra={"kind": getattr(event, "kind", "unknown")},
            )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Consume commands until the stop event is set.

        Handles one command at a time. Start and stop operations are
        sub-second, so concurrency would add complexity without benefit.
        If a slow Redis write stalls the loop, the next command waits —
        the desired backpressure.
        """
        last_claim = 0.0

        while not stop.is_set():
            try:
                entries = await asyncio.wait_for(
                    self._redis.xreadgroup(
                        CMD_CONSUMER_GROUP,
                        self._consumer,
                        {REDIS_CMD_STREAM: ">"},
                        count=10,
                        block=_READ_BLOCK_MS,
                    ),
                    timeout=(_READ_BLOCK_MS / 1000.0) + 2.0,
                )
            except asyncio.TimeoutError:
                entries = []
            except RedisError:
                self._log.exception("xreadgroup failed; retrying in 2s")
                await asyncio.sleep(2.0)
                continue

            for _stream, messages in entries or []:
                for msg_id, fields in messages:
                    await self._handle_message(msg_id, fields)

            now = asyncio.get_event_loop().time()
            if now - last_claim > _CLAIM_INTERVAL_SEC:
                last_claim = now
                await self._claim_abandoned()

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, msg_id: str, fields: dict[str, Any]) -> None:
        payload = fields.get("payload")
        if not payload:
            self._log.warning("command has no payload",
                              extra={"msg_id": msg_id})
            await self._ack(msg_id)
            return

        try:
            command = parse_command(payload)
        except Exception:
            self._log.exception("unparsable command",
                                extra={"msg_id": msg_id})
            await self._ack(msg_id)
            return

        try:
            await self._dispatch(command)
        except Exception:
            self._log.exception("command handler raised",
                                extra={"kind": command.kind})
        finally:
            await self._ack(msg_id)

    async def _dispatch(self, command: Command) -> None:
        if isinstance(command, StartSessionCommand):
            await self._handle_start(command)
        elif isinstance(command, StopSessionCommand):
            await self._handle_stop(command)
        elif isinstance(command, PingCommand):
            self._log.debug("ping received",
                            extra={"request_id": command.request_id})
        else:
            self._log.warning("unknown command kind",
                              extra={"kind": getattr(command, "kind", "?")})

    async def _handle_start(self, cmd: StartSessionCommand) -> None:
        self._log.info("start command received", extra={
            "session_id": cmd.session_id,
            "session_name": cmd.session_name,
            "simulation_id": cmd.simulation_id,
            "request_id": cmd.request_id,
        })

        try:
            record = await self._manager.start_worker(
                session_id=cmd.session_id,
                session_name=cmd.session_name,
                simulation_id=cmd.simulation_id,
                config_params=cmd.config_params,
            )
        except AlreadyRunningError:
            self._log.info("start ignored; session already running",
                           extra={"session_id": cmd.session_id})
            return
        except WorkerStartupError as exc:
            await self.publish_event(SessionFailedEvent(
                session_id=cmd.session_id,
                session_name=cmd.session_name,
                error=f"startup failed: {exc}",
            ))
            return
        except WorkerManagerError as exc:
            await self.publish_event(SessionFailedEvent(
                session_id=cmd.session_id,
                session_name=cmd.session_name,
                error=f"manager error: {exc}",
            ))
            return

        await self.publish_event(SessionStartedEvent(
            session_id=record.session_id,
            session_name=record.session_name,
            simulation_id=record.simulation_id,
            port=record.port,
            pid=record.pid,
            systemd_unit="",
            advertised_endpoint="",
            request_id=cmd.request_id,
        ))

    async def _handle_stop(self, cmd: StopSessionCommand) -> None:
        self._log.info("stop command received", extra={
            "session_id": cmd.session_id,
            "session_name": cmd.session_name,
            "reason": cmd.reason,
            "request_id": cmd.request_id,
        })

        stopped = await self._manager.stop_worker(
            cmd.session_id, reason=cmd.reason,
        )
        if not stopped:
            self._log.info("stop ignored; session not tracked",
                           extra={"session_id": cmd.session_id})
            return

        await self.publish_event(SessionStoppedEvent(
            session_id=cmd.session_id,
            session_name=cmd.session_name,
            exit_code=0,
            reason=cmd.reason,
        ))

    # ------------------------------------------------------------------
    # Pending message recovery
    # ------------------------------------------------------------------

    async def _claim_abandoned(self) -> None:
        """Take over messages abandoned by a previous consumer.

        XAUTOCLAIM requires Redis 6.2+. On older servers the command
        fails and we disable the feature for the lifetime of the
        bridge. Recovery is a safety net for daemon crashes mid-handle,
        not a correctness requirement.
        """
        if self._xautoclaim_supported is False:
            return

        try:
            result = await self._redis.xautoclaim(
                REDIS_CMD_STREAM,
                CMD_CONSUMER_GROUP,
                self._consumer,
                min_idle_time=_CLAIM_MIN_IDLE_MS,
                start_id="0-0",
                count=10,
            )
        except RedisError as exc:
            msg = str(exc).lower()
            if "unknown command" in msg and "xautoclaim" in msg:
                if self._xautoclaim_supported is None:
                    self._log.warning(
                        "XAUTOCLAIM not supported by this Redis; "
                        "abandoned-message recovery disabled. "
                        "Upgrade to Redis 6.2 or newer to enable."
                    )
                self._xautoclaim_supported = False
                return
            self._log.exception("xautoclaim failed")
            return

        if self._xautoclaim_supported is None:
            self._xautoclaim_supported = True

        # redis-py returns (next_cursor, messages) on Redis 6.2 and
        # (next_cursor, messages, deleted_ids) on Redis 7.0+.
        if not result:
            return
        _, messages, *_ = result
        if not messages:
            return

        self._log.info("reclaimed abandoned commands",
                       extra={"count": len(messages)})
        for msg_id, fields in messages:
            await self._handle_message(msg_id, fields)

    # ------------------------------------------------------------------
    # Ack helper
    # ------------------------------------------------------------------

    async def _ack(self, msg_id: str) -> None:
        try:
            await self._redis.xack(
                REDIS_CMD_STREAM, CMD_CONSUMER_GROUP, msg_id,
            )
        except RedisError:
            # Message stays pending and is reclaimed later. Not fatal.
            self._log.warning("xack failed",
                              extra={"msg_id": msg_id})