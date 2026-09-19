"""The tracer abstraction the Solver and handlers depend on.

Per the design's "Tracing abstraction" section, this module defines a narrow
``Tracer`` protocol and two implementations:

- ``XRayTracer`` — the runtime adapter over ``aws_xray_sdk.core.xray_recorder``.
  It is the only thing in the domain that touches ``aws_xray_sdk``.
- ``FakeTracer`` — a dependency-free test double that records the ordered names
  of opened subsegments, so fully offline tests can assert the four exact
  subsegment names (``validate``, ``generate``, ``solve-backtracking``,
  ``check-solution``) appear.

The protocol deliberately does not leak any ``aws_xray_sdk`` type, so the Solver
and handlers depend on the abstraction, not the SDK (Dependency Inversion).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol, runtime_checkable

from aws_xray_sdk.core import xray_recorder


@runtime_checkable
class Tracer(Protocol):
    """Opens a named X-Ray subsegment for the duration of a ``with`` block."""

    def subsegment(self, name: str) -> AbstractContextManager[None]:
        """Return a context manager that opens the named subsegment on enter."""
        ...


class XRayTracer:
    """Adapter over ``aws_xray_sdk``'s recorder.

    Keeps the SDK import localized: a caller does
    ``with tracer.subsegment("generate"): ...`` and this adapter translates that
    into ``begin_subsegment`` / ``end_subsegment`` bookkeeping. The subsegment is
    closed on exit even if the wrapped block raises.
    """

    @contextmanager
    def subsegment(self, name: str) -> Iterator[None]:
        xray_recorder.begin_subsegment(name)
        try:
            yield
        finally:
            xray_recorder.end_subsegment()


class FakeTracer:
    """Test double recording the order subsegments are entered and exited.

    Has no ``aws_xray_sdk`` dependency, so it is usable in offline tests. The
    entered-order list is the primary assertion target; the exited-order list is
    recorded too, which is cheap and lets a test confirm subsegments close in the
    expected nesting order.
    """

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.exited: list[str] = []

    @contextmanager
    def subsegment(self, name: str) -> Iterator[None]:
        self.entered.append(name)
        try:
            yield
        finally:
            self.exited.append(name)
