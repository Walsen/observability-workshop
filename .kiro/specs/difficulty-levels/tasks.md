# Implementation Plan: Difficulty Levels

## Overview

This feature is delivered by a **Kiro Workflow recipe**, not by hand-authored
fine-grained coding tasks. These tasks are deliberately coarse: they author the
`Difficulty_Workflow_Recipe`, launch it against the approved `design.md` to
produce the implementation through an implement → review → fix loop, confirm the
run converged and the offline suite passes, and finally gate the result through
the existing `deploy-and-verify-release` release workflow.

The fine-grained decomposition — which files change, which functions are added,
which pytest/hypothesis tests are written — lives inside the recipe's step
prompts (see the "Deliverable: Implementation Workflow Recipe" section of
`design.md`), **not** here. Do not expand these tasks into per-file or
per-function coding steps.

## Tasks

- [x] 1. Author the implementation workflow recipe for this feature
  - Create a `*.workflow.json` under `.kiro/workflows/` (the
    `Difficulty_Workflow_Recipe`, e.g.
    `.kiro/workflows/implement-difficulty-levels.workflow.json`) that targets
    this spec's `design.md` and `requirements.md` as its authoritative
    specification.
  - Structure it as a `sequence` containing a `repeat` implement → review loop
    (e.g. `maxIterations: 3`, `onMaxIterations: "continue"`): a `wf-coder` step
    implements against the design (first iteration writes the change; later
    iterations consume the reviewer's `NEEDS_CHANGES` verdict as a fix pass),
    then a `semantic_reviewer` step renders the **final gating verdict** as a
    JSON artifact under `.agents/workflow/`; the loop's `fileCheck` stop
    condition reads that verdict (`APPROVED` / `passed: true`), iterating until
    converged or iterations are exhausted.
  - Use **registered workflow step agents only** (`wf-coder`,
    `semantic_reviewer`, other `wf-*`); the `.kiro/agents/` chat agents
    (`qa-engineer`, `fix-developer`, `release-coordinator`) are NOT valid
    workflow step agents and must not appear as steps.
  - Have the recipe run in a **git worktree** and **rebase onto mainline at the
    end** of the run, so only a converged, reviewed result is integrated.
  - Validate the recipe with `validate_workflow` before saving/finishing.
  - _Requirements: 10.1, 10.2, 10.4_

- [x] 2. Launch the implementation workflow against the approved design
  - Launch the `Difficulty_Workflow_Recipe` from task 1 (launching happens in
    this task layer — a workflow step cannot launch another workflow), in a git
    worktree, running the implement → review → fix loop until it converges.
  - Point it at this spec's `.kiro/specs/difficulty-levels/design.md` and
    `requirements.md` as the implementation target; the run produces the backend
    registry/solver/handler/model edits, the frontend `<select>` and wiring, and
    the pytest + hypothesis tests the design describes.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 9.1, 9.2, 10.4_

- [x] 3. Review the run's output and iterate if the loop did not converge
  - Confirm the reviewer's final verdict is `APPROVED`; if `NEEDS_CHANGES`
    persisted past `maxIterations`, adjust and re-launch the recipe (or re-run
    its loop) until the verdict converges.
  - Confirm the offline suite passes with no AWS credentials and no network
    beyond localhost: `just test` (includes the CDK synth assertions) and
    `just synth` both succeed; a failure under those conditions is a hard
    failure with no fallback.
  - _Requirements: 10.4, 11.1, 11.2, 11.3, 11.4, 11.5_

- [x] 4. Gate the result with the existing deploy-and-verify release workflow
  - Launch `.kiro/workflows/deploy-and-verify-release.workflow.json` from this
    task layer (not embedded as a step in the implementation recipe) to gate the
    converged change: offline gate, optional deploy, the QA/fix loop, live X-Ray
    trace verification, UI smoke, and the GO/NO-GO report.
  - _Requirements: 10.3_

## Notes

- These tasks are intentionally coarse: the feature is produced by the
  `Difficulty_Workflow_Recipe` (authored in task 1, launched in task 2), not by
  hand-written fine-grained coding steps. The per-file / per-function
  decomposition and the property/unit tests live in the recipe's step prompts
  and in `design.md`, so they are not re-listed here.
- Launching a workflow (tasks 2 and 4) stays in the task layer because a
  workflow step cannot launch another workflow.
- The implement → review loop is gated by the `semantic_reviewer`'s verdict;
  convergence is confirmed in task 3 before the release gate runs in task 4.
- Each task references the requirements it advances; Requirement 10 (delivery by
  a workflow recipe gated by deploy-and-verify) is threaded across all four
  tasks.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2"] },
    { "id": 2, "tasks": ["3"] },
    { "id": 3, "tasks": ["4"] }
  ]
}
```
