# Requirements Document

## Introduction

This feature adds player-selectable difficulty to the X-Ray Sudoku Demo. Today the Solver always carves a fixed number of cells (`_TARGET_REMOVALS = 40`) when generating a puzzle, so every game has the same number of givens. This feature lets a Player choose a difficulty level — easy, medium, or hard — when starting a new game, and makes "difficulty" mean the number of givens the generated puzzle presents: a harder level removes more cells (fewer givens), an easier level removes fewer (more givens).

The chosen difficulty flows from the no-build static Frontend, through the newGame request, into puzzle generation (as a per-level removal count), is persisted on the one `(gameId, playerId)` item in the Games_Table, and is reflected back on both the newGame response and the getGame read. The levels are selected by name from an Open/Closed registry so that adding another level later (for example, `expert`) is a single new registry entry rather than an edit spread across the generator.

The feature preserves every invariant the current system already guarantees: a generated puzzle still has exactly one solution; the same seed and the same difficulty still reproduce the same puzzle; the Solver still records the four X-Ray subsegments (`validate`, `generate`, `solve-backtracking`, `check-solution`) with the Tracer — not the Solver — opening them; DynamoDB access stays scoped to the one `(gameId, playerId)` item under unchanged least-privilege IAM; and every response, including every error, carries the CORS headers that authorize the Amplify origin.

Difficulty is backward compatible with no data migration: an older stored item that has no difficulty attribute reads back as medium, and a newGame request that omits difficulty generates and stores a medium puzzle.

This document specifies the externally observable behavior of the feature. It does not prescribe implementation. The feature is delivered by a Kiro Workflow recipe launched by coarse tasks (see Requirement 10 and the Deliverable Model section), and the result is gated by the existing deploy-and-verify-release workflow.

## Glossary

- **X_Ray_Sudoku_Demo**: The complete demonstration application comprising the Frontend, API, Lambda functions, Solver, and Games_Table.
- **Difficulty_Level**: A named level of puzzle difficulty, one of `easy`, `medium`, or `hard`, whose meaning is the number of cells the Solver removes when generating a puzzle (equivalently, the number of givens the resulting puzzle presents).
- **Default_Difficulty_Level**: The Difficulty_Level `medium`, used wherever a difficulty is absent on create or on read.
- **Valid_Difficulty_Level_Set**: The set of Difficulty_Level names the X_Ray_Sudoku_Demo accepts, initially `{easy, medium, hard}`.
- **Removal_Count**: The number of cells the Solver removes from a complete solution when generating a puzzle, determined by the selected Difficulty_Level and replacing the previously fixed value of 40.
- **Difficulty_Registry**: A name-keyed collection that maps each Difficulty_Level name to the generation strategy (its Removal_Count) for that level, from which a level is selected by name rather than by a conditional chain.
- **Solver**: The server-side Sudoku component that generates puzzles and solves boards; it records four named X-Ray subsegments through the injected Tracer.
- **Tracer**: The injected component that opens and closes the Solver's X-Ray subsegments; the Solver's own computation opens no subsegments itself.
- **Solver_Subsegment_Names**: The four exact X-Ray subsegment names the workshop trace expects: `validate`, `generate`, `solve-backtracking`, and `check-solution`.
- **newGame_Lambda**: The Lambda function behind `POST /games` that validates the request body, generates a puzzle through the Solver, persists the Game, and returns the initial board.
- **getGame_Lambda**: The Lambda function behind `GET /games/{gameId}` that loads one `(gameId, playerId)` item and returns its current board and status.
- **Games_Table**: The Amazon DynamoDB table that stores game state, one item per game per Player, keyed by `gameId` and `playerId`.
- **Game_Item**: One stored item in the Games_Table for a single `(gameId, playerId)` pair.
- **Player**: A user of the game, identified by a client-generated Player_Id.
- **Player_Id**: A client-generated identifier that distinguishes one Player from another without managed authentication.
- **Game_Id**: A unique identifier for a single Sudoku game instance.
- **Frontend**: The no-build static HTML/CSS/JS Sudoku game hosted on AWS Amplify Hosting; it has no bundler or build step.
- **Amplify_Origin**: The single cross-origin value for the Frontend that the API authorizes through CORS headers; never the `*` wildcard.
- **Difficulty_Field**: The optional JSON body field named `difficulty` on the newGame request that carries the requested Difficulty_Level.
- **Seed**: The value used to seed the random generator the Solver uses, such that the same Seed drives reproducible generation.
- **Deploy_And_Verify_Workflow**: The existing Kiro Workflow recipe `deploy-and-verify-release.workflow.json` that gates a release through an offline gate, optional deploy, a QA/fix loop, live X-Ray trace verification, UI smoke, and a GO/NO-GO report.
- **Difficulty_Workflow_Recipe**: The Kiro Workflow recipe (a `*.workflow.json` file under `.kiro/workflows/`) that implements this feature when launched by the coarse tasks of this spec.

