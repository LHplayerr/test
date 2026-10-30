from django.contrib import admin

from control_plane.apps.sessions_mgr.models import (
    Session,
    SessionEvent,
    SimulationCatalog,
)


@admin.register(SimulationCatalog)
class SimulationCatalogAdmin(admin.ModelAdmin):
    list_display = ("simulation_id", "display_name", "version", "is_active")
    list_filter = ("is_active",)
    search_fields = ("simulation_id", "display_name")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("id", "label", "simulation_id", "status", "created_at")
    list_filter = ("status", "simulation_id")
    search_fields = ("id", "label", "token")
    readonly_fields = ("id", "token", "created_at")


@admin.register(SessionEvent)
class SessionEventAdmin(admin.ModelAdmin):
    list_display = ("session_id", "event", "occurred_at")
    list_filter = ("event",)
    readonly_fields = ("session", "event", "detail", "occurred_at")

    def has_add_permission(self, request):  # noqa: ARG002
        return False

    def has_change_permission(self, request, obj=None):  # noqa: ARG002
        return False