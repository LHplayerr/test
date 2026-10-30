from django.urls import path

from control_plane.apps.dashboard import consumers


websocket_urlpatterns = [
    path("ws/sessions/", consumers.SessionsConsumer.as_asgi()),
]
