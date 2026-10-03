"""A description in context_files is not a file: the run's research still reaches the writer
(probe 3 Oct, 6145c6cf: ["wgall.com research output"], ["<content extracted from ...>"])."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rsc5", ROOT / "vera" / "dag" / "research_sources_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_descriptions_are_not_files():
    assert not C.names_real_files('["wgall.com research output"]')
    assert not C.names_real_files(["<content extracted from web.research query: x>"])
    assert C.wants_sources('["wgall.com research output"]', "wgall.com blog post")


def test_real_files_with_and_without_a_listing():
    assert C.names_real_files(["data.csv"]) and not C.wants_sources(["data.csv"], "")
    assert C.names_real_files(["/workspace/notes.md"], known=["notes.md", "x.py"])
    assert not C.names_real_files(["ghost.md"], known=["notes.md"])
    assert not C.names_real_files(["journal.json"], known=["journal.json"])
    assert C.wants_sources(["ghost.md"], "", known=["notes.md"])


def test_the_loop_passes_the_run_listing():
    assert "known=list(workdir_files or []) + list(saved_run_files or {})" in SRC
