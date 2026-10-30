"""Live updates: event consumer → channel group → WebSocket → browser.

Uses the in-memory channel layer so no Redis is needed. Talks raw ASGI
through asgiref's ApplicationCommunicator: channels.testing would be
shorter but imports daphne, which the project does not depend on.
"""

from __future__ import annotations

import json

from asgiref.testing import ApplicationCommunicator
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.test import SimpleTestCase, override_settings

from control_plane.asgi import application
from control_plane.apps.dashboard.consumers import SESSIONS_GROUP
from control_plane.apps.dashboard.routing import websocket_urlpatterns


_IN_MEMORY = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


@override_settings(CHANNEL_LAYERS=_IN_MEMORY)
class LiveUpdateTests(SimpleTestCase):
    async def _connect(self, origin: bytes = b"http://localhost:8000",
                       app=application):
        comm = ApplicationCommunicator(app, {
            "type": "websocket",
            "path": "/ws/sessions/",
            "headers": [(b"origin", origin), (b"host", b"localhost:8000")],
            "subprotocols": [],
        })
        await comm.send_input({"type": "websocket.connect"})
        reply = await comm.receive_output(timeout=2)
        return comm, reply["type"] == "websocket.accept"

    async def _disconnect(self, comm) -> None:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait(timeout=2)

    async def test_group_message_reaches_browser(self):
        comm, connected = await self._connect()
        self.assertTrue(connected)

        summary = {"id": "abc", "slug": "cip_live", "status": "running"}
        await get_channel_layer().group_send(SESSIONS_GROUP, {
            "type": "session.changed", "session": summary,
        })

        out = await comm.receive_output(timeout=2)
        self.assertEqual(out["type"], "websocket.send")
        self.assertEqual(json.loads(out["text"]),
                         {"type": "session.changed", "session": summary})
        await self._disconnect(comm)

    @override_settings(ALLOWED_HOSTS=["localhost"])
    async def test_foreign_origin_rejected(self):
        # The validator reads ALLOWED_HOSTS once, when it is built, and
        # dev settings allow "*". Build one under production-like hosts.
        app = AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns))
        comm, connected = await self._connect(
            origin=b"http://evil.example", app=app,
        )
        self.assertFalse(connected)
        await comm.wait(timeout=2)
