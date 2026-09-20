"""Offline tests for the player-statistics script (task 16).

These pin the behavior of ``scripts.player_stats``: a read-only, all-time
player-statistics report over the Games_Table. The script is structured for
dependency inversion — a PURE aggregation core (``compute_player_stats`` /
``estimate_synthetic_games``) separated from the AWS I/O (a paginated scan and
table-name resolution) — so the whole of this suite runs fully offline: no AWS
credentials, no network, no ``boto3`` client constructed. The scan and the
table-name discovery are exercised against hand-rolled fakes injected in place
of the real boto3 client (mirroring ``test_repository.py``'s FakeTable), so no
``moto`` dependency is added.

Item shape throughout is the four projected attributes the script reads —
``gameId``, ``playerId``, ``status``, ``createdAt`` — as plain unmarshalled
dicts. ``createdAt`` is ISO-8601 UTC, matching what the live table stores.

Property test (min 100 iterations) for the two aggregation invariants —
``distinct_players <= total_games`` and ``sum(games_by_status.values()) ==
total_games`` — lives at the bottom, referencing the invariant it checks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from scripts import player_stats

# --- helpers -----------------------------------------------------------------

_BASE = datetime(2026, 9, 19, 22, 0, 0, tzinfo=UTC)


def _iso(offset_seconds: float) -> str:
    """An ISO-8601 UTC timestamp ``offset_seconds`` after the fixed base."""
    return (_BASE + timedelta(seconds=offset_seconds)).isoformat()


def _item(
    game_id: str,
    player_id: str,
    status: str = "in_progress",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a projected game item (the four attributes the script reads)."""
    return {
        "gameId": game_id,
        "playerId": player_id,
        "status": status,
        "createdAt": created_at if created_at is not None else _iso(0),
    }


# --- compute_player_stats: totals, distinct players, status tally ------------


def test_compute_counts_total_games_as_the_number_of_items() -> None:
    items = [
        _item("g1", "p1"),
        _item("g2", "p1"),
        _item("g3", "p2"),
    ]
    stats = player_stats.compute_player_stats(items)
    assert stats.total_games == 3


def test_compute_counts_distinct_players_ignoring_duplicate_player_ids() -> None:
    # p1 appears on three games, p2 on one: two distinct players, four games.
    items = [
        _item("g1", "p1"),
        _item("g2", "p1"),
        _item("g3", "p1"),
        _item("g4", "p2"),
    ]
    stats = player_stats.compute_player_stats(items)
    assert stats.distinct_players == 2
    assert stats.total_games == 4


def test_compute_tallies_games_by_status() -> None:
    items = [
        _item("g1", "p1", status="in_progress"),
        _item("g2", "p2", status="solved"),
        _item("g3", "p3", status="solved"),
    ]
    stats = player_stats.compute_player_stats(items)
    assert stats.games_by_status == {"in_progress": 1, "solved": 2}


def test_compute_handles_empty_input_without_crashing() -> None:
    stats = player_stats.compute_player_stats([])
    assert stats.total_games == 0
    assert stats.distinct_players == 0
    assert stats.games_by_status == {}
    # The synthetic estimate over no games is zero, not an error.
    assert stats.estimated_synthetic_games == 0
    assert stats.estimated_organic_games == 0


def test_compute_status_tally_sums_to_total_games() -> None:
    items = [
        _item("g1", "p1", status="in_progress"),
        _item("g2", "p1", status="solved"),
        _item("g3", "p2", status="in_progress"),
    ]
    stats = player_stats.compute_player_stats(items)
    assert sum(stats.games_by_status.values()) == stats.total_games


# --- estimate_synthetic_games: the ~5-minute cadence heuristic ---------------


def test_estimate_counts_a_five_minute_spaced_series_as_synthetic() -> None:
    # Ten games spaced exactly 5 minutes apart: the canary cadence.
    items = [_item(f"g{i}", f"p{i}", created_at=_iso(i * 300)) for i in range(10)]
    synthetic = player_stats.estimate_synthetic_games(items)
    assert synthetic == 10


def test_estimate_counts_irregularly_spaced_games_as_organic() -> None:
    # Spacings of 12s, 47s, 3s, 900s — nothing near the 5-minute cadence.
    offsets = [0, 12, 59, 62, 962]
    items = [_item(f"g{i}", f"p{i}", created_at=_iso(o)) for i, o in enumerate(offsets)]
    synthetic = player_stats.estimate_synthetic_games(items)
    # No regular ~5-min series: at most the trivial single-anchor, never a run.
    assert synthetic <= 1
    organic = len(items) - synthetic
    assert organic >= len(items) - 1


