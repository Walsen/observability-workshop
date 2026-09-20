# Design Document

## Overview

The X-Ray Sudoku Demo is a single deployable application in two halves joined by
one manual handoff. The **backend** is an AWS CDK (Python) app defining an API
Gateway REST API, four Python 3.12 Lambda functions, and a DynamoDB table, all
instrumented for AWS X-Ray. The **frontend** is a no-build static site (HTML +
CSS + JS) hosted on AWS Amplify Hosting, connected manually through the Amplify
console with Git branch-based previews.

The demo's reason for existing is a single, coherent X-Ray trace that begins in
the browser and flows through every backend component. The browser originates
the trace (`X-Amzn-Trace-Id`); API Gateway propagates it into the Lambda; the
Lambda opens four named subsegments inside the solver and, because `boto3` is
patched, its DynamoDB calls appear as subsegments too. The result is one trace
spanning frontend → API → Lambda → DynamoDB.

Multiple players share the demo with no managed authentication: the frontend
generates a `playerId`, sends it on every request, and every Lambda scopes each
DynamoDB access to the single item identified by `(gameId, playerId)`.

This design conforms to the workspace steering: a single backend `uv` project
(devbox provides the interpreter, uv owns dependencies), `just` as the command
surface, structured single-line JSON logging, least-privilege IAM per Lambda,
and a fully offline test suite (solver/handler unit tests plus CDK synth +
template assertions, with no AWS credentials and no network beyond localhost).

_Language: Python 3.12 for all backend and infrastructure code; plain
HTML/CSS/JS for the frontend._

## Project / Folder Structure

```
observability-workshop/
├── devbox.json                # pinned toolchain (python, uv, just, jq, yq-go,
│                              #   aws-cdk-cli, awscli2, git, gh)
├── devbox.lock
├── .envrc                     # devbox → direnv activation
├── Justfile                   # test, lint, typecheck, synth, deploy
├── pyproject.toml             # backend Python manifest (uv-owned)
├── uv.lock
│
├── frontend/                  # no-build static site (Requirement 10)
│   ├── index.html             # board UI
│   ├── styles.css
│   ├── app.js                 # playerId generation, trace header, API calls
│   ├── config.js              # reads API_ENDPOINT_URL (Amplify-provided)
│   └── amplify.yml            # publishes the static files, no build step
│
└── backend/
    ├── app.py                 # CDK app entry point (env: us-east-1)
    ├── infra/
    │   └── sudoku_stack.py     # the CDK Stack (API, Lambdas, table, outputs)
    ├── handlers/               # Lambda proxy handlers (one per endpoint)
    │   ├── __init__.py
    │   ├── new_game.py
    │   ├── get_game.py
    │   ├── submit_move.py
    │   ├── solve.py
    │   ├── responses.py        # proxy-response + CORS helpers
    │   └── logging_config.py   # structured JSON logger setup
    ├── domain/                 # pure, AWS-free domain logic
    │   ├── __init__.py
    │   ├── solver.py           # generate / validate / solve / check
    │   ├── board.py            # Board type + placement rules
    │   └── tracing.py          # Tracer abstraction (X-Ray adapter + fake)
    ├── data/
    │   └── games_repository.py # DynamoDB adapter (load/save one item)
    └── tests/
        ├── test_solver.py
        ├── test_handlers.py
        ├── test_repository.py
        └── test_infra_synth.py # offline CDK template assertions
```

The `domain/` package contains no `boto3`, no API Gateway types, and no X-Ray
calls beyond the injected `Tracer` interface — it is exercised entirely offline.
`handlers/` translates the proxy event to and from the domain and owns error
mapping and logging. `data/` and `domain/tracing.py` are the adapter seams that
get faked in tests.

## Data Model: the Games Table

A single DynamoDB table, `Games_Table`, stores one item per game per player.

### Key design

| Attribute | Key role | Rationale |
|-----------|----------|-----------|
| `gameId`  | **Partition key (PK)** | Each game is an independent entity; the `gameId` is the natural, high-cardinality identity for a game instance and spreads writes evenly across partitions. |
| `playerId`| **Sort key (SK)**       | A game is always addressed together with its player. Making `playerId` the sort key means a single `GetItem`/`PutItem` on `(gameId, playerId)` is the exact, cheapest access pattern for every endpoint, and it is impossible to touch a game without naming a player. |

`gameId` is the partition key and `playerId` is the sort key. The choice is
driven by the access pattern: every read and write in this demo is a point
operation on one fully-specified `(gameId, playerId)` pair (Requirement 7.3),
so a composite primary key with `GetItem`/`PutItem` is the right primitive — no
query, scan, or GSI is needed. Because both key parts are always supplied
together, one player's item is unreachable when addressing another player's
game, which is the whole of the isolation model.

### Item attributes

