from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from msagent.cli.bootstrap.initializer import initializer
from msagent.core.paths import AppPaths


@pytest.fixture(autouse=True)
def isolate_msagent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent unit tests from reading or writing the user's real msAgent home."""
    test_home = tmp_path / "msagent-home"
    test_paths = AppPaths.from_home(test_home)
    previous_paths = initializer.app_paths
    previous_registries = initializer._registries

    monkeypatch.setenv("MSAGENT_HOME", str(test_home))
    initializer.app_paths = test_paths
    initializer._registries = {}
    try:
        yield
    finally:
        initializer.app_paths = previous_paths
        initializer._registries = previous_registries