def test_estimate_splits_a_mix_of_cadence_and_bursts() -> None:
    # A clean 5-minute series of 6 canary games...
    series = [_iso(i * 300) for i in range(6)]
    # ...plus a burst of 3 organic games seconds apart, far off the cadence.
    burst = [_iso(5000), _iso(5003), _iso(5009)]
    items = [
        _item(f"g{i}", f"p{i}", created_at=ts)
        for i, ts in enumerate(series + burst)
    ]
    synthetic = player_stats.estimate_synthetic_games(items)
    # The six cadence games are counted; the three-second burst is not a
    # 5-minute series, so it stays organic.
    assert synthetic == 6
    assert len(items) - synthetic == 3


def test_estimate_accepts_spacing_within_tolerance() -> None:
    # 5 minutes ± a few seconds of jitter is still the canary cadence.
    offsets = [0, 300, 602, 898, 1201]  # ~300s apart, within a 90s tolerance
    items = [_item(f"g{i}", f"p{i}", created_at=_iso(o)) for i, o in enumerate(offsets)]
    synthetic = player_stats.estimate_synthetic_games(items)
    assert synthetic == len(items)


def test_estimate_rejects_spacing_outside_tolerance() -> None:
    # 5 minutes is the cadence; a 3-second tolerance rejects a 30s jitter.
    offsets = [0, 330, 660]  # 330s spacing, 30s off 300s
    items = [_item(f"g{i}", f"p{i}", created_at=_iso(o)) for i, o in enumerate(offsets)]
    synthetic = player_stats.estimate_synthetic_games(
        items, cadence_minutes=5, tolerance_seconds=3
    )
    assert synthetic <= 1


def test_compute_reports_organic_as_total_minus_synthetic() -> None:
    series = [_iso(i * 300) for i in range(5)]
    burst = [_iso(9000), _iso(9002)]
    items = [
        _item(f"g{i}", f"p{i}", created_at=ts) for i, ts in enumerate(series + burst)
    ]
    stats = player_stats.compute_player_stats(items)
    assert (
        stats.estimated_organic_games
        == stats.total_games - stats.estimated_synthetic_games
    )


# --- resolve_table_name: explicit arg > env var > discovery ------------------


class _FakeCfnClient:
    """Stand-in for a CloudFormation client exposing ``describe_stacks``."""

    def __init__(self, outputs: list[dict[str, str]]) -> None:
        self._outputs = outputs
        self.describe_calls: list[str] = []

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        self.describe_calls.append(StackName)
        return {"Stacks": [{"Outputs": self._outputs}]}


