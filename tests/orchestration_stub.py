"""A fake ``Vera.vera.capability_orchestration`` for loading one module under test — SCOPED.

Several UI tests load a capability module straight from its file against a stand-in
orchestration (a fake Redis, a recording ``capability`` decorator, no app). The stand-in
has to sit in ``sys.modules`` under the real package's name while the module executes,
because that is the name the module imports.

What it must NOT do is stay there. The stand-in ``Vera`` and ``Vera.vera`` packages carry
an empty ``__path__``, so once they are left behind every later ``import Vera.vera.<x>``
in the same pytest process resolves against nothing: the whole critical tier collected
after such a test failed with ``No module named 'Vera.vera.research'`` and
``cannot import name ... (unknown location)`` — 28 collection errors on the design tree
(2026-09-21). The module under test keeps the references it took at import time, so
restoring ``sys.modules`` afterwards costs the test nothing.

    with stubbed_orchestration(orch):
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
"""

import contextlib
import sys
import types

_NAMES = ("Vera", "Vera.vera", "Vera.vera.capability_orchestration")


@contextlib.contextmanager
def stubbed_orchestration(orch):
    """Install ``orch`` as ``Vera.vera.capability_orchestration`` for the block, then put back
    exactly what ``sys.modules`` held before — the real packages if they were imported, nothing
    if they were not."""
    saved = {name: sys.modules.get(name) for name in _NAMES}
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch
    try:
        yield orch
    finally:
        for name, before in saved.items():
            if before is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = before
