"""Development settings.

DEBUG on, verbose errors, SQLite by default, permissive hosts. Not
suitable for anything reachable from another machine.
"""

from control_plane.settings.base import *  # noqa: F401,F403
from control_plane.settings.base import _env_bool


DEBUG = True

ALLOWED_HOSTS = ["*"]

# CSRF protection stays on, but in dev we accept connections over
# plain HTTP from any host.
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

# SQLite by default; can be overridden by DATABASE_URL in the shell.
# Nothing in the project depends on Postgres-specific features.