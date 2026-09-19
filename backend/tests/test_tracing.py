"""Focused example tests for the tracer abstraction (task 3.1).

These are minimal example tests proving the module's shape holds; the fuller
FakeTracer unit tests belong to task 3.2. They run fully offline: FakeTracer
touches no AWS, and the XRayTracer test monkeypatches ``xray_recorder`` so no
real X-Ray/AWS call is made.
"""

from __future__ import annotations

from backend.domain.tracing import FakeTracer, Tracer, XRayTracer


def test_tracing_example_fake_tracer_records_names_in_order() -> None:
    tracer: Tracer = FakeTracer()

    # Nested deliberately (not collapsed) to exercise enter/exit nesting order.
    with tracer.subsegment("generate"):  # noqa: SIM117
        with tracer.subsegment("solve-backtracking"):
            pass
    with tracer.subsegment("check-solution"):
        pass

    assert isinstance(tracer, FakeTracer)
    # Entered-order is the key requirement: nested-then-sequential order holds.
    assert tracer.entered == ["generate", "solve-backtracking", "check-solution"]
    # Exit order is recorded too: the inner subsegment closes before its parent.
    assert tracer.exited == ["solve-backtracking", "generate", "check-solution"]


def test_tracing_example_xray_tracer_opens_and_closes_named_subsegment(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _RecorderSpy:
        def begin_subsegment(self, name: str) -> None:
            calls.append(("begin", name))

        def end_subsegment(self) -> None:
            calls.append(("end", ""))

    monkeypatch.setattr(
        "backend.domain.tracing.xray_recorder", _RecorderSpy(), raising=True
    )

    tracer: Tracer = XRayTracer()
    with tracer.subsegment("validate"):
        calls.append(("body", "validate"))

    assert calls == [("begin", "validate"), ("body", "validate"), ("end", "")]


# --- FakeTracer unit tests (task 3.2) -------------------------------------
#
# Fuller normal/edge/error-case coverage of the FakeTracer test double. These
# complement the two `test_tracing_example_*` tests above without duplicating
# their exact assertions.

import pytest


def test_faketracer_starts_empty() -> None:
    """A fresh FakeTracer records nothing until a subsegment is opened."""
    tracer = FakeTracer()

    assert tracer.entered == []
    assert tracer.exited == []


def test_faketracer_context_manager_enters_and_exits_cleanly() -> None:
    """A single subsegment records its name on both enter and normal exit."""
    tracer = FakeTracer()

    with tracer.subsegment("validate"):
        # On enter the name is recorded; it has not yet been closed.
        assert tracer.entered == ["validate"]
        assert tracer.exited == []

    # On normal exit the same name is recorded as closed.
    assert tracer.entered == ["validate"]
    assert tracer.exited == ["validate"]


def test_faketracer_records_sequential_subsegments_in_order() -> None:
    """Sequential (non-nested) subsegments enter and exit in call order."""
    tracer = FakeTracer()

    with tracer.subsegment("validate"):
        pass
    with tracer.subsegment("generate"):
        pass
    with tracer.subsegment("solve-backtracking"):
        pass

    assert tracer.entered == ["validate", "generate", "solve-backtracking"]
    # With no nesting, exit order matches enter order.
    assert tracer.exited == ["validate", "generate", "solve-backtracking"]


def test_faketracer_nesting_records_outer_then_inner_enter_inner_then_outer_exit() -> (
    None
):
    """Nested subsegments enter outer-first and close inner-first."""
    tracer = FakeTracer()

    with tracer.subsegment("generate"):  # noqa: SIM117
        with tracer.subsegment("solve-backtracking"):
            # Both are open; the inner has not closed yet.
            assert tracer.entered == ["generate", "solve-backtracking"]
            assert tracer.exited == []

    # Enter reflects outer-then-inner; exit reflects inner-then-outer.
    assert tracer.entered == ["generate", "solve-backtracking"]
    assert tracer.exited == ["solve-backtracking", "generate"]


def test_faketracer_closes_subsegment_when_body_raises() -> None:
    """An exception in the body still closes the subsegment and propagates."""
    tracer = FakeTracer()

    with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
        with tracer.subsegment("solve-backtracking"):
            raise ValueError("boom")

    # The subsegment was still recorded as opened and then closed.
    assert tracer.entered == ["solve-backtracking"]
    assert tracer.exited == ["solve-backtracking"]


def test_faketracer_closes_inner_and_outer_when_inner_body_raises() -> None:
    """A raise inside a nested body unwinds inner-then-outer as it propagates."""
    tracer = FakeTracer()

    with pytest.raises(ValueError, match="boom"):  # noqa: SIM117
        with tracer.subsegment("generate"):
            with tracer.subsegment("solve-backtracking"):
                raise ValueError("boom")

    assert tracer.entered == ["generate", "solve-backtracking"]
    # Inner closes first as the exception propagates out through the outer.
    assert tracer.exited == ["solve-backtracking", "generate"]


def test_faketracer_satisfies_tracer_protocol() -> None:
    """FakeTracer is a structural match for the runtime-checkable Tracer."""
    tracer = FakeTracer()

    assert isinstance(tracer, Tracer)
