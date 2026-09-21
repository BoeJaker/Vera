"""code.explode's extractor (vera/research/code_explode_core.py, EXPLODE.md §7.2): files and classes as groups,
symbols as cards with exact spans, every edge with a resolution — exact by name / import / self, heuristic by bare
name, external to a stub — from three engines that each sign their cards. And the capability over it: repo files
(never outside the repo), one hop of imports, a snippet, a record."""
import asyncio
import os

import pytest

from vera.research import code_explode_core as C
from vera.research import explode_capabilities as X

PKG_A = '''"""agents"""
import os
from typing import Dict
import httpx
from vera.research.nlp_capabilities import nlp_ner
from vera.research import nlp_dispatch_core

class BaseAgent:
    def start(self):
        return self.name

class Agent(BaseAgent):
    """the agent"""
    @capability("agent.run")
    async def run_stream(self, prompt: str) -> Dict:
        out = await self._offload(prompt)
        cap_nlp_ner(prompt)
        return out

    async def _offload(self, text):
        return await nlp_ner(text)

async def cap_nlp_ner(text):
    ents = nlp_ner(text)
    node, why = nlp_dispatch_core.route("ner")
    len(ents)
    return httpx.get("http://x")

def unused_helper():
    try:
        from vera.research import code_explode_core
    except ImportError:
        pass
    return code_explode_core.detect_lang("a.py")
'''
PKG_B = '''async def nlp_ner(text):
    return _dispatch(text)

async def _dispatch(text):
    return chunk_text(text)

def chunk_text(t):
    return [t]
'''
PKG_C = '''def route(task):
    return ("gpu-250", "least loaded")
'''


def _srcs():
    return [{"path": "vera/agents/agents.py", "text": PKG_A}, {"path": "vera/research/nlp_capabilities.py", "text": PKG_B},
            {"path": "vera/research/nlp_dispatch_core.py", "text": PKG_C}]


def _by(d):
    return {c["id"]: c for c in d["cards"]}


def _edges(d, kind=None):
    by = _by(d)
    return [(by[e["from"]]["title"], by[e["to"]]["title"], e["resolution"], e.get("label", "")) for e in d["edges"] if not kind or e["kind"] == kind]


def test_python_symbols_have_exact_spans_and_the_groups_are_files_and_classes():
    d = C.explode_sources(_srcs())
    assert d["ok"] and d["kind"] == "code" and d["source"]["engines"] == ["ast"] and d["source"]["partial"] is False
    gids = [g["id"] for g in d["groups"]]
    assert "vera/agents/agents.py" in gids and "vera/agents/agents.py::Agent" in gids and "vera/agents/agents.py::BaseAgent" in gids and "ext" in gids
    agent = next(g for g in d["groups"] if g["id"] == "vera/agents/agents.py::Agent")
    assert agent["parent"] == "vera/agents/agents.py" and agent["kind"] == "class"
    by = _by(d)
    rs = by["vera/agents/agents.py::Agent.run_stream"]
    assert rs["kind"] == "method" and rs["group"] == "vera/agents/agents.py::Agent" and rs["by"] == "ast"
    body = PKG_A[rs["span"]["start"]:rs["span"]["end"]]
    assert body.startswith("@capability") and "return out" in body and rs["span"]["line"] == 15
    assert rs["subtitle"].startswith("async ·") and any(f["k"] == "decorators" and "@capability" in f["v"] for f in rs["fields"])
    assert any(f["k"] == "args" and f["v"] == "prompt" for f in rs["fields"])
    fn = by["vera/agents/agents.py::cap_nlp_ner"]
    assert fn["kind"] == "function" and fn["group"] == "vera/agents/agents.py" and PKG_A[fn["span"]["start"]:fn["span"]["end"]].startswith("async def cap_nlp_ner")
    mod = by["vera/agents/agents.py::<module>"]
    assert mod["kind"] == "module" and any(f["k"] == "stdlib" and "os" in f["v"] and "typing" in f["v"] for f in mod["fields"])
    assert "os" not in [c["title"] for c in d["cards"] if c["kind"] == "external"], "the standard library is a field, not a stub"


