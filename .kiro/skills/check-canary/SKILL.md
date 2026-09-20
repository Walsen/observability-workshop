---
name: check-canary
description: Check the CloudWatch Synthetics canary that drives this project's deployed X-Ray Sudoku frontend. Use when the user asks about the canary, synthetics, UI monitoring, uptime, probe/heartbeat health, canary failures, or whether the site is up.
---

# Check the Synthetics canary

Report the health of the `xraysudokudemosddbbe0` CloudWatch Synthetics canary
that drives the deployed X-Ray Sudoku frontend (the Amplify site) every 5
minutes, and diagnose any failures.

## Prerequisites

- The `awslabs.cloudwatch-applicationsignals-mcp-server` MCP server must be connected
  (configured in `.kiro/settings/mcp.json`, profile `Walsen`, region `us-east-1`).
  If its tools aren't available, tell the user to enable/reconnect that server and stop.
- The stack must be deployed. Stack name: `XraySudokuDemoStack`. Region: `us-east-1`.
  The canary of interest is `xraysudokudemosddbbe0`. Note the account also contains
  an unrelated `process-endpoint` canary from another project — ignore it.

## Workflow

1. **List canaries.** Call `list_canaries` to confirm `xraysudokudemosddbbe0`
   exists and read its current state (RUNNING / STOPPED / ERROR), schedule,
   runtime, and last-started time. If it isn't there, tell the user the canary
   isn't deployed and stop.

2. **Report status.** State plainly whether the canary is running and when it
   last ran. A canary running every 5 minutes should have a recent last-run
   time; if the last run is stale (well over 5 minutes ago), call that out.

3. **Analyze failures.** Call `analyze_canary_failures(canary_name="xraysudokudemosddbbe0")`
   to get pass/fail history and root-cause detail. A failure here means the UI
   flow broke — New Game or Solve failed, or a CORS/API error surfaced.
   Summarize:
   - recent success rate / whether it's currently passing or failing
   - for any failures: the root cause (HTTP status, exception, timeout, CORS
     error, etc.) and the failing run's timestamp
   - artifacts (screenshots / HAR / logs) the analysis surfaced

4. **Correlate when it's failing.** If the canary is failing, the frontend, the
   API, or a Lambda behind it is likely the cause. Cross-check the backing
   Lambdas (`XraySudokuDemoStack-NewGameFunction*` / `XraySudokuDemoStack-SolveFunction*`)
   and the API with `audit_services`, or use the `show-traces` skill, to see
   whether a Lambda is erroring or slow. Tie the canary failure to the
   underlying fault when you can.

5. **Summarize.** Give a short verdict: is monitoring green, and if not, what
   broke and where. Also mention the CloudWatch alarm
   (`XraySudokuDemoStack-SudokuCanarySuccessAlarm...` on the `SuccessPercent`
   metric) as the automated signal that fires when the canary stops passing.

## Notes

- The canary is a **browser (Puppeteer)** canary that drives the Amplify Sudoku
  UI: it clicks New Game, asserts a 9x9 board of 81 cells, clicks Solve, and
  asserts the solved state. It is NOT an HTTP probe.
- Runtime is `syn-nodejs-puppeteer-13.0`; canary `ActiveTracing` is **true**, so
  canary runs **DO** produce X-Ray traces spanning Browser → API Gateway →
  Lambda → DynamoDB — you CAN expect canary spans in X-Ray.
- Report only what the tools return. If a value isn't available, say so rather
  than estimating.
