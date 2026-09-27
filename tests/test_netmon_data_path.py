"""netmon and the netmap fallback find their SQLite file without calling .get on VeraConfig (an object, not a dict) -
the call raised on every netmon capability from 20 Jul 2026."""
import ast
import os

import pytest

pytestmark = pytest.mark.critical
ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.mark.parametrize("rel", ["vera/netmon/netmon_capabilities.py", "vera/execution/exec_capabilities.py"])
def test_no_dict_get_on_the_config_object(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src)
    bad = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "get" and isinstance(n.func.value, ast.Name) and n.func.value.id == "cfg"
           and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "VERA_DATA_DIR"]
    assert not bad, f"{rel}: cfg.get('VERA_DATA_DIR') at {bad}"
    assert 'os.getenv("VERA_DATA_DIR") or (Path.home() / ".vera")' in src
