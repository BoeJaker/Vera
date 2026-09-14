"""
The widget catalogue (UI redesign m1 foundations; the WidgetSpec board §3): the
sources built from the live capability registry (a shape read off a
capability's name, a hand list for the well-known reads, the streams), and the
four capabilities a renderer or editor asks - widget.forms, widget.sources,
widget.validate, widget.render_spec. The module runs against a stub
orchestrator with a few fake capabilities registered. The wiring (the module
files, the libraries' routes, register_ui's sections/options) is held
text-level.
"""
import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load():
    orch = types.ModuleType("Vera.vera.capability_orchestration")
    orch.CAPABILITY_REGISTRY = {}

    def capability(name, **kw):
        def deco(fn):
            orch.CAPABILITY_REGISTRY[name] = {"func": fn, "raw": fn, "meta": kw, "schema": kw.get("schema"), "description": kw.get("description", ""), "http_method": kw.get("http_method")}
            return fn
        return deco

    class _App:
        def get(self, *a, **k): return lambda fn: fn
        def post(self, *a, **k): return lambda fn: fn

    async def sysmon_history(node="", window="1h", trace_id=None): return {}
    async def sysmon_status(node="", trace_id=None): return {}
    async def code_write(path="", text="", trace_id=None): return {}
    async def jobs_list(limit=50, trace_id=None): return {}
    async def foo_bar(trace_id=None): return {}
    async def ollama_instances(trace_id=None): return {"gpu-250": {"url": "x", "status": "ok"}, "cpu-246": {"url": "y", "status": "ok"}}
    async def obs_workers(trace_id=None): return {"w1": {"host": "ct126", "state": "busy"}, "w2": {"host": "ct121", "state": "idle"}}
    async def fabric_sources_add(url="", trace_id=None): return {"id": "x"}
    async def proxmox_guests(cluster="", limit=50, trace_id=None): return {"ok": True, "guests": [{"vmid": 100, "name": "vera", "status": "running"}]}
    async def mesh_topology(trace_id=None): return {"nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "a"}]}
    orch.CAPABILITY_REGISTRY.update({
        "obs.workers": {"func": obs_workers, "raw": obs_workers, "description": "Every worker with its host and state.", "http_method": "GET", "schema": {"type": "object", "properties": {}, "required": []}},
        "ollama.instances": {"func": ollama_instances, "raw": ollama_instances, "description": "Every Ollama instance with its models and load.", "http_method": "GET"},
        "fabric.sources.add": {"func": fabric_sources_add, "raw": fabric_sources_add, "description": "Add a source.", "http_method": "POST", "schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": []}},
        "proxmox.guests": {"func": proxmox_guests, "raw": proxmox_guests, "description": "The guests of a cluster: vmid, name, status. Inputs: cluster, limit.", "schema": {"type": "object", "properties": {"cluster": {"type": "string", "description": "the cluster id"}, "limit": {"type": "integer", "default": 50}}, "required": ["cluster"]}},
        "mesh.topology": {"func": mesh_topology, "raw": mesh_topology, "description": "The mesh as nodes and edges.", "http_method": "GET"},
        "sysmon.history": {"func": sysmon_history, "meta": {"description": "cpu / ram / gpu series per node"}},
        "sysmon.status": {"func": sysmon_status, "meta": {"description": "a snapshot"}},
        "code.write": {"func": code_write, "meta": {"description": "write a file"}},
        "jobs.list": {"func": jobs_list, "meta": {"description": "the jobs"}},
        "foo.bar": {"func": foo_bar, "meta": {"description": "nothing to tell"}},
    })
    orch.APP = _App(); orch.capability = capability
    pkg = types.ModuleType("Vera"); pkg.__path__ = []
    sub = types.ModuleType("Vera.vera"); sub.__path__ = []
    sys.modules["Vera"] = pkg; sys.modules["Vera.vera"] = sub; sys.modules["Vera.vera.capability_orchestration"] = orch

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

    c = load("widget_catalog_under_test", ("vera", "widgets", "widget_catalog.py"))
    return c, orch


