"""REST API for session management.

Every endpoint is public. Access to a specific session is gated by the
capability token in the URL.

Endpoints:

    GET    /api/simulations/                list available simulations
    GET    /api/sessions/                   list active sessions (no tokens)
    POST   /api/sessions/                   create a session
    GET    /api/sessions/<token>/           session detail (token required)
    DELETE /api/sessions/<token>/           stop a session (token required)
    GET    /api/sessions/<token>/events/    session event log
"""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from control_plane.apps.sessions_mgr import orchestrator
from control_plane.apps.sessions_mgr.models import (
    Session,
    SessionEvent,
    SimulationCatalog,
)
from control_plane.apps.sessions_mgr.serializers import (
    SessionCreateSerializer,
    SessionDetailSerializer,
    SessionEventSerializer,
    SessionSummarySerializer,
    SimulationCatalogSerializer,
)
from shared.logging import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------

@api_view(["GET"])
def list_simulations(request):  # noqa: ARG001
    qs = SimulationCatalog.objects.filter(is_active=True)
    return Response(SimulationCatalogSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Sessions collection (GET list, POST create)
# ---------------------------------------------------------------------------

@api_view(["GET", "POST"])
def sessions_collection(request):
    if request.method == "GET":
        return _list_sessions(request)
    return _create_session(request)


def _list_sessions(request):
    """Active sessions, safe to show to anyone.

    Only sessions that are not in a terminal state by default. Pass
    ?include_stopped=1 to see everything.
    """
    qs = Session.objects.all()
    include_stopped = request.query_params.get("include_stopped") in (
        "1", "true", "yes",
    )
    if not include_stopped:
        qs = qs.exclude(
            status__in=[Session.STATUS_STOPPED, Session.STATUS_FAILED]
        )
    return Response(SessionSummarySerializer(qs, many=True).data)


def _create_session(request):
    """Create a session and ask the daemon to start it.

    Returns the full session record including the capability token.
    This is the only time the token is returned in a list-shaped
    response; every other endpoint requires the token in the URL.

    If a slug is supplied and already in use, the prior session is
    stopped and its slug released so this request can take it over.
    """
    ser = SessionCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)

    sim_id = ser.validated_data["simulation_id"]
    catalog = SimulationCatalog.objects.get(simulation_id=sim_id)
    slug = ser.validated_data.get("slug")

    if slug:
        try:
            _reclaim_slug(slug)
        except Exception:
            log.exception("slug reclaim failed", extra={"slug": slug})
            return Response(
                {"detail": "could not stop prior session using this slug"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    try:
        session = Session.objects.create(
            simulation_id=sim_id,
            simulation_version=catalog.version,
            slug=slug,
            label=ser.validated_data.get("label", ""),
            config_params=ser.validated_data.get("config_params", {}),
            status=Session.STATUS_PENDING,
        )
    except IntegrityError:
        # Backstop for a race between _reclaim_slug and this create.
        # Only the slug column has a uniqueness constraint that user
        # input can realistically collide on.
        return Response(
            {"detail": f"slug {slug!r} is already in use"},
            status=status.HTTP_409_CONFLICT,
        )

    _record_event(session, SessionEvent.EVENT_CREATED)

    try:
        orchestrator.request_start(session)
    except Exception:
        log.exception("failed to publish start command",
                      extra={"session_id": session.external_id()})
        session.status = Session.STATUS_FAILED
        session.save(update_fields=["status"])
        _record_event(
            session, SessionEvent.EVENT_FAILED,
            detail={"error": "could not reach command bus"},
        )
        return Response(
            SessionDetailSerializer(session, context=_ctx()).data,
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        SessionDetailSerializer(session, context=_ctx()).data,
        status=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# Session detail (GET, DELETE)
# ---------------------------------------------------------------------------

@api_view(["GET", "DELETE"])
def session_detail(request, token: str):
    """Get or stop one session. Requires the capability token."""
    session = get_object_or_404(Session, token=token)

    if request.method == "GET":
        return Response(
            SessionDetailSerializer(session, context=_ctx()).data
        )

    if session.is_terminal:
        return Response(
            SessionDetailSerializer(session, context=_ctx()).data,
            status=status.HTTP_200_OK,
        )

    session.status = Session.STATUS_STOPPING
    session.save(update_fields=["status"])
    _record_event(session, SessionEvent.EVENT_STOPPING)

    try:
        orchestrator.request_stop(session, reason="user_request")
    except Exception:
        log.exception("failed to publish stop command",
                      extra={"session_id": session.external_id()})
        return Response(
            {"detail": "could not reach command bus"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        SessionDetailSerializer(session, context=_ctx()).data
    )


# ---------------------------------------------------------------------------
# Session events
# ---------------------------------------------------------------------------

@api_view(["GET"])
def session_events(request, token: str):
    """The event log for one session. Requires the capability token."""
    session = get_object_or_404(Session, token=token)
    events = session.events.all()[:100]
    return Response(SessionEventSerializer(events, many=True).data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reclaim_slug(slug: str) -> None:
    """Release `slug` from any existing session so a new one can take it.

    Stops the prior session if still live, then clears its slug field
    and moves it to a terminal state. Raises if the stop command
    cannot be published, in which case the prior row is left untouched
    so a retry can succeed later.
    """
    prior = Session.objects.filter(slug=slug).first()
    if prior is None:
        return

    if not prior.is_terminal:
        orchestrator.request_stop(prior, reason="replaced_by_new_session")

    prior.slug = None
    prior.status = Session.STATUS_STOPPED
    prior.stopped_at = timezone.now()
    prior.save(update_fields=["slug", "status", "stopped_at"])

    _record_event(prior, SessionEvent.EVENT_STOPPED, {
        "reason": "replaced_by_new_session",
    })


def _ctx() -> dict:
    return {"gateway_host": getattr(settings, "GATEWAY_PUBLIC_HOST", "")}


def _record_event(session: Session, event: str, detail: dict | None = None) -> None:
    SessionEvent.objects.create(
        session=session,
        event=event,
        detail=detail or {},
    )