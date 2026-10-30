"""Dashboard proxy: /s/<name>/ forwards to the worker's loopback server.

A tiny http.server stands in for the worker so no simulation has to
run. The error cases are the point: a student opening a link to a
session that is gone must get a clear answer, not a hang.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import TestCase

from control_plane.apps.sessions_mgr.models import Session


class _StubWorker(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            body, ctype = b"<html>worker page</html>", "text/html; charset=utf-8"
        elif self.path == "/api/state":
            body, ctype = b'{"state": {}}', "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxyTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.worker = ThreadingHTTPServer(("127.0.0.1", 0), _StubWorker)
        threading.Thread(target=cls.worker.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.worker.shutdown()
        cls.worker.server_close()
        super().tearDownClass()

    def _session(self, **kwargs) -> Session:
        fields = {
            "simulation_id": "cip",
            "slug": "cip_live",
            "status": Session.STATUS_RUNNING,
            "http_port": self.worker.server_address[1],
        }
        fields.update(kwargs)
        return Session.objects.create(**fields)

    def test_serves_worker_page(self):
        self._session()
        r = self.client.get("/s/cip_live/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"<html>worker page</html>")
        self.assertEqual(r["Content-Type"], "text/html; charset=utf-8")

    def test_relative_state_fetch_lands_on_worker(self):
        # shell.html fetches 'api/state' relative to /s/cip_live/.
        self._session()
        r = self.client.get("/s/cip_live/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"state": {}})
        self.assertEqual(r["Cache-Control"], "no-store")

    def test_worker_404_is_relayed(self):
        self._session()
        r = self.client.get("/s/cip_live/nope")
        self.assertEqual(r.status_code, 404)

    def test_session_without_slug_by_uuid(self):
        s = self._session(slug=None)
        r = self.client.get(f"/s/{s.id}/")
        self.assertEqual(r.status_code, 200)

    def test_bare_url_redirects_to_trailing_slash(self):
        self._session()
        r = self.client.get("/s/cip_live")
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], "/s/cip_live/")

    def test_unknown_session_404(self):
        r = self.client.get("/s/ghost/")
        self.assertEqual(r.status_code, 404)

    def test_not_running_409(self):
        self._session(status=Session.STATUS_STARTING)
        r = self.client.get("/s/cip_live/")
        self.assertEqual(r.status_code, 409)
        self.assertIn(b"starting", r.content)

    def test_no_http_port_yet_409(self):
        self._session(http_port=None)
        r = self.client.get("/s/cip_live/")
        self.assertEqual(r.status_code, 409)

    def test_dead_worker_502(self):
        self._session(http_port=_free_port())
        r = self.client.get("/s/cip_live/")
        self.assertEqual(r.status_code, 502)

    def test_only_get(self):
        self._session()
        r = self.client.post("/s/cip_live/")
        self.assertEqual(r.status_code, 405)
