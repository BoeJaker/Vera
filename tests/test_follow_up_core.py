"""A gate follow-up is skipped only when an earlier step in its batch made its files -
never because the file it must EDIT already exists (census 2026-09-30)."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import follow_up_core as F  # noqa: E402


def test_an_edit_of_a_file_that_already_existed_always_runs():
    # author-then-edit: timer.html existed before the batch; the follow-up edits it.
    assert F.redundant(["timer.html"], {"timer.html": True}, {"timer.html": True}) is False


def test_a_file_created_earlier_in_the_batch_makes_the_duplicate_redundant():
    assert F.redundant(["app.py"], {"app.py": False}, {"app.py": True}) is True


def test_every_named_file_must_be_new_in_this_batch():
    assert F.redundant(["a.py", "b.py"], {"a.py": False, "b.py": True},
                       {"a.py": True, "b.py": True}) is False


def test_unknown_is_never_evidence():
    assert F.redundant(["a.py"], {}, {"a.py": True}) is False           # start unknown
    assert F.redundant(["a.py"], {"a.py": False}, {}) is False          # now unknown
    assert F.redundant(["a.py"], None, None) is False


def test_a_step_naming_no_file_always_runs():
    assert F.redundant([], {}, {}) is False


def test_the_loop_probes_before_the_batch_and_uses_the_rule():
    src = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")
    i = src.index('"type": "agent_loop_v6.follow_up_skipped"')
    block = src[i - 4000:i]
    assert "_fu_before" in block and "_follow_up_core.redundant(" in block
    assert "if all(_fu_exist.get(p) is True for p in _fu_paths):" not in src
