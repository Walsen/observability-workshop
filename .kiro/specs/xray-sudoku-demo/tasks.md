# Implementation Plan: X-Ray Sudoku Demo

## Overview

This plan builds the X-Ray Sudoku Demo bottom-up, following TDD throughout: each
implementation sub-task writes failing tests first (pytest for concrete cases,
hypothesis for the nine correctness properties in `design.md`), then implements
to green. The whole test suite — solver, handler, and repository unit tests plus
CDK synth and `aws_cdk.assertions.Template` assertions — is fully offline: no AWS
credentials and no network beyond localhost.

The build order is: reproducible dev environment → pure domain (board rules,
solver with four named subsegments, tracer abstraction) → data layer (DynamoDB
repository scoped to `(gameId, playerId)`) → Lambda handlers (with 400/404
mapping, CORS on every response, structured JSON logging) → CDK infrastructure →
no-build static frontend. End-to-end trace verification against the deployed
stack is a separate task, explicitly outside the offline suite.

All code examples and implementation use Python 3.12 (backend/infra) and plain
HTML/CSS/JS (frontend), per `design.md`.

## Tasks

- [ ] 1. Bootstrap the reproducible development environment
  - [x] 1.1 Create the devbox toolchain manifest and activation
    - Run `devbox init`, then add each tool at an exact version (resolve each with `devbox search`): `python`, `uv`, `just`, `jq`, `yq-go`, `aws-cdk-cli`, `awscli2`, `git`, `gh` — use exact-version placeholders where a version must be pinned, never `@latest`
    - Commit `devbox.lock` alongside `devbox.json` for reproducibility
    - Run `devbox generate direnv` to produce `.envrc` (`eval "$(devbox generate direnv --print-envrc)"`) and `direnv allow`
    - Add `.devbox/`, `.venv/`, and any generated credential tree to `.gitignore`
    - _Steering: dev-environment.md (toolchain, pinning, required files); Design: Toolchain and Dependencies_

  - [x] 1.2 Initialize the uv Python project with the correct dependency split
    - Run `uv init` for the backend and create `pyproject.toml` targeting Python 3.12
    - Add imported (runtime) dependencies: `aws-cdk-lib`, `constructs`, `boto3`, `aws-xray-sdk`
    - Add dev dependencies: `pytest`, `hypothesis`, `ruff`, `mypy`
    - Keep `aws-cdk-cli` and `awscli2` out of `pyproject.toml` (they are devbox-invoked, not imported); keep `aws-cdk-lib`/`boto3` out of `devbox.json`
    - Commit `uv.lock`
    - _Steering: dev-environment.md (Python: uv owns dependencies, import-vs-invoke); Design: uv dependency split_

  - [x] 1.3 Create the Justfile command surface
    - Define recipes: `just test` (pytest, offline), `just lint` (`ruff check`), `just typecheck` (`mypy`), `just synth` (`cdk synth`), `just deploy` (`cdk deploy`)
    - Ensure `just test` and `just synth` require no AWS credentials or network beyond localhost
    - _Steering: dev-environment.md (just is the command surface); Design: Justfile recipe surface_

  - [x] 1.4 Create the backend package skeleton
    - Create `backend/` with packages `handlers/`, `domain/`, `data/`, `infra/`, `tests/` and the `__init__.py` files
    - Confirm `pytest` discovers the (empty) `tests/` package and runs green via `just test`
    - _Design: Project / Folder Structure_

- [x] 2. Implement the domain board and placement rules
  - [x] 2.1 Implement the Board type and placement rules
    - In `domain/board.py`, define the 9×9 board representation (nested list of ints, 0 = empty), the `Move` type, `is_valid_placement(board, row, col, value)`, and `is_complete_and_valid(board)`
    - Use a defined iteration order over cells, rows, columns, and boxes (no set iteration, no incidental dict ordering)
    - _Requirements: 4.2, 6.1; Design: Domain: the Solver and Board_

  - [x]* 2.2 Write property test for placement validity
    - **Property 3: Move validity equals the absence of a placement conflict**
    - **Validates: Requirements 4.2, 4.3**
    - Also assert an invalid move leaves the board unchanged; min 100 iterations
    - **Feature: xray-sudoku-demo, Property 3**

  - [x]* 2.3 Write property test for solved detection
    - **Property 6: Solved detection matches full-board validity**
    - **Validates: Requirements 4.5**
    - Min 100 iterations over completely filled boards
    - **Feature: xray-sudoku-demo, Property 6**

  - [x]* 2.4 Write unit tests for board edge cases
    - Empty board, a board one move from complete, and a contradictory board (duplicate in a row/column/box)
    - _Requirements: 4.2, 4.5_

