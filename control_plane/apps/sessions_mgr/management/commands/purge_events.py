"""Delete SessionEvent rows older than the retention window.

Default retention is 30 days, configurable with --days. Run from cron
or a systemd timer.

Usage:
    python manage.py purge_events
    python manage.py purge_events --days 7
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from control_plane.apps.sessions_mgr.models import SessionEvent


class Command(BaseCommand):
    help = "Delete session events older than the retention window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days", type=int, default=30,
            help="Retention window in days (default: 30).",
        )

    def handle(self, *args, **options) -> None:
        days = options["days"]
        if days < 1:
            self.stderr.write("--days must be >= 1")
            return

        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = SessionEvent.objects.filter(
            occurred_at__lt=cutoff
        ).delete()

        self.stdout.write(self.style.SUCCESS(
            f"purged {deleted} event(s) older than {days} day(s)"
        ))