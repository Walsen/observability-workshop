"""Read-only error report for the demo's X-Ray faults and canary alarm (task 17).

This is a read-only tool the ``get-errors`` skill can fall back to through
``just error-report``. Like ``verify_trace.py`` and ``player_stats.py`` it lives
under ``scripts/`` and is EXPLICITLY OUTSIDE the offline ``just test`` suite: the
X-Ray and CloudWatch reads it runs need AWS credentials and the network. What
*is* offline-testable — and what the tests in
``backend/tests/test_error_report.py`` exercise — is the pure
formatting/parsing core and the fault-summary/alarm plumbing, all reachable with
fakes injected in place of boto3 (dependency inversion, engineering-practices
§1).

The design deliberately splits two concerns (Single Responsibility):

* **Pure formatting/parsing** — :func:`xray_trace_console_url` builds the
  CloudWatch X-Ray console deep link for a trace id; :func:`summarize_fault_traces`
  turns raw ``GetTraceSummaries`` output into a small :class:`FaultTrace` list,
  keeping only summaries that carry a fault or an error and attaching each
  trace's console URL; :func:`format_report` / :func:`report_as_dict` render the
  human and machine forms. None of these touch AWS. Iteration order is defined
  throughout (faults are sorted by ``trace_id``); nothing reaching the output
  relies on set or incidental dict ordering (engineering-practices §2).

* **AWS I/O** — :func:`fetch_fault_trace_summaries` runs a paginated X-Ray
  ``get_trace_summaries`` (following ``NextToken``) over the window with a
  fault/error filter expression, then hands the summaries to the pure core;
  :func:`read_canary_alarm_state` reads the canary's CloudWatch alarm state via
  ``describe_alarms``. Both take the boto3 client as a parameter, so a test
  injects a fake and no real client is ever constructed at import time.

**Scope.** The report covers the demo's own footprint — X-Ray faults/errors in
the window (the four Sudoku Lambdas behind the API surface as fault/error
summaries) and the canary's ``SudokuCanarySuccessAlarm`` — not every fault in
the account. The demo's intentional ``400`` (invalid move) and ``404`` (unknown
game) responses show up as X-Ray *errors*, not *faults*; the report distinguishes
the two so an expected client error is not mistaken for a failure.

Operational logs go through a stdlib logger configured once, emitting one
single-line JSON object per event to **stderr** (engineering-practices §6), so
stdout carries only the report — the prose summary or, with ``--json``, the
machine-readable result. Never ``print()`` for logs; never log anything
sensitive.

Run it with:  ``just error-report``  (or ``python scripts/error_report.py``),
with AWS credentials in the environment (``AWS_PROFILE`` / ``AWS_REGION``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

# The canary's CloudWatch alarm carries this name prefix (Requirement 11.7); the
# alarm fires when the Synthetics SuccessPercent drops below threshold. There is
# an unrelated ``process-endpoint`` canary in the account — scoping by this
# prefix keeps the report to the demo's alarm only.
_ALARM_NAME_PREFIX = "XraySudokuDemoStack-SudokuCanarySuccessAlarm"

# The X-Ray filter expression scoping the summaries to the demo's failures. A
# fault is a 5xx / unhandled exception; an error is a 4xx (the demo's intentional
# 400/404). Both are kept; throttles would surface as faults too.
_FAULT_ERROR_FILTER = "fault = true OR error = true"

# Demo-scoping (Requirement 14.6). X-Ray FilterExpression cannot substring- or
# regex-match service names, so the fault/error filter above cannot be narrowed
# to the demo server-side; it would still return faults from unrelated projects
# in the account. Scoping is therefore enforced client-side: a trace is kept
# only if one of its ``ServiceIds[].Name`` values is a demo service — its name
# contains the stack substring below, or equals one of the exact names (the API
# Gateway stage or the canary, neither of which carries the stack substring).
_DEMO_SERVICE_SUBSTRING = "XraySudokuDemoStack"
_DEMO_SERVICE_NAMES = {"SudokuApi/prod", "xraysudokudemosddbbe0"}

_LOGGER_NAME = "error_report"

_DEFAULT_HOURS = 1


# --- structured logging (single-line JSON to stderr, configured once) --------


class _JsonFormatter(logging.Formatter):
    """Serialize a record to a single-line JSON object (stderr).

    Mirrors the handler logger's contract (engineering-practices §6): a fixed
    ``timestamp``/``level``/``logger``/``message`` schema plus whitelisted
    context, so nothing a caller attaches by accident can leak. ``json.dumps``
    escapes any newline, keeping every record on one physical line.
    """

    _CONTEXT_FIELDS = ("region", "hours", "faultCount", "alarmState")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field_name in self._CONTEXT_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        if record.exc_info is not None:
            exc_type = record.exc_info[0]
            payload["error"] = exc_type.__name__ if exc_type is not None else "error"
        return json.dumps(payload, ensure_ascii=False)


def _get_logger() -> logging.Logger:
    """Return the module logger, configured once with a single stderr handler.

    Idempotent: a repeat call reuses the installed JSON handler rather than
    stacking duplicates. Output goes to **stderr** so stdout stays reserved for
    the report the program prints as its result.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    already = any(isinstance(h.formatter, _JsonFormatter) for h in logger.handlers)
    if not already:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# --- pure formatting / parsing core (no AWS, no wall clock) ------------------