| Attribute      | Type   | Description |
|----------------|--------|-------------|
| `gameId`       | S (PK) | Unique game identifier (injected id source). |
| `playerId`     | S (SK) | Client-generated player identifier. |
| `board`        | S      | Current 9×9 board state as JSON (0 = empty cell). |
| `puzzle`       | S      | The original givens as JSON, so the board can be reset/re-solved. |
| `solution`     | S      | The unique solution as JSON, computed at generation. |
| `status`       | S      | `in_progress` \| `solved`. |
| `createdAt`    | S      | ISO-8601 timestamp (injected clock). |
| `updatedAt`    | S      | ISO-8601 timestamp (injected clock). |

Boards are stored as JSON strings (a nested list of ints) rather than native
DynamoDB structures to keep serialization explicit and round-trippable. The
`solution` is stored so `submitMove`'s "is the board complete and correct" check
and `solve`'s response do not have to recompute from scratch on every request,
though the solver remains the source of truth for correctness.

## Backend Components

### Domain: the Solver and Board

`domain/board.py` defines the board representation (a 9×9 grid of ints, 0 for
empty) and the pure placement rules: `is_valid_placement(board, row, col,
value)` returns whether a value can occupy a cell without duplicating within its
row, column, or 3×3 box, and `is_complete_and_valid(board)` returns whether a
filled board satisfies every constraint.

`domain/solver.py` exposes the four operations the endpoints need, each taking
its dependencies explicitly (Dependency Inversion, and the determinism rule from
steering §2):

```python
class Solver:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def generate(self, rng: random.Random) -> GeneratedPuzzle:
        # subsegment: "generate"
        # produces (puzzle_givens, unique_solution) using the injected rng,
        # so a given seed reproduces the same puzzle.
        ...

    def validate_move(self, board: Board, move: Move) -> bool:
        # subsegment: "validate"
        ...

    def solve(self, board: Board) -> Board | None:
        # subsegment: "solve-backtracking"
        # returns the completed board, or None if the board has no solution.
        ...

    def check_solution(self, board: Board) -> bool:
        # subsegment: "check-solution"
        ...
```

- The **random generator is injected** into `generate` (never module-level
  `random`), so the same seed reproduces the same puzzle and generation tests
  are deterministic.
- The **clock and id source are injected** into the handlers (never
  `datetime.now()` / `uuid.uuid4()` in domain logic), so a handler test pins the
  `gameId` and timestamps it stores.
- Iteration over cells, rows, columns, and boxes uses a defined order.

The **four X-Ray subsegments** are opened by the `Tracer` around the relevant
solver work, with these exact names (Requirement 6): `validate`, `generate`,
`solve-backtracking`, `check-solution`. Opening/closing subsegments is the
tracer's job, kept out of the solver's computation per Single Responsibility.

### Tracing abstraction

`domain/tracing.py` defines a narrow `Tracer` protocol:

```python
class Tracer(Protocol):
    def subsegment(self, name: str) -> ContextManager[None]: ...
```

Two implementations:
- `XRayTracer` — an **adapter** over `aws_xray_sdk.core.xray_recorder`, used at
  runtime.
- `FakeTracer` — records the ordered list of opened subsegment names, used in
  tests to assert the four exact names appear without any AWS dependency.

At Lambda module load, `boto3` is patched once so DynamoDB calls become
subsegments of the active trace:

```python
from aws_xray_sdk.core import patch_all
patch_all()  # patches boto3/botocore -> DynamoDB calls appear as subsegments
```

### DynamoDB repository (adapter)

`data/games_repository.py` adapts the domain's load/save to the boto3 table API
behind a narrow interface, so handlers depend on the abstraction and tests
substitute a fake:

```python
class GamesRepository:
    def __init__(self, table) -> None:            # boto3 Table (patched)
        self._table = table

    def load(self, game_id: str, player_id: str) -> Game | None:
        # GetItem on Key={"gameId": game_id, "playerId": player_id}
        ...

    def save(self, game: Game) -> None:
        # PutItem of the fully-formed item (gameId=PK, playerId=SK)
        ...
```

Every method addresses exactly the `(gameId, playerId)` item — this is where
Requirement 7.3 isolation is enforced. The table name arrives from an
environment variable set by CDK.

### Lambda handlers

Each handler is a Lambda proxy integration: it receives the API Gateway proxy
event and returns the proxy response shape itself (no web framework). Shared
concerns live in `handlers/responses.py` (building proxy responses and attaching
CORS headers, including on errors) and `handlers/logging_config.py` (the
structured JSON logger).

| Handler | Route | Reads from event | Behavior |
|---------|-------|------------------|----------|
| `new_game` | `POST /games` | `playerId` (body) | `solver.generate(rng)` → assign `gameId` (injected id source) → `repo.save(game)` → return `gameId` + initial board. |
| `get_game` | `GET /games/{gameId}` | `gameId` (path), `playerId` (query/header) | `repo.load(gameId, playerId)`; return board, or **404** if absent. |
| `submit_move` | `POST /games/{gameId}/moves` | `gameId`, `playerId`, `move` | `repo.load` → `solver.validate_move`; **invalid → 400/invalid response**; valid → apply, `repo.save`; if board complete & correct (`check_solution`) → status `solved`. |
| `solve` | `POST /games/{gameId}/solve` | `gameId`, `playerId` | `repo.load` → `solver.solve(board)`; return completed board, or **no-solution response** if `None`. |

