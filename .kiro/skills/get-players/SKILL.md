---
name: get-players
description: "Report how many people are playing this project's deployed X-Ray Sudoku demo — distinct player count, active users, number of players, games played, and usage stats — from the DynamoDB Games table. Use when the user asks how many players/users there are, how many people are playing, the player count, how many games have been played, or for usage statistics."
---

# Report player statistics for this solution

Report player and usage statistics for the deployed X-Ray Sudoku demo (stack
`XraySudokuDemoStack`, `us-east-1`) by scanning the DynamoDB Games table through
the `just player-stats` recipe. Give the distinct-player count, total games,
games-by-status, and the ESTIMATED synthetic (canary) vs organic split — always
labelled as an estimate.

## Prerequisites

- The stack must be deployed (`XraySudokuDemoStack`, `us-east-1`) so there is a
  Games table with data to scan. If nothing is deployed, tell the user and stop.
- AWS credentials with read access to the Games table (scan) and, for table-name
  discovery, `cloudformation:DescribeStacks` / `dynamodb:ListTables`. Use AWS
  profile `Walsen` (`AWS_PROFILE=Walsen`), region `us-east-1`.
- This skill uses the project's own `just player-stats` recipe — a read-only
  script. There is **no DynamoDB MCP server**; do not look for one. Run the
  recipe from the repo root inside the devbox environment.

## Workflow

1. **Run the recipe.** From the repo root, run `just player-stats` for the prose
   report, or `just player-stats --json` when you want machine-readable output to
   parse. The script resolves the table name itself (an explicit `--table-name`,
   else the `GAMES_TABLE` env var, else the `XraySudokuDemoStack` CloudFormation
   output, else a scan for the `XraySudokuDemoStack-GamesTable` prefix), runs a
   paginated, projected read-only scan, and prints the statistics. Operational
   logs go to stderr as single-line JSON; the report itself is on stdout.

2. **Report the numbers.** Relay exactly what the tool returns:
   - **distinct players** — the number of unique `playerId` values (this is the
     "how many people are playing" / "player count" answer, with the caveats
     below).
   - **total games** — how many games have been created all-time.
   - **games by status** — the per-status tally (e.g. `in_progress`, `solved`),
     which sums to the total.

3. **Report the synthetic vs organic split — as an ESTIMATE.** The tool also
   returns `estimated_synthetic_games` and `estimated_organic_games`. Present
   these clearly as an **estimate**, not a fact: the canary
   (`xraysudokudemosddbbe0`) creates a game roughly every 5 minutes using
   plain-UUID `playerId`s that are indistinguishable from real players, so the
   split is inferred from the ~5-minute `createdAt` cadence, not measured. Say so
   in the same breath as the numbers.

4. **Summarize.** Give a short verdict: how many distinct players, how many games,
   and roughly how much of that looks like canary traffic versus real usage —
   framed as an estimate.

## Notes

- **A "player" is a browser-generated `playerId` with no auth.** The frontend
  mints a UUID per browser and stores it in `localStorage`. So the same person on
  two devices (or two browsers) counts as two players, and someone who clears
  `localStorage` starts fresh as a new player. "Distinct players" means distinct
  ids, which is a proxy for people, not a headcount.
- **Synthetic (canary) traffic cannot be cleanly separated.** The canary uses
  plain-UUID `playerId`s just like real players, so it can only be ESTIMATED from
  the regular ~5-minute creation cadence. The synthetic/organic split is a
  documented heuristic; never present it as exact.
- **No time window by default.** The scan counts all-time games (a full-table
  scan). That is fine at demo scale, but it is every game ever created, not a
  recent-activity or "active users right now" figure.
- The recipe is read-only — it only scans; it never writes to the table.
- Report only what the tool returns. If a value isn't available, say so rather
  than inventing a number.
