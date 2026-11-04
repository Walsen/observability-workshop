"""Tests for the difficulty registry (feature: difficulty-levels).

These pin ``backend.domain.difficulty``: the registry maps the valid names to
strategies, resolution works by name (and rejects unknown names), the default is
``medium``, and — Property 2 — the removal counts are strictly ordered
``easy < medium < hard`` with ``medium`` equal to the pre-feature fixed value of
40. The module is pure, so no ``boto3`` or AWS is involved.
"""

from __future__ import annotations

import pytest

from backend.domain.difficulty import (
    DEFAULT_DIFFICULTY,
    DIFFICULTY_REGISTRY,
    VALID_DIFFICULTY_LEVELS,
    DifficultyStrategy,
    UnknownDifficultyError,
    is_valid_difficulty,
    resolve,
)

# The pre-feature fixed removal target; medium must equal it so medium is
# behavior-preserving (design Property 2, Requirement 7 backward compatibility).
_PRE_FEATURE_MEDIUM_REMOVALS = 40


# --- registry structure (Requirements 4.1, 4.2) ------------------------------


def test_registry_keys_equal_the_valid_set() -> None:
    assert tuple(DIFFICULTY_REGISTRY.keys()) == VALID_DIFFICULTY_LEVELS


def test_valid_set_is_easy_medium_hard_in_ascending_order() -> None:
    assert VALID_DIFFICULTY_LEVELS == ("easy", "medium", "hard")


def test_every_level_maps_to_a_strategy_with_a_positive_removal_count() -> None:
    for name in VALID_DIFFICULTY_LEVELS:
        strategy = DIFFICULTY_REGISTRY[name]
        assert isinstance(strategy, DifficultyStrategy)
        assert strategy.name == name
        assert strategy.removal_count > 0


def test_default_difficulty_is_medium() -> None:
    assert DEFAULT_DIFFICULTY == "medium"
    assert DEFAULT_DIFFICULTY in DIFFICULTY_REGISTRY


# --- resolution by name (Requirement 4.2) ------------------------------------


def test_resolve_returns_the_registered_strategy_for_a_known_name() -> None:
    for name in VALID_DIFFICULTY_LEVELS:
        assert resolve(name) is DIFFICULTY_REGISTRY[name]


def test_resolve_rejects_an_unknown_name() -> None:
    with pytest.raises(UnknownDifficultyError):
        resolve("expert")


def test_is_valid_difficulty_accepts_only_registered_string_names() -> None:
    for name in VALID_DIFFICULTY_LEVELS:
        assert is_valid_difficulty(name)
    assert not is_valid_difficulty("expert")
    assert not is_valid_difficulty("")
    # Non-strings are rejected (a non-string difficulty is a 400 at the boundary).
    assert not is_valid_difficulty(123)
    assert not is_valid_difficulty(None)
    assert not is_valid_difficulty(True)


# --- Property 2: removal counts strictly ordered, medium == 40 ---------------
# Feature: difficulty-levels, Property 2: Removal counts are strictly ordered by
# difficulty.
# Validates: Requirements 2.2, 2.3


def test_property_removal_counts_are_strictly_ordered_easy_lt_medium_lt_hard() -> None:
    easy = DIFFICULTY_REGISTRY["easy"].removal_count
    medium = DIFFICULTY_REGISTRY["medium"].removal_count
    hard = DIFFICULTY_REGISTRY["hard"].removal_count

    assert easy < medium < hard


def test_property_medium_removal_count_equals_the_pre_feature_fixed_value() -> None:
    assert DIFFICULTY_REGISTRY["medium"].removal_count == _PRE_FEATURE_MEDIUM_REMOVALS
