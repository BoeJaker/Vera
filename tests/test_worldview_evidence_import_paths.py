"""Guard both import paths used by tests, sandboxes, and production."""
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.critical

_MODULES = (
    "worldview.evidence_provider",
    "worldview.reranking_shadow",
    "worldview.context_ranker",
)


@pytest.mark.parametrize(("prefix", "cwd_parent"), [
    ("vera.", False),
    ("Vera.vera.", True),
])
def test_worldview_evidence_modules_support_runtime_and_test_import_paths(
        prefix, cwd_parent):
    root = Path(__file__).resolve().parents[1]
    cwd = root.parent if cwd_parent else root
    modules = ",".join(repr(prefix + module) for module in _MODULES)
    script = (
        "import importlib; "
        f"mods=({modules},); "
        "[importlib.import_module(name) for name in mods]"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, text=True,
        capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
