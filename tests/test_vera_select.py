"""One way to pick from a list: vera-select.js enhances a plain field by
attribute - search, filter, tick, chips, typed values only where allowed - and
writes back what the field always held. The list-like inputs carry it."""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

# every field that takes the control: (panel, id, source)
FIELDS = [
    ("chat/chat_panel.html", "loopBaseCaps", "caps"), ("chat/chat_panel.html", "loopLongCaps", "caps"),
    ("chat/chat_panel.html", "loopSkillAllow", "skills"), ("chat/chat_panel.html", "loopSkillDeny", "skills"),
    ("dag/dag_workshop_panel.html", "alAllowed", "caps"), ("dag/dag_workshop_panel.html", "alBaseCaps", "caps"),
    ("dag/dag_workshop_panel.html", "alLongRunCaps", "caps"), ("dag/dag_workshop_panel.html", "bTags", "static"),
    ("evolve/evolve_panel.html", "tk-caps", "caps"), ("evolve/evolve_panel.html", "tk-tags", "static"),
    ("evolve/evolve_panel.html", "bn-labels", "static"), ("evolve/evolve_panel.html", "ct-model", "models"),
    ("evolve/evolve_panel.html", "st-editq-model", "models"), ("evolve/evolve_panel.html", "cs-target", "instances"),
    ("ide/ide_inspect_panel.html", "bp-caps", "caps"), ("ui builder/ui_builder_panel.html", "pb_caps", "caps"),
    ("dream/dream_pipelines_panel.html", "f-stages", "caps"),
    ("agents/agent_panel.html", "f-quick_opener_model", "models"), ("agents/agent_panel.html", "f-memory_tags", "static"),
    ("agents/agent_panel.html", "f-stop", "static"), ("calendar/calendar_panel.html", "set-model", "models"),
    ("email/email_panel.html", "s-model", "models"), ("workers/workers_ollama_panel.html", "mt-model", "models"),
    ("workers/workers_ollama_panel.html", "emb-model", "models"), ("workers/workers_ollama_panel.html", "mt-tags", "static"),
    ("workers/workers_ollama_panel.html", "conn-f-tags", "tags"), ("workers/workers_ollama_panel.html", "sbx-cfg-host", "docker-hosts"),
    ("routing_panel.html", "v8-agent", "agents"), ("telegram/telegram_panel.html", "cfg-agent", "agents"),
    ("telegram/telegram_panel.html", "ev-types", "static"), ("openclaw/openclaw_panel.html", "agent-id-input", "agents"),
    ("openclaw/openclaw_panel.html", "cfg-agent-id", "agents"), ("ide/vscode_panel.html", "dep-host", "docker-hosts"),
    ("execution/exec_panel.html", "f-tags", "tags"), ("execution/netmap_panel.html", "mon-cidrs", "static"),
    ("foundry/foundry_panel.html", "pxePFeats", "features"), ("operator/operator_studio_panel.html", "allowlist", "static"),
    ("ontologies/ontologies_panel.html", "f-tags", "static"), ("ontologies/ontologies_panel.html", "b-tags", "static"),
    ("skills/skills_panel.html", "f-tags", "static"), ("skills/skills_panel.html", "b-tags", "static"),
    ("fabric/fabric_panel.html", "fsSrcTags", "tags"), ("fabric/fabric_panel.html", "skTags", "tags"),
    ("fabric/fabric_panel.html", "obTags", "tags"), ("fabric/fabric_panel.html", "tsManTags", "tags"),
    ("fabric/fabric_panel.html", "stealthTags", "tags"), ("fabric/fabric_panel.html", "iotSerialTags", "tags"),
]


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_control_is_served_and_knows_the_estates_sources():
    caps = read("vera", "ui builder", "ui_capabilities.py")
    assert caps.count('@APP.get("/ui/vera-select.js"') == 1 and 'Path(__file__).parent.parent / "vera-select.js"' in caps
    js = read("vera", "vera-select.js")
    for src in ("caps", "models", "logins", "machines", "docker-hosts", "instances", "agents", "skills", "features", "static", "url", "tags"):
        assert f"source('{src}'," in js, src
    assert "'/mcp/tools'" in js and "'/agents/models'" in js and "veraEstate.logins()" in js
    for attr in ("data-select-single", "data-select-new", "data-select-sep", "data-select-prefix", "data-select-url", "data-select-options"):
        assert attr.replace("data-select-", "A('") in js or attr in js, attr
    assert "new Event('input', {bubbles: true})" in js and "new Event('change', {bubbles: true})" in js, "handlers see a typed edit"
    assert "MutationObserver" in js, "rows a panel renders later get it too"
    assert "Object.defineProperty(el, 'value'" in js, "a value set from code shows as chips"


def test_every_list_like_field_carries_the_control_and_its_page_loads_it():
    for panel, fid, src in FIELDS:
        s = read("vera", *panel.split("/"))
        m = re.search(r"<(?:input|textarea)[^>]*\bid=\"" + re.escape(fid) + r"\"[^>]*>", s)
        assert m, (panel, fid)
        assert f'data-select="{src}"' in m.group(0), (panel, fid, m.group(0)[:120])
        assert s.count('<script src="/ui/vera-select.js"></script>') == 1, panel
    # the typed-value escape hatch only where free text is legitimate
    for panel, fid, src in FIELDS:
        s = read("vera", *panel.split("/"))
        tag = re.search(r"<(?:input|textarea)[^>]*\bid=\"" + re.escape(fid) + r"\"[^>]*>", s).group(0)
        if src in ("caps", "skills", "features", "instances"):
            assert (src == "skills") == ("data-select-new" in tag), (panel, fid, "caps/features/instances are closed lists")
    vs = read("vera", "ide", "vscode_panel.html")
    assert '<script src="/ui/vera-estate.js"></script>' in vs, "docker-hosts reads the one list"


@needs_node
def test_pluck_walks_arrays_and_maps():
    js = read("vera", "vera-select.js")
    harness = ("var window = {}; var document = {readyState:'complete', getElementById(){return null}, querySelectorAll(){return []}, body:null, "
               "addEventListener(){}, createElement(){return {style:{},setAttribute(){},appendChild(){},addEventListener(){}}}, head:{appendChild(){}}};\n"
               "var fetch = () => Promise.resolve({json: () => Promise.resolve({})});\n" + js +
               "\nconst P=window.veraSelect.pluck; console.log(JSON.stringify([P({hosts:[{tags:['a','b']},{tags:['b','c']},{}]},'hosts[].tags[]'),"
               "P({features:[{id:'x'},{id:'y'}]},'features[].id'), P({instances:{a:{models:['m1']},b:{models:['m2']}}},'instances.*.models[]'), P({x:{y:'z'}},'x.y')]));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness); path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:600]
        out = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
    finally:
        os.unlink(path)
    assert out == '[["a","b","b","c"],["x","y"],["m1","m2"],["z"]]'
