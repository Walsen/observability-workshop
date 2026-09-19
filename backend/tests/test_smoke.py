"""Smoke test proving the backend package skeleton imports.

This test exists so the (otherwise empty) test suite discovers at least one
item and exits green. It also verifies every package in the skeleton created by
task 1.4 is importable, which is the point of the skeleton.
"""

import importlib


def test_backend_packages_import() -> None:
    for name in (
        "backend",
        "backend.handlers",
        "backend.domain",
        "backend.data",
        "backend.infra",
        "backend.tests",
    ):
        assert importlib.import_module(name) is not None