@dataclass(frozen=True)
class FaultTrace:
    """One X-Ray trace that carried a fault or an error, distilled for the report.

    ``has_fault`` is a 5xx / unhandled failure; ``has_error`` is a 4xx (the
    demo's intentional invalid-move 400 / unknown-game 404), which is an expected
    client error rather than a failure. ``root_cause_message`` is best-effort
    from the summary (it may be ``None``). ``console_url`` is the clickable
    CloudWatch X-Ray deep link for the trace.
    """

    trace_id: str
    has_fault: bool
    has_error: bool
    http_status: int | None
    root_cause_message: str | None
    console_url: str


def xray_trace_console_url(trace_id: str, region: str = "us-east-1") -> str:
    """Build the CloudWatch X-Ray console deep link for a single trace.

    Shape (us-east-1):
    ``https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#xray:traces/{trace_id}``.
    Pure string construction, so a test asserts the exact URL offline.
    """
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#xray:traces/{trace_id}"
    )


def _extract_http_status(summary: dict[str, Any]) -> int | None:
    """Pull ``Http.HttpStatus`` from a trace summary, or ``None`` if absent."""
    http = summary.get("Http")
    if isinstance(http, dict):
        status = http.get("HttpStatus")
        if isinstance(status, int):
            return status
    return None


def _extract_root_cause_message(summary: dict[str, Any]) -> str | None:
    """Best-effort exception message from a summary's fault/error root causes.

    ``GetTraceSummaries`` nests root causes as
    ``FaultRootCauses[].Services[].EntityPath[].Exceptions[].Message``. This
    walks that structure defensively and returns the first message found, or
    ``None`` when the summary carries no structured root cause — the report then
    simply shows the trace without a message rather than crashing.
    """
    for key in ("FaultRootCauses", "ErrorRootCauses"):
        for root_cause in summary.get(key, []) or []:
            if not isinstance(root_cause, dict):
                continue
            for service in root_cause.get("Services", []) or []:
                if not isinstance(service, dict):
                    continue
                for entity in service.get("EntityPath", []) or []:
                    if not isinstance(entity, dict):
                        continue
                    for exception in entity.get("Exceptions", []) or []:
                        if isinstance(exception, dict):
                            message = exception.get("Message")
                            if isinstance(message, str) and message:
                                return message
    return None


def _service_names(summary: dict[str, Any]) -> list[str]:
    """Collect the service names a summary is attributed to.

    ``GetTraceSummaries`` carries ``ServiceIds: [{Name, Names, AccountId,
    Type}]``. This reads each entry's ``Name`` and, defensively, any string in
    its ``Names`` list, walking the structure without assuming every field is
    present or well-typed.
    """
    names: list[str] = []
    for service in summary.get("ServiceIds", []) or []:
        if not isinstance(service, dict):
            continue
        name = service.get("Name")
        if isinstance(name, str) and name:
            names.append(name)
        for extra in service.get("Names", []) or []:
            if isinstance(extra, str) and extra:
                names.append(extra)
    return names


def _is_demo_service(name: str) -> bool:
    """True if ``name`` names one of the demo's own X-Ray services.

    A demo service either contains the ``XraySudokuDemoStack`` substring (the
    API Gateway stage's Lambdas and the DynamoDB table) or equals one of the
    exact names that do not carry it (the ``SudokuApi/prod`` stage or the canary).
    """
    return _DEMO_SERVICE_SUBSTRING in name or name in _DEMO_SERVICE_NAMES


def _is_in_demo_scope(summary: dict[str, Any]) -> bool:
    """True if any of the summary's ServiceIds names a demo service.

    A summary with no ServiceIds, or only non-demo ServiceIds, is out of scope
    (Requirement 14.6): it belongs to an unrelated project in the account and is
    dropped rather than reported as the demo's own failure.
    """
    return any(_is_demo_service(name) for name in _service_names(summary))


