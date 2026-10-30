"""Session, catalog, and event models.

Design notes:

- ``Session`` holds the durable record of a simulation session. Runtime
  fields (status, port, pid) are mirrors of state that lives in Redis.
  The database is history; Redis is the truth about what is running now.

- ``SimulationCatalog`` is a snapshot of the plugin registry, refreshed
  by a management command. It lets the UI list available simulations
  without importing every plugin into the web process.

- ``SessionEvent`` is append-only. Every state transition is recorded.
  Retention is thirty days by default; a management command purges
  older rows.
"""

from __future__ import annotations

import re
import secrets
import uuid

from django.core.validators import RegexValidator
from django.db import models


# A slug is a legal IEC 61131-3 identifier, so it can be used verbatim
# in an OPC UA node path without client-side sanitization. This is what
# keeps CODESYS Data Sources stable across session re-creations.
_SLUG_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SLUG_RE = re.compile(_SLUG_PATTERN)
slug_validator = RegexValidator(
    regex=_SLUG_PATTERN,
    message=(
        "slug must be lowercase, start with a letter, and contain only "
        "letters, digits, and underscores (max 64 characters)"
    ),
)


def _capability_token() -> str:
    """Return a URL-safe capability token. 256 bits of randomness."""
    return secrets.token_urlsafe(32)


class SimulationCatalog(models.Model):
    """One row per plugin discovered in sim_plugins/.

    Populated by ``manage.py sync_catalog``. ``is_active`` lets an
    operator hide a plugin from the UI without deleting its row.
    """

    simulation_id = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128)
    version = models.CharField(max_length=32)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    discovered_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "simulation_catalog"
        ordering = ["simulation_id"]

    def __str__(self) -> str:
        return f"{self.simulation_id} ({self.version})"


class Session(models.Model):
    """A single simulation session."""

    STATUS_PENDING = "pending"
    STATUS_STARTING = "starting"
    STATUS_RUNNING = "running"
    STATUS_STOPPING = "stopping"
    STATUS_STOPPED = "stopped"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_STARTING, "Starting"),
        (STATUS_RUNNING, "Running"),
        (STATUS_STOPPING, "Stopping"),
        (STATUS_STOPPED, "Stopped"),
        (STATUS_FAILED, "Failed"),
    ]

    # UUID primary key so the URL token and the session ID are distinct
    # values; leaking one does not leak the other.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Capability token. Whoever presents this can view and stop the
    # session. Rotatable by setting a new value.
    token = models.CharField(
        max_length=64, unique=True, default=_capability_token, editable=False,
    )

    # Recorded at creation from SimulationCatalog, not from the plugin,
    # so the record is stable even if the plugin is later removed.
    simulation_id = models.CharField(max_length=64)
    simulation_version = models.CharField(max_length=32, blank=True)

    # Stable identifier used in the OPC UA tree instead of the UUID.
    # Recreating with the same slug stops the prior session and takes
    # over the name.
    slug = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        validators=[slug_validator],
    )

    label = models.CharField(max_length=128, blank=True)
    config_params = models.JSONField(default=dict, blank=True)

    # Runtime state, mirrored from Redis. The daemon is authoritative.
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    port = models.IntegerField(null=True, blank=True)
    http_port = models.IntegerField(null=True, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    advertised_endpoint = models.CharField(max_length=256, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        if self.slug:
            return f"{self.slug} [{self.status}]"
        if self.label:
            return f"{self.label} [{self.status}]"
        return f"{self.id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.STATUS_STOPPED, self.STATUS_FAILED)

    def external_id(self) -> str:
        """Identifier used in the OPC UA address space.

        The slug when one is set, otherwise the string form of the UUID.
        """
        return self.slug or str(self.id)

    def save(self, *args, **kwargs):
        # Field validators run on form full_clean(), not on direct
        # model construction. A malformed slug breaks the OPC UA tree
        # for CODESYS, so reject it here too.
        if self.slug and not _SLUG_RE.match(self.slug):
            raise ValueError(
                f"invalid session slug {self.slug!r}: must be lowercase, "
                "start with a letter, and contain only letters, digits, "
                "and underscores (max 64 characters)"
            )
        super().save(*args, **kwargs)


class SessionEvent(models.Model):
    """Append-only lifecycle event log."""

    EVENT_CREATED = "created"
    EVENT_STARTING = "starting"
    EVENT_STARTED = "started"
    EVENT_READY = "ready"
    EVENT_STOPPING = "stopping"
    EVENT_STOPPED = "stopped"
    EVENT_FAILED = "failed"
    EVENT_REAPED = "reaped"
    EVENT_CHOICES = [
        (EVENT_CREATED, "Created"),
        (EVENT_STARTING, "Starting"),
        (EVENT_STARTED, "Started"),
        (EVENT_READY, "Ready"),
        (EVENT_STOPPING, "Stopping"),
        (EVENT_STOPPED, "Stopped"),
        (EVENT_FAILED, "Failed"),
        (EVENT_REAPED, "Reaped"),
    ]

    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="events",
    )
    event = models.CharField(max_length=16, choices=EVENT_CHOICES)
    detail = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_events"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["session", "occurred_at"]),
            models.Index(fields=["occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.session_id} {self.event} @ {self.occurred_at}"