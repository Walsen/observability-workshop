---
name: show-traces
description: Show X-Ray / Application Signals tracing data for recent requests to this project's API Gateway + Lambdas + DynamoDB. Use when the user asks to see traces, latency, faults, errors, or the request path for the deployed stack.
---

# Show tracing data for current requests

Summarize recent distributed traces for the X-Ray Sudoku demo
(`XraySudokuDemoStack`): the path Browser/Canary → API Gateway (REST, `/prod`
stage) → Lambda (NewGame / GetGame / SubmitMove / Solve) → DynamoDB (Games
table), plus the `xraysudokudemosddbbe0` Synthetics canary.

## Prerequisites

- The `awslabs.cloudwatch-applicationsignals-mcp-server` MCP server must be connected
  (configured in `.kiro/settings/mcp.json`, profile `Walsen`, region `us-east-1`).
  If its tools aren't available, tell the user to enable/reconnect that server and stop.
- The stack must be deployed. Stack name: `XraySudokuDemoStack`. Region: `us-east-1`.

## Workflow

1. **Pick the time window.** Default to the **last 30 minutes** unless the user
   gives one ("today", "last hour", a specific range). Use ISO 8601 with an
   explicit UTC offset (e.g. `2026-09-19T20:00:00+00:00`).

2. **Find the service.** Call `list_monitored_services` to locate the stack's
   Lambda services (look for `XraySudokuDemoStack-NewGameFunction` /
   `XraySudokuDemoStack-SolveFunction` / the other Sudoku functions). If
   Application Signals lists no services, fall back to raw X-Ray via
   `query_sampled_traces` filtered by the function name(s) — Application Signals
   may not be enabled even though X-Ray tracing is.

3. **Pull traces.** Use `query_sampled_traces` for the window to get the trace
   summaries. For a specific slow/failed request, use `search_transaction_spans`
   to drill into the span breakdown (API Gateway → Lambda → DynamoDB) and the
   custom solver subsegments (`generate` / `validate` / `solve-backtracking` /
   `check-solution`).

4. **Summarize for the user.** Report, concisely:
   - number of traces in the window, and the time range actually used
   - latency: p50 / p90 / max response time (call out slow outliers)
   - errors vs faults: any 4xx/5xx, throttles, or exceptions — with the trace ID
   - the downstream call breakdown: time spent in the DynamoDB GetItem/PutItem
     subsegment and time spent in the named solver subsegments (`generate` /
     `validate` / `solve-backtracking` / `check-solution`)
   - if the user asked about the canary, use `analyze_canary_failures` for
     `xraysudokudemosddbbe0` and report pass/fail + root cause

5. **If there are zero traces**, say so plainly and suggest generating some:
   open the Amplify site (https://main.de4qj9oj2by93.amplifyapp.com) and click
   New Game / Solve, or note that the canary produces a steady trickle every 5
   minutes. Don't invent trace data.

## Notes

- Traces can lag ~30–60s after a request; if the user just made a call, wait
  briefly or widen the window before concluding there's nothing.
- The canary drives the UI every 5 minutes and (`ActiveTracing` is true)
  produces canary-originated traces, so expect a steady trickle even with no
  manual play.
- The four custom solver subsegments to look for inside the Lambdas are
  `generate` (new game) and `validate` / `solve-backtracking` /
  `check-solution` (solve/move).
- Report only what the tools return. If a value isn't available, say so rather
  than estimating.
