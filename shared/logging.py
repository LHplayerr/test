"""Structured JSON logging shared by Django and the worker pool.

One format means one parser, one filter set, and one way to correlate
log lines across processes by session_id.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any


_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per record, newline-delimited.

    Any keyword passed via ``extra=`` becomes a top-level field. Standard
    LogRecord attributes are filtered out; everything else is included.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS:
                continue
            if value is None:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Call once per process."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # asyncua logs per-message at INFO on some transports. At 10 Hz per
    # session, that is unusable noise in the logs.
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger that stamps the given context on every record.

        log = get_logger(__name__, session_id=sid)
        log.info("worker started")   # -> {"session_id": sid, ...}
    """
    return logging.LoggerAdapter(logging.getLogger(name), context)