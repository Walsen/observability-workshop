# Design Document

## Overview

This design adds player-selectable difficulty to the X-Ray Sudoku Demo. "Difficulty" means the number of cells the Solver removes when generating a puzzle: a harder level removes more cells (fewer givens), an easier level removes fewer (more givens). The chosen level flows from the no-build Frontend, through `POST /games`, into puzzle generation as a per-level `Removal_Count` resolved from a name-keyed registry, is persisted on the one `(gameId, playerId)` item, and is reflected back on both the newGame response and the getGame read.

The design preserves every invariant the current system guarantees: a generated puzzle still has exactly one solution; the same seed and the same difficulty still reproduce the same puzzle; the Solver still records exactly the four X-Ray subsegments (`validate`, `generate`, `solve-backtracking`, `check-solution`) with the Tracer opening them; DynamoDB access stays scoped to the one `(gameId, playerId)` item under unchanged least-privilege IAM; and every response, including every error, carries the CORS headers authorizing the Amplify origin. The feature is backward compatible with no data migration: a legacy item with no `difficulty` attribute reads back as `medium`, and a newGame request that omits `difficulty` generates and stores a `medium` puzzle.

### Deliverable Model

The deliverable of this feature has two parts:

1. **An implementation workflow recipe** — a `*.workflow.json` file under `.kiro/workflows/` (the `Difficulty_Workflow_Recipe`) that, when launched against this approved design, drives a coder → reviewer loop until the implementation converges. The fine-grained decomposition (which files change, which functions are added) lives in that recipe's step prompts, **not** in `tasks.md`.
2. **The resulting code change** that the workflow produces — the backend, frontend, and test edits described below.

This design document is the authoritative target for both parts. It describes **what** the code change must be — the architecture, components, data model, API contract, error handling, tracing, determinism, and tests — so the workflow has an unambiguous specification to implement against. **No finished implementation source is written in this phase**; the code is produced later by the workflow the spec's tasks launch, and gated by the existing `deploy-and-verify-release.workflow.json`.

The "Deliverable: Implementation Workflow Recipe" section near the end specifies the recipe to be authored (its structure and agent steps), while leaving the final JSON itself to a task.

## Architecture

The change touches four seams, in dependency order, leaving every other seam untouched:

```
Frontend (static)          app.js / index.html / styles.css
  difficulty <select> ──┐  medium preselected; value in newGame body; display returned difficulty
                        │
POST /games body  ──────┘  { playerId, difficulty? }
  new_game.handle()        validate difficulty at the boundary against the registry
      │                    absent -> 'medium'; invalid/non-string -> 400 naming `difficulty`
      │                    query/path difficulty treated as absent
      ▼
  Difficulty_Registry      name -> generation strategy (its Removal_Count)   [Open/Closed, Strategy]
      │  resolve by name (no if/elif)
      ▼
  Solver.generate(rng, removal_count)   one `generate` subsegment around the whole generation
      │  carve to removal_count, uniqueness-checked, seeded by rng only
      ▼
  Game(..., difficulty)    dataclass gains a `difficulty` attribute
      │
      ▼
  game_to_item / game_from_item   store difficulty; default a missing stored difficulty to 'medium'
      │
      ▼
  GamesRepository.save/load        unchanged: one (gameId, playerId) item
      │
      ▼
  newGame 201 { gameId, board, status, difficulty }
  getGame 200 { gameId, board, status, difficulty }   (difficulty defaults to medium if absent)
```

The registry is the one new structural element. Everything else is an additive change to an existing component: the Solver gains a parameter, the handler gains a validated field, the Game dataclass gains an attribute, and the (de)serialization gains a defaulted round-trip.

### Design principles applied

