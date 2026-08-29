"""__init__.py must survive being written - the artifact path sanitiser ate it.

Census build-multifile (2026-08-29) burned its entire 25-minute wall cap on this.
`code.author(path="statkit/__init__.py")` returned ok=true with the requested
path, while `fs_path` came back as `/workspace/statkit/init__.py`: _safe_seg did
`s.strip("._")`, which strips the LEADING underscores off a real filename. The
package therefore had no __init__.py no matter how many times it was authored
(six), every `from statkit import stats` failed, and ide.fs.read correctly
reported /workspace/statkit/__init__.py missing - the read was honest, the write
was not.

The segment guard still has to reject traversal, so the fallback stays for
segments that are ENTIRELY dots/underscores.
"""
import pytest

try:
    from Vera.vera.execution import exec_capabilities as E
except Exception:                                      # pragma: no cover
    E = None

pytestmark = pytest.mark.skipif(E is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    """A skipped suite proves nothing - assert we really have the function."""
    assert E is not None and hasattr(E, "_safe_seg")


@pytest.mark.parametrize("name", [
    "__init__.py",      # the one that cost a whole census goal
    "__main__.py",
    "_private.py",
    "__pycache__",
    ".gitignore",       # dotfiles are legitimate workspace files
    ".env",
    "stats.py",
    "test_stats.py",
    "a-b_c.1.py",
])
def test_real_filenames_are_preserved_exactly(name):
    assert E._safe_seg(name) == name


@pytest.mark.parametrize("seg", [".", "..", "...", "_", "__", "._.", "", "   "])
def test_meaningless_or_traversal_segments_still_collapse(seg):
    """Whatever else changes, a segment can never come back as . or .."""
    out = E._safe_seg(seg)
    assert out == "default"
    assert out not in (".", "..")


def test_separators_and_illegal_characters_are_still_neutralised():
    assert "/" not in E._safe_seg("a/b")
    assert "\\" not in E._safe_seg("a\\b")
    assert E._safe_seg("we ird?*.py") == "we_ird__.py"
    assert len(E._safe_seg("x" * 300)) == 80


def test_a_package_path_survives_segment_by_segment():
    """The exact failing call: statkit/__init__.py must not become init__.py."""
    parts = [E._safe_seg(p) for p in "statkit/__init__.py".split("/")]
    assert parts == ["statkit", "__init__.py"]
    assert "/".join(parts) == "statkit/__init__.py"