def test_widget_forms_filters_by_board_and_carries_the_names():
    all_ = _run(C.widget_forms())
    assert all_["ok"] and all_["count"] >= 110 and all_["boards"]["widgets"] >= 60 and all_["boards"]["motion"] >= 25 and all_["boards"]["iso"] >= 20
    assert all(f.get("name") and isinstance(f.get("boards"), list) for f in all_["forms"])
    iso = _run(C.widget_forms(board="iso"))
    assert iso["count"] == all_["boards"]["iso"] and all("iso" in f["boards"] for f in iso["forms"])
    q = _run(C.widget_forms(q="thermometers"))
    assert [f["id"] for f in q["forms"]] == ["thermo"]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


C, ORCH = _load()


def test_a_capabilitys_name_says_its_shape_and_a_write_is_never_a_source():
    assert C.guess_shape("sysmon.history") == "series"
    assert C.guess_shape("jobs.list") == "items"
    assert C.guess_shape("obs.health") == "values"
    assert C.guess_shape("obs.pending") == "level"
    assert C.guess_shape("markets.bars") == "ohlcv"
    assert C.guess_shape("code.write") == "" and C.guess_shape("exec.code.run") == "" and C.guess_shape("canvas.create") == ""
    assert C.guess_shape("foo.bar") == "", "no telling from the name"


def test_sources_come_from_the_live_registry_plus_the_hand_list_and_the_streams():
    src = C.sources()
    ids = {s["id"]: s for s in src}
    assert "sysmon.history" in ids and ids["sysmon.history"]["shape"] == "series" and ids["sysmon.history"]["refresh_min"] == "5s"
    assert ids["sysmon.history"]["args"] == ["node", "window"], "the capability's own arguments"
    assert "jobs.list" in ids and ids["jobs.list"]["note"] == "shape from the name"
    assert "code.write" not in ids and "foo.bar" not in ids
    assert ids["obs.node_temps"]["unit"] == "°C" and ids["obs.node_temps"]["note"] == "not registered here"
    assert ids["stream:events"]["shape"] == "events" and ids["stream:events"]["refresh_min"] == "live"
    assert [s["id"] for s in C.sources(shape="series")] and all(s["shape"] == "series" for s in C.sources(shape="series"))
    assert all("sysmon" in s["id"] for s in C.sources(q="sysmon"))
    assert C.source("panel:system-monitor")["shape"] == "panel" and C.source("nope") is None


def test_widget_forms_lists_the_catalogue_and_filters_by_shape():
    r = _run(C.widget_forms())
    assert r["ok"] and r["count"] >= 66 and "sizes" in r and r["sizes"]["xl"].startswith("a panel")
    s = _run(C.widget_forms(shape="series"))
    assert s["count"] and all(f["shape"] == "series" for f in s["forms"]) and any(f["id"] == "trace" for f in s["forms"])
    q = _run(C.widget_forms(q="heat"))
    assert [f["id"] for f in q["forms"]] == ["heat"]


def test_widget_sources_capability():
    r = _run(C.widget_sources(shape="items", limit=5))
    assert r["ok"] and len(r["sources"]) <= 5 and r["count"] >= 5 and all(s["shape"] == "items" for s in r["sources"])


