"""WebSocket consumer for live session updates.

The event consumer (run_event_consumer) writes each lifecycle event to
the database and then announces it to the SESSIONS_GROUP channel group.
Every connected browser is a member of that group and gets the message
forwarded as JSON. Browsers only listen; nothing they send is acted on.

Messages carry the session summary, never the capability token: the
group is broadcast to every open list page.
"""

from __future__ import annotations

from channels.generic.websocket import AsyncJsonWebsocketConsumer


SESSIONS_GROUP = "sessions"


class SessionsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self) -> None:
        await self.channel_layer.group_add(SESSIONS_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, code: int) -> None:  # noqa: ARG002
        await self.channel_layer.group_discard(SESSIONS_GROUP, self.channel_name)

    async def session_changed(self, message: dict) -> None:
        """Handler for group messages of type "session.changed"."""
        await self.send_json({
            "type": "session.changed",
            "session": message["session"],
        })