- **Single Responsibility** — difficulty validation stays at the handler boundary; the registry owns the name → strategy mapping; the Solver still only computes; the (de)serialization still only translates; the Tracer still opens subsegments. No component takes on a second job.
- **Open/Closed** — levels live in a name-keyed registry. Adding `expert` later is one new entry; the generation algorithm does not change.
- **Liskov Substitution** — every difficulty strategy is usable wherever a strategy is expected; strategies differ only in the `Removal_Count` they carry, never in the shape of the board they produce (always 9x9).
- **Interface Segregation** — the Solver is handed a board and an rng and a removal count, not the event, the config, or the registry. The registry lookup happens at the boundary, and only the resolved count crosses into the Solver.
- **Dependency Inversion** — unchanged: the Solver still depends on the injected `Tracer`; the handler still depends on the injected `Dependencies` bundle (tracer, solver, repository, clock, id source, rng). The registry is a pure data structure with no AWS dependency, so it is trivially exercised offline.

## Components and Interfaces

### 1. Difficulty registry (new) — `backend/domain/difficulty.py`

A new pure module introduces the `Difficulty_Registry`: a name-keyed, insertion-ordered mapping from a difficulty name to its generation strategy. Each strategy carries the one thing generation needs — a `Removal_Count` — behind a narrow interface, so the Solver receives a resolved count and never the registry (Interface Segregation).

Shape (described, not implemented):

- A frozen `DifficultyStrategy` value with a `removal_count: int` field (and the level name for logging/clarity). This is the Strategy object; a level differs from another only by its `removal_count`.
- `DIFFICULTY_REGISTRY`: an ordered, name-keyed mapping `{name -> DifficultyStrategy}` for `easy`, `medium`, `hard`. Insertion order is `easy`, `medium`, `hard` so any iteration over it has a defined order.
- `DEFAULT_DIFFICULTY = "medium"` (the `Default_Difficulty_Level`).
- `VALID_DIFFICULTY_LEVELS`: the ordered tuple of registered names, derived from the registry keys, used by the handler to validate input and to populate the 400 body (`Valid_Difficulty_Level_Set`).
- `resolve(name) -> DifficultyStrategy`: a by-name lookup. An unknown name raises a module-private `UnknownDifficultyError` the handler maps to a 400; there is no `if/elif` chain anywhere.

Adding a level is a single new registry entry (Requirement 4.3); because `VALID_DIFFICULTY_LEVELS` and the Frontend's accepted values both derive from the registry keys, no other backend code changes.

**Candidate removal counts (a design decision to confirm):**

| Level  | Removal_Count | Givens (81 − removals) | Rationale |
|--------|---------------|------------------------|-----------|
| easy   | 30            | ~51                    | `< 40`: more givens, easier. |
| medium | **40**        | ~41                    | **Equals the current `_TARGET_REMOVALS`, so `medium` is behavior-preserving.** |
| hard   | 50            | ~31                    | `> 40`: fewer givens, harder. |

The ordering invariant that must hold is `easy < medium < hard` (Requirements 2.2, 2.3), and `medium = 40` is fixed so existing medium behavior is unchanged (Requirement 7, backward compatibility of the default). The exact `easy = 30` and `hard = 50` are reasonable defaults to confirm during review; any pair satisfying `easy < 40 < hard` with uniqueness still achievable is acceptable. Carving already restores any removal that breaks uniqueness, so a higher target never compromises the single-solution guarantee — it only bounds how many cells generation attempts to blank.

### 2. Solver — `backend/domain/solver.py`

Today `Solver.generate(self, rng)` carves a fixed `_TARGET_REMOVALS = 40` via `_carve_unique_puzzle`. The change:

