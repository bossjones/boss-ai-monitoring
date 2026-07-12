"""Smoke test: the package imports and declares a version."""

import boss_ai_monitoring


def test_package_imports() -> None:
    assert boss_ai_monitoring.__version__