class _FakeDynamoListClient:
    """Stand-in for a DynamoDB client exposing a paginated ``list_tables``."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_tables(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def test_resolve_table_name_prefers_explicit_arg_over_env_and_discovery(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GAMES_TABLE", "from-env-table")
    # Discovery clients that would raise if consulted — they must NOT be.
    resolved = player_stats.resolve_table_name(
        explicit="explicit-table",
        cfn_client_factory=lambda: (_ for _ in ()).throw(AssertionError("used cfn")),
        dynamodb_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("used ddb")
        ),
    )
    assert resolved == "explicit-table"


def test_resolve_table_name_falls_back_to_env_var_when_no_arg(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GAMES_TABLE", "from-env-table")
    resolved = player_stats.resolve_table_name(
        explicit=None,
        cfn_client_factory=lambda: (_ for _ in ()).throw(AssertionError("used cfn")),
        dynamodb_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("used ddb")
        ),
    )
    assert resolved == "from-env-table"


def test_resolve_table_name_discovers_via_cloudformation_output(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("GAMES_TABLE", raising=False)
    cfn = _FakeCfnClient(
        outputs=[
            {"OutputKey": "ApiEndpointUrl", "OutputValue": "https://x"},
            {
                "OutputKey": "GamesTableName",
                "OutputValue": "XraySudokuDemoStack-GamesTableB32AB610-OX8NLC83DEAV",
            },
        ]
    )
    resolved = player_stats.resolve_table_name(
        explicit=None,
        cfn_client_factory=lambda: cfn,
        dynamodb_client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("should not need list_tables")
        ),
    )
    assert resolved == "XraySudokuDemoStack-GamesTableB32AB610-OX8NLC83DEAV"
    assert cfn.describe_calls == ["XraySudokuDemoStack"]


def test_resolve_table_name_falls_back_to_prefix_match_on_list_tables(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("GAMES_TABLE", raising=False)
    # CloudFormation yields no matching output, so discovery falls through to
    # listing tables and matching the GamesTable prefix — across two pages.
    cfn = _FakeCfnClient(outputs=[{"OutputKey": "ApiEndpointUrl", "OutputValue": "x"}])
    ddb = _FakeDynamoListClient(
        pages=[
            {"TableNames": ["unrelated-a", "other-b"], "LastEvaluatedTableName": "other-b"},
            {
                "TableNames": [
                    "XraySudokuDemoStack-GamesTableB32AB610-OX8NLC83DEAV",
                    "SomethingElse",
                ]
            },
        ]
    )
    resolved = player_stats.resolve_table_name(
        explicit=None,
        cfn_client_factory=lambda: cfn,
        dynamodb_client_factory=lambda: ddb,
    )
    assert resolved == "XraySudokuDemoStack-GamesTableB32AB610-OX8NLC83DEAV"
    # It paged: two list_tables calls, the second carrying the start key.
    assert len(ddb.calls) == 2
    assert ddb.calls[1].get("ExclusiveStartTableName") == "other-b"


# --- scan_game_items: paginated scan with projection + reserved-word alias ---


class _FakeScanClient:
    """DynamoDB client stand-in whose ``scan`` returns two pages.

    Records the ``scan`` kwargs so the test can assert the ProjectionExpression
    and the ``#s`` alias for the ``status`` reserved word, and drives pagination
    via ``LastEvaluatedKey`` exactly as boto3 does.
    """

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.scan_calls: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scan_calls.append(kwargs)
        return self._pages[len(self.scan_calls) - 1]


def _marshalled(item: dict[str, str]) -> dict[str, dict[str, str]]:
    """A low-level (client API) marshalled item: every attribute an ``S``."""
    return {key: {"S": value} for key, value in item.items()}


def test_scan_aggregates_all_items_across_two_pages() -> None:
    page1 = {
        "Items": [
            _marshalled(_item("g1", "p1", status="in_progress")),
            _marshalled(_item("g2", "p1", status="solved")),
        ],
        "LastEvaluatedKey": {"gameId": {"S": "g2"}, "playerId": {"S": "p1"}},
    }
    page2 = {
        "Items": [
            _marshalled(_item("g3", "p2", status="solved")),
        ]
    }
    client = _FakeScanClient(pages=[page1, page2])

    stats = player_stats.scan_and_compute(client, table_name="Games")

    # All three items across both pages were aggregated.
    assert stats.total_games == 3
    assert stats.distinct_players == 2
    assert stats.games_by_status == {"in_progress": 1, "solved": 2}


def test_scan_pages_via_last_evaluated_key() -> None:
    page1 = {
        "Items": [_marshalled(_item("g1", "p1"))],
        "LastEvaluatedKey": {"gameId": {"S": "g1"}, "playerId": {"S": "p1"}},
    }
    page2 = {"Items": [_marshalled(_item("g2", "p2"))]}
    client = _FakeScanClient(pages=[page1, page2])

    player_stats.scan_and_compute(client, table_name="Games")

    # Two scan calls: the second carries the ExclusiveStartKey from page one.
    assert len(client.scan_calls) == 2
    assert client.scan_calls[1]["ExclusiveStartKey"] == page1["LastEvaluatedKey"]


def test_scan_projects_only_the_four_attributes_and_aliases_status() -> None:
    client = _FakeScanClient(pages=[{"Items": [_marshalled(_item("g1", "p1"))]}])

    player_stats.scan_and_compute(client, table_name="Games")

    call = client.scan_calls[0]
    assert call["TableName"] == "Games"
    # status is a DynamoDB reserved word, so it is aliased via #s.
    names = call["ExpressionAttributeNames"]
    assert names["#s"] == "status"
    projection = call["ProjectionExpression"]
    for attribute in ("gameId", "playerId", "createdAt"):
        assert attribute in projection
    assert "#s" in projection
    # The raw reserved word is not projected directly (only via the alias).
    assert "status" not in projection.replace("#s", "")


# --- property: the two aggregation invariants hold across generated inputs ---
# Feature: xray-sudoku-demo, Property: player-stats aggregation invariants


_player_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12
)
_status_values = st.sampled_from(["in_progress", "solved", "abandoned"])


@st.composite
def _item_lists(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Generate a list of projected game items with varied players/statuses."""
    count = draw(st.integers(min_value=0, max_value=40))
    items: list[dict[str, Any]] = []
    for i in range(count):
        player_id = draw(_player_ids)
        status = draw(_status_values)
        # Spread createdAt arbitrarily so the synthetic estimate exercises too.
        offset = draw(st.integers(min_value=0, max_value=100_000))
        items.append(_item(f"g{i}", player_id, status=status, created_at=_iso(offset)))
    return items


@given(items=_item_lists())
@settings(max_examples=100)
def test_property_distinct_players_bounded_and_status_tally_sums_to_total(
    items: list[dict[str, Any]],
) -> None:
    """Aggregation invariants: distinct_players <= total_games, and the status
    tally sums to total_games.

    Validates: Requirements 13.1, 13.2, 13.3.

    There cannot be more distinct players than games (each game names exactly
    one player), and every game falls into exactly one status bucket, so the
    per-status counts partition the games and must sum back to the total. The
    synthetic/organic split must likewise partition the total.
    """
    stats = player_stats.compute_player_stats(items)

    assert stats.total_games == len(items)
    assert stats.distinct_players <= stats.total_games
    assert sum(stats.games_by_status.values()) == stats.total_games
    assert (
        stats.estimated_synthetic_games + stats.estimated_organic_games
        == stats.total_games
    )
    assert 0 <= stats.estimated_synthetic_games <= stats.total_games
