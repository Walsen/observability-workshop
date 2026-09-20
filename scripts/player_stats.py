"""Read-only player-statistics report over the Games_Table (task 16).

This is a read-only tool the ``get-players`` skill drives through
``just player-stats``. Like ``verify_trace.py`` it lives under ``scripts/`` and
is EXPLICITLY OUTSIDE the offline ``just test`` suite: the scan it runs needs
AWS credentials and the network. What *is* offline-testable — and what the tests
in ``backend/tests/test_player_stats.py`` exercise — is the pure aggregation
core and the table-name/scan plumbing, all reachable with fakes injected in
place of boto3 (dependency inversion, engineering-practices §1).

The design deliberately splits two concerns (Single Responsibility):

* **Pure aggregation** — :func:`compute_player_stats` takes already-unmarshalled
  items (plain dicts with ``gameId``/``playerId``/``status``/``createdAt``) and
  returns a :class:`PlayerStats`. It touches no AWS and no wall clock, so every
  count is a deterministic function of its input. Iteration order is defined
  throughout (items are sorted by ``createdAt`` then ``gameId``); nothing
  reaching the output relies on set or incidental dict ordering
  (engineering-practices §2).

* **AWS I/O** — :func:`scan_and_compute` runs a paginated, projected
  ``Scan`` (following ``LastEvaluatedKey``) and unmarshals each item before
  handing the list to the pure core. :func:`resolve_table_name` discovers the
  table. Both take the boto3 client (or a factory) as a parameter, so a test
  injects a fake and no real client is ever constructed at import time.

**Honesty about the synthetic estimate.** The Synthetics canary drives the UI
every 5 minutes with plain-UUID ``playerId``s indistinguishable from real
players, so synthetic traffic CANNOT be cleanly excluded by id. It can only be
ESTIMATED from the ~5-minute ``createdAt`` cadence. :func:`estimate_synthetic_games`
is a documented heuristic, not a fact, and the report labels it as an estimate.

Operational logs go through a stdlib logger configured once, emitting one
single-line JSON object per event to **stderr** (engineering-practices §6), so
stdout carries only the report — the prose summary or, with ``--json``, the
machine-readable result. Never ``print()`` for logs; never log board/solution
data or anything sensitive.

Run it with:  ``just player-stats``  (or ``python scripts/player_stats.py``),
with AWS credentials in the environment (``AWS_PROFILE`` / ``AWS_REGION``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

_STACK_NAME = "XraySudokuDemoStack"
# The CloudFormation output the stack could expose for the table name, and the
# prefix every GamesTable physical name carries (CDK derives it from the logical
# id ``GamesTable``). Discovery tries the output first, then a prefix match.
_TABLE_NAME_OUTPUT_ID = "GamesTableName"
_TABLE_NAME_PREFIX = f"{_STACK_NAME}-GamesTable"

# The four attributes the report reads. ``status`` is a DynamoDB reserved word,
# so it is projected through the ``#s`` expression-attribute-name alias.
_STATUS_ALIAS = "#s"
_PROJECTED_ATTRIBUTES = ("gameId", "playerId", _STATUS_ALIAS, "createdAt")
_PROJECTION_EXPRESSION = ", ".join(_PROJECTED_ATTRIBUTES)

# Default canary cadence and the jitter tolerated around it when estimating the
# synthetic share. 5 minutes is the canary schedule (Requirement 11.2); 90s of
# tolerance absorbs run-start jitter without pulling in unrelated bursts.
_DEFAULT_CADENCE_MINUTES = 5
_DEFAULT_TOLERANCE_SECONDS = 90

_LOGGER_NAME = "player_stats"


# --- structured logging (single-line JSON to stderr, configured once) --------


class _JsonFormatter(logging.Formatter):
    """Serialize a record to a single-line JSON object (stderr).

    Mirrors the handler logger's contract (engineering-practices §6): a fixed
    ``timestamp``/``level``/``logger``/``message`` schema plus whitelisted
    context, so nothing a caller attaches by accident can leak. ``json.dumps``
    escapes any newline, keeping every record on one physical line.
    """

    _CONTEXT_FIELDS = ("tableName", "region", "totalGames", "distinctPlayers")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field_name in self._CONTEXT_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        if record.exc_info is not None:
            exc_type = record.exc_info[0]
            payload["error"] = exc_type.__name__ if exc_type is not None else "error"
        return json.dumps(payload, ensure_ascii=False)


def _get_logger() -> logging.Logger:
    """Return the module logger, configured once with a single stderr handler.

    Idempotent: a repeat call reuses the installed JSON handler rather than
    stacking duplicates. Output goes to **stderr** so stdout stays reserved for
    the report the program prints as its result.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    already = any(isinstance(h.formatter, _JsonFormatter) for h in logger.handlers)
    if not already:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# --- pure aggregation core (no AWS, no wall clock) ---------------------------


