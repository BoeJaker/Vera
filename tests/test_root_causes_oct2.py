"""Census 2026-10-02 root causes: generated Python modules ran code at import (the
web-page 'render on load' rule reached Python), and operator.test.run - pytest in
Vera's own repo - failed silently for a loop's project."""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

_spec = importlib.util.spec_from_file_location("rfr", ROOT / "vera" / "dag" / "result_failure_reason.py")
RFR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RFR)


def test_browser_rules_only_for_web_files_and_the_import_rule_for_python():
    i = SRC.index("async def cap_code_author(")
    body = SRC[i:i + 20000]
    assert "IF A BROWSER WILL CHECK IT" not in body                     # moved out of the prompt
    assert "_CODE_AUTHOR_BROWSER_RULES if lang in _CODE_AUTHOR_WEB_LANGS" in body
    assert '_CODE_AUTHOR_PY_MODULE_RULE if lang == "python"' in body
    consts = SRC[:i]
    assert "IF A BROWSER WILL CHECK IT" in consts and "RENDER STATE ON LOAD" in consts
    assert "if __name__ ==" in consts.split("_CODE_AUTHOR_PY_MODULE_RULE = (", 1)[1][:800]


def test_operator_test_run_is_never_offered_to_a_loop():
    deny = SRC[SRC.index("_V5_LOOP_DENYLIST = {"):]
    deny = deny[:deny.index("}") + 1]
    assert '"operator.test.run"' in deny


def test_err_and_out_fields_are_read_not_reported_as_no_detail():
    res = {"ok": False, "code": 4, "out": "", "err": "ERROR: file or directory not found: statkit"}
    assert RFR.failure_reason(res, 4) == "ERROR: file or directory not found: statkit"
    res2 = {"ok": False, "code": 1, "out": "FAILED test_stats.py::test_mode", "err": ""}
    assert "FAILED test_stats.py" in RFR.failure_reason(res2, 1)