## Requirements

### Requirement 1: Select a difficulty when starting a new game

**User Story:** As a Player, I want to choose a difficulty level when I start a new game from the Frontend, so that I can play a puzzle that matches the challenge I want.

#### Acceptance Criteria

1. WHEN the Frontend presents the new-game control, THE Frontend SHALL offer the Player a selection among the Difficulty_Level values `easy`, `medium`, and `hard`.
2. THE Frontend SHALL present `medium` as the preselected Difficulty_Level before the Player makes a selection.
3. WHEN the Player starts a new game with a selected Difficulty_Level, THE Frontend SHALL include that Difficulty_Level as the Difficulty_Field in the newGame request JSON body alongside the Player_Id.
4. WHEN the Player starts a new game and the Frontend sends a Difficulty_Level, THE Frontend SHALL send the Difficulty_Field as a value from the Valid_Difficulty_Level_Set.
5. WHEN the newGame response returns a Difficulty_Level, THE Frontend SHALL display the Difficulty_Level of the current game to the Player.

### Requirement 2: Difficulty determines the number of givens

**User Story:** As a Player, I want a harder level to present fewer givens than an easier level, so that difficulty is a meaningful choice.

#### Acceptance Criteria

1. THE Solver SHALL determine the Removal_Count for a generation from the selected Difficulty_Level rather than from a single fixed value.
2. WHERE the selected Difficulty_Level is `hard`, THE Solver SHALL use a Removal_Count greater than the Removal_Count it uses for `medium`.
3. WHERE the selected Difficulty_Level is `medium`, THE Solver SHALL use a Removal_Count greater than the Removal_Count it uses for `easy`.
4. WHEN the Solver generates a puzzle for a selected Difficulty_Level, THE Solver SHALL produce a puzzle whose solution is unique.
5. WHEN the Solver removes cells for a selected Difficulty_Level, THE Solver SHALL retain only removals that leave the remaining givens with exactly one completion.

### Requirement 3: Difficulty flows through newGame into generation, storage, and the response

**User Story:** As a Player, I want the difficulty I chose to drive the puzzle I receive and to be recorded with my game, so that the game I resume is the one I started.

#### Acceptance Criteria

1. WHEN the newGame_Lambda receives a request carrying a Difficulty_Field in the Valid_Difficulty_Level_Set, THE newGame_Lambda SHALL select the corresponding generation strategy from the Difficulty_Registry.
2. WHEN the newGame_Lambda generates a puzzle, THE newGame_Lambda SHALL use the Removal_Count of the selected Difficulty_Level.
3. WHEN the newGame_Lambda stores a Game_Item, THE newGame_Lambda SHALL persist the selected Difficulty_Level on the Game_Item for the `(gameId, playerId)` pair.
4. WHEN the newGame_Lambda completes successfully, THE newGame_Lambda SHALL include the selected Difficulty_Level in the newGame response.
5. WHEN the getGame_Lambda returns a game, THE getGame_Lambda SHALL include the Difficulty_Level stored on the Game_Item in the response.

### Requirement 4: Levels are selected by name from an Open/Closed registry