Handler skeleton (illustrative):

```python
def handler(event, context):
    logger = get_logger()
    try:
        game_id, player_id = parse_path_and_body(event)     # validates shape
        with tracer.subsegment("validate"):
            ...
        game = repo.load(game_id, player_id)
        if game is None:
            return not_found(game_id, player_id)             # 404 + CORS
        ...
        return ok(payload)                                   # 200 + CORS
    except ValidationError as exc:
        logger.warning(...); return bad_request(exc.field)   # 400 + CORS
    except ClientError:
        logger.exception(...); return server_error()         # 500 + CORS
```

## API Gateway (REST) Design

A single REST API (`RestApi`) with **X-Ray active tracing enabled on the stage**
(Requirement 8.1) and Lambda proxy integrations.

### Resources / methods

| Resource | Method | Integration |
|----------|--------|-------------|
| `/games` | `POST` | `new_game` |
| `/games/{gameId}` | `GET` | `get_game` |
| `/games/{gameId}/moves` | `POST` | `submit_move` |
| `/games/{gameId}/solve` | `POST` | `solve` |

### CORS

CORS is configured on the API and **locked to the Amplify origin** (not `*`)
(Requirement 8.2/8.3). Preflight `OPTIONS` responses advertise the allowed
methods (`GET`, `POST`, `OPTIONS`) and headers, including `X-Amzn-Trace-Id` and
the header/query carrying `playerId`. Every handler response, success or error,
also carries the cross-origin headers, so a 400/404 remains readable to the
browser.

### Active tracing

The API Gateway stage has `tracing_enabled=True`, so the API segment joins the
same trace and propagates context to the Lambda.

## End-to-End Trace Propagation

1. **Browser originates the trace.** `frontend/app.js` starts a root trace id
   and sends `X-Amzn-Trace-Id` on every `fetch` to the API (Requirement 1.1).
2. **API Gateway continues the trace.** With active tracing enabled, the API
   records its own segment under the incoming trace id and passes the context to
   the Lambda invocation (Requirement 1.2).
3. **Lambda continues and enriches the trace.** The X-Ray SDK creates the Lambda
   segment under the same trace; the solver opens the four named subsegments
   (`validate`, `generate`, `solve-backtracking`, `check-solution`).
4. **DynamoDB calls appear as subsegments.** Because `patch_all()` patched
   `boto3`, each `GetItem`/`PutItem` is recorded as a subsegment of the Lambda
   segment (Requirement 1.3).
5. **One coherent trace.** Frontend → API → Lambda → DynamoDB all share a single
   trace id, so the whole path shows up as one trace in the X-Ray console
   (Requirement 1.4).

```
Browser (root: X-Amzn-Trace-Id)
  └─ API Gateway segment
       └─ Lambda segment
            ├─ subsegment: validate | generate | solve-backtracking | check-solution
            └─ subsegment: DynamoDB GetItem / PutItem   (via patched boto3)
```

## Multi-User Model

There is no Cognito and no managed auth. The frontend generates a `playerId`
once (e.g. a UUID stored in `localStorage`) (Requirement 7.1) and includes it on
every request (Requirement 7.2). Every handler passes `(gameId, playerId)` to
the repository, which addresses exactly that one item (Requirement 7.3). Two
players with the same `gameId` value would still address different items because
the sort key differs, and no endpoint ever enumerates games — so one player can
neither read nor mutate another player's game.

## Infrastructure as Code (CDK)

`backend/app.py` instantiates the stack with an explicit environment pinned to
**us-east-1** (Requirement 9.4). `backend/infra/sudoku_stack.py` defines:

- The **DynamoDB table** (`gameId` PK, `playerId` SK, on-demand billing).
- **Four Lambda functions** on the **Python 3.12 runtime** with **X-Ray tracing
  `Active`** (Requirement 9.2), each receiving the table name via environment
  variable.
- The **REST API** with active tracing and the CORS configuration above.
- A **`CfnOutput`** emitting the `API_Endpoint_URL` (Requirement 9.3).
- **Least-privilege IAM per Lambda** (Requirement 9.5): each function is granted
  only the DynamoDB actions it performs on the `Games_Table` — `get_game` read;
  `new_game` and `submit_move` read+write; `solve` read (plus write only if it
  persists the solution) — via the table's grant helpers, plus
  `xray:PutTraceSegments` / `xray:PutTelemetryRecords` for trace writes. No
  Lambda receives blanket table or account access.

## Synthetic Canary Monitoring (CloudWatch Synthetics)

