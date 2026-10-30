from django.apps import AppConfig


class SessionsMgrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "control_plane.apps.sessions_mgr"
    verbose_name = "Session Manager"