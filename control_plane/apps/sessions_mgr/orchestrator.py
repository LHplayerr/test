"""Session orchestrator.

The Django side of the Redis bridge. Publishes StartSessionCommand and
StopSessionCommand to the command stream; a separate consumer process
receives lifecycle events on the event stream.

Uses the synchronous redis client. Django views are synchronous by
default and the operations here are single XADD calls, so an async
client would add complexity without benefit. The event consumer, which
is a long-running process, uses the async client instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import redis
from django.conf import settings

from shared.constants import REDIS_CMD_STREAM, STREAM_MAXLEN
from shared.logging import get_logger
from shared.schemas import (
    PingCommand,
    StartSessionCommand,
    StopSessionCommand,
)


log = get_logger(__name__)


_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        url = getattr(settings, "REDIS_URL", None) or "redis://localhost:6379/0"
        _client = redis.from_url(url, decode_responses=True)
    return _client


def reset_for_tests() -> None:
    global _client
    _client = None


def _publish(command) -> str:
    client = _get_client()
    client.xadd(
        REDIS_CMD_STREAM,
        {"payload": command.model_dump_json()},
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    return command.request_id


def request_start(session) -> str:
    """Publish a start command for the given Session row.

    session_id carries the UUID (identity for the daemon and Redis),
    session_name carries the slug or UUID (the folder the gateway
    creates in the OPC UA tree). Both are always sent, so a slug
    reuse cannot confuse the two.
    """
    session_id = str(session.id)
    session_name = session.external_id()
    command = StartSessionCommand(
        session_id=session_id,
        session_name=session_name,
        simulation_id=session.simulation_id,
        config_params=session.config_params or {},
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    request_id = _publish(command)
    log.info("start command published", extra={
        "session_id": session_id,
        "session_name": session_name,
        "simulation_id": session.simulation_id,
        "request_id": request_id,
    })
    return request_id


def request_stop(session, reason: str = "user_request") -> str:
    """Publish a stop command for the given Session row."""
    session_id = str(session.id)
    session_name = session.external_id()
    command = StopSessionCommand(
        session_id=session_id,
        session_name=session_name,
        reason=reason,
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    request_id = _publish(command)
    log.info("stop command published", extra={
        "session_id": session_id,
        "session_name": session_name,
        "reason": reason,
        "request_id": request_id,
    })
    return request_id


def ping() -> str:
    """Send a liveness ping. Useful for health checks."""
    command = PingCommand(
        request_id=str(uuid.uuid4()),
        issued_at=datetime.now(timezone.utc),
    )
    return _publish(command)