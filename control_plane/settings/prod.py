"""Production settings.

Assumes a reverse proxy or direct exposure on the intranet. Cookies
are marked secure, HSTS is on, and the app refuses to start without a
real secret key.
"""

import os

from control_plane.settings.base import *  # noqa: F401,F403
from control_plane.settings.base import _env_list


DEBUG = False

if os.environ.get("DJANGO_SECRET_KEY", "").startswith("dev-"):
    raise RuntimeError(
        "DJANGO_SECRET_KEY is still the development default. "
        "Set a real secret in /etc/cip-sim/env."
    )

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS")

# Cookies. Even though there is no login, the CSRF and session cookies
# are still used and should carry the Secure flag when served over TLS.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# HSTS is only meaningful over TLS. If TLS is terminated upstream and
# forwarded as HTTPS, enable this; otherwise leave it off. It is
# enabled by setting DJANGO_HSTS=1 in the environment.
if os.environ.get("DJANGO_HSTS", "").lower() in ("1", "true", "yes"):
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Trust the X-Forwarded-Proto header when behind a reverse proxy that
# terminates TLS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")