- `generate(self, rng, removal_count)` takes an explicit per-level `removal_count` (an `int`) instead of relying on the module constant. The caller (the handler) resolves the count from the registry and passes it in.
- `_carve_unique_puzzle(solution, rng, removal_count)` uses the passed count as its carving target instead of `_TARGET_REMOVALS`. Every candidate removal is still uniqueness-checked (`_count_solutions(..., limit=2) == 1`), so the result keeps exactly one solution regardless of the target (Requirements 2.4, 2.5).
- The module constant `_TARGET_REMOVALS` is removed (its role moves to the registry) or retained only as the `medium` value's documented origin. The registry is the single source of truth for removal counts.
- **Tracing is unchanged.** `generate` opens the `generate` subsegment exactly once around the whole operation, regardless of `removal_count` (Requirement 8.3). The four subsegment names (`validate`, `generate`, `solve-backtracking`, `check-solution`) and the fact that the **Tracer**, not the Solver's computation, opens them (Requirement 8.1, 8.2) do not change.
- **Determinism is unchanged.** All randomness still comes from the injected `rng`; the fill and carve still iterate cells in the explicit row-major / `rng`-shuffled order already in place (Requirements 5.1, 5.2, 5.3). The same seed and the same `removal_count` reproduce the same puzzle. The Solver does not import the registry — it only receives an `int`, keeping the determinism seam exactly as narrow as it is today.

`generate` must remain callable such that two independent `Solver` instances given an equally seeded `rng` and the same `removal_count` produce identical `puzzle` and `solution` boards.

### 3. newGame handler — `backend/handlers/new_game.py`

`POST /games` currently validates `playerId` from the JSON body via `_ValidationError(field, message)` → `bad_request(field, msg)`, builds a `Game(...)`, calls `deps.repository.save(game)`, and returns `201 {gameId, board, status}`. The change adds difficulty parsing and threading:

- **Parse an optional `difficulty` from the JSON body.** Extend `_parse_player_id` (or add a sibling `_parse_difficulty`) so body parsing yields `(player_id, difficulty_name)`.
  - **Absent** `difficulty` → default to `DEFAULT_DIFFICULTY` (`'medium'`) with **no 400** (Requirements 7.1, 6.5 default arm).
  - **Present but not a string**, or **present string not in `VALID_DIFFICULTY_LEVELS`** → raise `_ValidationError(field="difficulty", message=...)`, which the boundary maps to `bad_request("difficulty", msg)` → **400** naming field `difficulty` (Requirements 6.1, 6.2). Never a 500, never a raw stack trace (Requirement 6.4).
  - **Difficulty supplied as a query-string or path parameter** rather than in the JSON body → treated as **absent**; only `event["body"]` is read for `difficulty`, so query/path difficulty applies the default `medium` (Requirement 6.5).
- **The 400 body lists the valid set.** Because an unknown/invalid-difficulty 400 must tell the player the accepted values (Requirement 6.3), the error path includes `VALID_DIFFICULTY_LEVELS` in the body. This needs a small addition to the `responses` helper (see component 6) so a `bad_request` for difficulty can carry the valid set.
- **Validation happens against the registry at the boundary.** The handler validates the name is in `VALID_DIFFICULTY_LEVELS` before touching the domain, then resolves the strategy via `resolve(name)` and passes its `removal_count` to `deps.solver.generate(deps.new_rng(), strategy.removal_count)`.
- **Difficulty is persisted and echoed.** The `Game(...)` is built with the chosen `difficulty` (component 4); the handler stores it (Requirement 3.3) and includes it in the `201` response: `{gameId, board, status, difficulty}` (Requirement 3.4).
- **CORS on every response.** All responses — including the 400 for a bad difficulty — go through the `responses` helpers that attach the Amplify-scoped CORS headers (Requirement 6.6).
- **Logging.** The structured log line for a created game gains the chosen `difficulty` in `extra`; a rejected difficulty logs at `WARNING` with the field, as `playerId` rejections already do. The `gameId`, `playerId`, endpoint, and trace id context is unchanged.

### 4. getGame handler — `backend/handlers/get_game.py`

`GET /games/{gameId}` loads the one `(gameId, playerId)` item and returns `{gameId, board, status}`. The change:

- Include the stored `difficulty` in the response: `{gameId, board, status, difficulty}` (Requirement 3.5).
- The `difficulty` is read off the loaded `Game` (component 4), where a stored item that predates the feature deserializes with `difficulty` defaulted to `'medium'` (Requirement 7.3). The handler therefore always has a concrete difficulty to report, with no special-casing of its own.
- Isolation and error mapping are unchanged: still exactly one `(gameId, playerId)` point-access, still `404` on a miss, still CORS on every response.

