"""The Sudoku Solver: pure domain logic with X-Ray subsegments injected.

This module is pure domain logic: no ``boto3``, no ``aws_cdk``, and no direct
X-Ray calls — the only observability dependency is the injected
:class:`~backend.domain.tracing.Tracer`, which opens named subsegments (Dependency
Inversion). The solver's own computation contains no tracing: each public method
opens its subsegment once and delegates to a private, pure helper, keeping
subsegment bookkeeping out of the recursion per Single Responsibility.

Task 4.1 implements :meth:`Solver.solve` (subsegment ``solve-backtracking``) and
:meth:`Solver.check_solution` (subsegment ``check-solution``). Task 4.4 adds
:meth:`Solver.generate` (subsegment ``generate``) and
:meth:`Solver.validate_move` (subsegment ``validate``), completing the four named
subsegments the workshop trace expects.

Iteration over cells uses a defined row-major ``range`` order throughout (no set
iteration, no reliance on dict ordering), so solving is deterministic as the
engineering-practices steering requires.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from backend.domain.board import (
    SIZE,
    Board,
    Move,
    is_complete_and_valid,
    is_valid_placement,
)
from backend.domain.tracing import Tracer

# How many cells generation blanks out of the 81. Bounding removals keeps each
# generate cheap enough for a 100+ iteration property test while leaving a
# comfortable ~40 givens; every accepted removal is still uniqueness-checked, so
# the puzzle keeps exactly one solution regardless of this value.
_TARGET_REMOVALS = 40


@dataclass(frozen=True)
class GeneratedPuzzle:
    """The result of :meth:`Solver.generate`: givens plus their unique solution.

    ``puzzle`` is the board handed to the player, with ``0`` marking the cells
    they must fill; ``solution`` is the single full completion those givens admit.
    Both are 9x9 boards, and every non-zero cell of ``puzzle`` equals the
    corresponding cell of ``solution``.
    """

    puzzle: Board
    solution: Board


class Solver:
    """Computes Sudoku solutions and checks completed boards.

    The Solver takes a :class:`Tracer` by constructor injection and uses it to
    open the exact X-Ray subsegment names the workshop trace expects
    (``solve-backtracking``, ``check-solution``). It performs no AWS calls of any
    kind itself.
    """

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    def solve(self, board: Board) -> Board | None:
        """Return a completed solution for ``board``, or ``None`` if unsolvable.

        Works on a copy, so the caller's board is never mutated. The returned
        board is fully filled, satisfies every Sudoku constraint, and agrees with
        every non-zero given cell of the input. Returns ``None`` when the givens
        admit no valid completion.

        The ``solve-backtracking`` subsegment is opened once around the whole
        solve; the recursion itself does no tracing.
        """
        with self._tracer.subsegment("solve-backtracking"):
            # Requirement 5.4 / Property 5: a board whose givens already carry a
            # direct contradiction (the same value twice in a row, column, or box)
            # admits no completion. Refute it up front rather than searching: this
            # both rejects a full-but-contradictory board — which _backtrack would
            # otherwise report "solved", since it succeeds the moment no empty cell
            # remains without validating the pre-existing cells — and short-circuits
            # a sparse contradiction in O(cells) instead of a near-unbounded search.
            if not self._givens_are_consistent(board):
                return None
            working = [row[:] for row in board]
            if self._backtrack(working):
                return working
            return None

    def check_solution(self, board: Board) -> bool:
        """Return whether ``board`` is a complete, valid Sudoku solution.

        Opens the ``check-solution`` subsegment and delegates to the pure
        ``is_complete_and_valid`` board predicate.
        """
        with self._tracer.subsegment("check-solution"):
            return is_complete_and_valid(board)

    def validate_move(self, board: Board, move: Move) -> bool:
        """Return whether ``move`` is a legal placement on ``board``.

        A move is legal when its target cell is currently empty and placing the
        value there duplicates nothing within its row, column, or 3x3 box. A move
        onto an already-filled cell is rejected: players fill empty cells, so
        overwriting a given (or a cell they already filled) is not a valid move.

        Opens the ``validate`` subsegment and delegates the conflict check to the
        pure ``is_valid_placement`` predicate.
        """
        with self._tracer.subsegment("validate"):
            if board[move.row][move.col] != 0:
                return False
            return is_valid_placement(board, move.row, move.col, move.value)

    def generate(self, rng: random.Random) -> GeneratedPuzzle:
        """Return a puzzle with a unique solution, driven entirely by ``rng``.

        First fills an empty board into a complete, valid solution, trying digits
        in an ``rng``-shuffled order so different seeds yield different solutions.
        Then removes cells one at a time — in an ``rng``-shuffled cell order —
        keeping a removal only while the remaining givens still admit exactly one
        completion. The result therefore has exactly one solution, and because all
        randomness comes from ``rng`` alone, an equally seeded generator reproduces
        the same puzzle and solution.

        The ``generate`` subsegment is opened once around the whole operation; the
        fill, uniqueness count, and cell removal are pure helpers with no tracing.
        """
        with self._tracer.subsegment("generate"):
            solution = self._full_solution(rng)
            puzzle = self._carve_unique_puzzle(solution, rng)
            return GeneratedPuzzle(puzzle=puzzle, solution=solution)

    @staticmethod
    def _givens_are_consistent(board: Board) -> bool:
        """Return whether every filled cell of ``board`` is a legal placement.

        Pure check over the input givens: iterating cells in row-major order, each
        non-zero cell must satisfy the placement rules against the rest of the
        board. Because ``is_valid_placement`` excludes the target cell itself,
        calling it with the value the cell already holds asks exactly "does this
        value duplicate elsewhere in its row, column, or box?" — so a ``False``
        for any filled cell means the givens carry a direct contradiction.
        """
        for row in range(SIZE):
            for col in range(SIZE):
                value = board[row][col]
                if value != 0 and not is_valid_placement(board, row, col, value):
                    return False
        return True

    def _backtrack(self, board: Board) -> bool:
        """Fill ``board`` in place by backtracking; return whether it succeeded.

        Pure recursion with no tracing: selects the next empty cell in row-major
        order, tries each digit that placement rules permit, and recurses. On a
        dead end it restores the cell to empty and reports failure so the caller
        can try the next candidate.
        """
        cell = self._next_empty_cell(board)
        if cell is None:
            return True  # no empty cells remain: the board is complete

        row, col = cell
        for value in range(1, SIZE + 1):
            if is_valid_placement(board, row, col, value):
                board[row][col] = value
                if self._backtrack(board):
                    return True
                board[row][col] = 0  # undo and try the next candidate
        return False

    @staticmethod
    def _next_empty_cell(board: Board) -> tuple[int, int] | None:
        """Return the first empty cell in row-major order, or ``None`` if full."""
        for row in range(SIZE):
            for col in range(SIZE):
                if board[row][col] == 0:
                    return row, col
        return None

    def _full_solution(self, rng: random.Random) -> Board:
        """Return a complete, valid board, filled using only ``rng``.

        Same shape as ``_backtrack``, but candidate digits are tried in an
        ``rng``-shuffled order rather than ascending, so the completed board
        varies with the seed. Starting from an empty grid the fill always
        succeeds, so this returns a full board.
        """
        board: Board = [[0] * SIZE for _ in range(SIZE)]
        self._fill(board, rng)
        return board

    def _fill(self, board: Board, rng: random.Random) -> bool:
        """Fill ``board`` in place using ``rng``-shuffled candidates; report success."""
        cell = self._next_empty_cell(board)
        if cell is None:
            return True

        row, col = cell
        candidates = list(range(1, SIZE + 1))
        rng.shuffle(candidates)
        for value in candidates:
            if is_valid_placement(board, row, col, value):
                board[row][col] = value
                if self._fill(board, rng):
                    return True
                board[row][col] = 0
        return False

    def _carve_unique_puzzle(self, solution: Board, rng: random.Random) -> Board:
        """Blank cells of ``solution`` in ``rng`` order while uniqueness holds.

        Considers cells in an ``rng``-shuffled order, tentatively clearing each and
        keeping the clear only while the remaining board still has exactly one
        completion (a cleared cell that admits a second completion is restored).
        Carving stops once ``_TARGET_REMOVALS`` cells have been blanked, which
        bounds the work per call so a 100+ iteration property test stays fast; the
        result still has exactly one solution because every accepted removal was
        uniqueness-checked. The defined shuffle order makes carving reproducible
        for a given seed.
        """
        puzzle = [row[:] for row in solution]
        cells = [(row, col) for row in range(SIZE) for col in range(SIZE)]
        rng.shuffle(cells)
        removed = 0
        for row, col in cells:
            if removed >= _TARGET_REMOVALS:
                break
            value = puzzle[row][col]
            puzzle[row][col] = 0
            if self._count_solutions(puzzle, limit=2) == 1:
                removed += 1
            else:
                puzzle[row][col] = value  # restore: removal broke uniqueness
        return puzzle

    def _count_solutions(self, board: Board, limit: int) -> int:
        """Count completions of ``board``, stopping once ``limit`` is reached.

        Counts on a working copy so the caller's board is untouched. The ``limit``
        short-circuit keeps this cheap for the uniqueness check, which only needs
        to distinguish "exactly one" from "two or more".
        """
        working = [row[:] for row in board]
        return self._count(working, limit)

    def _count(self, board: Board, limit: int) -> int:
        """Recursively count completions of ``board`` in place, capped at ``limit``.

        Selects the empty cell with the fewest legal candidates first
        (minimum-remaining-values). This prunes the search dramatically compared
        with first-empty-cell selection, keeping the uniqueness check fast even
        when many cells are blank. A cell with no candidate ends the branch at
        zero completions.
        """
        cell = self._most_constrained_cell(board)
        if cell is None:
            return 1  # a full board is one completion

        row, col, candidates = cell
        total = 0
        for value in candidates:
            board[row][col] = value
            total += self._count(board, limit - total)
            board[row][col] = 0
            if total >= limit:
                return total
        return total

    @staticmethod
    def _most_constrained_cell(board: Board) -> tuple[int, int, list[int]] | None:
        """Return the empty cell with fewest legal digits, or ``None`` if full.

        Scans cells in row-major order (a defined order) and returns the first
        empty cell holding the smallest candidate set seen, together with those
        candidates. Returning early on a cell with a single candidate is a cheap,
        common speedup.
        """
        best: tuple[int, int, list[int]] | None = None
        for row in range(SIZE):
            for col in range(SIZE):
                if board[row][col] != 0:
                    continue
                candidates = [
                    value
                    for value in range(1, SIZE + 1)
                    if is_valid_placement(board, row, col, value)
                ]
                if best is None or len(candidates) < len(best[2]):
                    best = (row, col, candidates)
                    if len(candidates) <= 1:
                        return best
        return best