def test_calls_resolve_exact_by_name_import_and_self_heuristic_by_bare_name_external_to_a_stub():
    d = C.explode_sources(_srcs())
    calls = _edges(d, "CALLS")
    assert ("run_stream", "_offload", "exact", "") in calls, "self.method() inside its class is exact"
    assert ("run_stream", "cap_nlp_ner", "exact", "") in calls, "a function of the same file is exact"
    assert ("_offload", "nlp_ner", "exact", "") in calls, "an imported name resolved into a parsed file is exact"
    assert ("cap_nlp_ner", "route", "exact", "") in calls, "module.fn() through an imported module is exact"
    assert ("nlp_ner", "_dispatch", "exact", "") in calls and ("_dispatch", "chunk_text", "exact", "") in calls
    assert ("cap_nlp_ner", "httpx", "external", "") in calls, "an imported package outside the parsed set is a stub"
    assert not any(a == "cap_nlp_ner" and b == "len" for a, b, _, _ in calls), "builtins are not calls the diagram is about"
    assert ("unused_helper", "detect_lang", "heuristic", "by name") not in calls   # nothing named detect_lang in the parsed set → nothing invented
    inh = _edges(d, "INHERITS")
    assert ("Agent", "BaseAgent", "exact", "") in inh
    imps = _edges(d, "IMPORTS")
    assert ("agents.py", "nlp_capabilities.py", "exact", "nlp_ner") in imps and ("agents.py", "nlp_dispatch_core.py", "exact", "nlp_dispatch_core") in imps
    assert ("agents.py", "httpx", "external", "") in imps
    by = _by(d)
    assert "external:1" in by["vera/agents/agents.py::cap_nlp_ner"]["badges"]
    layers = {l["id"]: l for l in d["layers"]}
    assert layers["code.ast"]["count"] == len([c for c in d["cards"] if c["by"] == "ast"]) and layers["code.contains"]["on"] is False and layers["code.calls"]["on"]
    ks = {a["key"]: a for a in d["assessments"]}
    assert ks["syntax"]["score"] == 1.0 and 0 < ks["resolved"]["score"] <= 1


def test_broken_python_is_partial_and_says_so_without_tree_sitter():
    d = C.explode_sources([{"path": "x.py", "text": "def ok():\n    return 1\n\ndef broken(:\n    pass\n"}])
    assert d["source"]["partial"] is True and d["source"]["errors"] and d["assessments"][0]["score"] == 0.0
    assert d["source"]["tree_sitter"] is C.HAS_TREE_SITTER
    if not C.HAS_TREE_SITTER:
        assert [c["kind"] for c in d["cards"]] == ["module"], "ast recovers nothing from broken code; tree-sitter would"


@pytest.mark.skipif(not C.HAS_TREE_SITTER, reason="tree_sitter_language_pack not installed — proved in an ephemeral container")
def test_tree_sitter_reads_broken_python_and_javascript():
    d = C.explode_sources([{"path": "x.py", "text": "import os\n\ndef ok(a):\n    return helper(a)\n\ndef broken(:\n    pass\n\ndef helper(a):\n    return a\n"}])
    titles = {c["title"] for c in d["cards"]}
    assert d["source"]["engines"] == ["tree-sitter"] and "ok" in titles and "helper" in titles and d["source"]["partial"] is True
    assert ("ok", "helper", "exact", "") in _edges(d, "CALLS")
    ok = next(c for c in d["cards"] if c["title"] == "ok")
    assert "def ok(a)" in d["source"]["text"][ok["span"]["start"]:ok["span"]["end"]] if isinstance(d["source"].get("text"), str) else True
    j = C.explode_sources([{"path": "a.js", "text": "import { b } from './b.js';\nexport class Widget extends Base {\n  draw() { return this.paint(b()); }\n  paint(x) { return x }\n}\nconst go = async () => { new Widget().draw( };\n"}])
    jt = {c["title"] for c in j["cards"]}
    assert j["source"]["engines"] == ["tree-sitter"] and "Widget" in jt and "draw" in jt and "paint" in jt and "go" in jt
    assert ("draw", "paint", "exact", "") in _edges(j, "CALLS")