### 5. Game model and (de)serialization — `backend/data/game.py`

The `Game` dataclass gains a `difficulty` attribute, and the stored-item round-trip gains a backward-compatible default:

- **`Game` dataclass**: add `difficulty: Difficulty` (a `Literal["easy", "medium", "hard"]`, mirroring how `status: Status` is modeled). It is a required constructor field for new games; both handlers always supply it (the newGame handler from the validated request, defaulted to medium; getGame reconstructs it from the item).
- **`game_to_item(game)`**: add `"difficulty": game.difficulty` to the item dict. Difficulty is a short enum string, stored as a plain attribute (not JSON-encoded like the boards).
- **`game_from_item(item)`**: read `item.get("difficulty")` and **default a missing value to `DEFAULT_DIFFICULTY` (`'medium'`)** before validating it through a small `_validate_difficulty` (mirroring `_validate_status`). This is the whole of the backward-compatibility story (Requirements 7.3, 7.4): a legacy item with no `difficulty` key reads back as `medium` with no migration. A present-but-invalid stored value raises `ValueError`, consistent with how `_validate_status` guards status.
- Keep the module pure (no boto3, no AWS). The default-on-read lives here, in the deserialization seam, not in the handler.

To avoid a circular import, `DEFAULT_DIFFICULTY` / the `Difficulty` literal can live in the new `backend/domain/difficulty.py` and be imported by `game.py`, or the literal can be defined in `game.py` and the registry import the type — the design requires only that the default and the valid names derive from a single source so adding a level stays a one-entry change.

### 6. Responses helper — `backend/handlers/responses.py`

`bad_request(field, message)` returns `{error, field}`. To satisfy Requirement 6.3 (a difficulty 400 lists the valid set), add an optional way to attach the accepted values to the body — e.g. an optional `valid_values` parameter on `bad_request`, or a thin `bad_difficulty(valid_values)` wrapper — producing a body like `{error, field: "difficulty", validDifficulties: [...]}`. CORS headers are already attached to every response by `_json_response`; no change to the CORS behavior is needed.

### 7. Frontend (no-build static HTML/CSS/JS)

The frontend has no toolchain; it is plain `index.html`, `app.js`, `styles.css` served as-is. The change is additive and framework-free:

- **`index.html`**: add a difficulty `<select>` to the existing `.controls` group, labelled for assistive tech, with three `<option>`s — `easy`, `medium`, `hard` — and `medium` as the preselected option (`selected`) (Requirements 1.1, 1.2). It sits alongside the existing New Game / Solve buttons.
- **`app.js`**:
  - Read the selected difficulty from the `<select>` when starting a new game and include it in the `newGame()` POST body alongside `playerId`: `{ playerId, difficulty }` (Requirements 1.3, 1.4). Because the control is a fixed three-option select, the value sent is always from the valid set.
  - On the newGame (and resumed getGame) response, **display the returned `difficulty`** to the player — e.g. in the game-id footer line or a small status element — so the current game's difficulty is visible (Requirement 1.5). The display reads the server-returned value, so it reflects what was actually stored.
  - No change to trace origination, the `X-Amzn-Trace-Id` header, or the other three API calls.
- **`styles.css`**: minimal styling for the `<select>` consistent with the existing controls; no new dependencies.

No bundler, transpiler, or build step is introduced; `amplify.yml` is unchanged.

### 8. Infrastructure (CDK) — unchanged

No IAM, table, or API change. Each Lambda keeps exactly the DynamoDB actions it already holds on the Games table plus X-Ray write; difficulty adds no broader table or account access (Requirement 9.2). Access stays scoped to the one `(gameId, playerId)` item (Requirement 9.1). The CDK synth assertions over the synthesized template are unaffected and must stay offline (Requirement 11.5).

