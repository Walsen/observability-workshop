"""Consolidated offline CDK synth template assertions (task 10.6).

This is the comprehensive synth-assertion suite the design's "Testing Strategy
(CDK synth)" names: with ``aws_cdk.assertions.Template`` and no account access,
it pins the whole of the design's list — four Lambdas at ``python3.12`` with
tracing ``Active``, the table present with the ``gameId``/``playerId`` key
schema, the API with active tracing and Amplify-scoped CORS, a ``CfnOutput`` for
the URL, region us-east-1, and IAM policies scoped to the table plus
``xray:PutTraceSegments`` (Requirements 8.1, 8.2, 9.1, 9.2, 9.3, 9.4, 9.5).

Most of that list is already asserted in depth by the three scoped files that
introduced each resource, and this suite does **not** duplicate them:

- **Lambdas** — four functions, ``python3.12``, tracing ``Active``, handler
  entry points, and the ``GAMES_TABLE_NAME``/``ALLOWED_ORIGIN`` environment
  variables: ``test_infra_lambdas.py``.
- **API** — one ``RestApi``, stage ``TracingEnabled``, the four proxy methods,
  the OPTIONS preflight, and CORS scoped to the Amplify origin (never ``*``):
  ``test_infra_api.py``.
- **IAM / output** — the DynamoDB grants scoped to the table (never ``*``), the
  read-only/write-only/read-write split across the four functions, and the API
  URL ``CfnOutput`` being an intrinsic: ``test_infra_iam.py``.

What task 10.6 adds here are the design-listed items **not** asserted in any of
those files, plus one consolidated smoke test that ties the list together:

1. **The DynamoDB table key schema** (Requirement 9.1). The table was defined in
   task 10.2 without a dedicated test (synth was checked by hand), so its
   ``gameId`` (HASH) / ``playerId`` (RANGE) key schema, string attribute types,
   and on-demand billing are pinned here for the first time.
2. **Region us-east-1** (Requirement 9.4). The stack is created with
   ``env=Environment(region="us-east-1")``; that the synthesized stack actually
   targets that region — ``stack.region == "us-east-1"`` — is asserted here.
3. **X-Ray write IAM** (Requirement 9.5, design "plus ``xray:PutTraceSegments``").
   Because each function is created with ``tracing=ACTIVE``, CDK inlines
   ``xray:PutTraceSegments`` / ``xray:PutTelemetryRecords`` onto every function
   role's policy. ``test_infra_iam.py`` deliberately scoped itself to DynamoDB
   least-privilege and did not assert this; it is asserted here so the design's
   "table plus ``xray:PutTraceSegments``" is provably covered.
4. **A consolidated resource-count smoke test** — the whole stack synthesizes
   offline and the headline counts (one API, one table, four Lambdas, the URL
   output) are right — tying the design's list together in one place.

Everything here runs fully offline: ``Template.from_stack`` synthesizes the app
with no account context lookup, no credentials, and no network. The region is
pinned on the ``env`` without an account, so synth performs no account lookup.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from backend.infra.sudoku_stack import API_URL_OUTPUT_ID, SudokuStack

# The region the stack is pinned to (Requirement 9.4). Region-only ``env`` — no
# account — so synth resolves nothing from an account and stays offline.
_EXPECTED_REGION = "us-east-1"

# The X-Ray write actions CDK inlines onto every function role because the
# function is created with ``tracing=ACTIVE``. X-Ray write is inherently
# unscoped (``Resource: "*"``) — there is no per-trace ARN to scope to — so this
# is the AWS-normal shape, distinct from the DynamoDB grants which *are* scoped
# to the table (asserted in ``test_infra_iam.py``).
_XRAY_WRITE_ACTIONS = {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}


def _build_stack() -> SudokuStack:
    """Build the region-pinned, account-agnostic stack (no synth yet).

    Returned before turning it into a :class:`~aws_cdk.assertions.Template` so a
    test can read stack-level attributes such as :attr:`~aws_cdk.Stack.region`.
    """
    app = cdk.App()
    return SudokuStack(app, "TestStack", env=cdk.Environment(region=_EXPECTED_REGION))


def _synthesize_template() -> assertions.Template:
    """Synthesize the stack (region-pinned, account-agnostic) into a Template."""
    return assertions.Template.from_stack(_build_stack())


def _xray_actions(statement: dict) -> set[str]:
    """Return the ``xray:`` actions named in one IAM policy statement."""
    actions = statement.get("Action")
    action_list = actions if isinstance(actions, list) else [actions]
    return {a for a in action_list if isinstance(a, str) and a.startswith("xray:")}


# --- Gap 1: the DynamoDB table key schema (Requirement 9.1) ----------------


def test_stack_defines_exactly_one_dynamodb_table() -> None:
    template = _synthesize_template()
    template.resource_count_is("AWS::DynamoDB::Table", 1)


def test_table_key_schema_is_gameid_hash_playerid_range() -> None:
    # Requirement 9.1 / design "Data Model: Key design": gameId is the partition
    # key (HASH) and playerId the sort key (RANGE), so every access is a single
    # point GetItem/PutItem on one fully-specified (gameId, playerId) item.
    template = _synthesize_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": [
                {"AttributeName": "gameId", "KeyType": "HASH"},
                {"AttributeName": "playerId", "KeyType": "RANGE"},
            ]
        },
    )


def test_table_key_attributes_are_strings() -> None:
    # Both key attributes are strings ("S"); the board/puzzle/solution are stored
    # as JSON string attributes by the repository, so only the two key attributes
    # are declared on the table.
    template = _synthesize_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "AttributeDefinitions": [
                {"AttributeName": "gameId", "AttributeType": "S"},
                {"AttributeName": "playerId", "AttributeType": "S"},
            ]
        },
    )


def test_table_uses_on_demand_billing() -> None:
    # On-demand (PAY_PER_REQUEST) billing: a workshop demo has spiky traffic and
    # no capacity to plan (design "on-demand billing").
    template = _synthesize_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"BillingMode": "PAY_PER_REQUEST"},
    )


# --- Gap 2: region us-east-1 (Requirement 9.4) -----------------------------


def test_stack_is_pinned_to_us_east_1() -> None:
    # Requirement 9.4: the synthesized stack targets us-east-1. Region is pinned
    # on the env without an account, so this holds without any account lookup.
    stack = _build_stack()
    assert stack.region == _EXPECTED_REGION
    # Synthesizing the same stack must not change that (and must not require an
    # account): the offline synth guarantee for the region pin.
    assertions.Template.from_stack(stack)
    assert stack.region == _EXPECTED_REGION


# --- Gap 3: X-Ray write IAM on every function role (Requirement 9.5) --------


def test_every_function_policy_grants_xray_write() -> None:
    # tracing=ACTIVE makes CDK inline xray:PutTraceSegments / PutTelemetryRecords
    # onto each function role's policy. The design's IAM list is "table plus
    # xray:PutTraceSegments"; the table part is in test_infra_iam.py, the xray
    # part is here. There is one policy per function (four), and each must carry
    # the full X-Ray write action pair.
    template = _synthesize_template()
    policies = template.find_resources("AWS::IAM::Policy")
    policies_with_xray: list[set[str]] = []
    for props in policies.values():
        statements = props["Properties"]["PolicyDocument"]["Statement"]
        granted: set[str] = set()
        for statement in statements:
            granted |= _xray_actions(statement)
        if granted:
            policies_with_xray.append(granted)
    # One per function: all four function roles get X-Ray write.
    assert len(policies_with_xray) == 4
    for granted in policies_with_xray:
        assert _XRAY_WRITE_ACTIONS <= granted


def test_at_least_the_trace_segment_action_is_present_somewhere() -> None:
    # A direct pin on the headline action the design names by name, independent
    # of how many statements carry it.
    template = _synthesize_template()
    policies = template.find_resources("AWS::IAM::Policy")
    all_xray: set[str] = set()
    for props in policies.values():
        for statement in props["Properties"]["PolicyDocument"]["Statement"]:
            all_xray |= _xray_actions(statement)
    assert "xray:PutTraceSegments" in all_xray


# --- Gap 4: consolidated whole-stack synth + headline resource counts -------


def test_whole_stack_synthesizes_with_expected_resource_counts() -> None:
    # The design's list, tied together in one place: the full stack synthesizes
    # offline and the headline counts are right — one REST API, one DynamoDB
    # table, four Lambda functions.
    template = _synthesize_template()
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::Lambda::Function", 4)


def test_api_url_cfn_output_is_present() -> None:
    # The CfnOutput for the API URL exists (its intrinsic shape is asserted in
    # test_infra_iam.py); this smoke suite confirms the headline output is part
    # of the consolidated template.
    template = _synthesize_template()
    outputs = template.find_outputs(API_URL_OUTPUT_ID)
    assert API_URL_OUTPUT_ID in outputs
