---
inclusion: always
---

# Engineering practices

These practices apply to all code written or modified in this project. The
examples use the project's actual stack; the principles hold regardless of
language.

## 0. Stack

X-Ray Sudoku Demo is a single deployable application in two halves:

- A **Python 3.12 CDK backend** — an API Gateway REST API in front of Lambda
  functions and a DynamoDB table, defined and deployed as AWS CDK in Python.
- A **no-build static frontend** — plain HTML, CSS, and JavaScript served as-is
  by AWS Amplify Hosting, with no bundler or build step.

- **Language:** Python 3.12 for all backend and infrastructure code.
- **HTTP:** Amazon API Gateway REST API with Lambda proxy integration. There is
  no web framework in the Lambda — each handler receives the proxy event and
  returns the proxy response shape itself. Validate the request shape (path
  parameters, body fields) at the handler boundary before touching the domain.
- **Testing:** `pytest` for example-based tests, `hypothesis` for
  property-based tests.
- **Logging:** standard-library `logging` emitting single-line JSON to stdout,
  which CloudWatch captures.
- **Observability:** AWS X-Ray. Handlers are instrumented with `aws-xray-sdk`;
  `boto3` is patched so DynamoDB calls appear as subsegments of the active
  trace.
- **IaC:** AWS CDK in Python (`aws-cdk-lib`, `constructs`).
- **Dependencies:** pinned to exact versions in the backend's `pyproject.toml`
  / `uv.lock`.
- **Cloud:** API Gateway, Lambda, DynamoDB, X-Ray, CloudWatch, and Amplify
  Hosting. Region us-east-1.

The frontend has no toolchain and no server-side code; it originates the trace
and calls the API. Multiple users are separated by a client-generated
`playerId`, with no Cognito and no managed authentication.

## 1. Clean Code & SOLID

- **Single Responsibility** — the Solver computes puzzles and solutions, the
  Lambda handler translates between the HTTP proxy event and the domain, the
  DynamoDB repository persists and loads game state, and the tracer opens
  subsegments. A handler that solves a board *and* formats the DynamoDB item
  *and* opens its own subsegments is doing several jobs; split them.
- **Open/Closed** — if difficulty levels or puzzle-generation strategies grow,
  they slot into a registry keyed by name rather than adding another arm to an
  if/elif chain. Adding an `expert` difficulty should mean one new entry, not an
  edit spread across the generator.
- **Liskov Substitution** — every puzzle-generation or difficulty strategy must
  be usable wherever a strategy is expected; they differ in the numbers they
  produce, never in the shape of the board they return.
- **Interface Segregation** — the Solver has no use for API Gateway or DynamoDB
  settings; don't hand it the whole event or the whole config. Pass a board and
  an injected random generator, nothing more.
- **Dependency Inversion** — depend on abstractions at every boundary: the
  DynamoDB repository, the X-Ray tracer, the random generator, the clock, and
  the id source. These are the boundaries that get faked in tests, so the
  solver and handlers can be exercised with no AWS calls at all.
- Favor small, well-named functions over long ones. Extract helpers instead of
  nesting deeply. If logic is copy-pasted, factor it out.
- Naming reveals intent (`is_valid_placement`, not `check`). Comments explain
  *why*; the code should already explain *what*.

## 2. Determinism and testability

Puzzle generation and solving must be reproducible so tests are deterministic
and a given seed reproduces the same puzzle. That makes the following
non-negotiable rather than stylistic.

- Never call module-level `random` functions in the Solver or puzzle generator.
  Take an explicit random generator as a parameter, seeded by the caller, so the
  same seed reproduces the same puzzle and every generation test is repeatable.
- Never call `datetime.now()` or `uuid.uuid4()` directly inside domain logic.
  Inject the clock and the id source, so a handler test can pin the `gameId` and
  the timestamps it stores rather than asserting around whatever the wall clock
  produced.
- Iteration over cells, rows, columns, and boxes must have a defined order. No
  set iteration and no reliance on incidental dict ordering anywhere the order
  reaches the board, the solution, or a stored item.
- Any new source of randomness, time, or identity must arrive with a property
  test showing the seeded-reproducibility property still holds.

## 3. Test-Driven Development (TDD)

- Write a failing test **before** the implementation: red → green → refactor.
- For bug fixes, first write a test that reproduces the bug and confirm it
  fails, then fix and confirm it passes.
- Use `pytest` for concrete behaviors and error cases, and `hypothesis` for the
  correctness properties the specs name: valid boards stay valid, a generated
  puzzle has exactly one solution, solve-then-check round-trips, an invalid move
  is always rejected, and a given seed reproduces a puzzle.
