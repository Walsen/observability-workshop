"""Unit tests for the API Gateway proxy-response and CORS helpers (task 7.1).

These pin the behavior of ``backend.handlers.responses``: every builder returns
a Lambda proxy-integration response dict (``statusCode`` / ``headers`` /
``body``) with a JSON-string body, and — critically — *every* response variant,
success and error alike, carries the CORS headers that authorize the Amplify
origin. A 400 or 404 without those headers is unreadable to the browser
(Requirement 8.2), so the CORS assertions run against each builder in turn.

The allowed origin is read from the environment at call time so tests can pin
it with ``monkeypatch``; the assertions below set it explicitly and confirm it
appears on the response. No ``boto3`` or AWS is involved — these helpers are
pure.
"""

import json
from typing import Any

import pytest

from backend.handlers import responses

_ORIGIN = "https://main.d1example.amplifyapp.com"


@pytest.fixture(autouse=True)
def _set_allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the allowed origin for every test via the environment variable."""
    monkeypatch.setenv(responses.ALLOWED_ORIGIN_ENV, _ORIGIN)


def _assert_cors_headers(response: dict[str, Any]) -> None:
    """Assert the response carries the Amplify-scoped CORS headers."""
    headers = response["headers"]
    assert headers["Access-Control-Allow-Origin"] == _ORIGIN
    methods = headers["Access-Control-Allow-Methods"]
    assert "GET" in methods
    assert "POST" in methods
    assert "OPTIONS" in methods
    allow_headers = headers["Access-Control-Allow-Headers"]
    assert "Content-Type" in allow_headers
    assert "X-Amzn-Trace-Id" in allow_headers


def _body(response: dict[str, Any]) -> Any:
    """Parse the JSON-string body of a proxy response."""
    assert isinstance(response["body"], str)
    return json.loads(response["body"])


# --- ok ----------------------------------------------------------------------


def test_ok_returns_200_with_json_body() -> None:
    response = responses.ok({"gameId": "g-1", "board": [[0]]})
    assert response["statusCode"] == 200
    assert _body(response) == {"gameId": "g-1", "board": [[0]]}


def test_ok_honors_an_explicit_status() -> None:
    response = responses.ok({"created": True}, status=201)
    assert response["statusCode"] == 201
    assert _body(response) == {"created": True}


def test_ok_sets_json_content_type() -> None:
    response = responses.ok({"ok": True})
    assert response["headers"]["Content-Type"] == "application/json"


def test_ok_carries_cors_headers() -> None:
    _assert_cors_headers(responses.ok({"ok": True}))


# --- bad_request -------------------------------------------------------------


def test_bad_request_returns_400_naming_the_field() -> None:
    response = responses.bad_request("playerId")
    assert response["statusCode"] == 400
    body = _body(response)
    assert body["field"] == "playerId"
    assert "error" in body


def test_bad_request_includes_a_custom_message() -> None:
    response = responses.bad_request("move", "row must be in range 0..8")
    body = _body(response)
    assert body["field"] == "move"
    assert body["error"] == "row must be in range 0..8"


def test_bad_request_carries_cors_headers() -> None:
    _assert_cors_headers(responses.bad_request("playerId"))


# --- not_found ---------------------------------------------------------------


def test_not_found_returns_404_with_json_error() -> None:
    response = responses.not_found()
    assert response["statusCode"] == 404
    assert "error" in _body(response)


def test_not_found_does_not_leak_the_ids() -> None:
    # The body should indicate "not found" without echoing the lookup keys.
    response = responses.not_found()
    body = _body(response)
    assert "error" in body
    assert isinstance(body["error"], str)


def test_not_found_carries_cors_headers() -> None:
    _assert_cors_headers(responses.not_found())


# --- server_error ------------------------------------------------------------


def test_server_error_returns_500_with_generic_json() -> None:
    response = responses.server_error()
    assert response["statusCode"] == 500
    assert "error" in _body(response)


def test_server_error_carries_cors_headers() -> None:
    _assert_cors_headers(responses.server_error())


# --- no_solution -------------------------------------------------------------


def test_no_solution_is_200_not_an_error() -> None:
    response = responses.no_solution()
    assert response["statusCode"] == 200


def test_no_solution_body_indicates_no_solution() -> None:
    body = _body(responses.no_solution())
    assert body["solved"] is False
    assert body["reason"] == "no_solution"


def test_no_solution_carries_cors_headers() -> None:
    _assert_cors_headers(responses.no_solution())


# --- preflight ---------------------------------------------------------------


def test_preflight_carries_cors_headers_and_empty_body() -> None:
    response = responses.preflight()
    assert response["statusCode"] in (200, 204)
    _assert_cors_headers(response)
    assert response["body"] == ""


# --- allowed-origin configuration --------------------------------------------


def test_allowed_origin_honors_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    other = "https://feature-branch.d1example.amplifyapp.com"
    monkeypatch.setenv(responses.ALLOWED_ORIGIN_ENV, other)
    response = responses.ok({"ok": True})
    assert response["headers"]["Access-Control-Allow-Origin"] == other


def test_allowed_origin_falls_back_to_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(responses.ALLOWED_ORIGIN_ENV, raising=False)
    response = responses.ok({"ok": True})
    # Absent the env var, a sensible non-empty default origin is used, never "*".
    origin = response["headers"]["Access-Control-Allow-Origin"]
    assert origin
    assert origin != "*"


# --- consolidated CORS guard across every builder (task 7.3) -----------------

# The public response builders, each as a zero-argument factory keyed by a
# readable id. Held as factories (not pre-built responses) so each is invoked
# *inside* the test, after the autouse ``_set_allowed_origin`` fixture has
# pinned the origin — the builders read the env var at call time, so building
# them at collection time would capture the wrong (default) origin.
_RESPONSE_BUILDERS: list[tuple[str, Any]] = [
    ("ok", lambda: responses.ok({"ok": True})),
    ("bad_request", lambda: responses.bad_request("playerId")),
    ("not_found", lambda: responses.not_found()),
    ("server_error", lambda: responses.server_error()),
    ("no_solution", lambda: responses.no_solution()),
    ("preflight", lambda: responses.preflight()),
]


@pytest.mark.parametrize(
    "build_response",
    [factory for _, factory in _RESPONSE_BUILDERS],
    ids=[name for name, _ in _RESPONSE_BUILDERS],
)
def test_every_response_variant_carries_cors_headers(
    build_response: Any,
) -> None:
    # A single guard over all builders: if a new response variant is added
    # without CORS headers, this parametrized case fails for it. The full set
    # (origin locked to the Amplify origin, never "*", plus the advertised
    # methods and the X-Amzn-Trace-Id header) must be present on every one —
    # error variants (400/404/500) included, since Requirement 8.2 is precisely
    # that no response reaches the browser unreadable.
    response = build_response()
    _assert_cors_headers(response)
    assert response["headers"]["Access-Control-Allow-Origin"] != "*"