- [x] 3. Implement the tracer abstraction
  - [x] 3.1 Implement the Tracer protocol, XRayTracer adapter, and FakeTracer
    - In `domain/tracing.py`, define the `Tracer` protocol with `subsegment(name) -> ContextManager[None]`
    - `XRayTracer` adapts `aws_xray_sdk.core.xray_recorder`; `FakeTracer` records the ordered list of opened subsegment names for offline assertions
    - _Requirements: 6.1, 6.2, 6.3, 6.4; Design: Tracing abstraction_

  - [x]* 3.2 Write unit tests for FakeTracer
    - Assert opened subsegment names are recorded in order and the context manager enters/exits cleanly
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 4. Implement the Solver with the four named subsegments
  - [x] 4.1 Implement solve (backtracking) and check_solution
    - In `domain/solver.py`, implement `solve(board) -> Board | None` wrapped in the `solve-backtracking` subsegment and `check_solution(board) -> bool` wrapped in the `check-solution` subsegment, via the injected `Tracer`
    - Return `None` when the board has no solution; keep subsegment open/close out of the computation itself (Single Responsibility)
    - _Requirements: 5.2, 5.3, 5.4, 6.3, 6.4; Design: Domain: the Solver and Board_

  - [x]* 4.2 Write property test for the backtracking solver
    - **Property 4: The backtracking solver returns a valid, consistent completion**
    - **Validates: Requirements 5.2, 5.3**
    - Min 100 iterations; assert the completion agrees with all original given cells
    - **Feature: xray-sudoku-demo, Property 4**

  - [x]* 4.3 Write property test for unsolvable boards
    - **Property 5: An unsolvable board yields no solution**
    - **Validates: Requirements 5.4**
    - Min 100 iterations over boards with a direct placement contradiction
    - **Feature: xray-sudoku-demo, Property 5**

  - [x] 4.4 Implement generate and validate_move
    - Implement `generate(rng: random.Random) -> GeneratedPuzzle` wrapped in the `generate` subsegment, producing givens plus a unique solution using only the injected `rng` (never module-level `random`)
    - Implement `validate_move(board, move) -> bool` wrapped in the `validate` subsegment
    - _Requirements: 2.2, 4.2, 6.1, 6.2; Design: Domain: the Solver and Board_

  - [x]* 4.5 Write property test for unique-solution generation
    - **Property 1: Generated puzzles have a unique solution**
    - **Validates: Requirements 2.2**
    - Min 100 iterations over seeds
    - **Feature: xray-sudoku-demo, Property 1**

  - [x]* 4.6 Write property test for seeded reproducibility
    - **Property 2: Puzzle generation is reproducible from a seed**
    - **Validates: Requirements 2.2** (and the determinism rule in engineering-practices steering)
    - Generate twice from equal-seeded generators; assert identical givens and solutions; min 100 iterations
    - **Feature: xray-sudoku-demo, Property 2**

  - [x]* 4.7 Write unit test asserting the four subsegment names
    - Using `FakeTracer`, assert each solver operation opens its exact subsegment name: `validate`, `generate`, `solve-backtracking`, `check-solution`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 5. Checkpoint - domain layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement the DynamoDB repository (data layer)
  - [x] 6.1 Implement the Game model and JSON board serialization
    - Define the `Game` domain object (gameId, playerId, board, puzzle, solution, status, createdAt, updatedAt) and round-trippable JSON (de)serialization of boards as nested lists of ints
    - _Requirements: 2.4, 3.2; Design: Data Model: the Games Table (item attributes)_

  - [x] 6.2 Implement GamesRepository load/save scoped to (gameId, playerId)
    - In `data/games_repository.py`, implement `load(game_id, player_id) -> Game | None` (GetItem on `Key={"gameId","playerId"}`) and `save(game)` (PutItem of the full item, gameId=PK, playerId=SK); read table name from an environment variable
    - Every access addresses exactly the one `(gameId, playerId)` item — enforce isolation here
    - _Requirements: 2.4, 3.2, 7.3; Design: DynamoDB repository (adapter)_

  - [x]* 6.3 Write property test for repository round-trip
    - **Property 7: Game state round-trips through the repository**
    - **Validates: Requirements 2.4, 2.5, 3.2**
    - Use a local fake table (moto or hand-rolled), no real AWS; min 100 iterations; assert single item keyed PK=gameId, SK=playerId
    - **Feature: xray-sudoku-demo, Property 7**

  - [x]* 6.4 Write property test for player isolation
    - **Property 9: Player isolation across item access**
    - **Validates: Requirements 7.3**
    - For two distinct `(gameId, playerId)` pairs, writing one leaves the other unchanged and a load for one never returns the other; min 100 iterations
    - **Feature: xray-sudoku-demo, Property 9**

  - [x]* 6.5 Write unit tests for repository key construction
    - Assert correct Key/Item shapes and that a missing item returns `None`, against the local fake table
    - _Requirements: 3.2, 3.3_

