from django.urls import path

from control_plane.apps.dashboard import views


app_name = "dashboard"

urlpatterns = [
    path("", views.session_list, name="session-list"),
    path("sessions/rows/", views.session_rows, name="session-rows"),
    path("sessions/new/", views.session_create, name="session-create"),
    path("sessions/<str:token>/", views.session_detail, name="session-detail"),
    # The trailing slash matters: the worker page fetches 'api/state'
    # relative to its own URL, which only lands under /s/<name>/ when
    # the page URL ends in a slash. APPEND_SLASH redirects the bare form.
    path("s/<str:name>/", views.session_proxy, name="session-proxy"),
    path("s/<str:name>/<path:path>", views.session_proxy,
         name="session-proxy-path"),
]
