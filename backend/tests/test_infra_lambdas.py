"""Offline CDK synth assertions for the four Lambda functions (task 10.3).

These synthesize :class:`~backend.infra.sudoku_stack.SudokuStack` in memory and
assert, over the resulting CloudFormation template, that the stack defines the
four game Lambda functions on the Python 3.12 runtime with X-Ray active tracing
and the ``GAMES_TABLE_NAME`` / ``ALLOWED_ORIGIN`` environment variables
(Requirements 9.1, 9.2; design "Infrastructure as Code (CDK)").

Everything here runs fully offline: ``aws_cdk.assertions.Template`` synthesizes
the app with no account context lookup, no credentials, and no network. The
comprehensive template suite (four Lambdas + table + API + CORS + IAM + output)
is task 10.6; this file is scoped to what task 10.3 introduces so the Lambda
definitions are pinned the moment they exist.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from backend.infra.sudoku_stack import SudokuStack

# The four handler entry points, as the dotted paths CDK sets as each function's
# Handler. The asset root is the repo root, so the ``backend.*`` package resolves
# exactly as it does for the test suite and the Lambda runtime.
_EXPECTED_HANDLERS = {
    "backend.handlers.new_game.lambda_handler",
    "backend.handlers.get_game.lambda_handler",
    "backend.handlers.submit_move.lambda_handler",
    "backend.handlers.solve.lambda_handler",
}


def _synthesize_template() -> assertions.Template:
    """Synthesize the stack (region-pinned, account-agnostic) into a Template."""
    app = cdk.App()
    stack = SudokuStack(app, "TestStack", env=cdk.Environment(region="us-east-1"))
    return assertions.Template.from_stack(stack)


def test_stack_defines_exactly_four_lambda_functions() -> None:
    template = _synthesize_template()
    template.resource_count_is("AWS::Lambda::Function", 4)


def test_every_lambda_uses_python_312_runtime() -> None:
    template = _synthesize_template()
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Runtime": "python3.12"}},
    )
    assert len(functions) == 4


def test_every_lambda_has_active_xray_tracing() -> None:
    template = _synthesize_template()
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"TracingConfig": {"Mode": "Active"}}},
    )
    assert len(functions) == 4


def test_each_handler_entry_point_is_present() -> None:
    template = _synthesize_template()
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = {
        props["Properties"]["Handler"] for props in functions.values()
    }
    assert handlers == _EXPECTED_HANDLERS


def test_every_lambda_receives_the_table_name_and_allowed_origin() -> None:
    template = _synthesize_template()
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 4
    for props in functions.values():
        variables = props["Properties"]["Environment"]["Variables"]
        # GAMES_TABLE_NAME is a Ref to the DynamoDB table, not a literal.
        assert "Ref" in variables["GAMES_TABLE_NAME"]
        # ALLOWED_ORIGIN is set (an operator-configured origin), never "*".
        assert "ALLOWED_ORIGIN" in variables
        assert variables["ALLOWED_ORIGIN"] != "*"


def test_every_lambda_uses_arm64_architecture() -> None:
    # Functions and the X-Ray SDK layer share one architecture (ARM64) because
    # the layer's wrapt dependency ships a compiled extension; a function and the
    # layer it loads must agree on the instruction set.
    template = _synthesize_template()
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Architectures": ["arm64"]}},
    )
    assert len(functions) == 4


def test_every_lambda_has_headroom_timeout_and_memory() -> None:
    # CDK's 3 s / 128 MB defaults are too tight for the instrumented Python cold
    # start (boto3 + aws_xray_sdk import, patch_all, first DynamoDB call under an
    # active segment), which times out. The stack raises both; assert every
    # function carries the raised values.
    template = _synthesize_template()
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Timeout": 10, "MemorySize": 256}},
    )
    assert len(functions) == 4


def test_stack_defines_one_xray_sdk_layer() -> None:
    # The stack supplies aws_xray_sdk (absent from the Lambda runtime) via a
    # single Lambda layer, so the handlers' import succeeds on cold start.
    template = _synthesize_template()
    template.resource_count_is("AWS::Lambda::LayerVersion", 1)


def test_xray_layer_is_python312_arm64_compatible() -> None:
    # The layer advertises the same runtime and architecture as the functions
    # that load it, matching the pinned aws-xray-sdk wheels built by
    # `just build-layer`.
    template = _synthesize_template()
    template.has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.12"],
            "CompatibleArchitectures": ["arm64"],
        },
    )


def test_every_lambda_references_the_xray_layer() -> None:
    # Each of the four functions attaches exactly the one layer (a Ref to the
    # LayerVersion), so none is left without aws_xray_sdk at runtime.
    template = _synthesize_template()
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 4
    for props in functions.values():
        layers = props["Properties"]["Layers"]
        assert len(layers) == 1
        # The layer is referenced by intrinsic (a Ref to the LayerVersion),
        # not hardcoded.
        assert "Ref" in layers[0]
