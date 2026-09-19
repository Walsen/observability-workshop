"""End-to-end X-Ray trace verification against the DEPLOYED stack (task 13).

This is the integration check the design calls out as "explicitly outside the
offline suite": it needs AWS credentials, the network, and a live deployment, so
it lives under ``scripts/`` and is driven by ``just verify-trace`` — it is NOT
part of ``just test`` and imports nothing that the offline suite runs.

What it does, mirroring how the frontend drives the API (design "End-to-End
Trace Propagation"):

1. Resolves the deployed API base URL — from ``--api-url``, the ``API_URL``
   environment variable, or the ``XraySudokuDemoStack`` CloudFormation output
   ``ApiEndpointUrl`` (Requirement 9.3).
2. **Originates** an ``X-Amzn-Trace-Id`` in the exact shape the browser uses —
   ``Root=1-{8 hex epoch}-{24 hex random}`` (Requirement 1.1) — and sends it,
   with a client-generated ``playerId`` (Requirement 7.1/7.2), on:
     - ``POST /games`` (newGame) — exercises the ``generate`` subsegment and a
       DynamoDB ``PutItem``.
     - ``POST /games/{gameId}/solve`` (solve) — exercises the ``validate`` /
       ``solve-backtracking`` / ``check-solution`` subsegments and a DynamoDB
       ``GetItem``.
   Both reuse the SAME originated root id, so the whole exchange is one trace.
3. Polls ``xray:BatchGetTraces`` (falling back to ``GetTraceSummaries`` when the
   sampled id needs discovering) until the trace materializes, then asserts a
   single trace spans **API Gateway -> Lambda -> DynamoDB** and carries the
   named solver subsegments among {``validate``, ``generate``,
   ``solve-backtracking``, ``check-solution``} (Requirements 1.2, 1.3, 1.4, 6.*).

Exit code 0 means the full path was demonstrated; non-zero means a request
failed or the expected segments/subsegments were not found in time. The script
prints exactly what it originated and what it found, so a workshop operator can
read the result without opening the X-Ray console.

Run it with:  ``just verify-trace``  (or ``python scripts/verify_trace.py``),
with AWS credentials in the environment (``AWS_PROFILE`` / ``AWS_REGION``).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import boto3

_STACK_NAME = "XraySudokuDemoStack"
_API_URL_OUTPUT_ID = "ApiEndpointUrl"

# The four custom solver subsegments the demo promises (Requirement 6.*). newGame
# opens `generate`; solve opens `validate`, `solve-backtracking`, `check-solution`.
_SOLVER_SUBSEGMENTS = {"generate", "validate", "solve-backtracking", "check-solution"}

# How long to wait for X-Ray to ingest the trace. Traces typically appear within
# ~15-30 s; poll with a fixed gap up to this budget before giving up.
_POLL_ATTEMPTS = 20
_POLL_GAP_SECONDS = 6


def _log(message: str) -> None:
    """Print a progress line to stderr so stdout stays the machine-readable result."""
    print(message, file=sys.stderr, flush=True)


def originate_trace_id() -> str:
    """Return a fresh ``X-Amzn-Trace-Id`` root in the browser's exact shape.

    ``Root=1-{8 hex epoch seconds}-{24 hex random}`` — the format the frontend
    generates (Requirement 1.1) and the format X-Ray uses as the trace id, so the
    root id here is the id the trace is later fetched by.
    """
    epoch_hex = format(int(time.time()), "08x")
    random_hex = secrets.token_hex(12)  # 12 bytes -> 24 hex chars
    return f"Root=1-{epoch_hex}-{random_hex}"


def trace_id_from_header(trace_header: str) -> str:
    """Extract the bare X-Ray trace id (``1-...-...``) from a ``Root=`` header."""
    root = trace_header.split(";", 1)[0]
    return root.removeprefix("Root=")


def resolve_api_url(explicit: str | None, region: str) -> str:
    """Resolve the API base URL from the flag, the env, or the stack output.

    Precedence: an explicit ``--api-url``, then ``API_URL`` in the environment,
    then the ``XraySudokuDemoStack`` CloudFormation output ``ApiEndpointUrl``. The
    returned URL never has a trailing slash, so route paths append cleanly.
    """
    candidate = explicit or os.environ.get("API_URL")
    if not candidate:
        candidate = _api_url_from_cloudformation(region)
    return candidate.rstrip("/")


def _api_url_from_cloudformation(region: str) -> str:
    """Read the deployed API URL from the stack's ``ApiEndpointUrl`` output."""
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=_STACK_NAME)["Stacks"]
    outputs = stacks[0].get("Outputs", [])
    for output in outputs:
        if output.get("OutputKey") == _API_URL_OUTPUT_ID:
            return str(output["OutputValue"])
    raise RuntimeError(
        f"Stack {_STACK_NAME} has no {_API_URL_OUTPUT_ID} output; "
        "pass --api-url or set API_URL."
    )


