---
name: get-cost
description: "Report the current AWS cost of this project's deployed solution (X-Ray Sudoku demo) via AWS Cost Explorer. Use when the user asks how much the solution costs, month-to-date spend, cost by service, or a cost forecast."
---

# Report the AWS cost of this solution

Report the current AWS cost of the deployed X-Ray Sudoku demo (stack
`XraySudokuDemoStack`, account `862307432587`, `us-east-1`) using AWS Cost
Explorer through the Billing & Cost Management MCP server. Give a per-service
breakdown, a total for the window, and — when asked — a month-end forecast.

## Prerequisites

- The AWS Billing & Cost Management MCP server (`awslabs.billing-cost-management`)
  must be connected and its `cost_explorer` tool available. If it isn't, tell the
  user to enable it and stop. It needs `ce:GetCostAndUsage` / `ce:GetCostForecast`
  (the Walsen / admin identity has this).
- Cost Explorer data is account-scoped and global; region does not matter for the
  query.
- The stack must be deployed (`XraySudokuDemoStack`, `us-east-1`) for there to be
  any cost.
- The precise tag-based mode ALSO requires: the `Project` cost-allocation tag has
  been ACTIVATED in the Billing console (Billing → Cost allocation tags), which
  only the management/payer account can do, AND ~24h of backfill has elapsed.
  Until then use the service-scoped fallback.

## Workflow

1. **Pick the time window.** Default to month-to-date (first of the current month
   → today). Support "last 7 days" and explicit start/end. Use `YYYY-MM-DD`; the
   Cost Explorer end date is EXCLUSIVE, so to include today set end = tomorrow.
   Use MONTHLY granularity for month-to-date and DAILY for short windows.

2. **Choose the mode.**
   - **Tag-primary (precise):** `cost_explorer` `getCostAndUsage` with a filter on
     tag `Project=xray-sudoku-demo`
     (`{"Tags":{"Key":"Project","Values":["xray-sudoku-demo"]}}`), grouped by
     SERVICE, metric `UnblendedCost`. This is the true per-stack cost. If it
     returns empty/zero and the tag may not be active yet, fall back to service
     mode and say so.
   - **Service-fallback (works now, account-wide for those services):**
     `getCostAndUsage` grouped by SERVICE, metric `UnblendedCost`; read off the
     solution's services: AWS Lambda, Amazon API Gateway, Amazon DynamoDB, AWS
     X-Ray, AmazonCloudWatch / CloudWatch Synthetics, Amazon S3 (canary
     artifacts), AWS Amplify, AWS Data Transfer. Sum them; state clearly this is
     the account's spend on those services, not tag-precise.
   - Exclude `Credit` and `Refund` record types by default.

3. **Report.** Give a concise per-service breakdown, the total for the window, and
   the window actually used. Amounts are estimates (CE lags a few hours; MTD is
   not the final invoice). The demo's footprint is typically tiny / free-tier for
   Lambda / DynamoDB / API Gateway / X-Ray; the canary (Synthetics runs + S3
   artifacts) and data transfer are the main small line items.

4. **Optional forecast.** If asked for a projection, call `cost_explorer`
   `getCostForecast` (metric `UNBLENDED_COST`, MONTHLY, start = today, end = first
   of next month, optionally filtered by the `Project` tag) and report the
   projected month-end cost plus its confidence interval.

5. **If cost is ~zero**, say so plainly (within free tier / negligible), not as an
   error.

## Notes

- Uses the `awslabs.billing-cost-management` MCP `cost_explorer` tool; do NOT use
  the AWS CLI or the CloudWatch Application Signals server.
- Tag-based (`Project=xray-sudoku-demo`) is precise but needs the one-time Billing
  activation + ~24h; service-based works immediately but is account-wide for those
  services. The two are COMPLEMENTARY — a tag-based figure can slightly under-count
  because some line items (certain Data Transfer, some CloudWatch / X-Ray usage)
  don't carry the tag.
- Cost Explorer API calls cost ~$0.01 each — negligible but nonzero.
- The account also has unrelated spend (e.g. Bedrock); the tag-based view excludes
  it, and the service-based view already excludes non-listed services but would
  include any OTHER project using the same services — so prefer tag-based once
  active.
- Report only what the tool returns. If a value isn't available, say so rather
  than estimating.
