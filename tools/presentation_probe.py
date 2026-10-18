"""Check each capability's declared presentation against a REAL answer, the way the page draws it (DA-10a).

For every capability that declares how its answer is drawn (Contract v2 ``presentation``, carried by /mcp/tools), and
that is on the read-only list below, the probe calls it once on the given Vera, reads the answer with
``VeraWidget.present`` in a real page (the Desk Lab, which loads the widget element and present.js), draws every record
in a ``<vera-widget>`` and records what came out:

  drawn       - every record drew something
  empty       - a record drew its "nothing to show" face (class="wempty") - check the answer: often it has no data
  undrawable  - present() gave no record, or no element, for a non-empty answer
  error       - the call failed or timed out
  skipped     - not on the read-only list (it runs a command, needs an id, or is heavy) - the reason is given

Read-only and bounded: one call per capability, in turn, no GPU. The report holds SHAPES only - element, kind, forms,
record counts, the answer's top-level keys - never values (the repo is public; real answers stay on the box).

usage: python3 tools/presentation_probe.py [--base https://localhost:8998] [--only a,b] [--out report.json]
"""
import argparse
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

# read-only capabilities and the arguments a probe call uses ({} = defaults). Anything declared and not here is skipped.
READ = {
    "backup.status": {}, "board.items": {"limit": 20}, "cal.events.list": {}, "census.live": {},
    "ci.board": {}, "ci.census": {}, "ci.fleet": {}, "ci.matrix": {}, "ci.pulse": {}, "ci.race": {}, "ci.tests": {},
    "dash.health.summary": {}, "docker.ps": {}, "dream.history": {}, "estate.health": {},
    "evolve.activity": {}, "evolve.pipeline.list": {"limit": 10}, "evolve.sandbox.list": {}, "evolve.tasks.overview": {},
    "evolve.unittest.history": {}, "fabric.entity_graph.snapshot": {}, "fabric.graphs.snapshot": {},
    "loop.ci.board": {}, "loop.ci.matrix": {}, "loop.ci.perf": {}, "loop.ci.race": {}, "markets.overview": {}, "obs.cluster": {},
    "mesh.topology": {}, "obs.events": {"limit": 50}, "obs.health": {}, "obs.modules": {}, "obs.node_temps": {},
    "ollama.request_log": {}, "ollama.route_stats": {}, "perf.stalls": {}, "syslog.errors": {},
    "sysmon.history": {"limit": 60}, "sysmon.status": {}, "topology.snapshot": {},
}
# needs another answer first: (source capability, JS expression over its answer `a` giving the arguments)
DEPENDS = {
    "ci.track": ("evolve.pipeline.list", "({ id: ((a.pipelines || a.items || a.rows || a || [])[0] || {}).id })"),
    "evolve.pipeline.get": ("evolve.pipeline.list", "({ id: ((a.pipelines || a.items || a.rows || a || [])[0] || {}).id })"),
    "markets.chart": ("markets.overview", "({ symbol: (JSON.stringify(a).match(/\"symbol\":\s*\"([^\"]+)\"/) || [])[1] })"),
    "evolve.pipeline.diff": ("evolve.pipeline.list", "({ id: ((a.pipelines || a.items || a.rows || a || [])[0] || {}).id })"),
}
SKIP = {
    "exec.bash.run": "runs a command",
    "code.read": "reads a file of a run's workspace - needs a run",
    "perf.scan": "an active scan of the event loop - heavy",
    "memory.graph_full": "the whole memory graph - heavy",
    "code.diff": "needs two revisions",
    "evolve.sandbox.diff": "needs a sandbox",
    "ci.run": "needs a run id", "ci.compare": "needs two run ids",
    "workshop.agent_loop.trace": "needs a loop session",
}

