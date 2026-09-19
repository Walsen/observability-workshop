"""Offline CDK synth assertions for the synthetic canary and alarm (task 14.3).

These synthesize :class:`~backend.infra.sudoku_stack.SudokuStack` in memory and
assert, over the resulting CloudFormation template, that the synthetic canary
and its ``SuccessPercent`` alarm appear **only** when the operator supplies the
``canary_target_url`` context, and take the shape the design and requirements
name (Requirements 11.1, 11.2, 11.5, 11.6, 11.7; design "Testing Strategy
(Canary / alarm template assertions)").

The canary is created *conditionally*: the stack reads
``self.node.try_get_context("canary_target_url")`` and builds the canary + alarm
only when it is present, so there are two synthesis contexts to pin:

- **with** the context — the ``AWS::Synthetics::Canary`` (runtime
  ``syn-nodejs-puppeteer-13.0``, ``rate(5 minutes)`` schedule, active tracing,
  ``TARGET_URL`` env var, an artifacts location) and the
  ``AWS::CloudWatch::Alarm`` on ``SuccessPercent`` exist; and
- **without** it — neither exists, so the existing offline backend template is
  unchanged (Requirement 11.1).

Everything here runs fully offline: ``aws_cdk.assertions.Template`` synthesizes
the app with no account context lookup, no credentials, and no network. Context
is supplied through the ``App``'s own context map (which
``self.node.try_get_context`` resolves), exactly as ``cdk synth -c ...`` would.

The concrete property shapes asserted below were taken from the synthesized
template under the pinned aws-cdk-lib (2.270.0) — e.g. active tracing lands at
``RunConfig.ActiveTracing`` and the canary env vars at
``RunConfig.EnvironmentVariables`` — rather than guessed, and the assertions use
``Match.object_like`` for the nested subsets so they stay robust to unrelated
fields.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk.assertions import Match

from backend.infra.sudoku_stack import (
    CANARY_TARGET_URL_CONTEXT_KEY,
    SudokuStack,
)

# A stand-in Amplify origin passed as the canary target. It is only ever staged
# locally (into the canary's ``TARGET_URL`` env var); synth makes no request to
# it, so any well-formed URL keeps the suite offline.
_CANARY_TARGET_URL = "https://main.example.amplifyapp.com"

# The CloudFormation resource types the canary wiring adds.
_CANARY_TYPE = "AWS::Synthetics::Canary"
_ALARM_TYPE = "AWS::CloudWatch::Alarm"


def _template_with_canary() -> assertions.Template:
    """Synthesize the stack **with** ``canary_target_url`` supplied as context.

    The context is set on the ``App`` so ``self.node.try_get_context`` resolves
    it — the same value ``cdk synth -c canary_target_url=<url>`` would supply.
    The env is region-only (no account), so synth performs no account lookup and
    stays offline.
    """
    app = cdk.App(context={CANARY_TARGET_URL_CONTEXT_KEY: _CANARY_TARGET_URL})
    stack = SudokuStack(
        app, "TestStack", env=cdk.Environment(region="us-east-1")
    )
    return assertions.Template.from_stack(stack)


def _template_without_canary() -> assertions.Template:
    """Synthesize the stack **without** any canary context (the default synth).

    No ``canary_target_url`` context is supplied, so ``_canary_target_url``
    returns ``None`` and neither the canary nor the alarm is created — the
    existing offline backend template (Requirement 11.1).
    """
    app = cdk.App()
    stack = SudokuStack(
        app, "TestStack", env=cdk.Environment(region="us-east-1")
    )
    return assertions.Template.from_stack(stack)


# --- WITH canary_target_url context: the canary exists and is configured ----


def test_canary_context_creates_exactly_one_canary() -> None:
    # WHEN the operator supplies the target URL, the stack adds exactly one
    # Synthetics canary (Requirement 11.1's converse — present with context).
    template = _template_with_canary()
    template.resource_count_is(_CANARY_TYPE, 1)


def test_canary_uses_the_puppeteer_runtime() -> None:
    # The canary runs the syn-nodejs-puppeteer runtime the browser script needs
    # (Requirement 11.5). Synthesized shape: Properties.RuntimeVersion is the
    # literal runtime string.
    template = _template_with_canary()
    template.has_resource_properties(
        _CANARY_TYPE,
        Match.object_like({"RuntimeVersion": "syn-nodejs-puppeteer-13.0"}),
    )


def test_canary_runs_every_five_minutes() -> None:
    # The canary is scheduled once every five minutes (Requirement 11.2).
    # Synthesized shape: Properties.Schedule.Expression == "rate(5 minutes)".
    template = _template_with_canary()
    template.has_resource_properties(
        _CANARY_TYPE,
        Match.object_like({"Schedule": {"Expression": "rate(5 minutes)"}}),
    )


def test_canary_has_active_tracing_enabled() -> None:
    # X-Ray active tracing is on, so each scheduled run joins the
    # browser -> API -> Lambda -> DynamoDB trace (Requirement 11.5).
    # Synthesized shape: active_tracing lands at RunConfig.ActiveTracing == true.
    template = _template_with_canary()
    template.has_resource_properties(
        _CANARY_TYPE,
        Match.object_like({"RunConfig": {"ActiveTracing": True}}),
    )


def test_canary_receives_target_url_env_var() -> None:
    # The operator-supplied target URL is passed to the script as the TARGET_URL
    # env var, so nothing is hardcoded in the asset. Synthesized shape: the env
    # vars land at RunConfig.EnvironmentVariables, keyed TARGET_URL.
    template = _template_with_canary()
    template.has_resource_properties(
        _CANARY_TYPE,
        Match.object_like(
            {
                "RunConfig": {
                    "EnvironmentVariables": {
                        "TARGET_URL": _CANARY_TARGET_URL
                    }
                }
            }
        ),
    )


def test_canary_has_an_artifacts_location() -> None:
    # The L2 auto-provisions an artifacts S3 bucket and points the canary at it
    # (Requirement 11.6). Synthesized shape: Properties.ArtifactS3Location is a
    # non-empty intrinsic (an ``s3://`` Fn::Join over the bucket Ref), not a
    # literal — so assert it is present and non-empty rather than pinning the
    # generated bucket name.
    template = _template_with_canary()
    canaries = template.find_resources(_CANARY_TYPE)
    assert len(canaries) == 1
    (props,) = (c["Properties"] for c in canaries.values())
    artifacts = props.get("ArtifactS3Location")
    assert artifacts, "canary must configure an artifacts S3 location"


def test_canary_success_alarm_is_configured() -> None:
    # Exactly one alarm, on the canary's SuccessPercent metric in the
    # CloudWatchSynthetics namespace, tripping when the success rate falls below
    # 100% over a single evaluation period (Requirement 11.7). Synthesized shape:
    # the metric is expressed inline as MetricName/Namespace with a CanaryName
    # dimension (not a Metrics array), so assert that subset with object_like.
    template = _template_with_canary()
    template.resource_count_is(_ALARM_TYPE, 1)
    template.has_resource_properties(
        _ALARM_TYPE,
        Match.object_like(
            {
                "MetricName": "SuccessPercent",
                "Namespace": "CloudWatchSynthetics",
                "ComparisonOperator": "LessThanThreshold",
                "Threshold": 100,
                "EvaluationPeriods": 1,
            }
        ),
    )


def test_canary_role_can_write_xray_segments() -> None:
    # With active tracing on, the canary runtime publishes an X-Ray segment per
    # run (Requirement 11.5), but the L2's auto-provisioned role omits X-Ray
    # write — so the stack grants it explicitly. Without it every run fails
    # AccessDeniedException before producing a result. Assert an IAM policy on
    # the canary role allows xray:PutTraceSegments / PutTelemetryRecords.
    template = _template_with_canary()
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Effect": "Allow",
                                    "Action": [
                                        "xray:PutTraceSegments",
                                        "xray:PutTelemetryRecords",
                                    ],
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_canary_alarm_targets_the_canary_by_dimension() -> None:
    # The alarm's SuccessPercent metric is dimensioned by CanaryName referencing
    # this canary, so the alarm watches *this* canary rather than an aggregate.
    # Synthesized shape: Dimensions is a list carrying a CanaryName entry.
    template = _template_with_canary()
    template.has_resource_properties(
        _ALARM_TYPE,
        Match.object_like(
            {
                "Dimensions": Match.array_with(
                    [Match.object_like({"Name": "CanaryName"})]
                )
            }
        ),
    )


# --- WITHOUT the context: no canary, no canary alarm (Requirement 11.1) ------


def test_default_synth_creates_no_canary() -> None:
    # Requirement 11.1: absent the canary_target_url context the stack
    # synthesizes with no Synthetics canary, leaving the existing offline backend
    # template unchanged.
    template = _template_without_canary()
    template.resource_count_is(_CANARY_TYPE, 0)


def test_default_synth_creates_no_alarm() -> None:
    # The success-rate alarm is the only CloudWatch alarm the stack defines, and
    # it is created only alongside the canary. Absent the context there is no
    # alarm at all (Requirement 11.1).
    template = _template_without_canary()
    template.resource_count_is(_ALARM_TYPE, 0)