def test_javascript_css_and_html_through_patterns_and_a_page_joins_selectors_to_elements():
    js = "import { helper } from './lib.js';\nconst api = require('./api.js');\n\nexport class Panel extends Base {\n  constructor(el) { this.el = el; }\n  render() {\n    const rows = helper(this.data);\n    return this.paint(rows);\n  }\n  paint(rows) { return api.draw(rows); }\n}\n\nexport async function boot() {\n  const p = new Panel(document.body);\n  p.render();\n  return fetch('/x');\n}\nconst tick = () => boot();\n"
    lib = "export function helper(d) { return d.map(x => x); }\n"
    css = "/* the panel */\n.panel { display: flex; gap: 4px }\n#stage .row:hover { color: red }\n@media (max-width: 600px) { .panel { flex-direction: column } }\nbody { margin: 0 }\n"
    html = "<!doctype html><html><head><link rel='stylesheet' href='panel.css'><script src='panel.js'></script></head><body>\n<div id='stage' class='panel wide'>\n  <div class='row'>first row</div>\n  <section id='notes'>notes here</section>\n</div></body></html>"
    d = C.explode_sources([{"path": "ui/panel.js", "text": js}, {"path": "ui/lib.js", "text": lib}, {"path": "ui/panel.css", "text": css}, {"path": "ui/index.html", "text": html}])
    # JS reads through tree-sitter when it is installed, through patterns otherwise — the checks below hold for both
    assert d["kind"] == "page" and d["source"]["engines"] == (["patterns", "tree-sitter"] if C.HAS_TREE_SITTER else ["patterns"]) and d["source"]["partial"] is False
    by = _by(d)
    panel = by["ui/panel.js::Panel"]
    ptxt = js[panel["span"]["start"]:panel["span"]["end"]]
    assert panel["kind"] == "class" and ptxt.startswith(("export class Panel", "class Panel")) and ptxt.rstrip().endswith("}")   # tree-sitter starts at `class`, the export keyword being its parent's
    render = by["ui/panel.js::Panel.render"]
    assert render["kind"] == "method" and render["group"] == "ui/panel.js::Panel" and js[render["span"]["start"]:render["span"]["end"]].lstrip().startswith("render()")
    assert by["ui/panel.js::boot"]["kind"] == "function" and by["ui/panel.js::boot"]["subtitle"].startswith("async") and by["ui/panel.js::tick"]["kind"] == "function"
    calls = _edges(d, "CALLS")
    assert ("render", "paint", "exact", "") in calls and ("render", "helper", "exact", "") in calls and ("tick", "boot", "exact", "") in calls
    assert ("Panel", "Base", "external", "") in _edges(d, "INHERITS")
    assert ("panel.js", "lib.js", "exact", "helper") in _edges(d, "IMPORTS")
    assert not any(a == "boot" and b == "fetch" for a, b, _, _ in calls), "fetch / document are the platform, not symbols"
    # css rules are cards, media-query rules included; html elements with an id or class are cards
    sel_cards = [c for c in d["cards"] if c["kind"] == "selector"]
    sels = {c["title"]: c for c in sel_cards}
    assert set(sels) == {".panel", "#stage .row:hover", "body"} and [c["title"] for c in sel_cards].count(".panel") == 2, "the @media rule is a rule of its own"
    assert [c for c in sel_cards if c["title"] == ".panel"][0]["subtitle"] == "rule · 2 declarations" and css[sels["body"]["span"]["start"]:sels["body"]["span"]["end"]] == "body { margin: 0 }"
    els = {c["title"]: c for c in d["cards"] if c["kind"] == "element"}
    assert "<div#stage.panel.wide>" in els and "<div.row>" in els and "<section#notes>" in els and any(f["v"] == "first row" for f in els["<div.row>"]["fields"])
    assert els["<div.row>"]["group"] == "ui/index.html"
    m = _edges(d, "MATCHES")
    assert (".panel", "<div#stage.panel.wide>", "heuristic", "") in m and ("#stage .row:hover", "<div.row>", "heuristic", "") in m
    assert ("index.html", "panel.css", "exact", "") in _edges(d, "IMPORTS") and ("index.html", "panel.js", "exact", "") in _edges(d, "IMPORTS")
    assert {l["id"] for l in d["layers"]} >= {"code.patterns", "code.calls", "code.imports", "page.css"}
    assert render["by"] == ("tree-sitter" if C.HAS_TREE_SITTER else "patterns") and sels["body"]["by"] == "patterns"


