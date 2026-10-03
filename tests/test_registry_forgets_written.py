"""A call that can write files makes the registry forget the files it names (the stale
report.md of probe 3 Oct, 7c5bf52c, generalised to scripts)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rsc4", ROOT / "vera" / "dag" / "registry_stale_core.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _arts():
    return {"data.csv": {"rel": "data.csv", "content": "a,b\n1,2\n"},
            "report.md": {"rel": "report.md", "content": "# R\n"},
            "big.bin": {"rel": "big.bin", "content": ""}}


def test_a_script_naming_a_file_forgets_it():
    a = _arts()
    got = R.forget_named(a, "exec.python.run", {"code": "open('data.csv','w').write('x')"})
    assert got == ["data.csv"] and a["data.csv"]["content"] == "" and a["report.md"]["content"]


def test_reads_and_unrelated_calls_forget_nothing():
    a = _arts()
    assert R.forget_named(a, "sandbox.session.fs.read", {"path": "data.csv"}) == []
    assert R.forget_named(a, "web.fetch", {"url": "https://x/report.md"}) == []
    assert R.forget_named(a, "exec.bash.run", {"command": "ls"}) == []
    assert all(a[k]["content"] for k in ("data.csv", "report.md"))


def test_write_caps():
    assert R.can_write("exec.bash.run") and R.can_write("sandbox.session.fs.write")
    assert not R.can_write("code.read")


def test_the_loop_forgets_after_a_write_call():
    assert SRC.count("_registry_stale_core.forget_named(artifacts, ") >= 1
