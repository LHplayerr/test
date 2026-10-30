"""HTTP dashboard for a running simulation.

Serves one HTML page and one JSON endpoint. Runs in a background thread so
it does not interfere with the OPC UA asyncio loop. The simulation's
snapshot() is called from this thread, so plugins that mutate state in
step() must use internal locking (CIP does; blinker's operations are
atomic enough for teaching purposes).

Plugin contract for the UI:
  sim_plugins/<sim_id>/body.html  — markup injected into the shell
  sim_plugins/<sim_id>/ui.js      — defines window.renderState(state)

Both are optional. A plugin without them gets a shell with no content.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shared.logging import get_logger
from sim_runtime.base import BaseSimulation

_SHELL_PATH = Path(__file__).parent / "shell.html"
_PLUGIN_ROOT = Path(__file__).parent.parent / "sim_plugins"


def _compose_page(sim: BaseSimulation) -> bytes:
    """Read the shell and the plugin's body/script, substitute, return bytes.

    Done once at startup. If a plugin's body.html or ui.js is missing,
    the corresponding placeholder is replaced with an empty string.
    """
    shell = _SHELL_PATH.read_text(encoding="utf-8")
    plugin_dir = _PLUGIN_ROOT / sim.SIMULATION_ID

    body_path = plugin_dir / "body.html"
    body = body_path.read_text(encoding="utf-8") if body_path.exists() else ""

    script_path = plugin_dir / "ui.js"
    script = script_path.read_text(encoding="utf-8") if script_path.exists() else ""

    html = (
        shell
        .replace("{SIMULATION_NAME}", sim.SIMULATION_NAME)
        .replace("{PLUGIN_BODY}", body)
        .replace("{PLUGIN_SCRIPT}", script)
    )
    return html.encode("utf-8")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _make_handler(
    sim: BaseSimulation,
    *,
    session_id: str,
    endpoint: str,
    page_bytes: bytes,
):
    log = get_logger(__name__, session_id=session_id)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page_bytes)
                return

            if self.path == "/api/state":
                payload = json.dumps({
                    "sessionId": session_id,
                    "simulationId": sim.SIMULATION_ID,
                    "endpoint": endpoint,
                    "state": sim.snapshot(),
                }).encode("utf-8")
                self._send(200, "application/json; charset=utf-8",
                           payload, no_cache=True)
                return

            if self.path == "/health":
                self._send(200, "text/plain; charset=utf-8", b"ok")
                return

            self.send_error(404, "Not Found")

        def _send(self, code: int, ctype: str, body: bytes,
                  no_cache: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if no_cache:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # Route access logs through the structured logger at DEBUG.
            # At 4 Hz per browser this would otherwise flood stderr.
            log.debug("http %s %s", self.address_string(), fmt % args)

    return Handler


class HTTPDashboard:
    """Background HTTP server exposing the simulation state.

    Usage:
        dash = HTTPDashboard(sim, host="0.0.0.0", port=port,
                             session_id=sid, endpoint=endpoint)
        dash.start()
        ...
        dash.stop()
    """

    def __init__(
        self,
        sim: BaseSimulation,
        *,
        host: str,
        port: int,
        session_id: str,
        endpoint: str,
    ) -> None:
        self._log = get_logger(__name__, session_id=session_id, port=port)
        page = _compose_page(sim)
        handler = _make_handler(
            sim, session_id=session_id, endpoint=endpoint, page_bytes=page
        )
        self._server = _ReusableThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"http-{session_id[:8]}",
            daemon=True,
        )
        self._started = False

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        if self._started:
            return
        self._thread.start()
        self._started = True
        self._log.info("HTTP dashboard started")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=timeout)
        self._started = False
        self._log.info("HTTP dashboard stopped")