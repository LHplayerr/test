"""Event consumer startup reconciliation ("ghost rows").

The reconciliation code runs its DB work in a thread, so these are
TransactionTestCases: rows must be committed to be visible there.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import fakeredis.aioredis
from channels.layers import get_channel_layer
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from control_plane.apps.dashboard.consumers import SESSIONS_GROUP
from control_plane.apps.sessions_mgr.management.commands.run_event_consumer import (
    Command,
)
from control_plane.apps.sessions_mgr.models import Session, SessionEvent
from shared.constants import HEARTBEAT_TTL, REDIS_HEARTBEAT_PREFIX


_IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _aged(session: Session, seconds: int = HEARTBEAT_TTL + 60) -> Session:
    # created_at is auto_now_add, so backdate it with an update.
    Session.objects.filter(id=session.id).update(
        created_at=timezone.now() - timedelta(seconds=seconds),
    )
    return session


@override_settings(CHANNEL_LAYERS=_IN_MEMORY)
class ReconcileTests(TransactionTestCase):
    def setUp(self) -> None:
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.cmd = Command()
        self.cmd._layer = get_channel_layer()

    def _run(self, coro):
        return asyncio.run(coro)

    def _make(self, status: str, **kwargs) -> Session:
        return _aged(Session.objects.create(
            simulation_id="cip", status=status,
            port=5000, http_port=6000, pid=123, **kwargs,
        ))

    def test_orphan_without_heartbeat_fails(self):
        s = self._make(Session.STATUS_RUNNING, slug="ghost")
        self._run(self.cmd._reconcile(self.redis))

        s.refresh_from_db()
        self.assertEqual(s.status, Session.STATUS_FAILED)
        self.assertIsNone(s.http_port)
        self.assertIsNone(s.port)
        self.assertIsNotNone(s.stopped_at)
        event = s.events.first()
        self.assertEqual(event.event, SessionEvent.EVENT_FAILED)
        self.assertEqual(event.detail, {"error": "orphaned_on_consumer_startup"})

    def test_live_session_with_heartbeat_untouched(self):
        s = self._make(Session.STATUS_RUNNING)

        async def go():
            await self.redis.set(f"{REDIS_HEARTBEAT_PREFIX}{s.id}", "{}")
            await self.cmd._reconcile(self.redis)

        self._run(go())
        s.refresh_from_db()
        self.assertEqual(s.status, Session.STATUS_RUNNING)

    def test_fresh_session_within_grace_untouched(self):
        s = Session.objects.create(simulation_id="cip",
                                   status=Session.STATUS_PENDING)
        self._run(self.cmd._reconcile(self.redis))
        s.refresh_from_db()
        self.assertEqual(s.status, Session.STATUS_PENDING)

    def test_stopping_orphan_becomes_stopped(self):
        s = self._make(Session.STATUS_STOPPING)
        self._run(self.cmd._reconcile(self.redis))
        s.refresh_from_db()
        self.assertEqual(s.status, Session.STATUS_STOPPED)
        self.assertEqual(s.events.first().event, SessionEvent.EVENT_STOPPED)

    def test_terminal_rows_ignored(self):
        s = self._make(Session.STATUS_STOPPED)
        self._run(self.cmd._reconcile(self.redis))
        self.assertEqual(s.events.count(), 0)

    def test_redis_down_touches_nothing(self):
        s = self._make(Session.STATUS_RUNNING)

        class Broken:
            async def exists(self, *_):
                raise ConnectionError("redis is down")

        self._run(self.cmd._reconcile(Broken()))
        s.refresh_from_db()
        self.assertEqual(s.status, Session.STATUS_RUNNING)

    def test_orphan_is_announced_without_token(self):
        s = self._make(Session.STATUS_RUNNING, slug="ghost")

        async def go():
            layer = self.cmd._layer
            channel = await layer.new_channel()
            await layer.group_add(SESSIONS_GROUP, channel)
            await self.cmd._reconcile(self.redis)
            return await asyncio.wait_for(layer.receive(channel), 2)

        msg = self._run(go())
        self.assertEqual(msg["type"], "session.changed")
        self.assertEqual(msg["session"]["id"], str(s.id))
        self.assertEqual(msg["session"]["status"], Session.STATUS_FAILED)
        self.assertNotIn("token", msg["session"])
