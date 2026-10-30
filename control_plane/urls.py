"""URL configuration."""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.urls import include, path


def health(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health, name="health"),
    path("api/", include("control_plane.apps.sessions_mgr.urls")),
    path("", include("control_plane.apps.dashboard.urls")),
]