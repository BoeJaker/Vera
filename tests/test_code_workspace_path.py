"""code.author and code.edit must agree on where a file lives.

Census goal build-multifile, 2026-08-27. The loop asked for
`/workspace/statkit/stats.py` and code.author reported:

    path:    "workspace/statkit/stats.py"
    fs_path: "/workspace/workspace/statkit/stats.py"      <- doubled

The container ended up holding BOTH trees. The step then spent ten cycles
hunting the file, failed code.edit three times ("stats.py does not exist or is
empty") and re-authored the whole file at ~47s a go.

The collapse that prevents this existed on code.edit ONLY - so READING was
fixed while WRITING kept producing the shadow path. Both now share one
definition, which is the actual guarantee: the two cannot drift apart again.

Imports the app module, so it runs in-container.
"""
import pytest

try:
    from Vera.vera.dag import dag_workshop_capabilities as M
except Exception:                                    # pragma: no cover
    M = None

pytestmark = pytest.mark.skipif(M is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    assert M is not None and hasattr(M, "_code_workspace_path")


@pytest.mark.parametrize("given", [
    "/workspace/statkit/stats.py",      # what the loop actually passed
    "workspace/statkit/stats.py",       # what code.author echoed back
    "statkit/stats.py",                 # already correct
])
def test_every_form_of_the_same_file_resolves_identically(given):
    """The three spellings the loop produced must be ONE file, not three."""
    assert M._code_workspace_path(given) == "statkit/stats.py"


def test_the_doubled_path_cannot_be_produced():
    """The exact shadow file: /workspace/workspace/statkit/stats.py."""
    out = M._code_workspace_path("/workspace/statkit/stats.py")
    assert not out.startswith("workspace/"), \
        "a leading workspace/ re-joins to /workspace/workspace/%s" % out


def test_a_repo_path_keeps_its_workspace_directory():
    """In a repo, `workspace/` can be a real top-level dir - do not eat it."""
    assert M._code_workspace_path("workspace/thing.py", repo="vera") == "workspace/thing.py"


def test_only_the_leading_segment_is_collapsed():
    """A nested `workspace` directory deeper down is a real directory."""
    assert M._code_workspace_path("src/workspace/thing.py") == "src/workspace/thing.py"
    assert M._code_workspace_path("workspace/workspace/x.py") == "workspace/x.py"


def test_traversal_and_separators_are_still_sanitised():
    assert ".." not in M._code_workspace_path("../../etc/passwd")
    assert M._code_workspace_path("a\\b\\c.py") == "a/b/c.py"


def test_author_and_edit_agree_on_the_same_input():
    """The property that actually failed: one file, two caps, one answer.

    Checked structurally against the SOURCE rather than by calling the caps
    (they generate), so a future edit that re-inlines a divergent copy fails
    here. Uses AST rather than a regex window - my first attempt bounded the
    search at the first `task =`, which in cap_code_author appears BEFORE the
    path assignment, so it read a body that did not contain the call.
    """
    import ast
    import inspect
    import pathlib

    src = pathlib.Path(inspect.getsourcefile(M)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in ("cap_code_author", "cap_code_edit"):
            bodies[node.name] = ast.get_source_segment(src, node) or ""
    assert set(bodies) == {"cap_code_author", "cap_code_edit"}, bodies.keys()

    for name, body in bodies.items():
        assert "_code_workspace_path(" in body, "%s must use the shared helper" % name
        # and must NOT carry its own private copy of the collapse
        assert 'parts[0] == "workspace"' not in body and '_pp[0] == "workspace"' not in body, \
            "%s re-inlined its own collapse - that is how the two drifted apart" % name


def test_every_path_taking_code_cap_uses_the_shared_helper():
    """Writer and READERS must agree, or a saved file cannot be found again.

    code.author now stores `statkit/stats.py`. A reader still calling the raw
    normaliser would look up `workspace/statkit/stats.py` for the same request
    and miss. Swept together rather than one cap at a time - fixing the writer
    alone is what produced this class of bug in the first place.
    """
    import ast
    import inspect
    import pathlib

    src = pathlib.Path(inspect.getsourcefile(M)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"cap_code_author", "cap_code_edit", "cap_prose_author",
            "cap_code_read", "cap_code_versions", "cap_code_diff", "cap_code_restore"}
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in want:
            seen[node.name] = ast.get_source_segment(src, node) or ""
    missing = want - set(seen)
    assert not missing, "caps not found: %s" % sorted(missing)
    for name, body in seen.items():
        assert "_code_workspace_path(" in body, \
            "%s still normalises its path without the workspace collapse" % name


def test_the_collapse_is_not_applied_twice():
    """Applying it twice would eat a genuine nested workspace/workspace/ path.

    Each cap collapses the CALLER's input exactly once; the shared store is not
    given a second pass. This pins the boundary, since a `workspace/workspace/x`
    path is legal and must survive as `workspace/x`.
    """
    once = M._code_workspace_path("workspace/workspace/x.py")
    assert once == "workspace/x.py"
    assert M._code_workspace_path(once) == "x.py", \
        "a second pass eats another segment - do not chain these calls"
