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

Property tests run a minimum of 100 iterations and each references its design
property below. End-to-end trace verification (Requirements 1.2/1.4) is an
integration concern performed against the deployed stack (a handful of example
traces via `aws xray`), explicitly outside the offline suite.

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