## Data Models

### Game item (DynamoDB), additive

The Games table item gains one attribute. Existing attributes are unchanged.

| Attribute   | Type   | Notes |
|-------------|--------|-------|
| `gameId`    | S      | Partition key. Unchanged. |
| `playerId`  | S      | Sort key. Unchanged. |
| `board`     | S      | JSON string of the 9x9 board. Unchanged. |
| `puzzle`    | S      | JSON string of the givens. Unchanged. |
| `solution`  | S      | JSON string of the unique solution. Unchanged. |
| `status`    | S      | `in_progress` \| `solved`. Unchanged. |
| `createdAt` | S      | ISO-8601. Unchanged. |
| `updatedAt` | S      | ISO-8601. Unchanged. |
| `difficulty`| S      | **New.** `easy` \| `medium` \| `hard`. **Missing on legacy items ⇒ read as `medium`.** |

No migration: `game_from_item` defaults an absent `difficulty` to `medium`, so items written before this feature read back correctly.

### API contract

**`POST /games`** request body:

```json
{ "playerId": "<string>", "difficulty": "easy" | "medium" | "hard" (optional) }
```

- `difficulty` absent → `medium`.
- `difficulty` present but not a string, or not one of the valid names → `400` with body `{ "error": "...", "field": "difficulty", "validDifficulties": ["easy","medium","hard"] }`.
- `difficulty` only in the query string / path → ignored, treated as absent (`medium`).

**`POST /games`** success response (`201`):

```json
{ "gameId": "<string>", "board": [[...9x9...]], "status": "in_progress", "difficulty": "medium" }
```

**`GET /games/{gameId}?playerId=<id>`** success response (`200`):

```json
{ "gameId": "<string>", "board": [[...9x9...]], "status": "in_progress", "difficulty": "medium" }
```

Every response, including the `400` and the `404`, carries the Amplify-scoped CORS headers.

## Error Handling

Difficulty failures are caught at the one boundary that already owns request validation — the newGame handler — and mapped to the documented response, following the existing `_ValidationError` → `bad_request` pattern:

| Condition | Response | Body | Notes |
|-----------|----------|------|-------|
| `difficulty` absent | — | — | Default to `medium`; no error (Req 7.1). |
| `difficulty` present, not a string | `400` | `{error, field: "difficulty", validDifficulties}` | Req 6.2, 6.3. |
| `difficulty` present, unknown name | `400` | `{error, field: "difficulty", validDifficulties}` | Req 6.1, 6.3. |
| `difficulty` only in query/path | — | — | Treated as absent → `medium` (Req 6.5). |
| any difficulty failure | never `500`, never a stack trace | | Req 6.4. |
| any response (200/201/400/404) | — | CORS headers present | Req 6.6. |

- Catch the specific expected failure (`_ValidationError`, `UnknownDifficultyError`) at the boundary — never a bare `except`. The existing broad `ClientError` handling for DynamoDB and the generic top-level 500 are unchanged.
- A present-but-invalid **stored** difficulty (should not occur in practice) raises `ValueError` from `_validate_difficulty` in deserialization, consistent with the existing status guard; the getGame boundary's existing error mapping covers it without leaking a stack trace.
- **Logging**: a rejected difficulty logs at `WARNING` naming the field; a created game logs at `INFO` with the chosen `difficulty`; context (`gameId`, `playerId`, endpoint, trace id) is unchanged. Never log anything sensitive.

## Tracing

Unchanged in every observable way. The Solver still records exactly `validate`, `generate`, `solve-backtracking`, and `check-solution` for any difficulty (Requirement 8.1). The **Tracer**, not the Solver's computation, opens and closes them (Requirement 8.2). `generate` opens **once** around the whole generation regardless of `removal_count` (Requirement 8.3) — the carving loop runs inside that single subsegment, so a larger removal count does not add or multiply subsegments. The end-to-end API Gateway → Lambda → DynamoDB trace, originated by the browser's `X-Amzn-Trace-Id`, is unaffected.