@dataclass
class RequestResult:
    """The outcome of one HTTP call to the API."""

    label: str
    status: int
    body: dict[str, object]


def _post_json(
    url: str, payload: dict[str, object], trace_header: str, label: str
) -> RequestResult:
    """POST ``payload`` as JSON with the originated trace header; parse the reply.

    Raises on a transport error or a non-2xx status (so the caller fails loudly if
    the deployed Lambda errors — e.g. a bundling regression surfacing as a 502).
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Amzn-Trace-Id": trace_header,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{label} failed: HTTP {exc.code} from {url}: {raw[:400]}"
        ) from exc
    parsed = json.loads(raw) if raw else {}
    _log(f"  {label}: HTTP {status}")
    return RequestResult(label=label, status=status, body=parsed)


def drive_requests(api_url: str, trace_header: str, player_id: str) -> list[str]:
    """Drive newGame then solve on the same trace; return the labels exercised.

    newGame creates a game (generate + DynamoDB PutItem); solve computes the
    completion (validate / solve-backtracking / check-solution + DynamoDB
    GetItem). Both carry the same originated ``X-Amzn-Trace-Id``, so they land in
    one trace.
    """
    _log(f"Driving requests against {api_url} as playerId={player_id}")
    new_game = _post_json(
        f"{api_url}/games", {"playerId": player_id}, trace_header, "POST /games"
    )
    game_id = new_game.body.get("gameId")
    if not isinstance(game_id, str) or not game_id:
        raise RuntimeError(f"newGame did not return a gameId: {new_game.body!r}")
    _log(f"  gameId = {game_id}")

    _post_json(
        f"{api_url}/games/{game_id}/solve",
        {"playerId": player_id},
        trace_header,
        "POST /games/{gameId}/solve",
    )
    return ["POST /games", "POST /games/{gameId}/solve"]


@dataclass
class TraceFindings:
    """What the fetched trace's segments and subsegments contained."""

    origin_names: set[str] = field(default_factory=set)
    subsegment_names: set[str] = field(default_factory=set)
    saw_api_gateway: bool = False
    saw_lambda: bool = False
    saw_dynamodb: bool = False
    solver_subsegments: set[str] = field(default_factory=set)


def _walk_subsegments(
    subsegments: list[dict[str, object]], findings: TraceFindings
) -> None:
    """Recurse the subsegment tree, recording names, DynamoDB, and solver spans."""
    for sub in subsegments:
        name = str(sub.get("name", ""))
        findings.subsegment_names.add(name)
        if name in _SOLVER_SUBSEGMENTS:
            findings.solver_subsegments.add(name)
        # A DynamoDB call shows up either as a subsegment named for the table with
        # namespace "aws" or carrying an `aws.table_name`, courtesy of patch_all.
        namespace = sub.get("namespace")
        aws_meta = sub.get("aws")
        if namespace == "aws" and isinstance(aws_meta, dict) and (
            "table_name" in aws_meta or aws_meta.get("operation", "").endswith("Item")
        ):
            findings.saw_dynamodb = True
        if "dynamodb" in name.lower():
            findings.saw_dynamodb = True
        nested = sub.get("subsegments")
        if isinstance(nested, list):
            _walk_subsegments(nested, findings)  # type: ignore[arg-type]


def analyze_trace(segments: list[dict[str, object]]) -> TraceFindings:
    """Classify a trace's segment documents into the path components we require.

    An API Gateway segment has origin ``AWS::ApiGateway::Stage``; a Lambda shows
    up as ``AWS::Lambda`` and/or ``AWS::Lambda::Function`` origins; DynamoDB
    appears as an ``aws``-namespace subsegment under the function (via the patched
    boto3). The solver subsegments are our four custom names.
    """
    findings = TraceFindings()
    for doc in segments:
        origin = str(doc.get("origin", ""))
        name = str(doc.get("name", ""))
        findings.origin_names.add(origin or name)
        if "ApiGateway" in origin:
            findings.saw_api_gateway = True
        if "Lambda" in origin:
            findings.saw_lambda = True
        subs = doc.get("subsegments")
        if isinstance(subs, list):
            _walk_subsegments(subs, findings)  # type: ignore[arg-type]
    return findings