# ── the derived source registry (defect 57): hand > measured > declared, domains, params, the never-words, redis.* ──
def test_the_source_registry_is_derived_from_the_capability_registry():
    S = C._src
    src = {s["id"]: s for s in C.sources(refresh=True)}
    # a quiet read with no hand entry and a measured envelope: the measured shape, its container as the map, the tier says so
    assert "ollama.instances" in src and src["ollama.instances"]["tier"] == "measured" and src["ollama.instances"]["shape"] == "items" and src["ollama.instances"]["map"] == {"rows": "$"}
    assert src["obs.workers"]["tier"] == "hand" and src["obs.workers"]["map"] == {"rows": "$"}      # the hand shape wins; the measured map still rides along
    # a write is never a source, whatever its group ("add" is a never-word)
    assert "fabric.sources.add" not in src and "code.write" not in src and "foo.bar" not in src
    # the declared tier: the shape from the name, the params from the declared schema (required flagged, default kept, the description)
    assert src["proxmox.guests"]["tier"] == "declared" and src["proxmox.guests"]["shape"] == "items" and src["proxmox.guests"]["domain"] == "Proxmox"
    assert src["proxmox.guests"]["required"] == ["cluster"] and [p["name"] for p in src["proxmox.guests"]["params"]] == ["cluster", "limit"] and src["proxmox.guests"]["params"][1]["default"] == 50
    assert src["proxmox.guests"]["desc"].startswith("The guests of a cluster")
    assert src["mesh.topology"]["shape"] == "graph" and src["mesh.topology"]["domain"] == "Mesh"
    # the hand list still wins and the streams ride along
    assert src["sysmon.history"]["tier"] == "hand" and src["sysmon.history"]["shape"] == "series" and src["stream:events"]["domain"] == "Streams"
    # the redis.* read family registered itself and is a source (the redis group reads)
    for cap in ("redis.info", "redis.keys", "redis.get", "redis.stream.tail"):
        assert cap in ORCH.CAPABILITY_REGISTRY and cap in src and src[cap]["domain"] == "Redis", cap
    assert src["redis.keys"]["shape"] == "items" and src["redis.stream.tail"]["shape"] == "events" and src["redis.info"]["shape"] == "values"
    assert src["redis.get"]["required"] == ["key"]
    # the capability answers the counts by domain, shape and tier, and filters by domain
    r = _run(C.widget_sources(domain="proxmox"))
    assert r["ok"] and r["domains"]["Proxmox"] >= 1 and r["tiers"]["measured"] >= 1 and r["shapes"]["items"] >= 1 and all(s["domain"] == "Proxmox" for s in r["sources"]) and r["total"] >= r["count"]
    # the never-words and the read words
    assert S.is_read("proxmox.guests") and S.is_read("obs.anything") and S.is_read("redis.keys") and not S.is_read("markets.alerts.ack") and not S.is_read("mesh.forget") and not S.is_read("fabric.sources.add") and not S.is_read("evolve.sandbox.status")
    # the cache: a second call is the cached list; refresh re-derives
    a = C._src.cached_at(); C.sources(); assert C._src.cached_at() == a; C.sources(refresh=True); assert C._src.cached_at() >= a


def test_shape_of_reads_an_envelope_the_way_the_element_does():
    S = C._src
    assert S.shape_of({"value": 3, "max": 10}) == ("level", {})
    assert S.shape_of([{"t": 1, "v": 2}])[0] == "series" and S.shape_of([1, 2, 3])[0] == "series"
    assert S.shape_of({"ok": True, "history": [{"t": 1, "v": 2}]}) == ("series", {"series": "history"})
    assert S.shape_of({"ok": True, "nodes": [{"hostname": "a", "load": 1}], "count": 1}) == ("items", {"rows": "nodes"})
    assert S.shape_of([{"t": 1, "kind": "x", "text": "y"}])[0] == "events" and S.shape_of({"entries": [{"ts": 1, "status": "ok"}]}) == ("events", {"events": "entries"})
    assert S.shape_of({"nodes": [], "edges": []}) == ("graph", {"nodes": "nodes", "links": "edges"})
    assert S.shape_of({"w1": {"a": 1}, "w2": {"a": 2}}) == ("items", {"rows": "$"})
    assert S.shape_of({"cpu": 41, "mem": 62}) == ("values", {}) and S.shape_of({"ok": True, "items": [], "count": 0}) == ("items", {"rows": "items"})
    assert S.shape_of([{"open": 1, "close": 2}])[0] == "ohlcv" and S.shape_of("x")[0] == "string"
    assert len(S.MEASURED) > 300 and S.MEASURED["obs.health"]["shape"] == "values" and S.MEASURED["topology.snapshot"]["shape"] == "graph" and S.MEASURED["obs.scheduler"]["shape"] == "items"


