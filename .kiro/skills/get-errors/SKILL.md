---
name: get-errors
description: Report errors happening now in this project's deployed X-Ray Sudoku demo — X-Ray faults and failed requests, synthetics/canary failures, what's broken — each with a clickable X-Ray console URL. Use when the user asks what's erroring, what's broken, about faults, failed requests, X-Ray errors, or canary failures.
---

# Report current errors

Report the errors currently affecting the deployed X-Ray Sudoku demo (stack
`XraySudokuDemoStack`, account `862307432587`, `us-east-1`): recent X-Ray
faults/errors on the demo's Lambdas behind the API, and any Synthetics canary
failures, each with a clickable X-Ray console link. Scope is the demo's own
footprint (the four Sudoku Lambdas + the `xraysudokudemosddbbe0` canary), not
every fault in the account.

## Prerequisites

- The `awslabs.cloudwatch-applicationsignals-mcp-server` MCP server must be connected
  (configured in `.kiro/settings/mcp.json`, profile `Walsen`, region `us-east-1`).
  If its tools aren't available, tell the user to enable/reconnect that server and
  stop — or fall back to the script (see "Script fallback" below).
- The stack must be deployed. Stack name: `XraySudokuDemoStack`. Region: `us-east-1`.
  The canary of interest is `xraysudokudemosddbbe0`; the account also has an
  unrelated `process-endpoint` canary — ignore it.

## Workflow

1. **Pick the time window.** Default to the **last 1 hour** unless the user gives
   one ("today", "last 15 minutes", a specific range). Use ISO 8601 with an
   explicit UTC offset (e.g. `2026-09-19T21:00:00+00:00`).

2. **Find X-Ray errors.** Use `query_sampled_traces` for the window to pull trace
   summaries, and `search_transaction_spans` / `audit_services` against the
   demo's Lambda services (`XraySudokuDemoStack-NewGameFunction` /
   `XraySudokuDemoStack-GetGameFunction` / `XraySudokuDemoStack-SubmitMoveFunction`
   / `XraySudokuDemoStack-SolveFunction`) to surface the faults and errors. For
   **each** error, report:
   - the **trace ID**,
   - the **operation / route** (e.g. `POST /games/{gameId}/moves`),
   - the **HTTP status / exception**, and
   - a **clickable X-Ray console URL** of this exact shape (do not invent one):
     `https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#xray:traces/{TRACE_ID}`
   - **Distinguish fault vs error.** A **fault** is a 5xx / unhandled exception —
     a real failure. An **error** is a 4xx: the demo's intentional invalid-move
     **400** and unknown-game **404** responses are **expected client errors, not
     failures** — call them out as such so they aren't mistaken for something
     broken.

3. **Check the canary.** Call
   `analyze_canary_failures(canary_name="xraysudokudemosddbbe0")` for any canary
   failures in the window: the root cause (HTTP status, exception, timeout, CORS
   error), the failing run's timestamp, and the artifacts (screenshots / HAR /
   logs) it surfaces. A canary failure means the New Game or Solve UI flow broke.

4. **Summarize.** Give a short list of the errors, each with its console URL,
   plus whether the **canary alarm** (`XraySudokuDemoStack-SudokuCanarySuccessAlarm...`
   on the Synthetics `SuccessPercent` metric) is currently firing. If there are
   **no errors**, say so plainly — the demo is healthy — **not** as an error.

## Script fallback

The same data is available without the MCP server through the script that backs
this skill:

```
just error-report            # last 1 hour, prose
just error-report --hours 6  # a wider window
just error-report --json     # machine-readable
```

`just error-report` (backed by `scripts/error_report.py`) runs a read-only,
paginated X-Ray `get_trace_summaries` scoped to faults/errors plus a CloudWatch
`describe_alarms` read of the canary alarm, and prints each fault trace with the
same console URL shape. It needs AWS credentials + network and is outside the
offline `just test` suite. Use it when the App Signals MCP server isn't
connected, or to double-check the interactive result.

## Notes

- **X-Ray sampling** means not every request appears in traces — an empty result
  means "no *sampled* faults in the window", not a guarantee of zero errors.
- The demo's **400 (invalid move)** and **404 (unknown game)** responses are
  intentional and show up as X-Ray *errors*, not *faults*; never report them as
  the stack being broken.
- Traces can lag ~30–60s after a request; if the user just triggered something,
  widen the window or wait briefly before concluding there's nothing.
- Report only what the tools return. **Do not invent trace ids or console URLs** —
  build a URL only for a trace id a tool actually returned.
