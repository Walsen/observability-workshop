"""Offline CDK synth assertion for the app-level cost-allocation tags (task 15.2).

Task 15.1 applies two cost-allocation tags — ``Project=xray-sudoku-demo`` and
``ManagedBy=cdk`` — at the **App** level in ``backend/app.py``'s ``main()``, via
``cdk.Tags.of(app).add(...)``, exposed as the module constant
``_COST_ALLOCATION_TAGS``. CDK propagates app-level tags down to every taggable
resource, where the synthesized template renders them as a ``Tags`` list of
``{"Key", "Value"}`` maps (Requirement 12.1; design "Cost Attribution and
Reporting").

The subtlety this file has to respect: the tags are added on the ``App`` inside
``app.py``'s ``main()``, **not** inside :class:`SudokuStack`. The other infra
suites synthesize the bare stack directly (``app = cdk.App(); stack =
SudokuStack(app, ...)``), a path that never runs ``app.py``'s ``main()`` — so
those stacks carry no ``Project`` tag at all. To assert the real tags propagate,
this suite reproduces exactly what ``app.py`` does: it builds the ``App``, then
applies the imported ``_COST_ALLOCATION_TAGS`` constant to it before
instantiating the stack. Importing the constant (rather than hard-coding the
key/value here) keeps the test tracking ``app.py`` — if the constant changes,
both ``app.py`` and this assertion move together.

Everything here runs fully offline: ``Template.from_stack`` synthesizes with no
account context lookup, no credentials, and no network. The region is pinned on
the ``env`` without an account, so synth performs no account lookup.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk.assertions import Match

from backend.app import _COST_ALLOCATION_TAGS
from backend.infra.sudoku_stack import SudokuStack

# The region the stack is pinned to (Requirement 9.4), matching app.py and the
# other infra suites. Region-only ``env`` — no account — keeps synth offline.
_EXPECTED_REGION = "us-east-1"

# The cost-allocation tag whose propagation this suite pins. The constant guard
# below asserts app.py still defines it; the propagation tests then assert it
# reaches taggable resources.
_PROJECT_TAG = {"Key": "Project", "Value": "xray-sudoku-demo"}
_MANAGED_BY_TAG = {"Key": "ManagedBy", "Value": "cdk"}


def _synthesize_tagged_template() -> assertions.Template:
    """Synthesize the stack with the app-level tags applied exactly as app.py.

    Reproduces ``backend/app.py``'s ``main()``: build the ``App``, apply every
    entry of the imported ``_COST_ALLOCATION_TAGS`` constant to it, then
    instantiate the region-pinned, account-agnostic stack. This exercises the
    real app-level tagging so the assertions cover the tags that will actually
    reach the deployed resources — not a stand-in applied at stack scope.
    """
    app = cdk.App()
    for tag_key, tag_value in _COST_ALLOCATION_TAGS.items():
        cdk.Tags.of(app).add(tag_key, tag_value)
    stack = SudokuStack(app, "TestStack", env=cdk.Environment(region=_EXPECTED_REGION))
    return assertions.Template.from_stack(stack)


def _synthesize_untagged_template() -> assertions.Template:
    """Synthesize the bare stack the way the other infra suites do (no tags).

    This is the path that does **not** run ``app.py``'s ``main()``; it documents
    that the ``Project`` tag comes from the app level, not from the stack itself.
    """
    app = cdk.App()
    stack = SudokuStack(app, "TestStack", env=cdk.Environment(region=_EXPECTED_REGION))
    return assertions.Template.from_stack(stack)


# --- Guard: the constant app.py exposes still names the expected tags -------


def test_cost_allocation_tags_constant_defines_project_and_managed_by() -> None:
    # Guards the constant the propagation assertions rely on: if app.py stops
    # defining Project=xray-sudoku-demo or ManagedBy=cdk, this fails first and
    # points straight at app.py rather than at a confusing template mismatch.
    assert _COST_ALLOCATION_TAGS["Project"] == "xray-sudoku-demo"
    assert _COST_ALLOCATION_TAGS["ManagedBy"] == "cdk"


# --- Propagation: taggable resources carry the Project tag ------------------


def test_dynamodb_table_carries_project_tag() -> None:
    # The DynamoDB table is a representative taggable resource. Its Tags render
    # as a list of {Key, Value} maps; array_with asserts the Project entry is
    # present regardless of ordering or the presence of the other (ManagedBy)
    # tag.
    template = _synthesize_tagged_template()
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        Match.object_like({"Tags": Match.array_with([_PROJECT_TAG])}),
    )


def test_lambda_function_carries_project_tag() -> None:
    # A Lambda function is the second representative taggable resource. There are
    # four functions; has_resource_properties passes when at least one matches,
    # and app-level tags propagate to all of them.
    template = _synthesize_tagged_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"Tags": Match.array_with([_PROJECT_TAG])}),
    )


def test_lambda_function_also_carries_managed_by_tag() -> None:
    # The second app-level tag propagates the same way; pinning it on a Lambda
    # confirms the whole _COST_ALLOCATION_TAGS set reaches resources, not just
    # the headline Project tag.
    template = _synthesize_tagged_template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"Tags": Match.array_with([_MANAGED_BY_TAG])}),
    )


# --- Documentation: without the app-level tags, no Project tag --------------


def test_bare_stack_without_app_tags_has_no_project_tag() -> None:
    # Secondary/documentary: synthesizing the stack the way the other infra
    # suites do — without running app.py's main() — leaves the DynamoDB table
    # with no Project tag. This makes explicit that the tagging originates at the
    # app level, so the primary assertions above are genuinely exercising
    # app.py's behavior rather than something SudokuStack does on its own.
    template = _synthesize_untagged_template()
    tables = template.find_resources("AWS::DynamoDB::Table")
    for props in tables.values():
        tags = props.get("Properties", {}).get("Tags", [])
        keys = {tag["Key"] for tag in tags if isinstance(tag, dict) and "Key" in tag}
        assert "Project" not in keys
