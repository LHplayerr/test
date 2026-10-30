"""Sync the simulation catalog with the plugins on disk.

Reads every plugin's SIMULATION_ID, name, and version via the same
registry the worker uses, and upserts one row per plugin. Deactivating
a plugin that no longer exists rather than deleting its row preserves
any session history that references it.

Usage:
    python manage.py sync_catalog
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from control_plane.apps.sessions_mgr.models import SimulationCatalog
from sim_runtime.registry import SimulationRegistry


class Command(BaseCommand):
    help = "Sync SimulationCatalog with the plugin registry."

    def handle(self, *args, **options) -> None:
        registry = SimulationRegistry()
        registry.load_from()

        seen_ids: set[str] = set()
        created = updated = 0

        for sim_id in registry.ids():
            cls = registry.get(sim_id)
            seen_ids.add(sim_id)

            _, was_created = SimulationCatalog.objects.update_or_create(
                simulation_id=sim_id,
                defaults={
                    "display_name": cls.SIMULATION_NAME,
                    "version": cls.SIMULATION_VERSION,
                    "description": (cls.__doc__ or "").strip().split("\n")[0],
                    "is_active": True,
                    "discovered_at": timezone.now(),
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        # Deactivate rows for plugins that no longer exist on disk.
        deactivated = SimulationCatalog.objects.exclude(
            simulation_id__in=seen_ids
        ).update(is_active=False)

        self.stdout.write(self.style.SUCCESS(
            f"catalog: {created} created, {updated} updated, "
            f"{deactivated} deactivated"
        ))