## Determinism

Generation remains driven solely by the injected, caller-seeded `rng` (Requirement 5.2). The Solver receives only an `int` `removal_count` in addition to the `rng`; it does not read the clock, module-level `random`, or any ambient state. Cells, rows, columns, and boxes are iterated in the explicit, defined order already present (`range`-based row-major, `rng.shuffle` of an explicitly built list) with no set iteration or incidental dict ordering (Requirement 5.3). Consequently the same seed and the same difficulty reproduce the same puzzle and solution (Requirement 5.1). A hypothesis property test for seeded reproducibility, parameterized by difficulty, is required and accompanies the feature (Requirement 5.4).

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

The following properties were derived from the acceptance-criteria prework. Redundant criteria were consolidated: the removal-count ordering (2.2, 2.3) into one monotonicity property; the uniqueness-preserving carve (2.5) into the unique-solution property (2.4); the invalid-difficulty error criteria (6.1–6.4) into one error-handling property; the determinism criteria (5.1–5.3) into one reproducibility property; and the subsegment criteria (8.1–8.3) into one tracing property.

### Property 1: Per-level removal count

*For any* seed and *any* registered difficulty level, the puzzle produced by generation has exactly `Removal_Count(level)` blanked cells (and therefore `81 − Removal_Count(level)` givens), where `Removal_Count` is the value the registry holds for that level.

**Validates: Requirements 2.1, 3.1, 3.2**

### Property 2: Removal counts are strictly ordered by difficulty

*For all* registered levels, the registry's removal counts satisfy `Removal_Count(easy) < Removal_Count(medium) < Removal_Count(hard)`, and `Removal_Count(medium)` equals the pre-feature fixed value (40).

**Validates: Requirements 2.2, 2.3**

### Property 3: Generated puzzle has a unique solution per level

*For any* seed and *any* registered difficulty level, the generated puzzle admits exactly one completion, and that completion equals the solution the generator reports.

**Validates: Requirements 2.4, 2.5**

### Property 4: Every level yields a standard 9x9 board

*For any* seed and *any* registered difficulty level, the generated puzzle and its solution are each 9x9 boards and the solution is a complete, valid Sudoku solution — levels differ only in their removal count, never in board shape.

**Validates: Requirements 4.4**

### Property 5: Difficulty is persisted and echoed through newGame

*For any* valid difficulty supplied in the newGame request body, the stored game item carries that difficulty and the `201` response body reports that same difficulty.

**Validates: Requirements 3.3, 3.4**

### Property 6: getGame reflects the stored difficulty

*For any* stored game, the getGame response reports the difficulty stored on the item; and for any stored item lacking a difficulty attribute, the reported difficulty is `medium`.

**Validates: Requirements 3.5, 7.3, 7.4**

### Property 7: Same seed and difficulty reproduce the same puzzle

*For any* seed and *any* registered difficulty level, two independent generations with that same seed and level produce identical puzzles and identical solutions.

**Validates: Requirements 5.1, 5.2, 5.3**

### Property 8: Invalid difficulty is rejected at the boundary

*For any* difficulty value that is present but is not a string, or is a string outside the valid set, the newGame handler returns a `400` whose body names the field `difficulty` and lists the valid difficulty names, and never returns a `500` or a raw stack trace.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

### Property 9: Difficulty supplied outside the body is treated as absent

*For any* request that carries a difficulty only as a query-string or path parameter (and not in the JSON body), the handler generates and stores a `medium` puzzle.

**Validates: Requirements 6.5**

### Property 10: Every response carries CORS headers

*For any* response the newGame or getGame handler produces — success, `400`, or `404` — the response includes the CORS headers authorizing the Amplify origin.

**Validates: Requirements 6.6**

### Property 11: The four solver subsegments are recorded once per generation

*For any* registered difficulty level, generating a puzzle records exactly the subsegment names `validate` where applicable and `generate` for generation, with `generate` opened exactly once regardless of the removal count, and the Solver's own computation opens no subsegment.

