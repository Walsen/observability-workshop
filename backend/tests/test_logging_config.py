"""Unit tests for the structured JSON logger (task 7.2).

These pin the behavior of ``backend.handlers.logging_config``: a logger
configured once at module load that emits a *single-line* JSON object per event
to stdout (never ``print()``), carrying the whitelisted context fields
``gameId``, ``playerId``, ``endpoint``, and ``traceId`` when they are supplied
via ``extra=`` on the logging call (engineering-practices steering §6; design
"Logging").

Rather than capture the module's real stdout handler, each test attaches a
handler writing to an in-memory ``StringIO`` and formatted by the module's own
``JsonFormatter``, so the emitted text is asserted directly: it must be valid
single-line JSON with the expected level, message, and context — and must never
carry unknown/sensitive attributes, which the whitelist keeps out.
"""

import io
import json
import logging
from typing import Any

import pytest

from backend.handlers import logging_config

# Context that a real handler would attach via ``extra=``.
_GAME_ID = "g-123"
_PLAYER_ID = "p-456"
_ENDPOINT = "new_game"
_TRACE_ID = "1-5759e988-bd862e3fe1be46a994272793"


def _capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """Build an isolated logger writing single-line JSON to a StringIO.

    Uses the module's own :class:`JsonFormatter` so the assertions exercise the
    real serialization, but routes output to memory and disables propagation so
    the test never touches the module's stdout handler or the root logger.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_config.JsonFormatter())
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


def _emitted_lines(stream: io.StringIO) -> list[str]:
    """Return the non-empty lines written to the stream."""
    return [line for line in stream.getvalue().splitlines() if line]


def _sole_record(stream: io.StringIO) -> dict[str, Any]:
    """Assert exactly one line was emitted and return it parsed as JSON."""
    lines = _emitted_lines(stream)
    assert len(lines) == 1, f"expected one log line, got {len(lines)}: {lines!r}"
    return json.loads(lines[0])


# --- single-line JSON shape --------------------------------------------------


def test_emits_valid_single_line_json() -> None:
    logger, stream = _capturing_logger("test.single_line")
    logger.info("game created")
    raw = _emitted_lines(stream)[0]
    # One physical line: no embedded newline breaks the CloudWatch line contract.
    assert "\n" not in raw
    record = json.loads(raw)
    assert record["message"] == "game created"


def test_record_includes_level_logger_name_and_timestamp() -> None:
    logger, stream = _capturing_logger("test.fields")
    logger.warning("move rejected")
    record = _sole_record(stream)
    assert record["level"] == "WARNING"
    assert record["logger"] == "test.fields"
    assert record["message"] == "move rejected"
    assert record["timestamp"]  # present and non-empty


def test_includes_whitelisted_context_from_extra() -> None:
    logger, stream = _capturing_logger("test.context")
    logger.info(
        "move accepted",
        extra={
            "gameId": _GAME_ID,
            "playerId": _PLAYER_ID,
            "endpoint": _ENDPOINT,
            "traceId": _TRACE_ID,
        },
    )
    record = _sole_record(stream)
    assert record["gameId"] == _GAME_ID
    assert record["playerId"] == _PLAYER_ID
    assert record["endpoint"] == _ENDPOINT
    assert record["traceId"] == _TRACE_ID


def test_omits_context_fields_that_were_not_supplied() -> None:
    logger, stream = _capturing_logger("test.absent_context")
    logger.info("puzzle solved", extra={"gameId": _GAME_ID})
    record = _sole_record(stream)
    assert record["gameId"] == _GAME_ID
    # Fields not supplied are simply absent, not emitted as null.
    assert "playerId" not in record
    assert "endpoint" not in record
    assert "traceId" not in record


def test_message_newlines_do_not_break_the_single_line_contract() -> None:
    logger, stream = _capturing_logger("test.newlines")
    logger.error("line one\nline two")
    lines = _emitted_lines(stream)
    # json.dumps escapes the newline, so the record stays on one physical line.
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "line one\nline two"


# --- whitelist / secret safety -----------------------------------------------


def test_unknown_and_sensitive_extra_attributes_are_not_serialized() -> None:
    logger, stream = _capturing_logger("test.whitelist")
    logger.info(
        "game created",
        extra={
            "gameId": _GAME_ID,
            "apiKey": "super-secret-token",
            "authorization": "Bearer abc.def.ghi",
        },
    )
    raw = _emitted_lines(stream)[0]
    record = json.loads(raw)
    # Whitelisted context is present...
    assert record["gameId"] == _GAME_ID
    # ...but arbitrary/sensitive attributes never reach the output.
    assert "apiKey" not in record
    assert "authorization" not in record
    assert "super-secret-token" not in raw
    assert "Bearer abc.def.ghi" not in raw


# --- exception logging -------------------------------------------------------


def test_exception_info_is_included_and_stays_single_line() -> None:
    logger, stream = _capturing_logger("test.exception")
    try:
        raise ValueError("bad move")
    except ValueError:
        logger.exception("handling failed", extra={"endpoint": _ENDPOINT})
    lines = _emitted_lines(stream)
    assert len(lines) == 1  # multi-line traceback must not split the record
    record = json.loads(lines[0])
    assert record["level"] == "ERROR"
    assert record["endpoint"] == _ENDPOINT
    exception = record["exception"]
    assert exception["type"] == "ValueError"
    assert exception["message"] == "bad move"


# --- configuration API -------------------------------------------------------


def test_get_logger_returns_a_logger() -> None:
    logger = logging_config.get_logger("backend.handlers.new_game")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "backend.handlers.new_game"


def test_configure_logging_is_idempotent() -> None:
    # Calling configure_logging repeatedly must not stack duplicate handlers on
    # the configured logger, which would emit each line more than once.
    logging_config.configure_logging()
    configured = logging_config.get_logger()
    before = len(configured.handlers)
    logging_config.configure_logging()
    logging_config.configure_logging()
    after = len(configured.handlers)
    assert before == after
    assert after == 1


def test_configured_logger_emits_each_line_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # End-to-end through the real (stdout) configuration: one call → one line.
    logging_config.configure_logging()
    logging_config.configure_logging()  # second call must not duplicate output
    logger = logging_config.get_logger("test.emit_once")
    logger.info("solved", extra={"gameId": _GAME_ID})
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "solved" in line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["gameId"] == _GAME_ID
    assert record["message"] == "solved"