- [x] 7. Implement shared handler concerns (responses, CORS, logging)
  - [x] 7.1 Implement proxy-response and CORS helpers
    - In `handlers/responses.py`, implement `ok`, `bad_request(field)`, `not_found(...)`, `server_error`, and a no-solution response; attach Amplify-origin-scoped CORS headers to every response (success and error)
    - _Requirements: 3.3, 4.3, 5.4, 8.2; Design: Error Handling, API Gateway CORS_

  - [x] 7.2 Implement the structured JSON logger
    - In `handlers/logging_config.py`, configure once at module load a logger emitting single-line JSON to stdout (never `print()`), with levels DEBUG/INFO/WARNING/ERROR/CRITICAL and context fields `gameId`, `playerId`, `endpoint`, `traceId`; use `logger.exception(...)` in except blocks; log nothing sensitive
    - _Steering: engineering-practices.md §6 (logging); Design: Logging_

  - [x]* 7.3 Write unit tests for responses and logger
    - Assert every response variant carries CORS headers (including 400/404) and that log output is single-line JSON with the required context fields
    - _Requirements: 8.2; Design: Error Handling, Logging_

- [x] 8. Implement the Lambda handlers
  - [x] 8.1 Patch boto3 for tracing at module load and wire dependencies
    - Call `patch_all()` once at Lambda module load so DynamoDB calls become subsegments; wire injected `Tracer`, `GamesRepository`, `rng`, clock, and id source (never `datetime.now()`/`uuid.uuid4()` in domain logic)
    - _Requirements: 1.3, 6.*; Design: Tracing abstraction, End-to-End Trace Propagation_

  - [x] 8.2 Implement the new_game handler
    - `POST /games`: read `playerId` from body (validate shape), `solver.generate(rng)`, assign `gameId` via injected id source, `repo.save(game)`, return `gameId` + initial board; INFO log on game created
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 7.3; Design: Lambda handlers_

  - [x] 8.3 Implement the get_game handler
    - `GET /games/{gameId}`: read `gameId` (path) and `playerId` (query/header), `repo.load`; return board or **404** if absent
    - _Requirements: 3.1, 3.2, 3.3, 7.3; Design: Lambda handlers_

  - [x] 8.4 Implement the submit_move handler
    - `POST /games/{gameId}/moves`: validate shape, `repo.load` (404 if absent), `solver.validate_move`; invalid → **400/invalid** response; valid → apply, `repo.save`; if `check_solution` → status `solved`; WARNING log on rejected move
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 7.3; Design: Lambda handlers_

  - [x] 8.5 Implement the solve handler
    - `POST /games/{gameId}/solve`: validate shape, `repo.load` (404 if absent), `solver.solve(board)`; return completed board or the documented no-solution response if `None`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 7.3; Design: Lambda handlers_

  - [x]* 8.6 Write property test for valid-move cell mutation
    - **Property 8: A valid move changes only its target cell**
    - **Validates: Requirements 4.4**
    - Using FakeTracer and in-memory fake repository; after apply+save the loaded board differs from the prior board in exactly the moved cell; min 100 iterations
    - **Feature: xray-sudoku-demo, Property 8**

  - [x]* 8.7 Write unit tests for handler error mapping and subsegments
    - 404 on missing item, 400 on malformed input (both with CORS headers), no-solution response for an unsolvable board, and the four subsegment names opened during a request; assert bad input never yields 500 or a raw stack trace; use FakeTracer + fake repo, pinned id source and clock
    - _Requirements: 3.3, 4.3, 5.4, 6.1, 6.2, 6.3, 6.4, 8.2_