This slice adds a **CloudWatch Synthetics browser canary** that behaves as a
synthetic Player: on a schedule it drives the *live* Amplify-hosted Frontend
with a headless browser, and because that browser makes the same API calls a
real user's browser makes (now that CORS is locked to the Amplify origin), each
scheduled run becomes a real end-to-end request that is traced through
CloudFront/Amplify → API Gateway → Lambda → DynamoDB. A CloudWatch **alarm** on
the canary's success rate makes a broken New Game or Solve flow surface
automatically (Requirement 11).

The canary complements the existing X-Ray story rather than changing it: the
frontend, API, Lambdas, and table are unchanged; the canary is simply a
scheduled, monitored synthetic user in front of them.

### Canary type and browser script

The canary is a **UI (browser) canary** on the Synthetics
`syn-nodejs-puppeteer` runtime — a headless Chromium driven by Puppeteer. Its
handler is a small **Node.js** script following the Synthetics handler contract
(`exports.handler`), using the `Synthetics` library's `executeStep` to record
each step as a named, screenshotted step (Synthetics captures screenshots on
each step and on failure automatically) (Requirement 11.6). The script steps
are:

1. **load-site** — navigate to the `Canary_Target_URL`.
2. **new-game** — click the "New Game" control and wait for the board.
3. **assert-board** — assert a 9×9 board (81 cells) has rendered.
4. **solve** — click the "Solve" control.
5. **assert-solved** — assert the board reaches the solved/completed state.

Any failed assertion fails the step, which fails the run and lowers
`SuccessPercent` (Requirement 11.3, 11.4).

Because this script is **Node, not Python**, and is *invoked* by the Synthetics
runtime rather than *imported* by any Python code, it is **not** a `uv`
dependency and does not appear in `pyproject.toml` (dev-environment steering:
import-vs-invoke). It is an **asset file** bundled by CDK. It lives at:

```
backend/infra/canary/
└── sudoku_canary.js        # Node/Puppeteer Synthetics handler (asset, not imported)
```

### CDK construct: stable aws_synthetics, alpha module intentionally avoided

The canary is defined with the **stable** `aws_cdk.aws_synthetics` module that
ships in the already-pinned `aws-cdk-lib` (2.270.0), using the **L2 `Canary`
construct**. The alpha module `aws_cdk.aws_synthetics_alpha` is **intentionally
not used** and is **not** added as a dependency — the stable module provides
both the L2 `Canary` and the L1 `CfnCanary`, so no alpha package is required.
The L2 `Canary` cleanly covers everything this slice needs:

- **runtime** — `Runtime.SYNTHETICS_NODEJS_PUPPETEER_*` (a
  `syn-nodejs-puppeteer` runtime).
- **schedule** — `Schedule.rate(Duration.minutes(5))` → `rate(5 minutes)`
  (Requirement 11.2).
- **test/code** — `Code.from_asset(...)` pointing at `backend/infra/canary/`
  with the handler `sudoku_canary.handler`.
- **active tracing** — `active_tracing=True`, enabling X-Ray on the canary runs
  (Requirement 11.5).
- **artifacts bucket** — the L2 construct provisions the artifacts S3 bucket and
  the canary's execution role for you (Requirement 11.6); the target URL is
  passed to the script via an environment variable.

If a future need exceeds the L2 surface, the fallback is the L1
`CfnCanary` from the *same stable module* — still no alpha package.

The **CloudWatch alarm** uses the stable `aws_cdk.aws_cloudwatch` module. The
alarm is built on the canary's `SuccessPercent` metric — obtained from the L2
canary's `metric_success_percent()` (equivalently a `cloudwatch.Metric` in the
`CloudWatchSynthetics` namespace, dimension `CanaryName`) — and alarms when
success drops below the threshold (e.g. `threshold=100`,
`comparison=LESS_THAN_THRESHOLD`, over N evaluation periods) (Requirement 11.7).

### Operator-configured target URL and conditional creation

The canary needs the live site URL, supplied the **same operator-configured way
the stack already handles `allowed_origin`**: as a **CDK context value**
`canary_target_url` (e.g. `-c canary_target_url=https://main.<id>.amplifyapp.com`),
documented so no URL is hardcoded and `cdk synth` stays offline. The stack reads
it with `self.node.try_get_context("canary_target_url")`:

- **When provided** — the stack creates the canary (with the URL passed into the
  script's environment), the artifacts bucket, and the alarm.
- **When absent** — the stack synthesizes **without** the canary, so the
  existing offline synth (which supplies no context) continues to resolve
  nothing from an account and produce the same backend template (Requirement
  11.1).

The offline template-assertion test synthesizes **with** the
`canary_target_url` context supplied, so it can assert the canary and alarm
resources exist.

### IAM (least privilege) and the offline guarantee

Consistent with the existing least-privilege framing: the canary runs under its
**own execution role** created by the L2 construct, scoped to running the
browser script, writing run artifacts to its artifacts bucket, publishing
CloudWatch metrics, and writing trace data to X-Ray — no blanket bucket or
account access (Requirement 11.8). This mirrors the per-Lambda least-privilege
model already used for the four handlers.

The canary/alarm resources are **synthesized and asserted entirely offline**
(no credentials, no network, `cdk synth` resolves nothing from an account),
exactly like the existing infra assertions — the browser script is a local asset
and the target URL comes from context, not an account lookup. The **live canary
is a billable, deploy-time resource** (scheduled canary runs, its artifacts S3
bucket, and the alarm), so it is **not** part of `just test`; it deploys with
the stack when `canary_target_url` is supplied to `cdk deploy` alongside the
existing `allowed_origin` context.

## Toolchain and Dependencies

### devbox packages (exact-version placeholders)

```json
{
  "packages": [
    "python@<exact>",
    "uv@<exact>",
    "just@<exact>",
    "jq@<exact>",
    "yq-go@<exact>",
    "aws-cdk-cli@<exact>",
    "awscli2@<exact>",
    "git@<exact>",
    "gh@<exact>"
  ]
}
```

### uv dependency split (import vs invoke)

- **uv dependencies (imported by code):** `aws-cdk-lib`, `constructs`, `boto3`,
  `aws-xray-sdk`, and — as dev dependencies — `pytest`, `hypothesis`, `ruff`,
  `mypy`.
- **devbox packages (invoked as commands):** `aws-cdk-cli` (the `cdk` binary),
  `awscli2` (including `aws xray` for trace inspection). There is no separate
  X-Ray CLI to pin.

`aws-cdk-lib` (imported) and `aws-cdk-cli` (invoked) are both needed, in
different files; `boto3` (imported) and `awscli2` (invoked) sit at different
layers of the same service.

### Justfile recipe surface

| Recipe | Runs |
|--------|------|
| `just test` | `pytest` — solver, handler, repository, and CDK synth-assertion tests, fully offline. |
| `just lint` | `ruff check`. |
| `just typecheck` | `mypy`. |
| `just synth` | `cdk synth` — resolves nothing from an account. |
| `just deploy` | `cdk deploy` — emits the `API_Endpoint_URL`. |

### Frontend

The frontend has no toolchain to pin: no bundler, no build step. Its only
build-time artifact is a minimal `amplify.yml` that publishes the static files.
Nothing about it belongs in `devbox.json` or `pyproject.toml`.

## Deployment Handoff

Deployment is two steps, by design (Requirement 10.4, and the demo's stated
manual join):

1. `just deploy` runs `cdk deploy`, which provisions the backend and prints the
   `API_Endpoint_URL` as a stack output.
2. The operator sets that URL as an Amplify configuration value (an environment
   variable the frontend's `config.js` reads). Amplify's branch-based
   deployments then serve `main` to the production URL (Requirement 10.5) and
   `feature/*` to preview URLs (Requirement 10.6). Amplify Hosting itself is
   connected manually in the console and is not defined in CDK.

## Error Handling

Following the engineering-practices steering:

- **Catch at the Lambda boundary only**, and catch expected types
  (`ValueError`/`ValidationError`, `botocore` `ClientError`), never bare
  `except:` except at a true top-level guard that logs.
- **Map documented failures to documented responses:**
  - Malformed board or move → **400** with a JSON body naming the offending
    field.
  - Unknown `gameId`/`playerId` → **404**.
  - A board with no solution → the documented no-solution response (not an
    error status).
  - Bad input **never** produces a **500** or a raw stack trace.
- **CORS headers on every response**, including 400 and 404, so the browser can
  read them.

## Logging

- A structured logger configured **once** at Lambda module load, emitting
  **single-line JSON to stdout**, captured by CloudWatch. Never `print()`.
- Levels: `DEBUG` developer detail; `INFO` operational events (game created,
  move accepted, puzzle solved); `WARNING` recoverable issues (move rejected);
  `ERROR` handled failures (`ClientError`, malformed request); `CRITICAL`
  unrecoverable.
- Every log line carries context: `gameId`, `playerId`, `endpoint`, and the
  X-Ray `traceId`. `logger.exception(...)` is used inside `except` blocks.
- Nothing sensitive is logged (no wholesale request headers or anything that
  could carry a token). Boards and ids are fine.

## Testing Strategy

The suite is **fully offline** — no AWS credentials, no network beyond
localhost — and combines `pytest` example tests with `hypothesis` property
tests.

- **Solver / domain (property + example):** puzzle uniqueness, seeded
  determinism, move validation, backtracking correctness, no-solution handling,
  and solved-detection are property tests over generated boards/seeds; concrete
  edge cases (an empty board, a board one move from complete, a contradictory
  board) are example tests.
- **Handlers (example + property):** using a `FakeTracer` and an in-memory fake
  repository — 404 on missing item, 400 on malformed input (with CORS headers on
  both), the four subsegment names opened, and the persistence round-trip /
  valid-move-application / player-isolation properties.
- **Repository (example):** load/save round-trip and correct key construction,
  against a local fake table (e.g. `moto` or a hand-rolled fake) — no real AWS.
- **CDK synth (SMOKE / template assertions):** `cdk synth` resolves nothing from
  an account; `aws_cdk.assertions.Template` asserts four Lambdas at
  `python3.12` with tracing `Active`, the table present with the
  `gameId`/`playerId` key schema, the API with active tracing and Amplify-scoped
  CORS, a `CfnOutput` for the URL, region us-east-1, and IAM policies scoped to
  the table plus `xray:PutTraceSegments`.

- **Canary / alarm (SMOKE / template assertions):** synthesizing the stack
  **with** the `canary_target_url` context supplied (still no credentials, no
  network), `aws_cdk.assertions.Template` asserts an `AWS::Synthetics::Canary`
  exists with a `syn-nodejs-puppeteer` runtime, a `rate(5 minutes)` schedule,
  active tracing enabled, and an artifacts location, plus an
  `AWS::CloudWatch::Alarm` on the canary `SuccessPercent` metric with the
  configured threshold/comparison. Synthesizing **without** the context asserts
  the canary is absent (Requirement 11.1). No property test applies to the
  canary — it is deploy-time infrastructure, not input-varying logic.

Property tests run a minimum of 100 iterations and each references its design
property below. End-to-end trace verification (Requirements 1.2/1.4) is an
integration concern performed against the deployed stack (a handful of example
traces via `aws xray`), explicitly outside the offline suite. Live verification
that the deployed canary runs green and produces a trace (Requirement 11) is
likewise outside the offline suite, since it depends on a billable live
deployment.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

### Property 1: Generated puzzles have a unique solution

For any seed, the puzzle produced by the Solver's generate step has exactly one
valid completion.

**Validates: Requirements 2.2**

### Property 2: Puzzle generation is reproducible from a seed

For any seed, generating a puzzle twice with a random generator constructed from
that same seed produces identical givens and identical solutions.

**Validates: Requirements 2.2** (and the determinism rule in the
engineering-practices steering)

### Property 3: Move validity equals the absence of a placement conflict

For any board and any candidate move, the Solver reports the move valid if and
only if the value does not already occur in the move's row, column, or 3×3 box;
and any move violating a placement rule is reported invalid and leaves the board
unchanged.

**Validates: Requirements 4.2, 4.3**

### Property 4: The backtracking solver returns a valid, consistent completion

For any solvable board, the Solver's backtracking step returns a fully filled
board that satisfies every Sudoku constraint and agrees with all the original
given cells.

**Validates: Requirements 5.2, 5.3**

### Property 5: An unsolvable board yields no solution

For any board that contains a direct placement contradiction, the Solver reports
that no solution exists rather than returning a board.

**Validates: Requirements 5.4**

### Property 6: Solved detection matches full-board validity

For any completely filled board, the Solver reports the game solved if and only
if the board satisfies every row, column, and box constraint.

**Validates: Requirements 4.5**

### Property 7: Game state round-trips through the repository

For any generated game, saving it to the repository and then loading it by its
`(gameId, playerId)` key returns an equal game, stored as the single item whose
partition key is `gameId` and sort key is `playerId`.

**Validates: Requirements 2.4, 2.5, 3.2**

### Property 8: A valid move changes only its target cell

For any stored game and any valid move, after the move is applied and saved the
loaded board differs from the prior board in exactly the moved cell and no
other.

**Validates: Requirements 4.4**

### Property 9: Player isolation across item access

For any two distinct `(gameId, playerId)` pairs, writing the item for one pair
leaves the other pair's item unchanged, and a load for one pair never returns
the other's game.

**Validates: Requirements 7.3**

## Cost Attribution and Reporting

This slice makes the demo's AWS spend attributable and reportable. It adds **no
runtime behavior and no billable infrastructure**: it tags the resources the
stack already defines so their cost can be isolated, and it adds a read-only
`get-cost` skill that queries Cost Explorer through the AWS Billing & Cost
Management MCP server. It complements the existing X-Ray/observability story —
tracing shows *where time goes* in a request; this shows *what the stack costs*
to run (Requirement 12).

### App-level cost-allocation tagging

Rather than tag each construct individually, the tags are applied once at the
**app level** in `backend/app.py`, and CDK's tag aspect propagates them down to
every taggable resource in the tree (the four Lambda functions, the DynamoDB
table, the REST API, the Synthetics canary and its artifacts bucket, and the
IAM roles):

```python
app = cdk.App()
cdk.Tags.of(app).add("Project", "xray-sudoku-demo")
cdk.Tags.of(app).add("ManagedBy", "cdk")
SudokuStack(app, _STACK_ID, env=cdk.Environment(region=_REGION))
app.synth()
```

Applying to the `App` (rather than the `Stack`) keeps the tags in one place and
ensures anything the stack adds later inherits them. The tag keys are `Project`
(value `xray-sudoku-demo`, the **Project_Tag**) and `ManagedBy` (value `cdk`)
(Requirement 12.1).

Two caveats matter for interpreting the numbers, and the skill documents both:

- **Not every line item carries the tag in Cost Explorer.** Some usage —
  notably certain **Data Transfer** and some **CloudWatch/X-Ray** line items —
  is not associated with a taggable resource, so a tag-filtered total can
  slightly **under-count** true spend. The service-scoped fallback (below) and
  the tag-scoped view are therefore **complementary**, not redundant.
- **Tag-based cost requires manual activation and a backfill delay** (see the
  prerequisite below).

### Two reporting modes

The `get-cost` skill reports cost two ways, and states which mode produced the
numbers:

1. **Tag-primary (precise, per-stack) — available after activation.** Filters
   `getCostAndUsage` on the Project_Tag (`Project = xray-sudoku-demo`). This is
   the precise cost of *this* solution's resources. It is only meaningful once
   the operator has activated the `Project` cost-allocation tag in Billing and
   ~24h of backfill has elapsed (Requirement 12.2).
2. **Service-fallback (immediate, account-wide for those services).** Groups
   `getCostAndUsage` by `SERVICE`, scoped to the services this stack uses —
   Lambda, API Gateway, DynamoDB, X-Ray, CloudWatch/Synthetics, S3, Amplify,
   and Data Transfer. This works **immediately** with no activation, but for a
   shared account it reflects account-wide spend on those services, not just
   this stack's (Requirement 12.6). The skill uses this when the tag is not yet
   active, and it is always a useful sanity check against the tag total.

### The `get-cost` skill

The skill lives at `.kiro/skills/get-cost/SKILL.md` and uses the
**`awslabs.billing-cost-management` MCP server's `cost_explorer` tool** with the
`getCostAndUsage` operation — **not** the AWS CLI, and **not** the CloudWatch
Application Signals MCP server. Its behavior:

- **Time window** — defaults to **month-to-date** (Requirement 12.3); also
  accepts **last 7 days** and an **explicit start/end range** (Requirement
  12.4). It chooses `DAILY` granularity for these short windows.
- **Metric** — `UnblendedCost`.
- **Record types** — excludes `Credit` and `Refund` by default via the
  `getCostAndUsage` filter (Requirement 12.5).
- **Grouping / filter** — tag-primary mode filters on the `Project` tag;
  service-fallback mode groups by `SERVICE` over the stack's services
  (Requirement 12.2, 12.6).
- **Optional forecast** — when a month-end projection is requested, it also
  calls `getCostForecast` for the remainder of the current month (Requirement
  12.7).
- **Read-only** — it only *reads* cost data; it provisions nothing and its only
  cost is the negligible per-request Cost Explorer API charge (Requirement
  12.8).

### Manual prerequisite: activating the Project cost-allocation tag

Tag-based cost attribution is **not automatic**. The operator must, **once**, in
the **management (payer) account only**:

1. Open the AWS Billing console → **Cost allocation tags**.
2. Activate the **`Project`** user-defined tag.
3. Wait **~24 hours** for AWS to backfill the tag onto cost data before the
   tag-filtered numbers become meaningful.

Until then, the skill's tag-primary mode returns little or nothing, and the
skill falls back to the service-scoped view (Requirement 12.6). The skill's
documentation states this prerequisite and the lag explicitly, and notes the
tag-vs-service views are complementary.

### Testing and the offline guarantee

Only the **CDK tagging** is covered by the offline suite: an
`aws_cdk.assertions.Template` assertion that a representative taggable resource
(e.g. the DynamoDB table and/or a Lambda) carries `Tags` including
`Project = xray-sudoku-demo` in the synthesized template. This resolves nothing
from an account and needs no credentials, exactly like the existing infra
assertions. Note that resources render `Tags` differently in CloudFormation
(a list of `{Key, Value}` vs. a map), so the assertion accommodates the shape of
the resource it checks.

The **skill itself is not part of `just test`**: it requires the Billing &
Cost Management MCP server and a real account with billing data, so — like the
end-to-end trace verification (task 13) and the live canary (task 14.4) — it is
exercised against an account, not in the offline pytest suite.

## Player Statistics Reporting

This slice makes the demo's usage reportable. Like the cost reporting above it
adds **no runtime behavior and no billable infrastructure**: it adds a
read-only script that scans the Games_Table and a `get-players` skill that
drives it. Where tracing shows *where time goes* and cost reporting shows *what
the stack costs*, this shows *who is playing and how much* — distinct players,
total games, games by status, and an estimated synthetic/organic split
(Requirement 13).

### Pure aggregation split from AWS I/O

The script `scripts/player_stats.py` is deliberately split into two concerns so
the whole of its logic that matters is offline-testable (Single Responsibility,
dependency inversion — engineering-practices §1):

- **Pure aggregation core.** `compute_player_stats(items)` takes
  already-unmarshalled game items (plain dicts with `gameId`, `playerId`,
  `status`, `createdAt`) and returns a frozen `PlayerStats` dataclass with
  `total_games`, `distinct_players`, `games_by_status`, and the
  synthetic/organic estimate. It touches no AWS and no wall clock, so every
  count is a deterministic function of its input. Iteration order is defined
  throughout — items are sorted by `createdAt` then `gameId`, and the status
  tally is emitted in sorted key order — so nothing reaching the output relies
  on set or incidental dict ordering (Requirement 13.6; engineering-practices
  §2).
- **AWS I/O layer.** `scan_and_compute(client, table_name)` runs the paginated,
  projected scan and hands the unmarshalled items to the pure core;
  `resolve_table_name(...)` discovers the table. Both take the boto3 client (or
  client factories) as parameters, so a test injects a fake and no real client
  is ever constructed at import time — `boto3` is imported lazily inside the CLI
  path only (Requirement 13.5).

### Table-name resolution

`resolve_table_name` mirrors how `verify_trace.py` resolves the API URL, with a
defined precedence (Requirement 13.7): an explicit `--table-name` argument, else
the `GAMES_TABLE` environment variable, else discovery — the `XraySudokuDemoStack`
CloudFormation `GamesTableName` output first, then a fallback of paging
`list_tables` and matching the `XraySudokuDemoStack-GamesTable` prefix. The table
name is never hardcoded; the two discovery clients arrive as factories so nothing
is constructed unless discovery is actually reached.

### The projected, paginated scan

`scan_and_compute` reads only the four attributes it needs via a
`ProjectionExpression`, keeping the scan cheap regardless of how large the game
items are (they also hold board/puzzle/solution data the report never touches).
`status` is a DynamoDB reserved word, so it is projected through the `#s`
`ExpressionAttributeNames` alias. The scan follows `LastEvaluatedKey` to the end
so every page is aggregated (Requirement 13.8). Only the four projected keys
reach the pure core, so nothing sensitive is carried along even if an item has
more attributes (engineering-practices §6).

### The synthetic estimate heuristic — and its honesty caveat

The Synthetics canary drives the UI every 5 minutes with plain-UUID `playerId`s
that are indistinguishable from real players' ids, so synthetic traffic **cannot
be cleanly excluded by id** — it can only be **estimated** from the ~5-minute
`createdAt` cadence. `estimate_synthetic_games(items, cadence_minutes=5,
tolerance_seconds=90)` sorts games by `createdAt` and counts those that fall on a
regular ~cadence-spaced series: walking the games in time order, whenever the gap
from the previous kept game is within tolerance of the cadence, both endpoints
are treated as part of a synthetic run. Games with an unparseable timestamp and
bursts spaced far from the cadence are left out (counted organic), and the result
is bounded to `[0, total]` with `organic = total − synthetic`. This is a
**documented heuristic, not a fact**: the dataclass, the prose report, and the
`--json` output all label the split as an estimate, and the `get-players` skill
repeats the caveat (Requirement 13.4).

### Boundary error handling and structured logging

The CLI catches expected failures at the boundary (engineering-practices §5): a
`RuntimeError` from resolution/validation, an `OSError` from
network/credential/transport, and a true top-level `except` for anything else
(e.g. a botocore `ClientError`). Each prints one JSON error line and exits
non-zero — never a raw traceback. Operational events go through a stdlib logger
configured once to emit single-line JSON to **stderr** (engineering-practices
§6), so stdout carries only the report (prose, or `--json`); it never `print()`s
logs and never logs board/solution data.

### The `get-players` skill and the command surface

The report is produced through the `just player-stats` recipe (Requirement
13.8), which runs `uv run python scripts/player_stats.py` and passes through
flags such as `--json` and `--table-name`. The skill at
`.kiro/skills/get-players/SKILL.md` documents the workflow and — like `get-cost`
— states the honesty caveats plainly: a "player" is a browser-generated
`playerId` with no auth (the same person on two devices counts twice; clearing
`localStorage` starts fresh), the canary split is only an estimate, and the scan
has no time window by default (all-time, full-table).

### Testing and the offline guarantee

The pure core and the plumbing are fully covered by the offline suite in
`backend/tests/test_player_stats.py`, with **no AWS and no network**: example
tests for the aggregation (totals, distinct players ignoring duplicate ids,
status tally, empty input) and the cadence heuristic (a 5-minute series counted
synthetic, irregular spacing counted organic, a mix split correctly, and the
tolerance boundary); table-name resolution precedence and the paginated scan
exercised against hand-rolled fake clients that record the `scan` kwargs (so the
`ProjectionExpression` and the `#s` reserved-word alias are asserted); and a
Hypothesis property test (≥100 examples) for the invariants that
`distinct_players ≤ total_games` and that the status tally and the
synthetic/organic split each sum to `total_games`. No `moto` dependency is added
— fakes suffice, mirroring `test_repository.py`.

The **script itself is not part of `just test`**: its scan needs AWS credentials
and the network, so — like the end-to-end trace verification (task 13), the live
canary (task 14.4), and the `get-cost` skill (task 15) — it is run against an
account via `just player-stats`, not in the offline pytest suite.
