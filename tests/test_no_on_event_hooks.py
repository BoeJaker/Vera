"""S28 / D28-5: no startup hook depends on @APP.on_event, which never fires under the app's lifespan.

Three hooks sat behind @APP.on_event("startup") and never ran: Platforms' credential warm-up (so five sync readers
got an empty credential after every restart), n8n's MCP auto-reconnect, and the workshop's tool-wrapper install.
They now start through start_at_import, like every other module's startup. This lint keeps the next one out."""
import ast
import os
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
ON_EVENT = re.compile(r"""^[ 	]*@\s*\w+\.on_event\(\s*["'](startup|shutdown)["']\s*\)""", re.M)   # decorator lines only


def _py(rel_dir="vera"):
    for base, _d, files in os.walk(os.path.join(ROOT, rel_dir)):
        for n in files:
            if n.endswith(".py"):
                p = os.path.join(base, n)
                yield p, open(p, encoding="utf-8", errors="ignore").read()


def test_no_module_registers_an_on_event_hook():
    hits = [f"{os.path.relpath(p, ROOT)}:{src[:m.start()].count(chr(10)) + 1}"
            for p, src in _py() for m in ON_EVENT.finditer(src)]
    assert not hits, f"@APP.on_event never fires under lifespan; use start_at_import: {hits}"


@pytest.mark.parametrize("rel, fn, name", [
    ("vera/platforms/platform_capabilities.py", "_warm_secrets_at_startup", "platform_warm_secrets"),
    ("vera/n8n/n8n_capabilities.py", "_autoconnect", "n8n_autoconnect"),
    ("vera/dag/dag_workshop_capabilities.py", "_workshop_install_hooks", "workshop_install_hooks"),
])
def test_the_three_hooks_start_through_start_at_import(rel, fn, name):
    tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
    starts = [(getattr(c.args[0], "id", ""), getattr(c.args[1], "value", ""))
              for c in ast.walk(tree) if isinstance(c, ast.Call)
              and getattr(c.func, "attr", "") == "start_at_import" and len(c.args) >= 2]
    assert (fn, name) in starts, starts
