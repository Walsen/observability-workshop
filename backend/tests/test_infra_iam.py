"""Offline CDK synth assertions for least-privilege IAM and the API URL output
(task 10.5).

These synthesize :class:`~backend.infra.sudoku_stack.SudokuStack` in memory and
assert, over the resulting CloudFormation template, two things task 10.5 adds
(Requirements 9.3, 9.5; design "Infrastructure as Code (CDK)"):

- **Least-privilege DynamoDB IAM per Lambda.** Each function is granted only the
  DynamoDB actions its handler performs, scoped to the ``Games_Table`` ARN
  (never ``"*"`` and never account-wide):

  =============  ===========  ===================================
  function       handler I/O  grant
  =============  ===========  ===================================
  ``get_game``   load         read only
  ``solve``      load         read only (it does not persist)
  ``new_game``   save         write only (it only ``PutItem`` s)
  ``submit_move``load + save  read + write
  =============  ===========  ===================================

  The load-only functions (``get_game``, ``solve``) must **not** be granted any
  write action — that is the crux of the least-privilege check.

- **The API endpoint URL is emitted as a stack output** (Requirement 9.3), so
  the operator can set it as the Amplify configuration value that ``config.js``
  reads (the two-step deploy handoff).

Everything here runs fully offline: ``aws_cdk.assertions.Template`` synthesizes
the app with no account context lookup, no credentials, and no network — the
table grant helpers and ``CfnOutput`` resolve nothing from an account. The
comprehensive template suite (Lambdas + table + API + CORS + IAM + output) is
task 10.6; this file is scoped to what task 10.5 introduces so the grants and
the output are pinned the moment they exist.

Rather than matching an IAM policy to a specific function's execution role
(brittle: the role is an auto-generated logical id), these assertions inspect
every DynamoDB policy statement in the template and reason about the *set* of
policies present: the read-only actions granted, the write actions granted, and
that every DynamoDB statement is scoped to the table. Two write-granting
policies (``new_game``, ``submit_move``) and two read-only policies
(``get_game``, ``solve``) must be present, and no more than two policies may
carry a write action — which is exactly what proves the read-only functions
lack write.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from backend.infra.sudoku_stack import API_URL_OUTPUT_ID, SudokuStack

# DynamoDB write actions no read-only function may ever be granted. The table's
# ``grant_write_data`` / ``grant_read_write_data`` helpers put these on a policy;
# ``grant_read_data`` never does.
_WRITE_ACTIONS = {
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
}

# A representative DynamoDB read action every read grant includes.
_READ_ACTION = "dynamodb:GetItem"


def _synthesize_template() -> assertions.Template:
    """Synthesize the stack (region-pinned, account-agnostic) into a Template."""
    app = cdk.App()
    stack = SudokuStack(app, "TestStack", env=cdk.Environment(region="us-east-1"))
    return assertions.Template.from_stack(stack)


def _dynamodb_statements() -> list[dict]:
    """Return every IAM policy statement in the template that names a DynamoDB action.

    Flattens all ``AWS::IAM::Policy`` documents down to the individual statements
    whose ``Action`` mentions ``dynamodb:``, so a test can reason over the set of
    DynamoDB grants without caring which auto-named role each hangs off.
    """
    template = _synthesize_template()
    policies = template.find_resources("AWS::IAM::Policy")
    dynamodb_statements: list[dict] = []
    for props in policies.values():
        statements = props["Properties"]["PolicyDocument"]["Statement"]
        for statement in statements:
            if _statement_actions(statement):
                dynamodb_statements.append(statement)
    return dynamodb_statements


def _statement_actions(statement: dict) -> set[str]:
    """Return the DynamoDB actions in one statement as a set."""
    actions = statement.get("Action")
    action_list = actions if isinstance(actions, list) else [actions]
    return {a for a in action_list if isinstance(a, str) and a.startswith("dynamodb:")}


def _dynamodb_actions_per_policy() -> list[set[str]]:
    """Return, per ``AWS::IAM::Policy``, the union of its DynamoDB actions.

    CDK emits one policy per function's execution role, but a *read* grant lands
    two statements on that policy (the table read actions plus the stream
    ``GetRecords``/``GetShardIterator`` pair), and a read+write grant lands even
    more. Reasoning per policy — one policy per function — is therefore the
    robust unit: it aggregates the function's whole DynamoDB action set
    regardless of how the grant split it across statements. Only policies that
    carry at least one DynamoDB action are returned, so unrelated policies (if
    any) are ignored.
    """
    template = _synthesize_template()
    policies = template.find_resources("AWS::IAM::Policy")
    per_policy: list[set[str]] = []
    for props in policies.values():
        statements = props["Properties"]["PolicyDocument"]["Statement"]
        actions: set[str] = set()
        for statement in statements:
            actions |= _statement_actions(statement)
        if actions:
            per_policy.append(actions)
    return per_policy


def test_the_api_url_is_emitted_as_a_stack_output() -> None:
    # Requirement 9.3: the deploy emits the URL the operator hands to Amplify.
    template = _synthesize_template()
    outputs = template.find_outputs(API_URL_OUTPUT_ID)
    assert API_URL_OUTPUT_ID in outputs


def test_the_api_url_output_references_the_api_not_a_literal() -> None:
    # The output's value is a CloudFormation intrinsic (Ref/Join) built from the
    # API and its stage, not a hardcoded string — it resolves at deploy time.
    template = _synthesize_template()
    output = template.find_outputs(API_URL_OUTPUT_ID)[API_URL_OUTPUT_ID]
    value = output["Value"]
    assert isinstance(value, dict)
    assert "Fn::Join" in value or "Ref" in value


def test_every_dynamodb_grant_is_scoped_to_the_table_not_wildcard() -> None:
    # Least privilege: no DynamoDB statement may grant "*" as its resource.
    statements = _dynamodb_statements()
    assert statements, "expected at least one DynamoDB grant"
    for statement in statements:
        resource = statement["Resource"]
        # The table grant helpers produce a Ref/Fn::GetAtt to the table ARN
        # (and its index/stream ARNs), never the literal "*".
        assert resource != "*"
        resources = resource if isinstance(resource, list) else [resource]
        for entry in resources:
            assert entry != "*"
            # Each resource entry is a CloudFormation intrinsic pointing at the
            # GamesTable, not a bare string.
            assert isinstance(entry, dict)


def test_all_four_functions_receive_a_dynamodb_grant() -> None:
    # get_game, solve (read), new_game (write), submit_move (read+write) — one
    # DynamoDB grant policy per function.
    per_policy = _dynamodb_actions_per_policy()
    assert len(per_policy) == 4


def test_exactly_two_functions_are_granted_write() -> None:
    # get_game and solve are load-only; only new_game (write) and submit_move
    # (read+write) touch DynamoDB writes. So exactly two of the four function
    # policies may carry a write action — which proves the two read-only
    # functions lack write entirely (least privilege, Requirement 9.5).
    per_policy = _dynamodb_actions_per_policy()
    write_granting = [actions for actions in per_policy if actions & _WRITE_ACTIONS]
    read_only = [actions for actions in per_policy if not (actions & _WRITE_ACTIONS)]
    assert len(write_granting) == 2
    assert len(read_only) == 2


def test_the_read_only_functions_can_read_the_item() -> None:
    # get_game and solve are load-only: their policy grants GetItem and no write.
    per_policy = _dynamodb_actions_per_policy()
    read_only = [actions for actions in per_policy if not (actions & _WRITE_ACTIONS)]
    assert len(read_only) == 2
    for actions in read_only:
        assert _READ_ACTION in actions


def test_the_read_write_function_can_both_read_and_write() -> None:
    # submit_move loads then saves: exactly one policy grants both GetItem and a
    # write action (new_game is write-only and does not read the item).
    per_policy = _dynamodb_actions_per_policy()
    read_and_write = [
        actions
        for actions in per_policy
        if (_READ_ACTION in actions) and (actions & _WRITE_ACTIONS)
    ]
    assert len(read_and_write) == 1


def test_the_write_only_function_writes_without_reading_the_item() -> None:
    # new_game only PutItems (never loads), so grant_write_data is the tightest
    # fit: its policy carries write actions but not GetItem — read+write here
    # would be excess privilege.
    per_policy = _dynamodb_actions_per_policy()
    write_only = [
        actions
        for actions in per_policy
        if (actions & _WRITE_ACTIONS) and (_READ_ACTION not in actions)
    ]
    assert len(write_only) == 1
