"""ASGI entry point.

Runs Django's HTTP handler and the Channels WebSocket handler behind
the same application. Uvicorn serves this in dev and prod.
"""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings.dev")

# Importing Django's ASGI application must happen before any module
# that touches the app registry (e.g. routing modules that import
# consumers). Order matters.
django_asgi = get_asgi_application()

from control_plane.apps.dashboard.routing import websocket_urlpatterns  # noqa: E402


application = ProtocolTypeRouter({
    "http": django_asgi,
    # The origin check rejects WebSocket connections opened by pages on
    # other sites; browsers do not apply same-origin rules to sockets.
    "websocket": AllowedHostsOriginValidator(
        URLRouter(websocket_urlpatterns),
    ),
})
