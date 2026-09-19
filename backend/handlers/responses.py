"""API Gateway proxy-response builders with Amplify-scoped CORS headers.

Each Lambda here is a REST API proxy integration, so a handler returns the
proxy response shape itself — ``{"statusCode": int, "headers": {...}, "body":
"<json string>"}`` — rather than leaning on a web framework. This module owns
that shape and, crucially, attaches the CORS headers that authorize the Amplify
origin to *every* response, success and error alike: a 400 or 404 that omits
them is unreadable to the browser (Requirement 8.2, and the "CORS on every
response" rule in the engineering-practices steering).

The allowed origin is read from the environment at *call time* (not import
time), so a test can pin it with ``monkeypatch`` and the deployed Lambda can
read whatever CDK set. CORS is locked to the configured Amplify origin, never
``*``.

The module is pure: no ``boto3``, no AWS calls, no I/O beyond building a dict.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Environment variable naming the single allowed cross-origin (the Amplify
# origin). Read at call time so tests and the deployed Lambda both control it.
ALLOWED_ORIGIN_ENV = "ALLOWED_ORIGIN"

# Fallback used only when the environment does not set the origin. It is a
# concrete placeholder origin rather than ``*`` so CORS is never wildcarded even
# by accident; the deployed stack always overrides it via ALLOWED_ORIGIN_ENV.
_DEFAULT_ALLOWED_ORIGIN = "http://localhost:8000"

# Methods and request headers the game endpoints accept cross-origin. The
# frontend sends ``playerId`` in the body or query string (not a bespoke
# header), so the header allow-list only needs the content type and the trace
# header the browser originates.
_ALLOW_METHODS = "GET, POST, OPTIONS"
_ALLOW_HEADERS = "Content-Type, X-Amzn-Trace-Id"

_CONTENT_TYPE_JSON = "application/json"

# A proxy response: statusCode, string-valued headers, and a string body.
ProxyResponse = dict[str, Any]


def _allowed_origin() -> str:
    """Return the configured Amplify origin, or the safe local default."""
    return os.environ.get(ALLOWED_ORIGIN_ENV) or _DEFAULT_ALLOWED_ORIGIN


def cors_headers() -> dict[str, str]:
    """Build the CORS headers authorizing the configured Amplify origin.

    Locked to the single origin from :data:`ALLOWED_ORIGIN_ENV` (never ``*``)
    and advertising the methods and headers the game endpoints use, so both
    preflight and actual responses satisfy the browser's same-origin checks.
    """
    return {
        "Access-Control-Allow-Origin": _allowed_origin(),
        "Access-Control-Allow-Methods": _ALLOW_METHODS,
        "Access-Control-Allow-Headers": _ALLOW_HEADERS,
    }


def _json_response(status: int, payload: Any) -> ProxyResponse:
    """Build a proxy response with a JSON body and CORS + content-type headers."""
    headers = cors_headers()
    headers["Content-Type"] = _CONTENT_TYPE_JSON
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(payload),
    }


def ok(payload: Any, status: int = 200) -> ProxyResponse:
    """A success response carrying ``payload`` as a JSON body (200 by default)."""
    return _json_response(status, payload)


def bad_request(field: str, message: str | None = None) -> ProxyResponse:
    """A 400 whose body names the offending ``field`` (Requirements 3.3, 4.3).

    ``message`` supplies human-readable detail; absent one, a generic message
    referencing the field is used. Malformed input is mapped here — never to a
    500 — so the browser always receives a readable, CORS-bearing 400.
    """
    error = message if message is not None else f"Invalid or missing field: {field}"
    return _json_response(400, {"error": error, "field": field})


def not_found() -> ProxyResponse:
    """A 404 for an unknown ``(gameId, playerId)`` pair (Requirement 3.3).

    The body says only that the game was not found; it does not echo the lookup
    keys, so one player cannot probe another's ids through the error body.
    """
    return _json_response(404, {"error": "Game not found."})


def server_error(message: str = "Internal server error.") -> ProxyResponse:
    """A generic 500 for an unexpected failure at the top-level boundary.

    Reserved for genuinely unexpected errors; documented bad input maps to
    :func:`bad_request`, never here. The body is deliberately generic so no
    stack trace or internal detail reaches the client.
    """
    return _json_response(500, {"error": message})


def no_solution() -> ProxyResponse:
    """The solve endpoint's "no solution exists" response (Requirement 5.4).

    An unsolvable board is a valid outcome, not an error, so this is a 200 whose
    body signals that no solution was found rather than an error status.
    """
    return _json_response(200, {"solved": False, "reason": "no_solution"})


def preflight() -> ProxyResponse:
    """A CORS preflight (OPTIONS) response: 204, CORS headers, empty body."""
    return {
        "statusCode": 204,
        "headers": cors_headers(),
        "body": "",
    }
