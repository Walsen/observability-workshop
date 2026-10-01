# Kiro Dynamic Workflows

This folder holds **Kiro Dynamic Workflow** definitions — sandboxed `ctx`-DSL
Python scripts that a KiroCrew session runs as a multi-phase, inspectable,
restartable orchestration (Workflows tab). Version-controlled here so the team
runs the same orchestration the same way.

## What a Dynamic Workflow is

A script with a pure-literal `META` dict and an `async def workflow(ctx)`
entrypoint. The sandbox allows only safe builtins and the `ctx` surface
(`ctx.agent`, `ctx.parallel`, `ctx.pipeline`, `ctx.phase`, `ctx.log`,
`ctx.args`, `ctx.budget`) — no imports, no I/O. The model runs only at the leaf
`ctx.agent(...)` calls. Each phase is a restart point via
`workflow_rerun_subtree`.

## Workflows here

### `deploy-and-verify-release.py`
Gates a release of the X-Ray Sudoku demo end to end, composing the repo's own
`just` recipes:

1. `offline-gate` — `just test` + `just synth` (offline, no credentials)
2. `deploy` — `just deploy` (optional; needs the `Walsen` SSO profile; mutates
   infra and costs money — **off by default**)
3. `verify-trace` — `just verify-trace` asserts the single end-to-end
   API Gateway → Lambda → DynamoDB X-Ray trace
4. `smoke-ui` — drives the live UI (New Game / Solve) and screenshots it
5. `report` — GO/NO-GO release report

- **Args:** `{"profile": "Walsen", "run_deploy": false}`
- **Credentials:** phase 1 is offline; phases 2–4 need the `Walsen` profile.
  Keep the `deploy` phase confirm-gated, not unattended.
- **Restart value:** a flaky live trace poll → rerun the `verify-trace` phase
  only, without redeploying.

## How to run

From a KiroCrew chat scoped to this repo, either:

- ask: *"run the saved workflow at `.kiro/workflows/deploy-and-verify-release.py`
  with run_deploy false"*, or
- use the `workflow_run` tool with `source` set to the file contents and `args`
  as above.

A run streams to the chat's **Workflows** panel and injects its result into the
chat on completion. Watch with `workflow_status` / `workflow_result`; restart a
phase with `workflow_rerun_subtree`.

> Validated against KiroCrew's `kiro_crew.workflows.validate` before commit.