def test_detect_lang_and_module_names():
    assert C.detect_lang("a/b.py") == "python" and C.detect_lang("x.tsx") == "typescript" and C.detect_lang("", "<html><body>") == "html"
    assert C.detect_lang("", "def f():\n  pass") == "python" and C.detect_lang("", "const x = () => 1") == "javascript" and C.detect_lang("", ".a { color: red }") == "css"
    assert C.module_names("vera/research/nlp_capabilities.py") == ["vera.research.nlp_capabilities", "research.nlp_capabilities", "nlp_capabilities", "Vera.vera.research.nlp_capabilities"]
    assert C.module_names("pkg/__init__.py") == ["pkg"]
    assert C.import_targets([{"module": "vera.research.nlp_capabilities", "name": "nlp_ner"}, {"module": "./lib.js"}, {"module": ".sibling", "name": ""}], "vera/agents/agents.py")[:2] == ["vera/research/nlp_capabilities.py", "vera/research/nlp_capabilities/__init__.py"]
    assert "vera/agents/lib.js" in C.import_targets([{"module": "./lib.js"}], "vera/agents/agents.py") and "vera/agents/sibling.py" in C.import_targets([{"module": ".sibling"}], "vera/agents/agents.py")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_the_capability_reads_repo_files_pulls_one_hop_of_imports_and_never_leaves_the_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "vera" / "agents").mkdir(parents=True); (root / "vera" / "research").mkdir(parents=True)
    (root / "vera" / "agents" / "agents.py").write_text(PKG_A, encoding="utf-8")
    (root / "vera" / "research" / "nlp_capabilities.py").write_text(PKG_B, encoding="utf-8")
    (root / "vera" / "research" / "nlp_dispatch_core.py").write_text(PKG_C, encoding="utf-8")
    (root / "vera" / "research" / "code_explode_core.py").write_text("def detect_lang(p):\n    return 'python'\n", encoding="utf-8")
    monkeypatch.setattr(X, "_repo_root", lambda: str(root))
    d = _run(X.explode_code(path="vera/agents/agents.py"))
    assert d["ok"] and set(d["source"]["paths"]) == {"vera/agents/agents.py", "vera/research/nlp_capabilities.py", "vera/research/nlp_dispatch_core.py", "vera/research/code_explode_core.py"}, "the seed and the repo files it imports, once"
    assert ("unused_helper", "detect_lang", "exact", "") in _edges(d, "CALLS"), "an import inside a function's try: resolves too"
    assert isinstance(d["source"]["text"], dict) and "vera/agents/agents.py" in d["source"]["text"]
    d0 = _run(X.explode_code(path="vera/agents/agents.py", depth=0))
    assert d0["source"]["paths"] == ["vera/agents/agents.py"] and ("_offload", "nlp_ner", "external", "") in _edges(d0, "CALLS"), "without the hop, our own unparsed module's name is a stub"
    stub = next(c for c in d0["cards"] if c["kind"] == "external" and c["title"] == "nlp_ner")
    assert stub["subtitle"] == "vera.research.nlp_capabilities · not in the parsed set"
    assert ("cap_nlp_ner", "nlp_dispatch_core", "external", "") in _edges(d0, "CALLS"), "module.fn() through an unparsed module of our own: the stub is the module called through"
    assert ("cap_nlp_ner", "httpx", "external", "") in _edges(d0, "CALLS")
    dd = _run(X.explode_code(path="vera/research", depth=0))
    assert sorted(dd["source"]["paths"]) == ["vera/research/code_explode_core.py", "vera/research/nlp_capabilities.py", "vera/research/nlp_dispatch_core.py"], "a directory lists its code files"
    out = _run(X.explode_code(path="../../etc/passwd"))
    assert "error" in out and out["skipped"][0]["why"] == "outside the repo"
    snip = _run(X.explode_code(text="def f():\n    return g()\n\ndef g():\n    return 1\n", lang="py"))
    assert snip["ok"] and snip["source"]["label"] == "pasted py" and ("f", "g", "exact", "") in _edges(snip, "CALLS") and snip["source"]["text"].startswith("def f")
    assert "required" in _run(X.explode_code())["error"]
