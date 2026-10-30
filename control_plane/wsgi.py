"""WSGI entry point.

Provided so `manage.py runserver` works out of the box during
development. Production runs the ASGI application via uvicorn, which
supports both HTTP and WebSocket. Nothing in the project depends on
this file beyond the dev server.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings.dev")

application = get_wsgi_application()