- A property test asserts an invariant across generated inputs; an example test
  pins one concrete case. Most behaviors deserve both.
- Test names state the expected behavior
  (`test_solver_rejects_move_that_duplicates_a_row`,
  `test_newgame_stores_item_keyed_by_game_and_player`).
- Every new function, handler, and class needs its normal case, edge cases, and
  error cases covered before it counts as done.
- Run the full suite before presenting a change as complete. The suite —
  including CDK synthesis and the assertions over the synthesized template —
  must pass with no AWS credentials and no network access beyond localhost.

## 4. Design patterns — apply when they fit, not by default

- Reach for a pattern only when it solves a real structural problem.
- **Strategy** — difficulty levels or puzzle-generation approaches are genuine
  strategies *if* the project grows more than one. Keep each independently
  testable and selected by name from a registry, not by a long conditional. Do
  not introduce the registry for a single difficulty; that is over-patterning.
- **Adapter** — the DynamoDB repository adapts the domain's load/save to the
  boto3 table API, and the X-Ray tracer adapts subsegment bookkeeping behind a
  narrow interface. Both are the seams that get faked in offline tests.
- Document *why* a pattern was chosen when it isn't obvious. A single
  conditional does not need a pattern.

## 5. Robust error handling

- Never let a raw stack trace reach a client. Catch at the boundaries: the
  Lambda handlers.
- Catch the exception types you expect (`ValueError`, `botocore` `ClientError`,
  a validation error), not bare `except:`. Broad catches belong only at a true
  top-level boundary, and must log there.
- Validate external input at the edge and map each documented failure mode to
  its documented response:
  - A malformed board or move returns **400** with a JSON body naming the
    offending field.
  - An unknown `gameId`/`playerId` returns **404**.
  - Bad input must never produce a **500**, and never a raw stack trace.
- Every response, including every error response, carries the CORS headers that
  authorize the Amplify origin — a 400 or 404 without them is unreadable to the
  browser.
- Fail loudly in development, safely in production.

## 6. Robust logging

- Configure a structured logger once at startup, emitting one single-line JSON
  object per event to stdout, which CloudWatch captures. Never `print()`.
- Levels: `DEBUG` for developer detail; `INFO` for operational events (game
  created, move accepted, puzzle solved); `WARNING` for recoverable issues
  (move rejected, retry); `ERROR` for handled failures (DynamoDB `ClientError`,
  malformed request); `CRITICAL` for unrecoverable ones.
- Include context: `gameId`, `playerId`, the endpoint name, and the X-Ray trace
  id. Use `logger.exception(...)` inside except blocks.
- Never log anything sensitive. Game boards and ids are fine; do not log request
  headers wholesale or anything that could carry a token.
- Errors handled per section 5 must still be logged. Silent failure is not
  acceptable.
- Configure format and output centrally, not ad hoc per module.

## 7. Security and secrets

- No secret in a committed file — tokens, credentials, or keys. There is no
  managed auth in this demo, so there is little to hold; load anything that does
  arise from environment values at runtime, or from Secrets Manager / SSM in
  AWS.
- **Least-privilege IAM per Lambda.** Each Lambda gets only the DynamoDB actions
  it actually performs on the `Games_Table` — `getGame` needs read, `newGame`
  and `submitMove` need read and write, `solve` needs read (and write only if it
  persists the solution) — plus X-Ray write permission. No Lambda gets blanket
  table or account access.
- **Scope every item access to the `(gameId, playerId)` pair.** A read or write
  addresses exactly that one item, so one player can never read or mutate
  another player's game. This is the whole of the multi-user isolation model —
  there is no auth layer behind it.
- **CORS is locked to the Amplify origin**, not `*`. The API answers preflight
  and returns cross-origin headers only for the frontend's origin.

## Tracing example: the four solver subsegments

The Solver records four X-Ray subsegments with these exact names, so the
workshop trace shows where time goes inside the Lambda:

- `validate` — validating a board or a submitted move against Sudoku placement
  rules.
- `generate` — generating a new puzzle.
- `solve-backtracking` — running the backtracking solver.
- `check-solution` — checking a completed board.

Opening and closing these subsegments is the tracer's job, kept out of the
solver's own computation per Single Responsibility.

## Applying these practices

When implementing a feature or fix:
1. Write the failing test(s) first.
2. Implement the smallest clean, SOLID-compliant change to pass them.
3. Introduce a design pattern only if the change reveals a genuine structural
   need for one.
4. Add explicit error handling for the new code's failure modes.
5. Add logging at the appropriate points and levels.
6. Run the test suite and confirm everything passes before considering the
   work done.
