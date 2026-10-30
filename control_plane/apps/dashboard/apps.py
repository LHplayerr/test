from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane.apps.dashboard"
    verbose_name = "Operator Dashboard"