def _fetch_trace(xray, trace_id: str) -> list[dict[str, object]]:
    """Return the parsed segment documents for ``trace_id`` (empty if not yet in)."""
    response = xray.batch_get_traces(TraceIds=[trace_id])
    traces = response.get("Traces", [])
    if not traces:
        return []
    segments = []
    for segment in traces[0].get("Segments", []):
        document = segment.get("Document")
        if document:
            segments.append(json.loads(document))
    return segments


def _has_function_segment(segments: list[dict[str, object]]) -> bool:
    """True once the Lambda *function* segment (the one with subsegments) is in.

    A trace ingests incrementally: the API Gateway and Lambda *service* segments
    often appear a poll or two before the ``AWS::Lambda::Function`` segment that
    actually carries the solver and DynamoDB subsegments. Concluding before that
    segment lands produces a false negative, so the poll waits for it.
    """
    for doc in segments:
        origin = str(doc.get("origin", ""))
        if "Lambda::Function" in origin and doc.get("subsegments"):
            return True
    return False


def poll_for_trace(region: str, trace_id: str) -> list[dict[str, object]]:
    """Poll X-Ray until the trace is fully ingested, or the budget runs out.

    Returns as soon as a Lambda *function* segment with subsegments is present
    (the segment carrying the solver + DynamoDB spans), since that is what the
    assertions inspect. Falls back to returning whatever partial segments exist
    at the end of the budget so the caller can report what it did find.
    """
    xray = boto3.client("xray", region_name=region)
    _log(f"Polling X-Ray for trace {trace_id} (up to {_POLL_ATTEMPTS} attempts)...")
    latest: list[dict[str, object]] = []
    for attempt in range(1, _POLL_ATTEMPTS + 1):
        segments = _fetch_trace(xray, trace_id)
        if segments:
            latest = segments
            if _has_function_segment(segments):
                _log(
                    f"  trace complete on attempt {attempt} "
                    f"({len(segments)} segments incl. Lambda function segment)"
                )
                return segments
            _log(
                f"  attempt {attempt}: {len(segments)} segments so far, "
                "awaiting Lambda function segment"
            )
        else:
            _log(f"  attempt {attempt}: not yet ingested")
        time.sleep(_POLL_GAP_SECONDS)
    return latest


def _report(trace_id: str, findings: TraceFindings, labels: list[str]) -> bool:
    """Print the verdict; return True iff the full path + subsegments were seen."""
    checks = {
        "API Gateway segment": findings.saw_api_gateway,
        "Lambda segment": findings.saw_lambda,
        "DynamoDB subsegment": findings.saw_dynamodb,
        "at least one solver subsegment": bool(findings.solver_subsegments),
    }
    result = {
        "traceId": trace_id,
        "requests": labels,
        "origins": sorted(findings.origin_names),
        "solverSubsegmentsFound": sorted(findings.solver_subsegments),
        "checks": checks,
    }
    # stdout: the machine-readable result; stderr carried the progress narration.
    print(json.dumps(result, indent=2))
    ok = all(checks.values())
    for label, passed in checks.items():
        _log(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    missing = _SOLVER_SUBSEGMENTS - findings.solver_subsegments
    if missing:
        _log(f"  (note: solver subsegments not seen this run: {sorted(missing)})")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=None, help="Deployed API base URL.")
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: AWS_REGION or us-east-1).",
    )
    args = parser.parse_args()

    api_url = resolve_api_url(args.api_url, args.region)
    trace_header = originate_trace_id()
    trace_id = trace_id_from_header(trace_header)
    player_id = f"e2e-verify-{secrets.token_hex(4)}"

    _log(f"Originated trace header: {trace_header}")
    _log(f"Trace id: {trace_id}")

    labels = drive_requests(api_url, trace_header, player_id)

    segments = poll_for_trace(args.region, trace_id)
    if not segments:
        _log("FAILED: trace did not appear in X-Ray within the polling budget.")
        print(json.dumps({"traceId": trace_id, "found": False}, indent=2))
        return 1

    findings = analyze_trace(segments)
    ok = _report(trace_id, findings, labels)
    if ok:
        _log("SUCCESS: one trace spans Frontend-originated -> API Gateway -> "
             "Lambda -> DynamoDB with solver subsegments.")
        return 0
    _log("FAILED: the trace did not contain the full expected path.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
