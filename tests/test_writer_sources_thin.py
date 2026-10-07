"""The writer still gets the research when the executor names only bookkeeping or a bare
URL (probe 3 Oct, 7c5bf52c: context_files=[journal.json], sources=<one URL>)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rsc3", ROOT / "vera" / "dag" / "research_sources_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_bookkeeping_only():
    assert C.bookkeeping_only('["/workspace/journal.json"]')
    assert C.bookkeeping_only(["journal.json"])
    assert not C.bookkeeping_only(["journal.json", "data.csv"])
    assert not C.bookkeeping_only("[]") and not C.bookkeeping_only(None)


def test_wants_sources():
    url = "https://lushbinary.com/blog/vector-database-benchmarks/"
    assert C.wants_sources('["/workspace/journal.json"]', url)
    assert C.wants_sources("[]", "")
    assert not C.wants_sources(["data.csv"], "")
    assert not C.wants_sources("[]", "x" * (C.THIN_SOURCES_CHARS + 1))


def test_wiring_keeps_what_the_executor_named():
    assert "_research_sources_core.wants_sources(" in SRC and "args.get(\"context_files\"), args.get(\"sources\")," in SRC
    assert 'args["sources"] = (_given + "\\n\\n" if _given else "") + _srcs' in SRC
