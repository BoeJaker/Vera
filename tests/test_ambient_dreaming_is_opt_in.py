"""Nothing should dream on a box nobody asked to dream.

Both the dream scheduler and the ambient director auto-started on boot on any
non-sandbox instance, because their code defaults said enabled=True.

Prod's persisted scheduler config already said `enabled: false` - someone had
turned it off - but the code default was never brought in line, so a fresh
instance or a cleared key resumed dreaming. The DIRECTOR had no persisted config
at all (`vera:dream:director:config` was empty), so it was running in production
purely on its default.

What that cost, measured 2026-08-31: three ambient "thinks" of 2775s, 2120s and
2364s - 121 minutes of CPU for 573 tokens between them, at 0.1 tok/s.

It is CPU-routed (job_type="dream_director" is deny_gpu), so it was not slowing
the GPU census - it was simply running unbidden.

Sandboxes were already correct: dream/_startup has its own is_dev_sandbox()
guard, verified live (mirror running=False, prod running=True). That guard is
load-bearing and must not be removed by anyone "simplifying" this.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "vera", "dream",
                   "dream_capabilities.py")


def _tree():
    with open(SRC, encoding="utf-8") as fh:
        return ast.parse(fh.read()), fh


def _dict_literal(name):
    """The literal value of a module-level dict assignment."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name:
            value = node.value
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in node.targets):
            value = node.value
        else:
            continue
        out = {}
        for k, v in zip(value.keys, value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                out[k.value] = v.value
        return out
    raise AssertionError("%s not found" % name)


def _src():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


# --- the defaults -----------------------------------------------------------

def test_the_dream_scheduler_is_off_by_default():
    assert _dict_literal("DEFAULT_CONFIG")["enabled"] is False


def test_the_ambient_director_is_off_by_default():
    assert _dict_literal("DIRECTOR_DEFAULTS")["enabled"] is False


def test_no_master_switch_falls_back_to_on():
    """A missing config key must not mean 'on' - that is the path by which a
    cleared or absent config quietly resumed dreaming."""
    src = _src()
    assert 'cfg.get("enabled", True)' not in src
    assert 'dcfg.get("enabled", True)' not in src


# --- what must NOT have changed --------------------------------------------

def test_the_sandbox_guard_is_still_there():
    """Sandboxes were already correct; this change must not disturb that."""
    src = _src()
    assert "if is_dev_sandbox():" in src
    i = src.index("if is_dev_sandbox():")
    assert "auto-start skipped" in src[i:i + 400]


def test_individual_dream_stages_are_untouched():
    """The per-stage `enabled: True` flags are a different thing entirely -
    they say what a dream cycle DOES once running, not whether it runs."""
    src = _src()
    assert src.count('"enabled":      True,') >= 3


def test_the_config_capability_can_still_turn_it_on():
    """Opt-in, not removed - there must still be a way to enable it."""
    src = _src()
    assert "dream.director.config" in src


def test_the_measurement_is_recorded_beside_the_change():
    src = _src()
    i = src.index("DIRECTOR_DEFAULTS")
    window = src[i:i + 900]
    assert "0.1 tok/s" in window or "121 minutes" in window
