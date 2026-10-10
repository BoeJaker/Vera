#!/usr/bin/env python3
"""Chat corpus run: type each approved case into the real chat page and judge what came back (four-minds §5.4).

    python3 tools/chat-corpus/run_chat_corpus.py [--only 1-6,27] [--gap 8] [--turn-timeout 180]
                                                 [--target https://127.0.0.1:8999] [--out ~/chat-corpus]
                                                 [--keep-sessions] [--force]

One fresh page and session per case (`chat-corpus-<run>-<n>`, passed as ?session=). The page runs as it does for a
person, with three differences: a capability call that changes something is answered with a stub and recorded as
"would call" (vera/minds/chat_corpus_core.effect); a loop hand-off is answered with a stub stream and recorded; and
after each case its session's memories are deleted (memory.session_delete, only `chat-corpus-` sessions) unless
--keep-sessions. Refuses to start while a census is running (--force overrides).

Needs Playwright (the host's langchain venv has it). Writes <out>/<run>/{results.json, summary.md}, <out>/latest.txt
and <out>/running.json while it runs. Judging is vera/minds/chat_corpus_core (pure, tested).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vera.minds import chat_corpus_core as CC  # noqa: E402

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Tees every /agents/chat/stream response into window.__cc (the page reads its own copy untouched), and turns the
# auto-loop hand-off and the phase frames on, as the owner's page has them.
INIT_JS = r"""
(() => {
  try { localStorage.setItem('vera_auto_loop', '1'); localStorage.setItem('vera:chat:phases', '1'); } catch (_) {}
  window.__cc = { streams: [] };
  const _f = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : ((input && input.url) || '');
    const resp = await _f.apply(this, arguments);
    if (url.indexOf('/agents/chat/stream') >= 0) {
      const rec = { body: (init && typeof init.body === 'string') ? init.body.slice(0, 4000) : '',
                    sent: performance.now(), first_ms: null, text: '', done: false, error: '' };
      window.__cc.streams.push(rec);
      try {
        const rd = resp.clone().body.getReader(); const dec = new TextDecoder();
        (async () => {
          for (;;) {
            const { done, value } = await rd.read();
            if (done) break;
            const chunk = dec.decode(value, { stream: true });
            if (rec.first_ms === null && /"type":\s*"token"/.test(chunk)) rec.first_ms = performance.now() - rec.sent;
            rec.text += chunk;
          }
          rec.done = true;
        })().catch(e => { rec.error = String(e); rec.done = true; });
      } catch (e) { rec.error = String(e); rec.done = true; }
    }
    return resp;
  };
})();
"""

LOOP_STUB = 'data: {"type": "error", "error": "chat corpus run: the loop was not started (route recorded)"}\n\n' \
            'data: {"type": "done"}\n\n'


def _http(target: str, path: str, body=None, timeout: float = 30.0):
    url = target.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def _cap(target: str, name: str, **arguments):
    out = _http(target, "/mcp/call", {"name": name, "arguments": arguments, "caller_kind": "eval",
                                      "session_id": "chat-corpus"}, timeout=60)
    return out.get("content", out) if isinstance(out, dict) else out


def census_busy(target: str) -> bool:
    try:
        return bool((_http(target, "/health", timeout=15).get("census") or {}).get("busy"))
    except Exception:
        return False


def run_case(browser, target: str, case: dict, run_id: str, turn_timeout: float, settle: float) -> dict:
    sid = CC.session_id(run_id, case["id"])
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 900})
    ctx.add_init_script(INIT_JS)
    page = ctx.new_page()
    state = {"armed": False, "calls": [], "loop": [], "inflight": 0, "last": time.monotonic()}

    def on_mcp(route, request):
        if not state["armed"] or request.method != "POST":
            return route.continue_()
        try:
            body = json.loads(request.post_data or "{}")
        except ValueError:
            body = {}
        name = str(body.get("name") or "")
        if CC.effect(name):
            state["calls"].append({"name": name, "would_call": True,
                                   "arguments": json.dumps(body.get("arguments") or {})[:400]})
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"content": CC.stub_result(name)}))
        state["calls"].append({"name": name, "would_call": False})
        return route.continue_()

    def on_loop(route, request):
        if not CC.is_loop_request(request.method, request.url):
            return route.continue_()
        state["loop"].append({"url": request.url.split("?", 1)[0], "body": (request.post_data or "")[:600]})
        return route.fulfill(status=200, content_type="text/event-stream", body=LOOP_STUB)

    def busy(delta):
        def f(req):
            if "/mcp/call" in req.url:
                state["inflight"] = max(0, state["inflight"] + delta)
                state["last"] = time.monotonic()
        return f

    page.route("**/mcp/call", on_mcp)
    page.route("**/workshop/agent_loop/**", on_loop)
    page.on("request", busy(+1))
    page.on("requestfinished", busy(-1))
    page.on("requestfailed", busy(-1))
    ev = {"agent": "assistant"}
    try:
        page.goto(f"{target.rstrip('/')}/chat_panel?session={urllib.parse.quote(sid)}", wait_until="load",
                  timeout=90_000)
        page.wait_for_selector("#chatInput", state="visible", timeout=60_000)
        page.wait_for_timeout(2500)          # the page's own start-up calls settle before the turn
        state["armed"] = True
        page.fill("#chatInput", case["text"])
        t0 = time.monotonic()
        page.press("#chatInput", "Enter")
        rec = None
        while time.monotonic() - t0 < turn_timeout:
            page.wait_for_timeout(500)
            streams = page.evaluate("() => (window.__cc && window.__cc.streams) || []")
            rec = next((s for s in streams if _sent(s, case["text"])), None)
            if rec and rec.get("done"):
                break
        ev["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        if not rec:
            ev["error"] = "the page sent no chat turn"
        else:
            ev["stream"] = rec.get("text") or ""
            ev["client_ttft_ms"] = int(rec["first_ms"]) if rec.get("first_ms") is not None else None
            if not rec.get("done"):
                ev["error"] = f"no end of turn in {turn_timeout:.0f} s"
            elif rec.get("error"):
                ev["error"] = rec["error"][:300]
            # the reply's cap calls and a loop hand-off happen after the stream ends: wait for the page to go quiet
            t1 = time.monotonic()
            while time.monotonic() - t1 < 60:
                page.wait_for_timeout(500)
                if state["inflight"] == 0 and time.monotonic() - state["last"] >= settle:
                    break
    except Exception as e:  # a page that does not load is the case's error, not the run's
        ev["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        ev["calls"], ev["loop"] = state["calls"], state["loop"]
        try:
            ctx.close()
        except Exception:
            pass
    try:
        ev["canvas"] = _http(target, "/canvas/get?id=" + urllib.parse.quote(CC.canvas_id(sid)), timeout=20)
    except Exception:
        ev["canvas"] = {}
    return ev


def _sent(stream: dict, text: str) -> bool:
    try:
        return str(json.loads(stream.get("body") or "{}").get("message") or "").strip().startswith(text.strip()[:60])
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=os.getenv("VERA_CHAT_CORPUS_TARGET", "https://127.0.0.1:8999"))
    ap.add_argument("--cases", default=str(Path(__file__).resolve().parent / "cases_v0.json"))
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.expanduser("~/chat-corpus"))
    ap.add_argument("--gap", type=float, default=8.0, help="seconds between cases")
    ap.add_argument("--turn-timeout", type=float, default=180.0)
    ap.add_argument("--settle", type=float, default=4.0, help="quiet seconds after the turn before judging")
    ap.add_argument("--keep-sessions", action="store_true", help="do not delete the run's chat-corpus- sessions")
    ap.add_argument("--force", action="store_true", help="run even while a census is running")
    a = ap.parse_args()

    doc = json.loads(Path(a.cases).read_text(encoding="utf-8"))
    cases = CC.select(CC.load_cases(doc), a.only)
    if not a.force and census_busy(a.target):
        print("a census is running - not starting (use --force to run beside it)")
        return 3
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed for", sys.executable)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    run_dir = out / run_id
    run_dir.mkdir()
    lock = out / "running.json"
    lock.write_text(json.dumps({"pid": os.getpid(), "run_id": run_id, "cases": len(cases)}), encoding="utf-8")
    run = {"run_id": run_id, "corpus": doc.get("version", "v0"), "target": a.target, "only": a.only,
           "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--ignore-certificate-errors"])
            try:
                for i, case in enumerate(cases):
                    if case.get("fixture"):
                        results.append(CC.evaluate(case, {}))
                        continue
                    if i and a.gap:
                        time.sleep(a.gap)
                    ev = run_case(browser, a.target, case, run_id, a.turn_timeout, a.settle)
                    r = CC.evaluate(case, ev)
                    sid = CC.session_id(run_id, case["id"])
                    if not a.keep_sessions and CC.ours(sid):
                        try:
                            r["cleanup"] = "deleted" if not (_cap(a.target, "memory.session_delete",
                                                                  session_id=sid) or {}).get("error") else "failed"
                        except Exception as e:
                            r["cleanup"] = f"failed: {e}"[:120]
                    results.append(r)
                    print(f"{case['id']:>3} {r['status']:<7} {r.get('route', ''):<4} "
                          f"ttft={((r.get('record') or {}).get('ttft_ms'))} {r.get('reason', '')}", flush=True)
                    (run_dir / "results.json").write_text(json.dumps({"run": run, "results": results}, indent=1),
                                                          encoding="utf-8")
            finally:
                browser.close()
    finally:
        run["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = CC.summarise(results)
        (run_dir / "results.json").write_text(json.dumps({"run": run, "summary": summary, "results": results},
                                                         indent=1), encoding="utf-8")
        md = CC.render_md(run, summary, results)
        (run_dir / "summary.md").write_text(md, encoding="utf-8")
        (out / "latest.txt").write_text(run_id + "\n", encoding="utf-8")
        try:
            lock.unlink()
        except OSError:
            pass
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
