"""One data_fabric module object whatever name imports it (11 Oct 2026: a full-name import executed the file a second
time, whose schedule("fabric_startup") replaced the loader copy's by name, so sys.modules["data_fabric"] - used by 40+
call sites, memory.seek among them - never connected Postgres or Chroma)."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_data_fabric_aliases_itself_before_anything_else_runs():
    src = _src("vera/fabric/data_fabric.py")
    alias = 'for _alias in ("data_fabric", "Vera.vera.fabric.data_fabric"):\n    sys.modules.setdefault(_alias, sys.modules[__name__])'
    assert alias in src
    assert src.index(alias) < src.index("from Vera.vera.config import cfg")
    assert src.index(alias) < src.index('schedule(_startup, interval=999999, name="fabric_startup")')


def test_training_state_aliases_itself_too():
    # loop_ledger and discovery import it by full name at call time; the loader loads it as `training_state`
    src = _src("vera/minds/training_state.py")
    alias = ('for _alias in ("training_state", "Vera.vera.minds.training_state"):\n'
             '    sys.modules.setdefault(_alias, sys.modules[__name__])')
    assert alias in src
    assert src.index(alias) < src.index("import Vera.vera.capability_orchestration as _orch")
    assert src.index(alias) < src.index("schedule(_retention_job")


def test_the_loader_skips_a_name_already_in_sys_modules():
    src = _src("vera/capability_orchestration.py")
    i = src.index("for _fpath in _module_files:")
    loop = src[i:i + 600]
    assert "if _mod_name in sys.modules:" in loop and loop.index("if _mod_name in sys.modules:") < loop.index(
        "spec_from_file_location")


def test_four_minds_modules_never_import_a_second_copy():
    for rel in ("vera/fabric/page_search.py", "vera/fabric/known_item.py"):
        src = _src(rel)
        assert "import Vera.vera.fabric.data_fabric" not in src, rel
        assert 'sys.modules.get("data_fabric") or sys.modules.get("Vera.vera.fabric.data_fabric")' in src, rel


def test_the_alias_pattern_keeps_one_object(tmp_path, monkeypatch):
    # the same two lines, run as a module loaded under one name and then imported under the other
    pkg = tmp_path / "pkga"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "fab.py").write_text("import sys\nRUNS = []\nRUNS.append(1)\n"
                                "for _alias in ('fab', 'pkga.fab'):\n    sys.modules.setdefault(_alias, sys.modules[__name__])\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    for k in ("fab", "pkga", "pkga.fab"):
        monkeypatch.delitem(sys.modules, k, raising=False)
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("fab", str(pkg / "fab.py"))
    mod = ilu.module_from_spec(spec)
    sys.modules["fab"] = mod
    spec.loader.exec_module(mod)
    import pkga.fab as again                                  # the full-name import a module would do
    assert again is mod and mod.RUNS == [1]