**User Story:** As a developer, I want difficulty levels to live in a name-keyed registry, so that adding a level is one new entry rather than edits scattered across the generator.

#### Acceptance Criteria

1. THE Difficulty_Registry SHALL map each Difficulty_Level name to its generation strategy.
2. WHEN generation needs the Removal_Count for a Difficulty_Level, THE X_Ray_Sudoku_Demo SHALL resolve the strategy by looking the Difficulty_Level name up in the Difficulty_Registry.
3. WHERE a new Difficulty_Level is added to the Difficulty_Registry, THE X_Ray_Sudoku_Demo SHALL make that level selectable through one added registry entry without modifying the generation algorithm.
4. THE X_Ray_Sudoku_Demo SHALL select every registered Difficulty_Level through the same registry lookup, so that each level produces a board of the standard 9x9 shape and differs only in its Removal_Count.

### Requirement 5: Deterministic generation per difficulty

**User Story:** As a developer, I want the same seed and the same difficulty to reproduce the same puzzle, so that generation stays deterministic and testable.

#### Acceptance Criteria

1. WHEN the Solver generates a puzzle from a given Seed for a given Difficulty_Level, THE Solver SHALL produce the same puzzle and the same solution on every generation with that Seed and that Difficulty_Level.
2. THE Solver SHALL derive all randomness for a generation from the injected random generator seeded by the caller, taking no module-level or wall-clock source of randomness.
3. THE Solver SHALL iterate cells, rows, columns, and boxes in an explicitly defined order during generation AND SHALL take no reliance on set iteration or incidental dict ordering.
4. WHEN a Difficulty_Level is introduced, THE X_Ray_Sudoku_Demo SHALL accompany it with a property-based test demonstrating that the same Seed and that Difficulty_Level reproduce the same puzzle.

### Requirement 6: Error handling for difficulty at the Lambda boundary

**User Story:** As a Player, I want a clear error when I send a difficulty the system does not accept, so that a mistake never produces a server error or a broken page.

#### Acceptance Criteria

1. IF the newGame_Lambda receives a Difficulty_Field whose value is not in the Valid_Difficulty_Level_Set, THEN THE newGame_Lambda SHALL return a 400 response whose JSON body names the field `difficulty`.
2. IF the newGame_Lambda receives a Difficulty_Field whose value is not a string, THEN THE newGame_Lambda SHALL return a 400 response whose JSON body names the field `difficulty`.
3. WHEN the newGame_Lambda returns a 400 for a difficulty failure, THE newGame_Lambda SHALL include in the JSON body the Valid_Difficulty_Level_Set so the Player learns the accepted values.
4. IF the newGame_Lambda receives an invalid Difficulty_Field, THEN THE newGame_Lambda SHALL return a response other than 500 and SHALL omit any raw stack trace from the response body.
5. WHEN the newGame_Lambda receives a difficulty supplied as a query-string parameter or a path parameter rather than in the JSON body, THE newGame_Lambda SHALL treat the request as carrying no Difficulty_Field and apply the Default_Difficulty_Level.
6. WHEN the newGame_Lambda or the getGame_Lambda returns any response, including every 400 and 404, THE Lambda SHALL include the CORS headers that authorize the Amplify_Origin.

### Requirement 7: Backward compatibility and defaults

**User Story:** As an operator, I want existing games and difficulty-free requests to keep working, so that this feature needs no data migration.

#### Acceptance Criteria

1. WHEN the newGame_Lambda receives a request with no Difficulty_Field, THE newGame_Lambda SHALL generate a puzzle using the Default_Difficulty_Level.
2. WHEN the newGame_Lambda stores a Game_Item for a request with no Difficulty_Field, THE newGame_Lambda SHALL persist the Default_Difficulty_Level on the Game_Item.
3. WHEN the getGame_Lambda loads a Game_Item that has no stored Difficulty_Level, THE getGame_Lambda SHALL report the Difficulty_Level as the Default_Difficulty_Level.
4. THE X_Ray_Sudoku_Demo SHALL read existing Game_Items that predate this feature without a data migration.