def test_the_redis_read_family_is_read_only_and_says_when_redis_is_absent():
    ORCH.REDIS = None
    for cap in ("redis.info", "redis.keys", "redis.get", "redis.stream.tail"):
        e = ORCH.CAPABILITY_REGISTRY[cap]
        assert e["meta"]["http_method"] == "GET" and e["meta"]["silent"] and e["meta"]["memory"] == "off", cap
    assert _run(C._src.redis_info())["error"] == "Redis not connected"
    assert _run(C._src.redis_get(key="")) == {"error": "Redis not connected"}
    src = _read("vera", "widgets", "widget_sources.py")
    for verb in ("r.set(", "r.delete(", "r.xadd(", "r.flushdb(", "r.hset(", "r.lpush(", "r.rpush(", "r.expire("):
        assert verb not in src, verb + " — the family only reads"


def test_widget_validate_binds_the_source_shape_to_the_form():
    ok = _run(C.widget_validate(record={"form": "trace", "source": "sysmon.history", "title": "GPU", "window": "1h"}))
    assert ok["valid"] and ok["record"]["read"]["window"] == "1h" and ok["template"]["reads"]["cap"] == "sysmon.history"
    bad = _run(C.widget_validate(record={"form": "trace", "source": "sysmon.status", "title": "x"}))
    assert not bad["valid"] and any("wants series" in p for p in bad["problems"])
    warn = _run(C.widget_validate(record={"name": "x", "form": "meter", "reads": {"cap": "obs.pending"}, "draw": {"form": "meter", "size": "S", "glow": 1}}))
    assert warn["valid"] and any("glow" in w for w in warn["warnings"]), "the template shape is accepted; unknown draw options are dropped with a warning"
    panel = _run(C.widget_validate(record={"form": "panel", "source": "panel:system-monitor", "title": "Monitor"}))
    assert panel["valid"] and panel["record"]["panel"] == "system-monitor"


def test_widget_render_spec_answers_source_form_and_size_in_one():
    r = _run(C.widget_render_spec(record={"form": "trace", "source": "sysmon.history", "title": "GPU", "frame": {"span": [6, 3]}}))
    assert r["ok"] and r["source"]["shape"] == "series" and r["form"]["id"] == "trace"
    assert r["size"]["size"] == "xl" and r["size"]["span"] == [6, 3] and "table" in r["size"]["composition"]
    t = _run(C.widget_render_spec(record={"form": "terminal", "source": "sandbox.session.exec", "size": "s"}))
    assert t["size"]["size"] == "xl", "a form without the asked size draws at the largest it has"


# ── the wiring, text-level ────────────────────────────────────────────────────

def test_the_modules_load_and_the_libraries_are_served():
    orch = _read("vera", "capability_orchestration.py")
    assert 'os.path.join(_here, "ui/libs.py"),' in orch and 'os.path.join(_here, "widgets/widget_catalog.py"),' in orch
    assert orch.index('"ui/scripts.py"') < orch.index('"ui/libs.py"') < orch.index('"widgets/widget_catalog.py"')
    libs = _read("vera", "ui", "libs.py")
    assert '@APP.get("/ui/iso.js"' in libs and '@APP.get("/ui/menus.js"' in libs
    assert os.path.exists(os.path.join(ROOT, "vera", "ui", "iso.js")) and os.path.exists(os.path.join(ROOT, "vera", "ui", "menus.js"))
    iso = _read("vera", "ui", "iso.js")
    assert "window.VeraISO = { proj, box, face, scene, fit, px, shade, edgeOfBox, route, segs, ring, discTf, frame, isoFitK, isoScene };" in iso
    assert "window.MENUS = { rows, kindOf, label, kinds:Object.keys(K) };" in _read("vera", "ui", "menus.js")


def test_register_ui_accepts_a_panels_sections_and_options():
    orch = _read("vera", "capability_orchestration.py")
    assert "sections: List[dict] = None,\n                options: List[dict] = None):" in orch
    assert '"sections":  [s for s in (sections or []) if isinstance(s, dict)],' in orch
    assert '"options":   [o for o in (options or []) if isinstance(o, dict)],' in orch


def test_the_registry_normalises_and_validates_through_the_record_module():
    reg = _read("vera", "widgets", "widget_registry.py")
    assert '_rec = _sibling("widget_record")' in reg
    assert "return _rec.normalise_template(t if isinstance(t, dict) else {})" in reg
    assert "return _rec.template_problems(t, FORMS, WHERES)" in reg
    assert "return _rec.to_template(t)" in reg, "the full record is accepted by the registry too"
