"""Correlation IDs, structured logging and per-stage timings.

Every request gets a correlation id that is attached to each log line and
returned in the ``X-Correlation-Id`` response header.  Each pipeline stage is
wrapped in ``stage()``, which records its duration and any counters into a
per-request trace.  That trace is returned alongside the answer, which is what
makes the assignment's Step 5 debugging questions answerable with data instead
of guesswork: for a slow or wrong answer you can see exactly which stage cost
the time and which chunks each stage passed on.

In production the same spans map onto Application Insights; the exporter is a
no-op unless APPLICATIONINSIGHTS_CONNECTION_STRING is set.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)
_trace: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "trace", default=None
)

_configured = False


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": get_correlation_id(),
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} " \
               f"[{get_correlation_id()[:8]}] {record.name}: {record.getMessage()}"
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += "  " + " ".join(f"{k}={v}" for k, v in extra.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # httpx logs every request at INFO; that is noise here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> "BoundLogger":
    return BoundLogger(logging.getLogger(name))


class BoundLogger:
    """Thin wrapper so call sites can pass structured fields as kwargs."""

    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    def _log(self, level: int, msg: str, **fields: Any) -> None:
        self._inner.log(level, msg, extra={"extra_fields": fields} if fields else None)

    def debug(self, msg: str, **f: Any) -> None:
        self._log(logging.DEBUG, msg, **f)

    def info(self, msg: str, **f: Any) -> None:
        self._log(logging.INFO, msg, **f)

    def warning(self, msg: str, **f: Any) -> None:
        self._log(logging.WARNING, msg, **f)

    def error(self, msg: str, **f: Any) -> None:
        self._log(logging.ERROR, msg, **f)

    def exception(self, msg: str, **f: Any) -> None:
        self._inner.exception(msg, extra={"extra_fields": f} if f else None)


# --------------------------------------------------------------------------
# Correlation id
# --------------------------------------------------------------------------


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(cid: str | None) -> str:
    cid = cid or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


# --------------------------------------------------------------------------
# Trace
# --------------------------------------------------------------------------


def start_trace() -> dict[str, Any]:
    tr: dict[str, Any] = {
        "correlation_id": get_correlation_id(),
        "stages": [],
        "tokens": {"prompt": 0, "completion": 0},
        "llm_calls": 0,
        "embedding_calls": 0,
        "cache_hits": 0,
        "started_at": time.time(),
    }
    _trace.set(tr)
    return tr


def current_trace() -> dict[str, Any] | None:
    return _trace.get()


def record_usage(prompt_tokens: int = 0, completion_tokens: int = 0,
                 llm_calls: int = 0, embedding_calls: int = 0,
                 cache_hits: int = 0) -> None:
    tr = current_trace()
    if tr is None:
        return
    tr["tokens"]["prompt"] += prompt_tokens
    tr["tokens"]["completion"] += completion_tokens
    tr["llm_calls"] += llm_calls
    tr["embedding_calls"] += embedding_calls
    tr["cache_hits"] += cache_hits


@contextmanager
def stage(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a pipeline stage and attach its measurements to the current trace.

    Yields a mutable dict; anything written into it lands in the trace, e.g.::

        with stage("retrieve") as st:
            hits = ...
            st["candidates"] = len(hits)
    """
    entry: dict[str, Any] = {"name": name, **fields}
    started = time.perf_counter()
    try:
        yield entry
    finally:
        entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
        tr = current_trace()
        if tr is not None:
            tr["stages"].append(entry)


def finish_trace() -> dict[str, Any]:
    tr = current_trace() or start_trace()
    tr["total_ms"] = round((time.time() - tr.pop("started_at", time.time())) * 1000, 1)
    return tr