**Validates: Requirements 8.1, 8.2, 8.3**

## Testing Strategy

Everything runs offline through `just test` and `just synth`, with no AWS credentials and no network beyond localhost (Requirement 11). CDK synth must resolve from no account. A failure under those conditions is a hard failure with no fallback.

### Example-based tests (`pytest`)

- **Per-level removal counts**: generate for each registered level and assert the removed-cell count matches the registry value; assert `medium == 40`.
- **Default on absent difficulty**: a newGame request with no `difficulty` generates and stores `medium`, and the `201` body reports `medium` (Requirements 7.1, 7.2).
- **Boundary 400s naming `difficulty` with CORS**: a non-string difficulty and an unknown-name difficulty each return `400` with `field == "difficulty"`, the valid set in the body, and CORS headers present; never `500` (Requirements 6.1–6.4, 6.6).
- **Query/path difficulty treated as absent**: a difficulty placed only in `queryStringParameters`/`pathParameters` yields a `medium` game (Requirement 6.5).
- **Backward-compat read**: deserialize a difficulty-less item and assert `difficulty == medium`; a getGame over such an item reports `medium` (Requirements 7.3, 7.4).
- **getGame reflects difficulty**: a stored game of each level is reported with that difficulty (Requirement 3.5).
- **Frontend presence (DOM/example)**: `index.html` has a difficulty `<select>` with the three values and `medium` preselected; `app.js` includes difficulty in the newGame body and displays the returned difficulty (Requirements 1.1–1.5). Covered with the project's existing frontend testing approach (DOM assertions / jsdom-style checks consistent with the current `app.js` test globals), introducing no toolchain.
- **Registry structure**: registry keys equal the valid set; each maps to a strategy with a positive removal count; a known name resolves and an unknown name is rejected (Requirements 4.1, 4.2).

### Property-based tests (`hypothesis`)

Each runs a minimum of 100 iterations and is tagged `Feature: difficulty-levels, Property {number}: {property_text}`. Properties over generation are parameterized by **both** a hypothesis-driven seed **and** each registered difficulty level, so they hold across the whole registry.