@dataclass(frozen=True)
class PlayerStats:
    """The computed player statistics — a pure function of the scanned items.

    ``estimated_synthetic_games`` / ``estimated_organic_games`` are an ESTIMATE
    derived from the ~5-minute canary cadence, not a measured fact; the canary's
    plain-UUID players cannot be separated from real ones by id. See
    :func:`estimate_synthetic_games`.
    """

    total_games: int
    distinct_players: int
    games_by_status: dict[str, int]
    estimated_synthetic_games: int
    estimated_organic_games: int
    cadence_minutes: int = _DEFAULT_CADENCE_MINUTES
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS


def _sort_key(item: dict[str, Any]) -> tuple[str, str]:
    """A defined ordering: by ``createdAt`` then ``gameId`` (both as strings).

    Missing values sort as empty strings so a malformed item never crashes the
    sort. This keeps every downstream count independent of set/dict ordering
    (engineering-practices §2).
    """
    return (str(item.get("createdAt", "")), str(item.get("gameId", "")))


def _parse_created_at(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``createdAt`` string; return ``None`` if unparseable.

    The live table stores e.g. ``2026-09-19T22:34:10.452257+00:00``. An item
    whose timestamp cannot be parsed simply does not participate in the cadence
    estimate rather than aborting the whole report.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def estimate_synthetic_games(
    items: list[dict[str, Any]],
    cadence_minutes: int = _DEFAULT_CADENCE_MINUTES,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> int:
    """Estimate how many games came from the ~5-minute canary cadence.

    HEURISTIC, NOT A FACT. The canary creates a game roughly every
    ``cadence_minutes`` with a plain-UUID ``playerId`` indistinguishable from a
    real player's, so synthetic traffic can only be *estimated* from the regular
    spacing of ``createdAt``, never cleanly excluded by id.

    The estimate counts games that fall on a regular ~cadence-spaced series:
    walking the games in ``createdAt`` order, whenever the gap from the previous
    kept game is within ``tolerance_seconds`` of the cadence, both endpoints are
    treated as part of a synthetic run. Games with an unparseable timestamp, and
    bursts spaced far from the cadence, are left out (counted organic). The
    result is in ``[0, len(items)]``.
    """
    parsed = sorted(
        parsed_ts
        for item in items
        if (parsed_ts := _parse_created_at(item.get("createdAt"))) is not None
    )
    if len(parsed) < 2:
        return 0

    cadence = cadence_minutes * 60
    lower, upper = cadence - tolerance_seconds, cadence + tolerance_seconds

    synthetic_indices: set[int] = set()
    previous_index = 0
    for index in range(1, len(parsed)):
        gap = (parsed[index] - parsed[previous_index]).total_seconds()
        if lower <= gap <= upper:
            # A cadence-spaced step: both ends belong to the synthetic run.
            synthetic_indices.add(previous_index)
            synthetic_indices.add(index)
            previous_index = index
        else:
            # Off cadence: this game anchors the next comparison but is not
            # itself counted unless a later step lands on it.
            previous_index = index
    return len(synthetic_indices)


def compute_player_stats(
    items: list[dict[str, Any]],
    cadence_minutes: int = _DEFAULT_CADENCE_MINUTES,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> PlayerStats:
    """Aggregate already-unmarshalled game items into :class:`PlayerStats`.

    Pure: touches no AWS and no wall clock, so the result is a deterministic
    function of ``items``. ``total_games`` is the item count, ``distinct_players``
    the number of unique ``playerId`` values, ``games_by_status`` the per-status
    tally (which partitions the games and so sums to ``total_games``), and the
    synthetic/organic split comes from :func:`estimate_synthetic_games` with
    ``organic = total - synthetic``.
    """
    ordered = sorted(items, key=_sort_key)

    total_games = len(ordered)
    distinct_players = len({str(item.get("playerId", "")) for item in ordered})

    status_counter: Counter[str] = Counter(
        str(item.get("status", "unknown")) for item in ordered
    )
    # Emit the status tally in a defined (sorted) key order.
    games_by_status = {status: status_counter[status] for status in sorted(status_counter)}

    synthetic = estimate_synthetic_games(ordered, cadence_minutes, tolerance_seconds)
    organic = total_games - synthetic

    return PlayerStats(
        total_games=total_games,
        distinct_players=distinct_players,
        games_by_status=games_by_status,
        estimated_synthetic_games=synthetic,
        estimated_organic_games=organic,
        cadence_minutes=cadence_minutes,
        tolerance_seconds=tolerance_seconds,
    )


# --- AWS I/O layer (injected client / factories, never a client at import) ---


class _ScanClient(Protocol):
    """The one DynamoDB client operation the scan needs — ``scan``."""

    def scan(self, **kwargs: Any) -> dict[str, Any]: ...


def _unmarshal_item(marshalled: dict[str, Any]) -> dict[str, Any]:
    """Flatten a low-level (client API) item to a plain string-valued dict.

    The four projected attributes are all DynamoDB strings (``{"S": ...}``);
    this reads the ``S`` value out of each, tolerating an already-flat value so
    the same function works whether the caller used the client or resource API.
    Only the four projected keys reach the pure core, so nothing sensitive is
    carried along even if the table item has more attributes.
    """
    flat: dict[str, Any] = {}
    for key, value in marshalled.items():
        if isinstance(value, dict) and "S" in value:
            flat[key] = value["S"]
        else:
            flat[key] = value
    return flat


def scan_and_compute(
    client: _ScanClient,
    table_name: str,
    cadence_minutes: int = _DEFAULT_CADENCE_MINUTES,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> PlayerStats:
    """Paginate a projected scan of ``table_name`` and aggregate the items.

    Reads only the four attributes via a ``ProjectionExpression`` (with ``#s``
    aliasing the ``status`` reserved word) to keep the scan cheap, follows
    ``LastEvaluatedKey`` to the end, unmarshals each item, and hands the whole
    list to :func:`compute_player_stats`. The client is injected, so this runs
    against a fake with no real AWS in the offline tests.
    """
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "TableName": table_name,
        "ProjectionExpression": _PROJECTION_EXPRESSION,
        "ExpressionAttributeNames": {_STATUS_ALIAS: "status"},
    }
    while True:
        response = client.scan(**scan_kwargs)
        for marshalled in response.get("Items", []):
            items.append(_unmarshal_item(marshalled))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return compute_player_stats(items, cadence_minutes, tolerance_seconds)


def resolve_table_name(
    explicit: str | None,
    cfn_client_factory: Callable[[], Any],
    dynamodb_client_factory: Callable[[], Any],
) -> str:
    """Resolve the Games_Table name: explicit arg > env var > discovery.

    Precedence, matching how the rest of the tooling resolves deploy-time
    values (verify_trace.py's URL resolution):

    1. an explicit ``--table-name`` (``explicit``);
    2. the ``GAMES_TABLE`` environment variable;
    3. discovery — the ``XraySudokuDemoStack`` CloudFormation ``GamesTableName``
       output, then a fallback of listing tables and matching the
       ``XraySudokuDemoStack-GamesTable`` prefix.

    The two discovery clients arrive as factories so nothing is constructed
    unless discovery is actually reached, and a test injects fakes.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("GAMES_TABLE")
    if from_env:
        return from_env

    discovered = _table_name_from_cloudformation(cfn_client_factory())
    if discovered is not None:
        return discovered

    matched = _table_name_by_prefix(dynamodb_client_factory())
    if matched is not None:
        return matched

    raise RuntimeError(
        f"Could not resolve the Games table name: no --table-name, no GAMES_TABLE, "
        f"no {_TABLE_NAME_OUTPUT_ID} output on stack {_STACK_NAME}, and no table "
        f"matching the {_TABLE_NAME_PREFIX!r} prefix."
    )


def _table_name_from_cloudformation(cfn_client: Any) -> str | None:
    """Return the stack's ``GamesTableName`` output value, or ``None`` if absent."""
    stacks = cfn_client.describe_stacks(StackName=_STACK_NAME)["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output.get("OutputKey") == _TABLE_NAME_OUTPUT_ID:
            return str(output["OutputValue"])
    return None


def _table_name_by_prefix(dynamodb_client: Any) -> str | None:
    """Return the first table whose name carries the GamesTable prefix.

    Pages ``list_tables`` via ``LastEvaluatedTableName`` and returns the first
    match in a defined order (the API returns names sorted), or ``None`` when no
    table matches.
    """
    start_key: str | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if start_key is not None:
            kwargs["ExclusiveStartTableName"] = start_key
        response = dynamodb_client.list_tables(**kwargs)
        for name in response.get("TableNames", []):
            if name.startswith(_TABLE_NAME_PREFIX):
                return str(name)
        start_key = response.get("LastEvaluatedTableName")
        if not start_key:
            return None


# --- reporting ---------------------------------------------------------------


def _region() -> str:
    """The AWS region to operate in (AWS_REGION env, else us-east-1)."""
    return os.environ.get("AWS_REGION", "us-east-1")


def format_report(stats: PlayerStats) -> str:
    """Render a human-readable prose report of the statistics.

    The synthetic/organic split is explicitly labelled an ESTIMATE so a reader
    never mistakes it for a measured fact.
    """
    lines = [
        "X-Ray Sudoku Demo — player statistics (all-time, full-table scan)",
        f"  Distinct players : {stats.distinct_players}",
        f"  Total games      : {stats.total_games}",
        "  Games by status  :",
    ]
    if stats.games_by_status:
        for status in sorted(stats.games_by_status):
            lines.append(f"      {status}: {stats.games_by_status[status]}")
    else:
        lines.append("      (none)")
    lines.append(
        "  Synthetic vs organic (ESTIMATE — from the "
        f"~{stats.cadence_minutes}-minute canary cadence, not a fact):"
    )
    lines.append(f"      estimated synthetic (canary) games: {stats.estimated_synthetic_games}")
    lines.append(f"      estimated organic (real) games   : {stats.estimated_organic_games}")
    lines.append(
        "  Note: a 'player' is a browser-generated playerId with no auth; the "
        "canary uses plain-UUID playerIds that cannot be cleanly separated from "
        "real players, only estimated from cadence."
    )
    return "\n".join(lines)


def _stats_as_json(stats: PlayerStats) -> str:
    """Render the statistics as a single machine-readable JSON object."""
    payload = asdict(stats)
    payload["estimate_note"] = (
        "estimated_synthetic_games / estimated_organic_games are a heuristic "
        "estimate from the ~5-minute canary cadence, not a measured fact"
    )
    return json.dumps(payload, indent=2, sort_keys=True)


# --- CLI ---------------------------------------------------------------------


def _boto3_session(profile: str | None, region: str) -> Any:
    """Build a boto3 Session, honouring an explicit profile or AWS_PROFILE.

    Imported lazily inside the function so importing this module never requires
    boto3 or constructs a client — the offline tests import the module and drive
    the pure/plumbing functions with fakes, touching none of this.
    """
    import boto3

    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def run(args: argparse.Namespace) -> int:
    """Resolve the table, scan it, and print the report. Returns an exit code."""
    logger = _get_logger()
    region = _region()
    session = _boto3_session(args.profile, region)

    table_name = resolve_table_name(
        explicit=args.table_name,
        cfn_client_factory=lambda: session.client("cloudformation"),
        dynamodb_client_factory=lambda: session.client("dynamodb"),
    )
    logger.info("resolved games table", extra={"tableName": table_name, "region": region})

    client = session.client("dynamodb")
    stats = scan_and_compute(client, table_name)
    logger.info(
        "computed player statistics",
        extra={
            "tableName": table_name,
            "region": region,
            "totalGames": stats.total_games,
            "distinctPlayers": stats.distinct_players,
        },
    )

    # stdout carries only the result — prose, or machine-readable JSON.
    if args.json:
        print(_stats_as_json(stats))
    else:
        print(format_report(stats))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table-name",
        default=None,
        help="Games table name (else GAMES_TABLE env, else stack discovery).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS profile to use (else AWS_PROFILE from the environment).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the prose report.",
    )
    args = parser.parse_args(argv)

    logger = _get_logger()
    try:
        return run(args)
    except RuntimeError as exc:
        # A resolution/validation failure we raised ourselves: one JSON error
        # line to stdout, non-zero exit, never a raw traceback. logger.exception
        # records the traceback to the log per engineering-practices §5.
        logger.exception("player stats failed")
        print(json.dumps({"error": "player_stats_failed", "detail": str(exc)}))
        return 2
    except OSError as exc:
        # Network/credential/transport failure from boto3 at the CLI boundary.
        logger.exception("player stats I/O error")
        print(json.dumps({"error": "io_error", "detail": str(exc)}))
        return 1
    except Exception as exc:
        # Anything else (e.g. a botocore ClientError) is caught at this true
        # top-level boundary so the operator sees one JSON line, not a stack
        # trace. It is logged with full context via logger.exception, per
        # engineering-practices §5.
        logger.exception("player stats unexpected error")
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}))
        return 1


# Keep a reference so linters don't flag the imported field helper as unused in
# environments that tree-shake; ``field`` documents intent on future extension.
_ = field

if __name__ == "__main__":
    raise SystemExit(main())