def summarize_fault_traces(
    trace_summaries: list[dict[str, Any]], region: str = "us-east-1"
) -> list[FaultTrace]:
    """Distil ``GetTraceSummaries`` output into a sorted list of demo fault traces.

    Keeps a summary only when it is BOTH a failure — ``HasFault`` or ``HasError``
    is true (clean traces are dropped) — AND in the demo's own footprint: at
    least one of its ``ServiceIds[].Name`` values is a demo service, i.e. the
    name contains ``XraySudokuDemoStack`` or equals ``SudokuApi/prod`` or the
    canary ``xraysudokudemosddbbe0`` (Requirement 14.6). This client-side scope
    check is what keeps the report from surfacing unrelated projects' faults,
    since X-Ray's server-side FilterExpression cannot substring-match service
    names; a summary with no ServiceIds, or only non-demo ones, is dropped as
    unscopable. Extracts the HTTP status and a best-effort root-cause message,
    and attaches each trace's console URL. The result is sorted by ``trace_id``
    so the report's order is defined and never depends on set or incidental dict
    ordering (engineering-practices §2). Empty input yields an empty list, not
    an error.
    """
    faults: list[FaultTrace] = []
    for summary in trace_summaries:
        has_fault = bool(summary.get("HasFault"))
        has_error = bool(summary.get("HasError"))
        if not (has_fault or has_error):
            continue
        if not _is_in_demo_scope(summary):
            continue
        trace_id = str(summary.get("Id", ""))
        faults.append(
            FaultTrace(
                trace_id=trace_id,
                has_fault=has_fault,
                has_error=has_error,
                http_status=_extract_http_status(summary),
                root_cause_message=_extract_root_cause_message(summary),
                console_url=xray_trace_console_url(trace_id, region=region),
            )
        )
    faults.sort(key=lambda fault: fault.trace_id)
    return faults


def _classify(fault: FaultTrace) -> str:
    """A short kind label: ``fault`` (5xx) vs ``error`` (expected 4xx)."""
    if fault.has_fault:
        return "fault"
    return "error"


def format_report(
    faults: list[FaultTrace], alarm_state: str | None, window_desc: str
) -> str:
    """Render a human-readable prose report of the faults and alarm state.

    The no-faults case reads plainly as healthy — "no errors or faults in
    window" — never as a failure. 5xx faults and expected 4xx client errors are
    labelled distinctly so a reader does not mistake the demo's intentional
    400/404 for a broken endpoint.
    """
    lines = [
        "X-Ray Sudoku Demo — error report (demo Lambdas + canary)",
        f"  Window          : {window_desc}",
        f"  Canary alarm    : {_format_alarm(alarm_state)}",
    ]
    if not faults:
        lines.append("  X-Ray faults    : none — no errors or faults in window (healthy)")
        return "\n".join(lines)

    fault_count = sum(1 for fault in faults if fault.has_fault)
    error_count = sum(1 for fault in faults if fault.has_error and not fault.has_fault)
    lines.append(
        f"  X-Ray faults    : {len(faults)} "
        f"({fault_count} fault/5xx, {error_count} error/4xx)"
    )
    lines.append(
        "  Note: a 'fault' is a 5xx/unhandled failure; an 'error' is a 4xx — the "
        "demo's intentional invalid-move (400) / unknown-game (404) responses are "
        "expected client errors, not failures."
    )
    for fault in faults:
        kind = _classify(fault)
        status = fault.http_status if fault.http_status is not None else "?"
        lines.append(f"    [{kind}] {fault.trace_id}  HTTP {status}")
        if fault.root_cause_message:
            lines.append(f"        cause: {fault.root_cause_message}")
        lines.append(f"        trace: {fault.console_url}")
    return "\n".join(lines)


def _format_alarm(alarm_state: str | None) -> str:
    """Render the canary alarm state, including the not-found case."""
    if alarm_state is None:
        return "not found (canary/alarm not deployed, or not readable)"
    if alarm_state == "ALARM":
        return "ALARM (firing — the canary success rate is below threshold)"
    return alarm_state


def report_as_dict(
    faults: list[FaultTrace], alarm_state: str | None, window_desc: str
) -> dict[str, Any]:
    """Render the report as a machine-readable dict (the ``--json`` form)."""
    return {
        "window": window_desc,
        "canary_alarm_state": alarm_state,
        "fault_count": len(faults),
        "faults": [asdict(fault) for fault in faults],
    }


# --- AWS I/O layer (injected client / factories, never a client at import) ---


class _TraceSummariesClient(Protocol):
    """The one X-Ray client operation the fetch needs — ``get_trace_summaries``."""

    def get_trace_summaries(self, **kwargs: Any) -> dict[str, Any]: ...


class _AlarmsClient(Protocol):
    """The one CloudWatch client operation the read needs — ``describe_alarms``."""

    def describe_alarms(self, **kwargs: Any) -> dict[str, Any]: ...


