"""Consume lifecycle events from the daemon and update the database.

Long-lived process. Reads the events stream via a consumer group,
updates the corresponding Session row, appends a SessionEvent, and
XACKs. After each change it announces the session to the dashboard's
channel group so open browser pages update live. Run as a systemd
service in production, or by hand during development:

    python manage.py run_event_consumer

On startup it first reconciles the database against Redis: any session
the database thinks is live but that has no heartbeat key is an orphan
left behind by a crash or a stack reset, and is closed out.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import redis.asyncio as aioredis
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from control_plane.apps.dashboard.consumers import SESSIONS_GROUP
from control_plane.apps.sessions_mgr.models import Session, SessionEvent
from shared.constants import (
    EVT_CONSUMER_GROUP,
    HEARTBEAT_TTL,
    REDIS_EVT_STREAM,
    REDIS_HEARTBEAT_PREFIX,
)
from shared.logging import get_logger
from shared.schemas import (
    SessionFailedEvent,
    SessionReadyEvent,
    SessionStartedEvent,
    SessionStoppedEvent,
    parse_event,
)


log = get_logger(__name__)


_READ_BLOCK_MS = 2000

_ORPHAN_REASON = "orphaned_on_consumer_startup"


class Command(BaseCommand):
    help = "Consume session lifecycle events and update the database."

    def handle(self, *args, **options) -> None:  # noqa: ARG002
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            self.stdout.write("interrupted")

    async def _run(self) -> None:
        redis_url = getattr(settings, "REDIS_URL", None) \
            or "redis://localhost:6379/0"
        client = aioredis.from_url(redis_url, decode_responses=True)
        self._layer = get_channel_layer()

        await self._ensure_group(client)
        await self._reconcile(client)
        consumer = f"control-plane-{timezone.now().timestamp():.0f}"

        self.stdout.write(f"event consumer started as {consumer}")
        log.info("event consumer started", extra={"consumer": consumer})

        try:
            while True:
                await self._consume_once(client, consumer)
        finally:
            await client.aclose()

    async def _ensure_group(self, client: aioredis.Redis) -> None:
        try:
            await client.xgroup_create(
                REDIS_EVT_STREAM, EVT_CONSUMER_GROUP,
                id="0", mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_once(
        self, client: aioredis.Redis, consumer: str
    ) -> None:
        try:
            entries = await asyncio.wait_for(
                client.xreadgroup(
                    EVT_CONSUMER_GROUP, consumer,
                    {REDIS_EVT_STREAM: ">"},
                    count=20, block=_READ_BLOCK_MS,
                ),
                timeout=(_READ_BLOCK_MS / 1000.0) + 2.0,
            )
        except asyncio.TimeoutError:
            return
        except Exception:
            log.exception("xreadgroup failed")
            await asyncio.sleep(1.0)
            return

        for _stream, messages in entries or []:
            for msg_id, fields in messages:
                await self._handle(fields)
                try:
                    await client.xack(
                        REDIS_EVT_STREAM, EVT_CONSUMER_GROUP, msg_id,
                    )
                except Exception:
                    log.exception("xack failed", extra={"msg_id": msg_id})

    async def _handle(self, fields: dict) -> None:
        payload = fields.get("payload")
        if not payload:
            return
        try:
            event = parse_event(payload)
        except Exception:
            log.exception("unparsable event")
            return

        # DB work is synchronous; run it in a thread so the event loop
        # stays responsive.
        session = await asyncio.to_thread(self._apply, event)
        if session is not None:
            await self._announce(session)

    async def _announce(self, session: Session) -> None:
        """Tell open dashboard pages that `session` changed.

        Best effort. The database is already updated and the event will
        be XACKed regardless; a page that misses this catches up on its
        next reload or reconnect.
        """
        if self._layer is None:
            return
        try:
            await self._layer.group_send(SESSIONS_GROUP, {
                "type": "session.changed",
                "session": _summary(session),
            })
        except Exception:
            log.exception("could not announce session change",
                          extra={"session_id": str(session.id)})

    async def _reconcile(self, client: aioredis.Redis) -> None:
        """Close out sessions the database thinks are live but are not.

        A live worker refreshes cip:hb:<uuid> every HEARTBEAT_INTERVAL
        seconds with a HEARTBEAT_TTL expiry, so a missing key means no
        worker. Sessions younger than HEARTBEAT_TTL are skipped: a fresh
        session has not had time to write its first heartbeat.

        If Redis cannot be read, nothing is touched. Without heartbeat
        data every session would look like an orphan.
        """
        try:
            candidates = await asyncio.to_thread(_live_sessions_before_grace)
            orphans = [
                s for s in candidates
                if not await client.exists(f"{REDIS_HEARTBEAT_PREFIX}{s.id}")
            ]
        except Exception:
            log.exception("reconciliation skipped: could not read state")
            return

        closed = await asyncio.to_thread(_close_orphans, orphans)
        log.info("reconciliation done",
                 extra={"checked": len(candidates), "orphans": len(closed)})
        for session in closed:
            await self._announce(session)

    def _apply(self, event) -> Session | None:
        close_old_connections()

        session = _lookup_session(event.session_id)
        if session is None:
            log.warning("event for unknown session",
                        extra={"session_id": event.session_id,
                               "session_name": event.session_name,
                               "kind": event.kind})
            return None

        if isinstance(event, SessionStartedEvent):
            session.status = Session.STATUS_RUNNING
            session.port = event.port
            session.pid = event.pid
            session.advertised_endpoint = event.advertised_endpoint
            if session.started_at is None:
                session.started_at = timezone.now()
            session.save(update_fields=[
                "status", "port", "pid",
                "advertised_endpoint", "started_at",
            ])
            _record(session, SessionEvent.EVENT_STARTED, {
                "port": event.port,
                "pid": event.pid,
            })

        elif isinstance(event, SessionReadyEvent):
            # The worker is accepting connections now. Record the
            # dashboard port so the control plane can proxy to it.
            session.http_port = event.http_port
            session.save(update_fields=["http_port"])
            _record(session, SessionEvent.EVENT_READY, {
                "port": event.port,
                "http_port": event.http_port,
                "pid": event.pid,
            })

        elif isinstance(event, SessionStoppedEvent):
            session.status = Session.STATUS_STOPPED
            session.stopped_at = timezone.now()
            session.port = None
            session.http_port = None
            session.pid = None
            session.advertised_endpoint = ""
            session.save(update_fields=[
                "status", "stopped_at", "port", "http_port", "pid",
                "advertised_endpoint",
            ])
            _record(session, SessionEvent.EVENT_STOPPED, {
                "reason": event.reason,
                "exit_code": event.exit_code,
            })

        elif isinstance(event, SessionFailedEvent):
            session.status = Session.STATUS_FAILED
            session.stopped_at = timezone.now()
            session.http_port = None
            session.save(update_fields=["status", "stopped_at", "http_port"])
            _record(session, SessionEvent.EVENT_FAILED, {
                "error": event.error,
            })

        return session


def _summary(session: Session) -> dict:
    """What the browser is told about a change. Never the token."""
    return {
        "id": str(session.id),
        "slug": session.slug,
        "simulation_id": session.simulation_id,
        "status": session.status,
        "port": session.port,
        "http_port": session.http_port,
    }


def _live_sessions_before_grace() -> list[Session]:
    close_old_connections()
    cutoff = timezone.now() - timedelta(seconds=HEARTBEAT_TTL)
    return list(
        Session.objects
        .exclude(status__in=[Session.STATUS_STOPPED, Session.STATUS_FAILED])
        .filter(created_at__lt=cutoff)
    )


def _close_orphans(orphans: list[Session]) -> list[Session]:
    """Move orphans to a terminal state. Returns the rows changed.

    A session that was already stopping is recorded as stopped, since
    that is where it was headed; anything else failed. The update is
    conditional on the status read earlier, so a row changed in the
    meantime (e.g. a stop from the API) is left alone.
    """
    close_old_connections()
    closed = []
    for s in orphans:
        stopping = s.status == Session.STATUS_STOPPING
        new_status = Session.STATUS_STOPPED if stopping else Session.STATUS_FAILED
        changed = Session.objects.filter(id=s.id, status=s.status).update(
            status=new_status,
            stopped_at=timezone.now(),
            port=None,
            http_port=None,
            pid=None,
            advertised_endpoint="",
        )
        if not changed:
            continue
        s.refresh_from_db()
        if stopping:
            _record(s, SessionEvent.EVENT_STOPPED, {"reason": _ORPHAN_REASON})
        else:
            _record(s, SessionEvent.EVENT_FAILED, {"error": _ORPHAN_REASON})
        closed.append(s)
    return closed


def _lookup_session(session_id: str) -> Session | None:
    """Find a session by its UUID.

    Events carry the UUID in session_id and the display name in
    session_name. Lookup is unambiguous even when a slug has been
    reused across sessions.
    """
    try:
        uuid.UUID(session_id)
    except (ValueError, TypeError):
        return None
    return Session.objects.filter(id=session_id).first()


def _record(session: Session, event: str, detail: dict) -> None:
    SessionEvent.objects.create(
        session=session, event=event, detail=detail or {},
    )