- [x] 9. Checkpoint - data and handler layers
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement the CDK infrastructure
  - [x] 10.1 Implement the CDK app entry point and stack scaffold
    - `backend/app.py` instantiates the stack with an explicit environment pinned to **us-east-1**; `infra/sudoku_stack.py` defines the stack shell
    - _Requirements: 9.1, 9.4; Design: Infrastructure as Code (CDK)_

  - [x] 10.2 Define the DynamoDB table
    - Table with `gameId` partition key and `playerId` sort key, on-demand billing
    - _Requirements: 9.1; Design: Data Model: Key design_

  - [x] 10.3 Define the four Lambda functions with X-Ray tracing
    - Four functions (new_game, get_game, submit_move, solve) on the Python 3.12 runtime with tracing `Active`, each receiving the table name via environment variable
    - _Requirements: 9.1, 9.2; Design: Infrastructure as Code (CDK)_

  - [x] 10.4 Define the REST API with active tracing and Amplify-scoped CORS
    - `RestApi` with the four resources/methods (proxy integrations), stage `tracing_enabled=True`, and CORS locked to the Amplify origin advertising `GET`/`POST`/`OPTIONS` and the `X-Amzn-Trace-Id` and playerId-carrying headers for preflight
    - _Requirements: 1.2, 8.1, 8.2, 8.3, 9.1; Design: API Gateway (REST) Design_

  - [x] 10.5 Grant least-privilege IAM per Lambda and emit the API URL
    - Grant only the DynamoDB actions each Lambda performs on the table (get_game read; new_game and submit_move read+write; solve read, plus write only if it persists the solution) via the table grant helpers, plus `xray:PutTraceSegments`/`xray:PutTelemetryRecords`; no blanket table/account access; add a `CfnOutput` emitting the `API_Endpoint_URL`
    - _Requirements: 9.3, 9.5; Design: Infrastructure as Code (CDK)_

  - [x]* 10.6 Write offline CDK synth template assertions
    - Using `aws_cdk.assertions.Template` (no account access): four Lambdas at `python3.12` with tracing `Active`, the table present with `gameId`/`playerId` key schema, the API with active tracing and Amplify-scoped CORS, a `CfnOutput` for the URL, region us-east-1, and IAM policies scoped to the table plus `xray:PutTraceSegments`
    - _Requirements: 8.1, 8.2, 9.1, 9.2, 9.3, 9.4, 9.5; Design: Testing Strategy (CDK synth)_

- [x] 11. Checkpoint - backend synthesizes and the full offline suite passes
  - Ensure all tests pass (`just test`) and `just synth` resolves nothing from an account, ask the user if questions arise.

- [x] 12. Implement the no-build static frontend
  - [x] 12.1 Implement config.js and playerId generation
    - `frontend/config.js` reads the Amplify-provided `API_ENDPOINT_URL`; `frontend/app.js` generates a `playerId` once (UUID in `localStorage`) and includes it on every API request
    - _Requirements: 7.1, 7.2, 10.1, 10.4; Design: Multi-User Model, Deployment Handoff_

  - [x] 12.2 Implement the board UI and API calls with the trace header
    - `frontend/index.html` + `frontend/styles.css` render the board; `frontend/app.js` calls newGame/getGame/submitMove/solve via `fetch`, originating and sending `X-Amzn-Trace-Id` on every outbound request
    - _Requirements: 1.1, 7.2, 10.1; Design: End-to-End Trace Propagation_

  - [x] 12.3 Add the Amplify publish configuration
    - `frontend/amplify.yml` publishes the static files with no build step
    - _Requirements: 10.2, 10.3; Design: Frontend_

- [~] 13. End-to-end trace verification against the deployed stack (outside the offline suite)
  - This task requires AWS credentials and a live deployment; it is explicitly NOT part of the offline `just test` suite
  - Deploy handoff (two steps, manual): (1) `just deploy` runs `cdk deploy`, which emits `API_Endpoint_URL`; (2) the operator sets that URL as an Amplify configuration value that `config.js` reads — Amplify Hosting is connected manually in the console (`main` → production, `feature/*` → preview) and is NOT defined in CDK
    - Write a script/test that drives a request through the deployed API and inspects the resulting trace with `aws xray get-trace-summaries` / `aws xray batch-get-traces`, asserting a single trace spans Frontend → API → Lambda → DynamoDB with the four solver subsegments present
    - _Requirements: 1.2, 1.4, 10.4, 10.5, 10.6; Design: End-to-End Trace Propagation, Deployment Handoff_

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation sub-tasks are never optional.
- Each task references the specific requirements and design sections it implements for traceability.
- The offline suite (tasks 2–12 tests) runs with no AWS credentials and no network beyond localhost; CDK synth resolves nothing from an account.
- Property tests run a minimum of 100 iterations and each references its design property number.
- Task 13 (end-to-end trace verification) is deliberately outside the offline suite because it depends on a live deployment and the manual Amplify handoff.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "1.4"] },
    { "id": 3, "tasks": ["2.1", "3.1"] },
    { "id": 4, "tasks": ["2.2", "2.3", "2.4", "3.2", "4.1", "4.4"] },
    { "id": 5, "tasks": ["4.2", "4.3", "4.5", "4.6", "4.7", "6.1", "7.1", "7.2"] },
    { "id": 6, "tasks": ["6.2", "7.3", "8.1"] },
    { "id": 7, "tasks": ["6.3", "6.4", "6.5", "8.2", "8.3", "8.4", "8.5"] },
    { "id": 8, "tasks": ["8.6", "8.7", "10.1"] },
    { "id": 9, "tasks": ["10.2", "10.3"] },
    { "id": 10, "tasks": ["10.4"] },
    { "id": 11, "tasks": ["10.5"] },
    { "id": 12, "tasks": ["10.6", "12.1", "12.2", "12.3"] },
    { "id": 13, "tasks": ["13"] }
  ]
}
```
