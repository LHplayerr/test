"""HTML views for the operator dashboard.

Reads the same Session rows the REST API does. Pages:

    /                          session list (no tokens, safe for anyone)
    /sessions/rows/            the list's table, re-fetched on live updates
    /sessions/new/             create-session form
    /sessions/<token>/         session detail with stop button
    /s/<name>/...              proxy to the worker's loopback dashboard

The detail page is gated by the capability token, exactly like the
REST endpoint it calls. The list never links to it: the only ways in
are the redirect after create and a bookmark.

Create and stop go through the REST API from the browser rather than
calling the API's internals, so the UI exercises the same code path as
curl and stack.sh.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
import uuid

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from control_plane.apps.sessions_mgr.models import Session, SimulationCatalog


# Worker dashboards are tiny and on loopback. If one does not answer
# within this, it is wedged; fail fast instead of tying up the browser.
_PROXY_TIMEOUT = 2.0

# The worker is always on loopback. An empty ProxyHandler stops urllib
# from routing these requests through an http_proxy set in the
# environment.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Response headers copied from the worker. Everything else (Server,
# Date, Content-Length, connection headers) is Django's business.
_PASSTHROUGH_HEADERS = ("Content-Type", "Cache-Control")


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------

def _list_context(request) -> dict:
    include_stopped = request.GET.get("include_stopped") in ("1", "true", "yes")
    qs = Session.objects.all()
    if not include_stopped:
        qs = qs.exclude(
            status__in=[Session.STATUS_STOPPED, Session.STATUS_FAILED],
        )
    return {
        "sessions": qs,
        "include_stopped": include_stopped,
        "endpoint_url": _endpoint_url(),
    }


def session_list(request):
    return render(request, "sessions/list.html", _list_context(request))


def session_rows(request):
    """Just the table, for the list page to swap in on a live update.

    Rendering stays on the server so the row markup lives in one
    template instead of being duplicated in JavaScript.
    """
    return render(request, "sessions/_table.html", _list_context(request))


# ---------------------------------------------------------------------------
# Create and detail
# ---------------------------------------------------------------------------

def session_create(request):
    simulations = SimulationCatalog.objects.filter(is_active=True)
    return render(request, "sessions/create.html", {
        "simulations": simulations,
    })


def session_detail(request, token: str):
    session = get_object_or_404(Session, token=token)
    return render(request, "sessions/detail.html", {
        "session": session,
        "endpoint_url": _endpoint_url(),
    })


def _endpoint_url() -> str:
    host = getattr(settings, "GATEWAY_PUBLIC_HOST", "")
    return f"opc.tcp://{host}/gateway/" if host else ""


# ---------------------------------------------------------------------------
# Dashboard proxy
# ---------------------------------------------------------------------------

@require_GET
async def session_proxy(request, name: str, path: str = ""):
    """Serve a worker's dashboard at /s/<name>/ through Django.

    The worker's HTTP server binds to 127.0.0.1 only, so the browser
    cannot reach it directly; Django fetches on its behalf. Only port
    8000 needs to be open in the firewall.

    This is an async view on purpose. Under ASGI, Django runs sync
    views on one shared thread, and every open dashboard polls four
    times a second. One wedged worker would then stall the whole UI
    for the length of the timeout. Here only the blocking HTTP call
    goes to a thread, and many can be in flight at once.
    """
    session = await _find_session(name)
    if session is None:
        return _plain(404, f"no session named {name!r}")

    if session.status != Session.STATUS_RUNNING or not session.http_port:
        return _plain(409, f"session {name!r} is {session.status}, not running")

    url = f"http://127.0.0.1:{session.http_port}/{path}"
    if request.META.get("QUERY_STRING"):
        url += "?" + request.META["QUERY_STRING"]

    try:
        status, headers, body = await asyncio.to_thread(_fetch, url)
    except (urllib.error.URLError, OSError):
        return _plain(502, f"session {name!r} dashboard is not responding")

    response = HttpResponse(body, status=status)
    for header in _PASSTHROUGH_HEADERS:
        if headers.get(header):
            response[header] = headers[header]
    return response


async def _find_session(name: str) -> Session | None:
    """Look up by slug first, then by UUID for sessions without one."""
    session = await Session.objects.filter(slug=name).afirst()
    if session is not None:
        return session
    try:
        session_id = uuid.UUID(name)
    except ValueError:
        return None
    return await Session.objects.filter(id=session_id).afirst()


def _fetch(url: str):
    """Blocking GET. Runs in a worker thread.

    A 4xx/5xx from the worker is a real answer and is relayed as-is;
    only a failure to get any answer raises.
    """
    try:
        with _LOOPBACK_OPENER.open(url, timeout=_PROXY_TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _plain(status: int, message: str) -> HttpResponse:
    return HttpResponse(
        message + "\n", status=status, content_type="text/plain; charset=utf-8",
    )
