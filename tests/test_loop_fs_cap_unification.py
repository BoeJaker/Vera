"""One filesystem story for the agentic loop: sandbox caps read, authors write.

Census build-multifile (2026-08-29) showed what two half-overlapping cap families
cost. The loop's toolkit carried ide.fs.read (an IDE cap), file CREATION was meant
to go through code.author, and yet the injected sys-exec-fileio skill told the
model to create files with ide.fs.write - a cap the step scope then refused. Told
to do one thing and blocked from doing it, the model improvised, ending on a step
titled "Force-persist __init__.py via ide.fs.read write-back": writing through a
read cap.

So: the sandbox caps are canonical for sandbox file I/O and share ONE
implementation with the ide caps (route_fs_*), the write caps are denied to the
loop, and the guidance may not teach a cap the loop cannot call.
"""
import ast
import os

import pytest

HERE = os.path.dirname(__file__)
SBX = os.path.join(HERE, "..", "vera", "remote", "session_sandbox_capabilities.py")
SKILLS = os.path.join(HERE, "..", "vera", "skills", "skills.py")


def _fn_src(path, name):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("%s not found in %s" % (name, path))


@pytest.mark.parametrize("fn,router", [
    ("cap_sbx_fs_read", "route_fs_read"),
    ("cap_sbx_fs_write", "route_fs_write"),
])
def test_sandbox_fs_caps_go_through_the_shared_router(fn, router):
    """The ide caps already route; these bypassed it and were quietly weaker.

    route_fs_write auto-creates the session's sandbox and route_fs_read carries
    the existence probe and basename self-correction. Calling _exec_in directly
    is what made the same request succeed via ide.fs.write and fail here with
    "no sandbox for this session".
    """
    body = _fn_src(SBX, fn)
    assert router + "(" in body, "%s must delegate to %s" % (fn, router)
    assert "_exec_in(" not in body, \
        "%s still drives the container itself - that is the drift that hid a bug" % fn


@pytest.mark.parametrize("fn", ["cap_sbx_fs_read", "cap_sbx_fs_write"])
def test_sandbox_fs_caps_default_the_session(fn):
    """A model-authored loop call cannot know the session id."""
    assert "_default_session_id()" in _fn_src(SBX, fn)


def _fileio_skill_text():
    src = open(SKILLS, encoding="utf-8").read()
    i = src.index('"id": "sys-exec-fileio"')
    j = src.find('"id": "', i + 10)
    return src[i:j if j != -1 else len(src)]


def test_the_file_skill_teaches_the_authoring_caps_for_creation():
    text = _fileio_skill_text()
    assert "code.author(" in text, "creation guidance must name code.author"
    assert "sandbox.session.fs.read(" in text, "read guidance must name the sandbox cap"


def test_the_file_skill_does_not_teach_a_write_cap_the_loop_is_denied():
    """The exact contradiction: told to ide.fs.write, then refused for it."""
    text = _fileio_skill_text()
    assert "ide.fs.write(path=" not in text, \
        "the skill still instructs a raw write the loop cannot perform"


try:
    from Vera.vera.dag import dag_workshop_capabilities as W
except Exception:                                      # pragma: no cover
    W = None


@pytest.mark.skipif(W is None, reason="app module not importable here")
def test_this_module_actually_imported_the_app():
    assert W is not None and hasattr(W, "_v5_cap_denied")


@pytest.mark.skipif(W is None, reason="app module not importable here")
@pytest.mark.parametrize("const", [
    "_V5_ESSENTIAL_ACTION_CAPS", "_V5_ALWAYS_FILE_CAPS",
    "_V5_ALWAYS_FILE_CAPS_RO", "_V5_CORE_SEED_CAPS",
])
def test_the_loop_reads_through_the_sandbox_cap(const):
    caps = tuple(getattr(W, const))
    assert "sandbox.session.fs.read" in caps, "%s lost the sandbox read cap" % const
    assert "ide.fs.read" not in caps, \
        "%s still seeds an IDE cap for sandbox file I/O" % const


@pytest.mark.skipif(W is None, reason="app module not importable here")
@pytest.mark.parametrize("cap", ["ide.fs.write", "sandbox.session.fs.write"])
def test_raw_write_caps_are_denied_to_the_loop(cap):
    """Creation goes via code.author / prose.author, or the generating cap."""
    assert W._v5_cap_denied(cap) is True


@pytest.mark.skipif(W is None, reason="app module not importable here")
@pytest.mark.parametrize("cap", ["sandbox.session.fs.read", "ide.fs.read",
                                 "code.author", "prose.author", "code.edit"])
def test_reads_and_authoring_stay_reachable(cap):
    """ide.fs.read is no longer SEEDED, but it is deliberately not hard-blocked:
    a custom profile naming it must keep working."""
    assert W._v5_cap_denied(cap) is False