def fetch_fault_trace_summaries(
    client: _TraceSummariesClient,
    start_time: float,
    end_time: float,
    region: str = "us-east-1",
) -> list[FaultTrace]:
    """Page X-Ray ``get_trace_summaries`` over the window, keeping fault/error traces.

    Applies a ``fault = true OR error = true`` filter expression so the account's
    unrelated *healthy* traffic is excluded server-side, follows ``NextToken`` to
    the end so every page is aggregated, and hands the raw summaries (which carry
    ``ServiceIds``) unchanged to :func:`summarize_fault_traces`. That pure layer
    keeps only fault/error summaries, drops any not in the demo's footprint
    (Requirement 14.6 — X-Ray FilterExpression cannot scope by service name, so
    this happens client-side), and attaches console URLs. The client is injected,
    so this runs against a fake with no real AWS in the offline tests.
    """
    summaries: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "StartTime": start_time,
        "EndTime": end_time,
        "FilterExpression": _FAULT_ERROR_FILTER,
    }
    while True:
        response = client.get_trace_summaries(**kwargs)
        summaries.extend(response.get("TraceSummaries", []))
        next_token = response.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return summarize_fault_traces(summaries, region=region)


def read_canary_alarm_state(client: _AlarmsClient) -> str | None:
    """Return the canary alarm's ``StateValue`` (OK/ALARM/INSUFFICIENT_DATA).

    Queries ``describe_alarms`` by the ``SudokuCanarySuccessAlarm`` name prefix
    so the unrelated ``process-endpoint`` canary's alarms are never picked up,
    and returns the first matching alarm's state, or ``None`` when no matching
    alarm exists (the canary/alarm isn't deployed) — a real answer, not a crash.
    """
    response = client.describe_alarms(AlarmNamePrefix=_ALARM_NAME_PREFIX)
    for alarm in response.get("MetricAlarms", []):
        name = str(alarm.get("AlarmName", ""))
        if name.startswith(_ALARM_NAME_PREFIX):
            return str(alarm.get("StateValue"))
    return None


# --- CLI ---------------------------------------------------------------------


def _region() -> str:
    """The AWS region to operate in (AWS_REGION env, else us-east-1)."""
    return os.environ.get("AWS_REGION", "us-east-1")


def _window(hours: int, now: float) -> tuple[float, float, str]:
    """Return ``(start_epoch, end_epoch, description)`` for the last ``hours``.

    ``now`` is injected (rather than read from the wall clock inside the pure
    layers) so the window is a deterministic function of its inputs; the CLI
    passes ``time.time()``.
    """
    start = now - hours * 3600
    desc = f"last {hours}h ({_iso(start)} - {_iso(now)})"
    return start, now, desc


def _iso(epoch: float) -> str:
    """Format an epoch as an ISO-8601 UTC string for the window description."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _boto3_session(profile: str | None, region: str) -> Any:
    """Build a boto3 Session, honouring an explicit profile or AWS_PROFILE.

    Imported lazily inside the function so importing this module never requires
    boto3 or constructs a client — the offline tests import the module and drive
    the pure/plumbing functions with fakes, touching none of this.
    """
    import boto3

    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def run(args: argparse.Namespace, session_factory: Callable[[str | None, str], Any]) -> int:
    """Fetch faults + the alarm state and print the report. Returns an exit code."""
    logger = _get_logger()
    region = _region()
    session = session_factory(args.profile, region)

    start, end, window_desc = _window(args.hours, time.time())
    faults = fetch_fault_trace_summaries(
        session.client("xray"), start, end, region=region
    )
    alarm_state = read_canary_alarm_state(session.client("cloudwatch"))

    logger.info(
        "computed error report",
        extra={
            "region": region,
            "hours": args.hours,
            "faultCount": len(faults),
            "alarmState": alarm_state,
        },
    )

    # stdout carries only the result — prose, or machine-readable JSON.
    if args.json:
        print(json.dumps(report_as_dict(faults, alarm_state, window_desc), indent=2))
    else:
        print(format_report(faults, alarm_state, window_desc))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        type=int,
        default=_DEFAULT_HOURS,
        help="How many hours back to scan for faults (default: 1).",
    )
    parser.add_argument(
        "--region",
        default=_region(),
        help="AWS region (default: AWS_REGION or us-east-1).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS profile to use (else AWS_PROFILE from the environment).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the prose report.",
    )
    args = parser.parse_args(argv)
    # --region overrides the environment for downstream region resolution.
    os.environ["AWS_REGION"] = args.region

    logger = _get_logger()
    try:
        return run(args, _boto3_session)
    except OSError as exc:
        # Network/credential/transport failure from boto3 at the CLI boundary.
        logger.exception("error report I/O error")
        print(json.dumps({"error": "io_error", "detail": str(exc)}))
        return 1
    except Exception as exc:
        # Anything else (e.g. a botocore ClientError / NoCredentialsError) is
        # caught at this true top-level boundary so the operator sees one JSON
        # line, not a stack trace. It is logged with full context via
        # logger.exception, per engineering-practices §5.
        logger.exception("error report unexpected error")
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
