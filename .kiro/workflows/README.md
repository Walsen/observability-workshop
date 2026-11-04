# Kiro Workflows

This folder holds **Kiro Workflow** recipes — declarative `*.workflow.json`
(JSON or YAML) definitions that the Kiro workflow runtime executes as a graph of
agent steps. Each step runs in its own session with fresh context, and progress
is reported back to the main chat session. Version-controlled here so the team
runs the same orchestration the same way.

## What a Kiro Workflow is

A recipe is a declarative graph, not a script. It has:

- a `name`, an optional `description`, and an `inputs` map of template variables
- a `steps` array of **nodes**: `step` (runs one agent), `sequence` (ordered),
  `repeat` (loop with a stop condition), `parallel` (concurrent branches), and
  `watch` (polls an external system).

Each `step` names an `agent` and carries a `prompt`. Dependencies are expressed
by **ordering** steps and referencing earlier output through `{{step_id.output}}`
template variables — there are no separate graph edges to maintain. A step may
declare `artifacts` (named files it writes) that later steps and the runtime can
reference, and a `repeat` loop stops when a `fileCheck` on a JSON artifact
matches.

See the [Kiro workflows announcement](https://kiro.dev/blog/introducing-workflows)
and the Workflows docs for the full model.

## Custom agents (`.kiro/agents/`)

This workflow runs three project-specific custom agents instead of the bundled
`wf-*` agents:

- **`qa-engineer`** — verifies the deployed stack end to end and renders a
  strict pass/fail verdict. Read-only (never edits code). Has three observation
  methods:
  - **Playwright** (the `playwright` MCP server) — drives the live Amplify UI in
    headless chromium: New Game, assert the 9x9 board, Solve, assert the
    round-trip, with screenshots and console/network inspection.
  - **CloudWatch Synthetics** — reads the `xraysudokudemosddbbe0` browser canary
    via the `awslabs.cloudwatch-applicationsignals-mcp-server` MCP server.
  - **X-Ray / Application Signals** — finds genuine 5xx faults (vs the demo's
    intentional 400/404) behind the UI.
  - Writes `qa-verdict.json`, which gates the loop.
- **`fix-developer`** — reads the QA verdict and fixes the blocking defects
  following the repo's engineering practices (TDD first, SOLID, boundary error
  handling, structured logging, determinism), runs the offline suite, and
  commits. Also runs the offline gate and the conditional deploy. Writes
  `fix-record.json`.
- **`release-coordinator`** — the manager. Reads every phase's output and writes
  the single GO/NO-GO `release-report.md`. Runs no tests and edits no code; it
  decides and reports, and flags disputed blockers for a human.

> The `qa-engineer` agent depends on the `playwright` MCP server defined in
> `.kiro/settings/mcp.json`. On this host (Amazon Linux 2023) the browser is
> provided through Nix — the server is pinned to `@playwright/mcp@0.0.80` and
> points `PLAYWRIGHT_BROWSERS_PATH` at the Nix `playwright-driver.browsers`
> bundle (chromium 1243), with `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`, because
> Playwright's own download links against system libraries that distro lacks.
> The bundle is pinned against GC by the `.devbox-gcroots/playwright-browsers`
> symlink (regenerate with
> `nix build --out-link .devbox-gcroots/playwright-browsers nixpkgs#playwright-driver.browsers`).

## Workflows here

### `deploy-and-verify-release.workflow.json`

Gates a release of the X-Ray Sudoku demo, composing the repo's own `just`
recipes with a QA/fix loop. A top-level `sequence`:

1. **offline-gate** (`fix-developer`) — `just test` + `just synth` (offline, no
   credentials); writes `offline-gate.json`
2. **deploy** (`fix-developer`) — conditional on the `run_deploy` input; runs
   `just deploy` only when `run_deploy` is true (mutates infra and costs money —
   **off by default**); writes `deploy.json`
3. **qa-fix-loop** — a `repeat` (max 3 iterations) of:
   - **qa** (`qa-engineer`) — Playwright + Synthetics + X-Ray verification;
     writes `qa-verdict.json`
   - **fix** (`fix-developer`) — fixes the blockers QA found (no-op if QA
     passed); writes `fix-record.json`

   The loop stops when `qa-verdict.json` shows `"passed": true`. QA is first in
   the body so its verdict gates the stop condition; `fix` no-ops once QA is
   clean. On exhausting iterations it continues to the report (which will read
   NO-GO from the unresolved verdict).
4. **verify-trace** (`qa-engineer`) — `just verify-trace` asserts the single
   end-to-end API Gateway → Lambda → DynamoDB X-Ray trace; writes
   `verify-trace.json`
5. **smoke-ui** (`qa-engineer`) — a final Playwright New Game / Solve smoke with
   screenshots
6. **report** (`release-coordinator`) — GO/NO-GO release report; writes
   `release-report.md`

Steps pass results via `{{offline-gate.output}}`, `{{deploy.output}}`,
`{{qa.output}}`, `{{verify-trace.output}}`, and `{{smoke-ui.output}}`; the
gating phases also write JSON artifacts under `.agents/workflow/`.

- **Inputs:** `profile` (SSO profile, default `Walsen`), `run_deploy` (default
  `false`).
- **Credentials:** phase 1 is offline; the deploy, QA, trace, and smoke phases
  need the `profile` credentials and network. Keep `run_deploy` false unless a
  deploy is intended.
- **Rerun value:** a flaky live trace poll → rerun the `verify-trace` step
  without redeploying; unresolved QA blockers → another QA/fix iteration.

## How to run

From a Kiro chat scoped to this repo, either:

- ask: *"run the saved workflow at
  `.kiro/workflows/deploy-and-verify-release.workflow.json` with run_deploy
  false"*, or
- launch it by absolute path through the workflow runtime, passing
  `inputs` such as `{"profile": "Walsen", "run_deploy": "false"}`.

A run executes in the background and reports progress back into the chat through
step messages. Validate a recipe with `validate_workflow` before committing a
change to it.
