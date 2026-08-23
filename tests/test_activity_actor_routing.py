"""Two small decisions that were each wrong in production.

1. `_activity_actor` — the recent-caps ring carried no attribution, so the system
   narrator reported its OWN ambient probing back to the user as if the user had
   done it ("I see you just checked your Docker containers" — the narrator's own
   gather had called docker.ps).

2. `_avoid_embed_share` — avoid_embed excluded the embedding node unconditionally,
   which on a 2-CPU pool funnelled every dream_director call onto one node.
   Observed live: cpu-247 queued while cpu-246 sat at in_use=0, so narrator calls
   hit the 180s queue timeout and fell back onto the GPU — defeating the deny_gpu
   the rule exists to protect.

Both are loaded straight from source (lowercase `vera.` path semantics) rather
than imported, so these stay pure and cannot drag in the app.
"""

import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _load(src_rel, *names):
    """exec just the named top-level defs out of a source file."""
    src_path = os.path.join(_ROOT, *src_rel.split("/"))
    with open(src_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    wanted = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and n.name in names]
    missing = set(names) - {n.name for n in wanted}
    assert not missing, f"not found in {src_rel}: {sorted(missing)}"
    from typing import Any, Dict, Optional
    ns = {"Dict": Dict, "Any": Any, "Optional": Optional,
          "_AVOID_EMBED_RELAX_AT": 1}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), src_path, "exec"), ns)
    return ns


# ── who caused a cap call ────────────────────────────────────────────────────

@pytest.mark.critical
@pytest.mark.parametrize("rec,expected", [
    ({},                                          "you"),      # browser UI = the user
    ({"via": ""},                                 "you"),
    ({"via": "claude"},                           "agent:claude-code"),
    ({"via": "mcp"},                              "agent:claude-code"),
    ({"via": "codex"},                            "agent:codex"),
    ({"bg": "dream_director"},                    "system:dream_director"),
    ({"sid": "dream:nightly"},                    "system:dream"),
    ({"sid": "v8:prog-1"},                        "system:loop"),
    ({"sid": "goal-42"},                          "system:goal"),
    # a background driver outranks everything else on the record
    ({"bg": "narrator", "via": "claude", "sid": "chat-1"}, "system:narrator"),
    # a machine session outranks the caller kind
    ({"sid": "dream:x", "via": ""},               "system:dream"),
])
def test_actor_attribution(rec, expected):
    fn = _load("vera/dream/dream_capabilities.py", "_activity_actor")["_activity_actor"]
    assert fn(rec) == expected


@pytest.mark.critical
def test_system_work_is_never_attributed_to_the_user():
    """The exact regression: the narrator's own probing must not read as 'you'."""
    fn = _load("vera/dream/dream_capabilities.py", "_activity_actor")["_activity_actor"]
    narrator_probe = {"name": "docker.ps", "bg": "dream_director", "sid": "dream:narrator"}
    assert fn(narrator_probe) != "you"
    assert fn(narrator_probe).startswith("system:")


# ── sharing work back onto an idle embed node ────────────────────────────────

def _share():
    return _load("vera/capability_orchestration.py",
                 "_avoid_embed_share")["_avoid_embed_share"]


@pytest.mark.critical
def test_shares_only_when_alternatives_busy_and_embed_idle():
    """THE regression: alternatives queueing while the embed node sits idle."""
    assert _share()(emb_busy=0, rest_min=1, relax_at=1) is True
    assert _share()(emb_busy=0, rest_min=4, relax_at=1) is True


@pytest.mark.critical
def test_keeps_embed_node_free_when_an_alternative_is_idle():
    """Normal case — never push generation onto the embed node needlessly."""
    assert _share()(emb_busy=0, rest_min=0, relax_at=1) is False


@pytest.mark.critical
def test_never_piles_onto_an_already_busy_embed_node():
    """If the embed node is working, it is not the relief valve."""
    assert _share()(emb_busy=1, rest_min=3, relax_at=1) is False
    assert _share()(emb_busy=5, rest_min=5, relax_at=1) is False


@pytest.mark.critical
def test_relax_at_zero_restores_the_hard_exclusion():
    """The escape hatch must actually disable sharing, not enable it always."""
    for rest_min in (0, 1, 9):
        assert _share()(emb_busy=0, rest_min=rest_min, relax_at=0) is False