### Requirement 8: Preserve the four solver subsegments

**User Story:** As a workshop attendee, I want the trace to keep showing where time goes inside the Solver, so that the demonstration still works after difficulty is added.

#### Acceptance Criteria

1. WHEN the Solver generates a puzzle for any Difficulty_Level, THE Solver SHALL record exactly the Solver_Subsegment_Names `validate`, `generate`, `solve-backtracking`, and `check-solution` with those exact names.
2. THE Tracer SHALL open and close the Solver's subsegments, so the Solver's own computation opens no subsegment.
3. WHEN a Difficulty_Level changes the Removal_Count, THE Solver SHALL open the `generate` subsegment once around the whole generation regardless of the Removal_Count.

### Requirement 9: Multi-user isolation and least privilege preserved

**User Story:** As an operator, I want difficulty to change nothing about data isolation or permissions, so that one Player still cannot touch another Player's game.

#### Acceptance Criteria

1. WHEN the newGame_Lambda or the getGame_Lambda reads or writes a Game_Item, THE Lambda SHALL address exactly the one `(gameId, playerId)` item.
2. THE X_Ray_Sudoku_Demo SHALL grant each Lambda the same DynamoDB actions on the Games_Table it already holds, adding no broader table or account access for difficulty.

### Requirement 10: Delivery by a Kiro Workflow recipe, gated by deploy-and-verify

**User Story:** As a maintainer, I want this feature implemented by a Kiro Workflow recipe launched by coarse tasks and gated by the existing release workflow, so that the result is produced and verified the same way every time.

#### Acceptance Criteria

1. THE tasks of this spec SHALL launch a Difficulty_Workflow_Recipe rather than enumerate hand-written fine-grained coding tasks.
2. THE Difficulty_Workflow_Recipe SHALL be stored as a `*.workflow.json` file under `.kiro/workflows/`.
3. WHEN the feature is complete, THE X_Ray_Sudoku_Demo SHALL gate the result through the Deploy_And_Verify_Workflow.
4. THE Difficulty_Workflow_Recipe SHALL produce the feature's implementation and SHALL NOT require fine-grained coding tasks authored by hand.

### Requirement 11: Offline suite and command surface

**User Story:** As a developer, I want to build and verify this feature offline through the documented commands, so that the suite passes on a clean machine with no cloud access.

#### Acceptance Criteria

1. WHEN a developer runs the documented test command inside the devbox environment, THE X_Ray_Sudoku_Demo SHALL run the suite through the `just test` recipe.
2. WHEN a developer runs the documented synthesis command inside the devbox environment, THE X_Ray_Sudoku_Demo SHALL synthesize the stack through the `just synth` recipe.
3. WHEN `just test` and `just synth` run with no AWS credentials and no network access beyond localhost, THE X_Ray_Sudoku_Demo SHALL complete both successfully.
4. IF `just test` or `just synth` fails under those offline conditions, THEN THE X_Ray_Sudoku_Demo SHALL treat the run as a hard failure with no fallback or retry.
5. WHEN the suite runs, THE X_Ray_Sudoku_Demo SHALL resolve CDK synthesis from no AWS account.

## Deliverable Model

This feature is implemented through a Kiro Workflow recipe, not through hand-written fine-grained coding tasks:

- The `tasks.md` for this spec defines coarse tasks that launch a `Difficulty_Workflow_Recipe` — a `*.workflow.json` recipe under `.kiro/workflows/` executed by the Kiro workflow runtime as a graph of agent steps (see `.kiro/workflows/README.md`). The spec does not enumerate fine-grained, hand-authored coding steps for the difficulty logic itself.
- The implementation produced by the recipe is gated by the existing `deploy-and-verify-release.workflow.json`: the offline gate (`just test` + `just synth`), an optional deploy, the QA/fix loop, live X-Ray trace verification, UI smoke, and the GO/NO-GO report.
- This requirements phase and the following design phase are where the human-reviewed thinking lives. No implementation code is written during requirements or design.
