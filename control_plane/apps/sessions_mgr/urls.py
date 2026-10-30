from django.urls import path

from control_plane.apps.sessions_mgr import views


urlpatterns = [
    path("simulations/", views.list_simulations, name="list-simulations"),
    path("sessions/", views.sessions_collection, name="sessions-collection"),
    path(
        "sessions/<str:token>/",
        views.session_detail,
        name="session-detail",
    ),
    path(
        "sessions/<str:token>/events/",
        views.session_events,
        name="session-events",
    ),
]