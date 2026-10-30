"""Message schemas for the Redis Streams boundary.

Every command and every event is one of these models. Django and the
daemon import the same definitions, so a field added on one side but
not the other fails at parse time rather than being silently dropped.

Session identification is split: ``session_id`` is the UUID, used for
database lookups and Redis keys; ``session_name`` is the slug when one
is set, otherwise the UUID, and is what appears in the OPC UA address
space. The two can diverge when a slug is reused, and identity is
always the UUID.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _StrictBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Commands: Django -> Worker Pool
# ---------------------------------------------------------------------------

class _CommandBase(_StrictBase):
    request_id: str = Field(description="Echoed back in the resulting event.")
    issued_at: datetime = Field(default_factory=_now)


class StartSessionCommand(_CommandBase):
    kind: Literal["start_session"] = "start_session"
    session_id: str
    session_name: str
    simulation_id: str
    config_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id", "session_name", "simulation_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or len(v) > 128:
            raise ValueError("must be 1-128 chars")
        return v


class StopSessionCommand(_CommandBase):
    kind: Literal["stop_session"] = "stop_session"
    session_id: str
    session_name: str
    reason: Literal[
        "user_request",
        "idle_timeout",
        "admin_action",
        "replaced_by_new_session",
    ] = "user_request"


class PingCommand(_CommandBase):
    kind: Literal["ping"] = "ping"


# ---------------------------------------------------------------------------
# Events: Worker Pool -> Django
# ---------------------------------------------------------------------------

class _EventBase(_StrictBase):
    occurred_at: datetime = Field(default_factory=_now)


class SessionStartedEvent(_EventBase):
    """Emitted by the daemon when a worker process is spawned."""

    kind: Literal["session_started"] = "session_started"
    session_id: str
    session_name: str
    simulation_id: str
    port: int
    pid: int
    systemd_unit: str
    advertised_endpoint: str
    request_id: str | None = None


class SessionReadyEvent(_EventBase):
    """Emitted by the worker itself once its OPC UA port is bound."""

    kind: Literal["session_ready"] = "session_ready"
    session_id: str
    session_name: str
    simulation_id: str
    port: int
    http_port: int
    pid: int


class SessionStoppedEvent(_EventBase):
    kind: Literal["session_stopped"] = "session_stopped"
    session_id: str
    session_name: str
    exit_code: int | None = None
    reason: str = "user_request"


class SessionFailedEvent(_EventBase):
    kind: Literal["session_failed"] = "session_failed"
    session_id: str
    session_name: str
    error: str


# ---------------------------------------------------------------------------
# Discriminated unions + parsers
# ---------------------------------------------------------------------------

Command = Annotated[
    Union[StartSessionCommand, StopSessionCommand, PingCommand],
    Field(discriminator="kind"),
]

Event = Annotated[
    Union[
        SessionStartedEvent,
        SessionReadyEvent,
        SessionStoppedEvent,
        SessionFailedEvent,
    ],
    Field(discriminator="kind"),
]

_COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)
_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def parse_command(raw: bytes | str) -> Command:
    return _COMMAND_ADAPTER.validate_json(raw)


def parse_event(raw: bytes | str) -> Event:
    return _EVENT_ADAPTER.validate_json(raw)