- **Property 1** — per-level removal count: for any seed and level, blanked-cell count equals the registry's count.
- **Property 2** — monotonic ordering of removal counts (a small, exhaustive check over the registry; stated as an invariant).
- **Property 3** — unique solution per level: for any seed and level, an independent completion count (capped at 2) is exactly 1 and equals the reported solution. Extends the existing `test_property_generated_puzzle_has_unique_solution` to be level-parameterized.
- **Property 4** — standard 9x9 shape per level.
- **Property 7** — seeded reproducibility per difficulty: for any seed and level, two independent `Solver` instances with equal seed and level produce identical puzzle and solution. Extends the existing `test_property_generation_is_reproducible_from_seed` to be level-parameterized (Requirement 5.4's required property).
- **Property 8** — invalid-difficulty error handling over generated invalid inputs (non-strings and strings outside the valid set) → `400` naming `difficulty`, valid set listed, never `500`.
- **Property 11** — for any level, a `Solver` wired to a `FakeTracer` records the expected subsegment names with `generate` entered exactly once.

Properties 5, 6, 9, 10 are naturally pinned by the example/handler tests above (they vary over a small, enumerable domain — the valid set plus the absent case); they are stated as properties for traceability and exercised by parameterizing the handler tests over the valid set rather than by separate hypothesis strategies.

### Infrastructure tests

The existing CDK synth assertions (API, IAM, Lambdas, tags, canary, synth) remain unchanged and must stay green offline. No new IAM grants, no table change (Requirements 9.1, 9.2, 11.5).

## Deliverable: Implementation Workflow Recipe

The feature's implementation is produced by a **Kiro Workflow recipe** — the `Difficulty_Workflow_Recipe` — authored as a `*.workflow.json` file under `.kiro/workflows/` (for example `.kiro/workflows/implement-difficulty-levels.workflow.json`). Authoring that JSON is itself a task; this section specifies the recipe's **shape and intent** so the task has an unambiguous target. The recipe is a declarative graph of agent steps (see `.kiro/workflows/README.md`), not a script.

### Intent

Implement this approved design against the repository through an **implement → review loop** that iterates until the reviewer's verdict converges, then integrate the result onto mainline. The recipe does not re-decide the design; it treats this `design.md` as the specification and produces the code change the design describes (the backend registry/solver/handler/model edits, the frontend `<select>` and wiring, and the pytest + hypothesis tests).

### Structure

- A top-level `sequence` containing a `repeat` loop (the implement/review loop) followed by nothing else the recipe must own beyond the loop's convergence — integration is handled at the end of the run (see "Worktree and integration").
- The `repeat` loop (e.g. `maxIterations: 3`, `onMaxIterations: "continue"`) body, in order so the reviewer's verdict gates the stop condition:
  1. **coder step** (`agent: wf-coder`) — implements against this design: reads `.kiro/specs/difficulty-levels/design.md` and `requirements.md`, and on the first iteration writes the change; on later iterations reads the reviewer's `NEEDS_CHANGES` verdict and applies the requested fixes. Follows the repo engineering practices (TDD first, SOLID, boundary error handling, structured logging, determinism) and runs the offline suite (`just test`, `just lint`, `just typecheck`) before finishing. Writes a coder record artifact under `.agents/workflow/`.
  2. **reviewer step** (`agent: semantic_reviewer`) — renders the **final verdict that gates the loop**: a behavioral review of the diff against this design, emitting a verdict artifact (JSON) whose status is `APPROVED` or `NEEDS_CHANGES`.
  - **Stop condition**: a `fileCheck` on the reviewer's verdict artifact (e.g. `jsonPath: "verdict"` equals `"APPROVED"`, or a boolean `passed: true`). The loop stops when the reviewer approves; otherwise the next iteration's coder step consumes the `NEEDS_CHANGES` feedback as a fix pass. On exhausting iterations the run continues (the unresolved verdict is surfaced).
- Steps pass results via `{{step_id.output}}` and named `artifacts` under `.agents/workflow/`, exactly as the existing release workflow does.

### Valid step agents

Workflow **step** agents are the **registered workflow agents only** — for example `wf-coder`, `wf-design`, `wf-design-reviewer`, `wf-planner`, `semantic_reviewer`, `wf-review-aggregator`. This recipe uses `wf-coder` (implement/fix) and `semantic_reviewer` (gating verdict).

The project chat agents under `.kiro/agents/` — `qa-engineer`, `fix-developer`, `release-coordinator` — are **not** valid workflow step agents and must **not** appear as steps in this recipe. (Those agents are the ones the downstream `deploy-and-verify-release.workflow.json` uses in its own definition; this implementation recipe is separate.)

### Worktree and integration

The workflow runs in a **git worktree** and **rebases onto mainline at the end** of the run, so the implement/review iterations happen in isolation and only a converged, reviewed result is integrated.

### Boundaries with the task layer

- **A workflow step cannot launch another workflow.** Launching this `Difficulty_Workflow_Recipe`, and later launching the release gate, stays in the **spec's task layer** (`tasks.md`), not inside this recipe as a step.
- The existing **`.kiro/workflows/deploy-and-verify-release.workflow.json`** is the **downstream release gate**: it runs the offline gate, an optional deploy, the QA/fix loop, live X-Ray trace verification, UI smoke, and the GO/NO-GO report. It is launched by a **later task** (per Requirement 10.3), **not embedded as a step** in this implementation recipe.
- Net task-layer shape (authored in `tasks.md`, out of scope for this design): (a) author the `Difficulty_Workflow_Recipe` JSON; (b) launch it to produce the implementation; (c) launch `deploy-and-verify-release.workflow.json` to gate the result.

The recipe should be validated with `validate_workflow` before it is committed.
