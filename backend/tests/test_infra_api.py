"""Offline CDK synth assertions for the REST API (task 10.4).

These synthesize :class:`~backend.infra.sudoku_stack.SudokuStack` in memory and
assert, over the resulting CloudFormation template, that the stack defines a
single REST API with X-Ray active tracing on the stage, the four
resources/methods wired as Lambda proxy integrations, and CORS preflight locked
to the Amplify origin (never ``*``) (Requirements 1.2, 8.1, 8.2, 8.3, 9.1;
design "API Gateway (REST) Design").

Everything here runs fully offline: ``aws_cdk.assertions.Template`` synthesizes
the app with no account context lookup, no credentials, and no network — a
``RestApi`` with ``LambdaIntegration`` resolves nothing from an account. The
comprehensive template suite (Lambdas + table + API + CORS + IAM + output) is
task 10.6; this file is scoped to what task 10.4 introduces so the API is pinned
the moment it exists.

The API Gateway template shapes are verbose, so these assertions favour
``resource_count_is`` and ``find_resources`` with ``HttpMethod`` filters over
deep exact matches, and inspect the synthesized JSON for the configured origin
rather than matching brittle nested integration ``Ref`` targets.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import assertions

from backend.infra.sudoku_stack import (
    _DEFAULT_ALLOWED_ORIGIN,
    SudokuStack,
)


def _synthesize_template() -> assertions.Template:
    """Synthesize the stack (region-pinned, account-agnostic) into a Template."""
    app = cdk.App()
    stack = SudokuStack(app, "TestStack", env=cdk.Environment(region="us-east-1"))
    return assertions.Template.from_stack(stack)


def _synthesized_json() -> str:
    """Return the synthesized template as a JSON string for substring checks."""
    return json.dumps(_synthesize_template().to_json())


def test_stack_defines_exactly_one_rest_api() -> None:
    template = _synthesize_template()
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)


def test_api_stage_has_xray_active_tracing_enabled() -> None:
    # Requirement 8.1 / 1.2: the stage joins the trace and propagates context.
    template = _synthesize_template()
    stages = template.find_resources(
        "AWS::ApiGateway::Stage",
        {"Properties": {"TracingEnabled": True}},
    )
    assert len(stages) == 1


def test_api_defines_the_four_game_methods() -> None:
    # POST /games, GET /games/{gameId}, POST .../moves, POST .../solve —
    # four functional methods (OPTIONS preflight methods are asserted separately).
    template = _synthesize_template()
    methods = template.find_resources("AWS::ApiGateway::Method")
    http_methods = sorted(
        props["Properties"]["HttpMethod"] for props in methods.values()
    )
    # Three POSTs (new_game, submit_move, solve), one GET (get_game), and four
    # OPTIONS preflight methods (one per resource that carries a method).
    assert http_methods.count("POST") == 3
    assert http_methods.count("GET") == 1


def test_functional_methods_are_lambda_proxy_integrations() -> None:
    template = _synthesize_template()
    methods = template.find_resources("AWS::ApiGateway::Method")
    proxy_methods = [
        props
        for props in methods.values()
        if props["Properties"]["HttpMethod"] in {"GET", "POST"}
    ]
    assert len(proxy_methods) == 4
    for props in proxy_methods:
        integration = props["Properties"]["Integration"]
        assert integration["Type"] == "AWS_PROXY"


def test_cors_preflight_options_methods_exist() -> None:
    # default_cors_preflight_options adds a mock OPTIONS method on every resource
    # that carries a method (Requirement 8.2/8.3).
    template = _synthesize_template()
    options_methods = template.find_resources(
        "AWS::ApiGateway::Method",
        {"Properties": {"HttpMethod": "OPTIONS"}},
    )
    # default_cors_preflight_options adds an OPTIONS to the API root as well as
    # to every declared resource: / , /games, /games/{gameId},
    # /games/{gameId}/moves, /games/{gameId}/solve — five in total.
    assert len(options_methods) == 5


def test_cors_is_scoped_to_the_amplify_origin_not_wildcard() -> None:
    # The preflight allow-origin must be the configured origin, never "*".
    template = _synthesize_template()
    body = _synthesized_json()
    assert _DEFAULT_ALLOWED_ORIGIN in body
    # No OPTIONS method may advertise "*" as its Access-Control-Allow-Origin.
    options_methods = template.find_resources(
        "AWS::ApiGateway::Method",
        {"Properties": {"HttpMethod": "OPTIONS"}},
    )
    for props in options_methods.values():
        responses = props["Properties"]["Integration"]["IntegrationResponses"]
        for response in responses:
            allow_origin = response["ResponseParameters"].get(
                "method.response.header.Access-Control-Allow-Origin"
            )
            if allow_origin is not None:
                assert allow_origin != "'*'"
                assert _DEFAULT_ALLOWED_ORIGIN in allow_origin


def test_cors_preflight_advertises_expected_methods_and_headers() -> None:
    template = _synthesize_template()
    options_methods = template.find_resources(
        "AWS::ApiGateway::Method",
        {"Properties": {"HttpMethod": "OPTIONS"}},
    )
    assert options_methods
    for props in options_methods.values():
        responses = props["Properties"]["Integration"]["IntegrationResponses"]
        params = responses[0]["ResponseParameters"]
        allow_methods = params[
            "method.response.header.Access-Control-Allow-Methods"
        ]
        for verb in ("GET", "POST", "OPTIONS"):
            assert verb in allow_methods
        allow_headers = params[
            "method.response.header.Access-Control-Allow-Headers"
        ]
        assert "Content-Type" in allow_headers
        assert "X-Amzn-Trace-Id" in allow_headers
