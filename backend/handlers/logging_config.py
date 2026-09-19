"""Structured single-line JSON logging for the Lambda handlers.

Every handler logs through this module so there is *one* logging setup, not an
ad-hoc configuration per handler (engineering-practices steering §6). The
logger emits one single-line JSON object per event to stdout, which CloudWatch
captures as a single log line — so ``print()`` is never used and a record must
never span more than one physical line.

Two things are deliberate:

* **Configure once, idempotently.** ``configure_logging()`` runs at Lambda
  module load. A cold start imports several handler modules, and Lambda reuses a
  warm container across invocations, so the call happens repeatedly; it must not
  stack duplicate ``StreamHandler``\\s or every line would be emitted several
  times. The guard keeps exactly one handler attached.

* **Whitelist the context, never dump the record.** Handlers attach context via
  ``extra={...}`` on the logging call. Only the known keys — ``gameId``,
  ``playerId``, ``endpoint``, ``traceId`` — are copied into the JSON. Serializing
  the whole ``LogRecord.__dict__`` would risk leaking whatever a caller happened
  to attach, which is exactly the "never log anything sensitive" rule; the
  whitelist makes that impossible by construction.

The module is pure standard library: no ``boto3``, no AWS, no third-party
dependency.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any, Final

# The single logger every handler configures and logs through. A named,
# non-root logger keeps the demo's output isolated from anything the runtime or
# a dependency attaches to the root logger.
LOGGER_NAME: Final = "xray_sudoku"

# The only context keys copied from a record's ``extra`` into the JSON output.
# Anything else a caller attaches is dropped, so an accidental token or a
# wholesale header never reaches the logs (steering §6, "never log secrets").
CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "gameId",
    "playerId",
    "endpoint",
    "traceId",
)

# ``LogRecord`` attribute names that map onto the fixed part of the JSON schema.
# Held so :class:`JsonFormatter` can distinguish schema fields and whitelisted
# context from every other (ignored) record attribute.
_TIMESTAMP_FMT: Final = "%Y-%m-%dT%H:%M:%S"


class JsonFormatter(logging.Formatter):
    """Serialize a :class:`logging.LogRecord` to a single-line JSON object.

    The emitted object always carries ``timestamp``, ``level``, ``logger``, and
    ``message``; it adds any of the whitelisted :data:`CONTEXT_FIELDS` that were
    supplied via ``extra=``; and, when an exception is being logged (e.g. via
    ``logger.exception``), it adds a nested ``exception`` object with the type,
    message, and a traceback string. ``json.dumps`` escapes any newline in a
    message or traceback, so the result is always exactly one physical line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, _TIMESTAMP_FMT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Copy only whitelisted context the caller attached via ``extra=``.
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        # Include structured exception info when logging inside an except block.
        if record.exc_info is not None:
            payload["exception"] = _exception_payload(record.exc_info)

        # ``json.dumps`` escapes embedded newlines, keeping the record on one
        # line; ``ensure_ascii=False`` keeps non-ASCII readable without ever
        # introducing a line break.
        return json.dumps(payload, ensure_ascii=False)


def _exception_payload(exc_info: Any) -> dict[str, str]:
    """Build a single-line-safe exception object from ``record.exc_info``.

    Returns the exception type name, its message, and a traceback rendered as a
    single string; ``json.dumps`` later escapes the embedded newlines so the
    surrounding record stays on one physical line.
    """
    exc_type, exc_value, exc_tb = exc_info
    type_name = exc_type.__name__ if exc_type is not None else "None"
    message = str(exc_value) if exc_value is not None else ""
    tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
    return {"type": type_name, "message": message, "traceback": tb}


def _install_json_handler(logger: logging.Logger) -> None:
    """Attach exactly one JSON stdout handler to ``logger`` (idempotent).

    Attaches a single ``StreamHandler`` writing single-line JSON to stdout, sets
    the level to ``INFO``, and disables propagation so the record is not
    re-emitted through any handler the runtime attached to the root logger. A
    repeat call detects the marker and leaves the one handler in place rather
    than stacking duplicates that would emit each line again.
    """
    # Reuse the JSON handler already installed on this logger if there is one,
    # so a repeat call neither adds a duplicate nor churns a new handler. Any
    # foreign handlers (e.g. a test-runner capture handler) are discarded so the
    # logger ends up with exactly one, our JSON, handler.
    existing = next(
        (h for h in logger.handlers if isinstance(h.formatter, JsonFormatter)),
        None,
    )
    handler = existing if existing is not None else logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def configure_logging() -> logging.Logger:
    """Configure the shared logger once and return it (idempotent).

    Runs at Lambda module load. The first call attaches the single JSON stdout
    handler; subsequent calls are no-ops, so the warm-container reuse and the
    several handler imports on a cold start never stack duplicate handlers.
    """
    logger = logging.getLogger(LOGGER_NAME)
    _install_json_handler(logger)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger for structured JSON logging.

    With no ``name`` the shared, configured logger is returned. With a ``name``
    (typically ``__name__``), that named logger is returned carrying its own
    single JSON handler, so its ``logger`` field identifies the module while the
    output shape and single-line contract stay identical. Configuration is
    ensured on first use and is idempotent per logger.
    """
    shared = configure_logging()
    if name is None or name == LOGGER_NAME:
        return shared
    logger = logging.getLogger(name)
    _install_json_handler(logger)
    return logger
