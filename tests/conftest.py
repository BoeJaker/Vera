"""Pytest fixtures for the Vera test suite.

Pure-unit tests import the operator submodules directly (``vera.operator.*`` —
stdlib-only, no server or browser). The ``orch`` fixture additionally imports the
full orchestrator app for contract/panel tests and SKIPS if this environment
lacks Vera's runtime deps (fastapi/redis/…), so the pure suite still runs green
anywhere.
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_ROOT)
for _p in (_ROOT, _PARENT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(scope="session")
def orch():
    """The imported orchestrator module with operator caps registered.

    Skips cleanly when the full runtime isn't installed here."""
    try:
        import Vera.vera.capability_orchestration as _orch  # noqa
        import Vera.vera.operator.operator_web_capabilities  # noqa: F401  registers operator.*
        return _orch
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"orchestrator/app unavailable in this env: {e}")