PROBE_JS = r"""
async ({ read, depends, skip, only, timeoutMs }) => {
  const W = window.VeraWidget; if (!W || typeof W.present !== 'function') return { error: 'VeraWidget.present not on this page' };
  const tools = await (await fetch('/mcp/tools')).json(); const list = Array.isArray(tools) ? tools : (tools.tools || []);
  W.presentations(list);
  const declared = list.filter((t) => t && t.presentation && t.presentation.status === 'declared').map((t) => t.name).sort();
  const call = async (name, args) => { const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), timeoutMs);
    try { const r = await fetch('/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ctl.signal,
        body: JSON.stringify({ name, arguments: args, caller_kind: 'claude', session_id: 'presentation-probe' }) });
      const j = await r.json(); return j && j.content !== undefined ? j.content : (j && j.result !== undefined ? j.result : j); }
    finally { clearTimeout(tm); } };
  const keysOf = (a) => Array.isArray(a) ? ['[' + a.length + ']'] : (a && typeof a === 'object' ? Object.keys(a).slice(0, 14) : [typeof a]);
  const host = document.createElement('div'); host.style.cssText = 'position:absolute;left:-4000px;top:0;width:520px'; document.body.appendChild(host);
  const draws = async (rec) => { const el = document.createElement('vera-widget'); el.style.cssText = 'display:block;width:480px;height:300px';
    el.setAttribute('size', 'm'); host.appendChild(el);
    /* drawn from the answer in hand: no source, so the widget never calls the capability again */
    const r2 = Object.assign({}, rec); delete r2.source; if (r2.read) r2.read = { map: r2.read.map };
    try { el.record = r2; } catch (e) { el.remove(); return { ok: false, why: 'render threw ' + String(e).slice(0, 60) }; }
    await new Promise((r) => setTimeout(r, 250)); const root = el.shadowRoot || el; const html = root.innerHTML || '';
    const empty = /class="wempty"/.test(html); const n = root.querySelectorAll('*').length; el.remove(); return { ok: !empty && n > 2, empty, nodes: n }; };
  const seen = {}, out = [];
  for (const name of declared) {
    if (only.length && !only.includes(name)) continue;
    if (skip[name]) { out.push({ cap: name, status: 'skipped', note: skip[name] }); continue; }
    let args = read[name];
    if (!args && depends[name]) { const [src, expr] = depends[name]; let a = seen[src]; if (a === undefined) { try { a = seen[src] = await call(src, read[src] || {}); } catch (e) { a = null; } }
      try { args = (new Function('a', 'return ' + expr))(a || {}); } catch (e) { args = null; }
      if (!args || Object.values(args).some((v) => v == null)) { out.push({ cap: name, status: 'skipped', note: 'no ' + src + ' answer to take its id from' }); continue; } }
    if (!args) { out.push({ cap: name, status: 'skipped', note: 'not on the read-only list' }); continue; }
    const t0 = performance.now(); let ans;
    try { ans = seen[name] = await call(name, args); } catch (e) { out.push({ cap: name, status: 'error', note: String(e).slice(0, 80), ms: Math.round(performance.now() - t0) }); continue; }
    const ms = Math.round(performance.now() - t0);
    if (ans && typeof ans === 'object' && !Array.isArray(ans) && (ans.error || ans.ok === false) && Object.keys(ans).length <= 4) { out.push({ cap: name, status: 'error', note: String(ans.error || 'ok: false').slice(0, 80), ms }); continue; }
    let pr; try { pr = W.present(name, ans, { args }); } catch (e) { out.push({ cap: name, status: 'undrawable', note: 'present threw ' + String(e).slice(0, 60), ms, keys: keysOf(ans) }); continue; }
    const decl = W.presentationOf(name) || {};
    const row = { cap: name, ms, keys: keysOf(ans), declared: { kind: decl.kind, form: decl.form || null }, element: pr.element, kind: pr.kind, records: pr.records.length, forms: pr.records.map((r) => r.form), why: pr.why };
    if (!pr.records.length || pr.element === 'none') { row.status = pr.element === 'receipt' || pr.element === 'job' ? 'drawn' : 'undrawable'; row.note = pr.empty || ''; out.push(row); continue; }
    const res = []; for (const r of pr.records.slice(0, 4)) res.push(await draws(r));
    row.draw = res; row.status = res.every((x) => x.ok) ? 'drawn' : (res.some((x) => x.empty) ? 'empty' : 'undrawable');
    out.push(row);
  }
  host.remove();
  return { declared: declared.length, rows: out };
}
"""


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="https://localhost:8998")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per call")
    a = ap.parse_args()
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        await page.goto(a.base.rstrip("/") + "/ui/desk-lab", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_function("() => window.VeraWidget && typeof window.VeraWidget.present === 'function'", timeout=60000)
        t0 = time.time()
        res = await page.evaluate(PROBE_JS, {"read": READ, "depends": DEPENDS, "skip": SKIP, "only": only, "timeoutMs": int(a.timeout * 1000)})
        await b.close()
    if res.get("error"):
        print(res["error"], file=sys.stderr)
        return 2
    rows = res["rows"]
    res.update({"base": a.base, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seconds": round(time.time() - t0, 1),
                "counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("drawn", "empty", "undrawable", "error", "skipped")}})
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    print("| capability | status | declared | drawn as | records | note |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        d = r.get("declared") or {}
        print("| %s | %s | %s | %s | %s | %s |" % (r["cap"], r["status"], (d.get("kind") or "") + ("/" + d["form"] if d.get("form") else ""),
                                                   ",".join(r.get("forms") or []) or (r.get("element") or ""), r.get("records", ""), (r.get("note") or "")[:70]))
    print("\n%d declared; %s" % (res["declared"], res["counts"]))
    # an empty face is the answer's own (no data); an undrawable or failed one is a defect
    return 1 if res["counts"]["undrawable"] or res["counts"]["error"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
