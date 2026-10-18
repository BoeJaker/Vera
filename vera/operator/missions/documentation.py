"""documentation.py — the operator's flagship mission.

End-to-end: resolve a target Vera (loop-lab sandbox by default, or any
``base_url``) → discover its live panels + capability registry → for each
documentation domain, seed representative data, screenshot every matching panel
(each rendered standalone at ``/ui/panel/window?id=…``), collect the domain's
capabilities, and regenerate the managed auto-blocks in ``documentation/NN.md``
plus the top-level gallery and an asset manifest.

Degrades gracefully: with no Playwright it still refreshes capability tables,
scaffolds and the gallery (skipping image capture with a clear note).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .. import browser_engine as _be
from .. import perception as _perception
from ..docs import doc_scaffold as _scaffold
from ..docs import domain_map as _dm
from ..docs import gallery as _gallery
from . import seeds as _seeds

log = logging.getLogger("vera.operator.mission.docs")


class PanelReadinessError(RuntimeError):
    """A required, named panel-readiness condition was not satisfied."""

    def __init__(self, condition: str, timeout_ms: int, detail: str = "",
                 evidence: Optional[List[Dict[str, Any]]] = None) -> None:
        self.condition = condition
        self.timeout_ms = int(timeout_ms)
        self.detail = str(detail or "")
        self.evidence = list(evidence or [])
        super().__init__(f"readiness condition '{condition}' was not met within {timeout_ms}ms")


DOC_VIEWPORT = {"width": 1440, "height": 900}

# Per-browser UI prefs the capture browser starts with. Vera keeps these in
# localStorage, each overriding a server-wide default that is someone's own
# preference: prod's global UI scale is 1.4, so every capture came out zoomed
# 140% into a 1440x900 window, and the shell defaulted to its top tabs rather
# than the left-hand menu the docs describe. Seeding them here changes only
# this browser - the server values are never written.
DOC_UI_STORAGE = {
    "vera:ui:scale": "1",
    "vera_tab_prefs": json.dumps({"lhm": True}),
    # Per-turn block backgrounds off: the docs show the chat as plain turns.
    "vera:ui:blocks": "off",
    # The README's chat turn should show the agent acting, not an approval card
    # waiting for a click. Only an on_request page ever sends a message.
    "vera_ask_before_acting": "0",
}

# Docs are shot in one theme whatever prod is set to today. The theme is
# server-authoritative - every page reconciles to GET /ui/theme after painting
# its cache - so a localStorage seed alone is overwritten. The capture browser
# therefore answers its own /ui/theme with this theme; the server is untouched.
DOC_THEME = "void"


async def _theme_payload(base_url: str, theme_id: str, attempts: int = 4) -> Optional[Dict[str, Any]]:
    import httpx
    data = None
    # A loaded or restarting target drops single requests; one miss must not
    # decide the theme of a whole run.
    async with httpx.AsyncClient(verify=False) as client:
        for attempt in range(attempts):
            data = await _fetch_json(client, base_url.rstrip("/") + "/ui/themes")
            if isinstance(data, dict):
                break
            await asyncio.sleep(3 * (attempt + 1))
    themes = (data or {}).get("themes") if isinstance(data, dict) else None
    items = themes.values() if isinstance(themes, dict) else (themes or [])
    for t in items:
        if isinstance(t, dict) and t.get("id") == theme_id and isinstance(t.get("vars"), dict):
            return {"theme": theme_id, "vars": t["vars"], "type": t.get("type") or ""}
    return None


async def _seed_browser_prefs(session, overrides: Optional[Dict[str, Any]] = None,
                              theme: Optional[Dict[str, Any]] = None) -> None:
    prefs = dict(DOC_UI_STORAGE)
    if theme:
        # Paint the pinned theme from the first frame, as the UI's own cache would.
        prefs.update({"vera:ui:theme": theme["theme"],
                      "vera:ui:themeVars": json.dumps(theme["vars"]),
                      "vera:ui:themeVarsFor": theme["theme"]})
    prefs.update({str(k): str(v) for k, v in (overrides or {}).items()})
    script = ("(() => { try { const p = " + json.dumps(prefs) + ";"
              " for (const k in p) localStorage.setItem(k, p[k]); } catch (e) {} })();")
    await session.context.add_init_script(script)
    if theme:
        from urllib.parse import urlparse
        body = json.dumps(theme)

        async def _answer(route):
            await route.fulfill(status=200, content_type="application/json", body=body)

        await session.context.route(lambda u: urlparse(u).path == "/ui/theme", _answer)


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "panel")).strip("-") or "panel"


# /mcp/tools is the whole catalogue with schemas (several MB, streamed): 15 s missed it on a busy prod.
CAPS_FETCH_TIMEOUT = 90


async def _fetch_json(client: Any, url: str) -> Any:
    timeout = CAPS_FETCH_TIMEOUT if url.rstrip("/").endswith("/mcp/tools") else 15
    try:
        r = await client.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("operator/docs: GET %s failed: %s", url, e)
        return None


def _make_call_target(base_url: str) -> Callable[..., Awaitable[Any]]:
    async def _call(name: str, args: Dict[str, Any]) -> Any:
        import httpx
        try:
            # verify=False: prod serves a self-signed cert on localhost.
            async with httpx.AsyncClient(timeout=60, verify=False) as c:
                r = await c.post(base_url.rstrip("/") + "/mcp/call",
                                 json={"name": name, "arguments": args or {}})
            r.raise_for_status()
            d = r.json()
            return d.get("result", d.get("content", d)) if isinstance(d, dict) else d
        except Exception as e:
            return {"error": str(e)}
    return _call


def _caps_for_domain(domain: Dict[str, Any], all_caps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prefixes = domain.get("cap_prefixes", []) or []
    out = []
    for c in all_caps:
        name = c.get("name", "")
        group = name.split(".")[0]
        if any(name.startswith(p + ".") or group == p for p in prefixes):
            out.append({
                "name": name, "method": c.get("http_method") or c.get("method") or "",
                "path": c.get("http_path") or c.get("path") or "",
                "description": c.get("description") or c.get("desc") or "",
            })
    out.sort(key=lambda x: x["name"])
    return out


def _assign_panels(panels: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Assign each live panel to at most one domain (exact id > match)."""
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for p in panels:
        slug = _dm.domain_for_panel(p)
        if slug:
            by_domain.setdefault(slug, []).append(p)
    return by_domain


def _capture_states(domain: Dict[str, Any], panel_id: str) -> List[Dict[str, Any]]:
    """Return trusted, declarative capture states for a panel."""
    configured = (domain.get("capture_states") or {}).get(panel_id) or []
    return list(configured) if configured else [{}]


def _state_name(panel_id: str, state: Dict[str, Any]) -> str:
    suffix = _safe_name(state.get("name") or "")
    return f"{_safe_name(panel_id)}-{suffix}" if state.get("name") else _safe_name(panel_id)


async def run_documentation_mission(params: Dict[str, Any],
                                    ctx: Dict[str, Any]) -> Dict[str, Any]:
    call_cap = ctx.get("call_cap")
    emit = ctx.get("emit")
    repo_root = ctx.get("repo_root") or os.getcwd()

    async def _emit(stage: str, message: str, **extra):
        if emit:
            try:
                await emit({"type": "operator.docs.progress", "stage": stage,
                            "message": message, **extra})
            except Exception:
                pass

    # ── resolve target ───────────────────────────────────────────────────────
    from ..targets import ensure_target
    target = params.get("target")
    if isinstance(target, str):
        target = {"kind": target}
    if not target:
        target = {"kind": "sandbox"}
    default_base = params.get("base_url") or ctx.get("default_base_url") or ""
    resolved = await ensure_target(target, call_cap, default_base)
    if not resolved.get("ready"):
        return {"error": resolved.get("error", "target not ready"), "target": resolved}
    base_url = params.get("base_url") or resolved["base_url"]
    await _emit("target", f"target ready: {base_url}", base_url=base_url,
                kind=resolved.get("kind"))

    # ── discover panels + capabilities ───────────────────────────────────────
    import httpx
    # verify=False: a live target serves Vera's self-signed HTTPS cert on
    # localhost; without this the /ui/panels + /mcp/tools discovery fetches fail
    # SSL verification → 0 panels discovered → an empty capture that would wipe
    # the manifest.
    async with httpx.AsyncClient(verify=False) as client:
        panels_raw = await _fetch_json(client, base_url.rstrip("/") + "/ui/panels")
        caps_raw = await _fetch_json(client, base_url.rstrip("/") + "/mcp/tools")
    panels = _normalise_panels(panels_raw)
    all_caps = caps_raw if isinstance(caps_raw, list) else \
        (caps_raw.get("tools") if isinstance(caps_raw, dict) else []) or []
    await _emit("discover", f"{len(panels)} panels · {len(all_caps)} capabilities",
                panels=len(panels), caps=len(all_caps))

    # Discovery failed (unreachable target / TLS) → abort BEFORE writing anything,
    # so a bad run can never wipe the manifest of previously-captured images.
    if not panels and not all_caps:
        return {"error": f"discovered 0 panels and 0 capabilities from {base_url} — "
                         f"the target is unreachable or its TLS cert was rejected. "
                         f"Nothing was written (existing images/manifest untouched).",
                "base_url": base_url, "target_kind": resolved.get("kind")}

    by_domain = _assign_panels(panels)
    domains = _dm.resolve_slugs(params.get("domains"))
    write_docs = params.get("write_docs", True)
    do_capture = params.get("capture", True) and _be.playwright_available()
    capture_note = "" if do_capture else _be.INSTALL_HINT if params.get("capture", True) else "capture disabled"
    settle_ms = int(params.get("settle_ms", 1400) or 1400)
    # Optional selective re-capture: only shoot these panel ids (any domain).
    panel_filter = set(str(x) for x in (params.get("panels") or []))
    full_page = bool(params.get("full_page", False))
    # seed=False captures the target's own data as-is. Seeds WRITE to the target,
    # so this is required when the target is prod (or a sandbox that shares prod's
    # Neo4j/Chroma) and its real data should be shown untouched.
    do_seed = bool(params.get("seed", True))
    # write_caps=False refreshes only the screenshot blocks and leaves each
    # doc's capability table as it is.
    write_caps = bool(params.get("write_caps", True))
    caps_note = ""
    if write_caps and not all_caps:
        # The catalogue did not come back (timeout, error, empty): writing tables from it would replace every
        # page's table with "No capabilities resolved" - 33 of 42 pages got that way (S31 / D32-3). Keep them.
        write_caps = False
        caps_note = "capability catalogue unavailable - every capability table left as it was"
        await _emit("warn", caps_note)

    docs_dir = os.path.join(repo_root, "documentation")
    assets_dir = os.path.join(docs_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    session = None
    if do_capture:
        try:
            session = await _be.start_session(base_url=base_url,
                                              viewport=dict(params.get("viewport") or DOC_VIEWPORT))
            theme_id = str(params.get("theme") or DOC_THEME)
            theme = await _theme_payload(base_url, theme_id)
            if theme is None:
                await _emit("warn", f"theme '{theme_id}' not found on target; "
                                    "capturing in the server's theme")
            await _seed_browser_prefs(session, params.get("ui_storage"), theme)
        except Exception as e:
            do_capture = False
            capture_note = f"browser start failed: {e}"
            await _emit("warn", capture_note)

    # Load any existing manifest so a partial run (a domain subset or a panel
    # re-capture) preserves everything it doesn't touch.
    manifest_prev: Dict[str, Any] = {}
    _man_path = os.path.join(assets_dir, "manifest.json")
    if os.path.exists(_man_path):
        try:
            with open(_man_path, "r", encoding="utf-8") as f:
                manifest_prev = json.load(f) or {}
        except Exception:
            manifest_prev = {}
    manifest: Dict[str, Any] = {"generated_at": _iso(), "base_url": base_url,
                                "target_kind": resolved.get("kind"),
                                "domains": dict(manifest_prev.get("domains", {}))}
    gallery_entries: List[Dict[str, Any]] = []
    total_shots = 0
    capture_failures: List[Dict[str, Any]] = []

    try:
        for domain in domains:
            slug = domain["slug"]
            # PANEL_DOCS assigns each live panel to exactly one domain.
            dpanels = by_domain.get(slug, [])
            caps = _caps_for_domain(domain, all_caps)
            shots: List[Dict[str, Any]] = []

            # seed
            seed_name = domain.get("seed") if do_seed else None
            if do_capture and seed_name:
                await _emit("seed", f"[{slug}] seeding ({seed_name})")
                await _seeds.run_seed(seed_name, _make_call_target(base_url))

            # Every capture target: each live panel's states, then the domain's
            # fixed pages - whole routes saved under a stable name, e.g. the
            # overview images the README leads with, which are not panels.
            targets = []
            for p in dpanels:
                pid = p.get("id", "")
                if pid:
                    targets.extend((p, pid, state, _state_name(pid, state))
                                   for state in _capture_states(domain, pid))
            for page in domain.get("pages") or []:
                pid = str(page["name"])
                # A page that calls the model (on_request) runs only when a
                # caller names it - never as part of a routine full run.
                if page.get("on_request") and pid not in panel_filter:
                    continue
                targets.append(({"id": pid, "label": page.get("label") or pid}, pid,
                                dict(page, capture_path=page["path"]), _safe_name(pid)))

            mode = "seeded" if seed_name else "default"
            for p, pid, state, shot_id in targets:
                rel = f"assets/{slug}/{shot_id}.png"
                abspath = os.path.join(docs_dir, rel)
                label = state.get("label") or p.get("label") or pid
                via, readiness, captured = "", [], False
                # A selective re-capture shoots only the named panels.
                if do_capture and session is not None and (not panel_filter or pid in panel_filter):
                    cap_url = panel_capture_url(base_url, p, state)
                    shoot_kw = dict(settle_ms=max(settle_ms, int(state.get("settle_ms", 0) or 0)),
                                    full_page=bool(state.get("full_page", full_page)), state=state)
                    capture = await _shoot_panel(session, cap_url["url"], abspath, **shoot_kw)
                    # The target restarting mid-run (another promotion) failed every
                    # remaining navigation at once - 32 panels in one run. Wait for it
                    # to answer again and retry this panel once.
                    if (not capture["ok"]
                            and str(capture.get("failed_condition", "")).startswith("navigation")
                            and await _wait_target_up(base_url)):
                        await _emit("warn", f"[{slug}] {label}: target came back, retrying")
                        capture = await _shoot_panel(session, cap_url["url"], abspath, **shoot_kw)
                    if capture["ok"]:
                        captured, via = True, cap_url["via"]
                        readiness = capture.get("readiness", [])
                        total_shots += 1
                        await _emit("shot", f"[{slug}] {label}",
                                    domain=slug, panel_id=pid, shot_id=shot_id,
                                    label=label, rel=rel,
                                    url=f"/docs/asset?path={rel}", via=via)
                    else:
                        failure = {
                            "domain": slug, "panel_id": pid, "shot_id": shot_id,
                            "condition": capture.get("failed_condition", "capture"),
                            "error": capture.get("error", "capture failed"),
                        }
                        capture_failures.append(failure)
                        await _emit("warn", f"[{slug}] {label}: {failure['error']}",
                                    **failure)
                # An image already on disk stays in the doc when this run did not
                # replace it - a failed capture, or a panel a selective re-capture
                # left out. Rebuilding the block from this run's shots alone
                # deleted every other panel from the doc.
                if not os.path.exists(abspath):
                    continue
                shots.append({"panel_id": shot_id, "source_panel_id": pid,
                              "label": label, "rel_path": rel,
                              "caption": state.get("caption") or label,
                              "mode": mode, "via": via, "readiness": readiness,
                              "captured": captured})

            # write doc auto-blocks (a domain of pages alone may have no doc)
            doc_name = domain.get("doc") or ""
            if write_docs and doc_name:
                doc_path = os.path.join(docs_dir, doc_name)
                existing = None
                if os.path.exists(doc_path):
                    with open(doc_path, "r", encoding="utf-8") as f:
                        existing = f.read()
                content = _scaffold.build_doc(
                    existing, number=doc_name.split("-")[0], title=domain["title"],
                    screenshots_content=_scaffold.render_screenshots_block(shots),
                    caps_content=(_scaffold.render_caps_block(caps) if write_caps else None))
                with open(doc_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # Always merge into the existing manifest entry so a partial run (a
            # selective re-capture, or a domain that captured fewer panels this
            # time) never drops previously-recorded images. Only shots taken in
            # this run replace an entry's metadata.
            prev = dict(manifest_prev.get("domains", {}).get(slug, {}))
            prev_panels = {pp.get("id"): pp for pp in prev.get("panels", [])}
            for s in shots:
                if s["captured"] or s["panel_id"] not in prev_panels:
                    prev_panels[s["panel_id"]] = {
                        "id": s["panel_id"], "label": s["label"], "shot": s["rel_path"],
                        "mode": s["mode"], "via": s.get("via", ""),
                        "readiness": s.get("readiness", [])}
            manifest["domains"][slug] = {
                "doc": doc_name, "title": domain["title"],
                "panels": list(prev_panels.values()), "cap_count": len(caps),
            }
            await _emit("domain", f"[{slug}] {len(shots)} shots · {len(caps)} caps",
                        slug=slug, shots=len(shots), caps=len(caps))
    finally:
        if session is not None:
            await _be.close_session(session.session_id)

    # Build the gallery from the FULL merged manifest (in domain-map order) so a
    # partial run still produces a complete index.
    for d in _dm.DOMAINS:
        info = manifest["domains"].get(d["slug"])
        if not info or not d.get("doc"):
            continue
        # The manifest remembers shots that were later removed; disk decides
        # what exists, as docs.assets does, so no cover links a missing file.
        pnls = [pp for pp in info.get("panels", [])
                if pp.get("shot") and os.path.exists(os.path.join(docs_dir, pp["shot"]))]
        gallery_entries.append({
            "slug": d["slug"], "title": info.get("title", d["title"]),
            "doc": info.get("doc", d["doc"]),
            "cover_rel": pnls[0]["shot"] if pnls else "",
            "shot_count": len(pnls), "cap_count": info.get("cap_count", 0)})

    # gallery + manifest
    if write_docs:
        gal = _gallery.build_gallery(gallery_entries, generated_at=_iso(),
                                     total_caps=len(all_caps))
        # README.md is authored navigation and must never be replaced by a
        # partial capture run. The replaceable visual index has its own file.
        with open(os.path.join(docs_dir, _gallery.OUTPUT_FILE), "w", encoding="utf-8") as f:
            f.write(gal)
    with open(os.path.join(assets_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    outcome = "complete" if not capture_failures else "partial"
    await _emit("done", f"documentation build {outcome}: {total_shots} screenshots, "
                        f"{len(capture_failures)} capture failures across {len(domains)} domains")
    return {"ok": not capture_failures, "partial": bool(capture_failures),
            "base_url": base_url, "target_kind": resolved.get("kind"),
            "domains": len(domains), "panels_discovered": len(panels),
            "screenshots": total_shots, "capabilities": len(all_caps),
            "capture_failures": capture_failures,
            # Live panels no doc claims - placed by editing PANEL_DOCS, never guessed.
            "unmapped_panels": sorted(p["id"] for p in panels
                                      if not _dm.domain_for_panel(p)),
            "captured": do_capture, "capture_note": capture_note,
            "caps_written": bool(write_docs and write_caps), "caps_note": caps_note,
            "docs_written": bool(write_docs), "manifest": "documentation/assets/manifest.json"}


def _normalise_panels(raw: Any) -> List[Dict[str, Any]]:
    """``/ui/panels`` may return a list or a {panels:[...]}/{id:panel} dict."""
    items: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        seq = raw.get("panels") if isinstance(raw.get("panels"), list) else None
        if seq is None:
            seq = list(raw.values()) if all(isinstance(v, dict) for v in raw.values()) else []
        raw = seq
    if isinstance(raw, list):
        for p in raw:
            if not isinstance(p, dict):
                continue
            pid = p.get("id") or p.get("panel_id") or ""
            if not pid:
                continue
            items.append({"id": pid, "label": p.get("label") or pid,
                          "tags": p.get("http_tags") or p.get("tags") or p.get("ui_caps") or [],
                          "mode": p.get("mode") or "inject",
                          "html": p.get("html") or ""})
    return items


_IFRAME_SRC = re.compile(r"""<iframe[^>]*\bsrc=["']([^"']+)["']""", re.IGNORECASE)


def panel_capture_url(base_url: str, panel: Dict[str, Any],
                      state: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Where to actually screenshot a panel.

    Most tab panels register an ``<iframe src="/xxx/panel">`` — that src is the
    REAL panel route, which renders with full fidelity. Screenshot it directly
    rather than the ``/ui/panel/window`` wrapper (whose degraded shim is why
    several panels came out wrong). Inline/element panels have no iframe, so we
    fall back to the panel window. Returns {url, via}."""
    base = base_url.rstrip("/")
    capture_path = str((state or {}).get("capture_path") or "").strip()
    if capture_path:
        if not capture_path.startswith("/") or capture_path.startswith("//"):
            raise ValueError("capture_path must be a same-origin absolute path")
        return {"url": base + capture_path, "via": "route"}
    html = panel.get("html") or ""
    m = _IFRAME_SRC.search(html)
    if m:
        src = m.group(1)
        if src.startswith("http"):
            return {"url": src, "via": "route"}
        return {"url": base + "/" + src.lstrip("/"), "via": "route"}
    return {"url": f"{base}/ui/panel/window?id={panel.get('id')}", "via": "window"}


# Default readiness: the panel shows real content. Registry widgets render into
# open shadow roots and the panel window wraps route panels in a same-origin
# iframe; body.innerText sees neither, so 15 populated panels read as empty (a
# palette button's emoji was the whole "page"). Walk shadow roots and same-origin
# frames, counting only rendered text: <style>/<script> bodies and hidden tabs
# would otherwise make any styled widget look populated.
# A visible placeholder line ("Loading…", "reading sysmon.status…", "Checking
# the estate…", "Waiting for an agentic run…") means not yet, however much
# toolbar text surrounds it: with only a length bar, eight panels were shot
# mid-load.
_MEANINGFUL_CONTENT_JS = """() => {
  const b = document.body; if (!b || b.scrollHeight <= 120) return false;
  const SKIP = /^(STYLE|SCRIPT|NOSCRIPT|TEMPLATE)$/;
  const PENDING = /^\\s*(loading|reading|checking|waiting for)\\b/i;
  let text = '', media = false, pending = false;
  const walk = (root, depth) => {
    if (!root || depth > 4 || text.length > 4000) return;
    if (root.querySelector && root.querySelector('canvas,svg,img[src],table tbody tr')) media = true;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n && text.length <= 4000; n = w.nextNode()) {
      if (n.nodeType === 3) {
        const p = n.parentElement;
        if (p && !SKIP.test(p.tagName) && (!p.checkVisibility || p.checkVisibility())) {
          text += n.nodeValue + ' ';
          if (PENDING.test(n.nodeValue)) pending = true;
        }
      } else {
        if (n.shadowRoot) walk(n.shadowRoot, depth + 1);
        if (n.tagName === 'IFRAME') {
          let d = null; try { d = n.contentDocument; } catch (e) {}
          if (d && d.body) walk(d.body, depth + 1);
        }
      }
    }
  };
  walk(b, 0);
  if (pending) return false;
  text = text.replace(/\\s+/g, ' ').trim();
  return text.length > 40 || media;
}"""

# Every visible rendered text in the page - shadow roots and same-origin frames
# included, <style>/<script> excluded - as one string.
_DEEP_TEXT_JS = """() => { let s = '';
  const walk = (r, d) => { if (!r || d > 4) return;
    const w = document.createTreeWalker(r, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (n.nodeType === 3) { const p = n.parentElement;
        if (p && !/^(STYLE|SCRIPT|NOSCRIPT|TEMPLATE)$/.test(p.tagName)
            && (!p.checkVisibility || p.checkVisibility())) s += n.nodeValue + ' '; }
      else { if (n.shadowRoot) walk(n.shadowRoot, d + 1);
        if (n.tagName === 'IFRAME') { let doc = null; try { doc = n.contentDocument; } catch (e) {}
          if (doc && doc.body) walk(doc.body, d + 1); } } } };
  walk(document.body, 0); return s; }"""

# "Loading" anywhere means not yet: a status line reads "1 nodes · 0 edges
# loading" while the graph is still arriving, and passed a start-only check.
_READY_TEXT_JS = """sel => { const e = document.querySelector(sel);
  if (!e) return false; const t = (e.innerText || e.textContent || '').trim();
  return t.length > 2 && !/^(starting|\\u2014|[.]{3})/i.test(t) && !/\\bloading\\b/i.test(t); }"""

# Count the first QUANTITY ("400 nodes"), not the first digit: "2D VIEW -- 0
# nodes" is an empty view whose first digit belongs to its title.
_READY_COUNT_JS = """sel => { const e = document.querySelector(sel);
  if (!e) return false; const t = e.innerText || e.textContent || '';
  if (/\\bloading\\b/i.test(t)) return false;
  const m = t.match(/(\\d[\\d,]*)\\s+[A-Za-z]/) || t.match(/\\d[\\d,]*/);
  return !!m && Number((m[1] || m[0]).replace(/,/g, '')) > 0; }"""


# A canvas that holds only its background has drawn nothing: the memory graph's
# status line read "906 nodes" over a blank canvas, and every text/count
# condition passed. "Background" includes a faint grid, which differs from the
# fill by a few levels and fooled a plain any-other-colour test; drawn marks
# stand well clear of the dominant luminance. Sampled, so a large canvas stays
# cheap. A canvas without a readable 2D context (WebGL, tainted) cannot be
# judged and passes.
_READY_PAINTED_JS = """sel => { const c = document.querySelector(sel);
  if (!c || !c.width || !c.height) return false;
  let g = null; try { g = c.getContext('2d'); } catch (e) { return true; }
  if (!g) return true;
  let d; try { d = g.getImageData(0, 0, c.width, c.height).data; } catch (e) { return true; }
  const lum = [], hist = new Map();
  for (let i = 0; i < d.length; i += 4 * 61) {
    const l = Math.round((d[i] * 299 + d[i + 1] * 587 + d[i + 2] * 114) / 1000);
    lum.push(l); hist.set(l, (hist.get(l) || 0) + 1);
  }
  let bg = 0, best = -1;
  for (const [l, n] of hist) if (n > best) { best = n; bg = l; }
  return lum.filter(l => Math.abs(l - bg) > 40).length > 20; }"""


async def _wait_ready(page, settle_ms: int,
                      state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Wait for declarative panel readiness and return bounded evidence.

    Network-idle and asset settling are observations because WebSocket panels may
    never become idle. Explicit selectors/text are requirements. When neither is
    configured, meaningful body content is the required fallback.
    """
    state = state or {}
    evidence: List[Dict[str, Any]] = []

    async def _observe(condition: str, required: bool, timeout_ms: int, awaitable) -> None:
        try:
            await awaitable
            evidence.append({"condition": condition, "required": required, "status": "ready"})
        except Exception as exc:
            evidence.append({"condition": condition, "required": required, "status": "timeout"})
            if required:
                raise PanelReadinessError(condition, timeout_ms, type(exc).__name__,
                                          evidence) from exc

    await _observe("network_idle", False, 4000,
                   page.wait_for_load_state("networkidle", timeout=4000))
    ready_selector = str(state.get("ready_selector") or "")
    ready_text = str(state.get("ready_text") or "")
    ready_count = str(state.get("ready_count") or "")
    # A slow backend (prod's full fabric graph, a worldview projection) needs
    # longer than the defaults; a state may raise its own explicit waits.
    explicit_ms = int(state.get("ready_timeout_ms") or 0)
    # Only the fallback blocks on this, so a longer wait costs time only on a
    # panel with no explicit state - and a loaded prod needed more than 10s.
    content_required = not (ready_selector or ready_text or ready_count)
    content_ms = (explicit_ms or 30000) if content_required else 10000
    await _observe(
        "meaningful_content", content_required, content_ms,
        page.wait_for_function(_MEANINGFUL_CONTENT_JS, timeout=content_ms))
    if ready_selector:
        ms = explicit_ms or 15000
        await _observe(f"selector:{ready_selector}", True, ms,
                       page.wait_for_selector(ready_selector, state="visible", timeout=ms))
    if ready_text:
        ms = explicit_ms or 20000
        await _observe(f"text:{ready_text}", True, ms,
                       page.wait_for_function(_READY_TEXT_JS, arg=ready_text, timeout=ms))
    if ready_count:
        ms = explicit_ms or 20000
        await _observe(f"count:{ready_count}", True, ms,
                       page.wait_for_function(_READY_COUNT_JS, arg=ready_count, timeout=ms))
    # Text that must NOT be on screen when the shot is taken - a widget grid
    # whose reads failed renders every tile as "read failed" over sample data.
    # Widgets draw inside shadow roots, which body.innerText never sees.
    for bad in state.get("reject_texts") or []:
        ms = explicit_ms or 20000
        await _observe(f"absent:{bad}", True, ms, page.wait_for_function(
            "t => !(" + _DEEP_TEXT_JS + ")().toLowerCase().includes(t)",
            arg=str(bad).lower(), timeout=ms))
    ready_painted = str(state.get("ready_painted") or "")
    if ready_painted:
        ms = explicit_ms or 20000
        await _observe(f"painted:{ready_painted}", True, ms,
                       page.wait_for_function(_READY_PAINTED_JS, arg=ready_painted, timeout=ms))
    try:
        await page.evaluate("""async () => {
          if (document.fonts && document.fonts.ready) await document.fonts.ready;
          const imgs=[...document.images].filter(i => !i.complete);
          await Promise.all(imgs.map(i => new Promise(r => {
            i.addEventListener('load',r,{once:true}); i.addEventListener('error',r,{once:true});
            setTimeout(r,3000);
          })));
        }""")
        evidence.append({"condition": "assets_settled", "required": False, "status": "ready"})
    except Exception:
        evidence.append({"condition": "assets_settled", "required": False, "status": "timeout"})
    try:
        await page.wait_for_timeout(max(300, int(settle_ms)))
    except Exception:
        pass
    return evidence


async def _wait_target_up(base_url: str, max_s: float = 180.0, every_s: float = 6.0) -> bool:
    """True once GET /health answers 200 (within max_s)."""
    import httpx
    deadline = time.time() + max_s
    async with httpx.AsyncClient(verify=False, timeout=8) as client:
        while time.time() < deadline:
            try:
                r = await client.get(base_url.rstrip("/") + "/health")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(every_s)
    return False


_STEP_KINDS = ("click", "fill", "press", "hover", "select", "wait_for", "wait_ms",
               "code_server_login", "chat_send")

# Length of the rendered text, shadow roots and same-origin frames included -
# a reply is finished when this stops changing.
_TRANSCRIPT_SIZE_JS = """() => { let n = 0; const walk = (r, d) => { if (!r || d > 4) return;
  const w = document.createTreeWalker(r, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  for (let x = w.nextNode(); x; x = w.nextNode()) {
    if (x.nodeType === 3) { const p = x.parentElement; if (p && !/^(STYLE|SCRIPT)$/.test(p.tagName)) n += x.nodeValue.length; }
    else { if (x.shadowRoot) walk(x.shadowRoot, d + 1);
      if (x.tagName === 'IFRAME') { let doc = null; try { doc = x.contentDocument; } catch (e) {} if (doc && doc.body) walk(doc.body, d + 1); } } } };
  walk(document.body, 0); return n; }"""


async def _gpu_free(origin: str) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            d = (await client.get(origin + "/health")).json()
        gate, census = d.get("gpu_gate") or {}, d.get("census") or {}
        return int(gate.get("held") or 0) == 0 and not census.get("busy")
    except Exception:
        return False


async def _chat_send(page, text: str, ms: int) -> None:
    """Hold one turn of a real conversation in the chat page being captured.

    Only used by pages marked on_request (the README's chat): it calls the
    model. Waits for a free GPU slot and no census first - one GPU call at a
    time - then waits until the reply has stopped changing and the slot is
    free again."""
    from urllib.parse import urlparse
    u = urlparse(page.url)
    origin = f"{u.scheme}://{u.netloc}"
    deadline = time.time() + ms / 1000
    while not await _gpu_free(origin):
        if time.time() > deadline:
            raise TimeoutError("GPU slot never freed")
        await asyncio.sleep(10)
    box = page.locator('textarea[placeholder^="Type a message"]').first
    await box.click(timeout=15000)
    await box.fill(text, timeout=15000)
    await box.press("Enter")
    await page.wait_for_timeout(5000)
    last, stable = -1, 0
    while time.time() < deadline:
        await page.wait_for_timeout(5000)
        size = await page.evaluate(_TRANSCRIPT_SIZE_JS)
        stable = stable + 1 if size == last else 0
        last = size
        if stable >= 3 and await _gpu_free(origin):
            return
    raise TimeoutError("reply did not settle")


async def _code_server_login(page, instance_id: str, ms: int) -> None:
    """Log the capture browser into a code-server instance behind Vera's proxy.

    The password is fetched at capture time from the target's own
    ide.vscode.password.reveal and typed into the login form - it is never part
    of a capture state, a log line or the repository. A page that is not the
    login form (already logged in) is left alone."""
    import httpx
    from urllib.parse import urlparse
    form = page.locator('input[type="password"]').first
    try:
        await form.wait_for(state="visible", timeout=min(ms, 8000))
    except Exception:
        return
    u = urlparse(page.url)
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        r = await client.post(f"{u.scheme}://{u.netloc}/mcp/call",
                              json={"name": "ide.vscode.password.reveal",
                                    "arguments": {"instance_id": instance_id}})
    data = r.json() if r.status_code == 200 else {}
    body = data.get("content", data.get("result", data)) if isinstance(data, dict) else {}
    secret = (body or {}).get("password") if isinstance(body, dict) else None
    if not secret:
        raise RuntimeError(f"no code-server password for instance '{instance_id}'")
    await form.fill(secret, timeout=ms)
    await form.press("Enter")
    await page.wait_for_selector('input[type="password"]', state="detached", timeout=ms)


async def _run_steps(page, steps: List[Dict[str, Any]]) -> None:
    """Put a panel into the state the docs show - a menu opened, an item
    selected, a tab chosen. A panel loaded and left alone was mostly an empty
    shell ("Select an agent from the list"). Steps are declarative and trusted
    (they come from domain_map), each one of _STEP_KINDS; a step that cannot be
    done fails the capture by name rather than shooting the wrong state.
    Selectors pierce open shadow roots (Playwright CSS does)."""
    for i, step in enumerate(steps):
        kind = next((k for k in _STEP_KINDS if k in step), "")
        sel = str(step.get(kind) or "") if kind not in ("wait_ms", "press") else ""
        ms = int(step.get("timeout_ms") or 15000)
        try:
            if kind == "wait_ms":
                await page.wait_for_timeout(int(step["wait_ms"]))
            elif kind == "code_server_login":
                await _code_server_login(page, sel, ms)
            elif kind == "chat_send":
                await _chat_send(page, sel, int(step.get("timeout_ms") or 900000))
            elif kind == "wait_for":
                await page.wait_for_selector(sel, state="visible", timeout=ms)
            elif kind == "press":
                target = step.get("on")
                if target:
                    await page.locator(target).first.press(str(step["press"]), timeout=ms)
                else:
                    await page.keyboard.press(str(step["press"]))
            else:
                loc = page.locator(sel).nth(int(step.get("nth") or 0))
                if kind == "click":
                    await loc.click(timeout=ms)
                elif kind == "hover":
                    await loc.hover(timeout=ms)
                elif kind == "fill":
                    await loc.fill(str(step.get("text") or ""), timeout=ms)
                elif kind == "select":
                    await loc.select_option(str(step.get("value") or ""), timeout=ms)
                else:
                    raise ValueError(f"unknown step {step!r}")
            if step.get("then_ms"):
                await page.wait_for_timeout(int(step["then_ms"]))
        except Exception as exc:
            raise PanelReadinessError(f"step{i + 1}:{kind}:{sel or step.get(kind)}", ms,
                                      type(exc).__name__) from exc


async def _shoot_panel(session, url: str, abspath: str, *, settle_ms: int = 1400,
                       full_page: bool = False,
                       state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Navigate, prove readiness, then capture with diagnostic metadata."""
    page = session.page
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise PanelReadinessError("navigation:domcontentloaded", 30000,
                                      type(exc).__name__) from exc
        state = state or {}
        click = str(state.get("click") or "")
        # A view built at click time from data that is still arriving stays
        # empty: the galaxy's 2D view snapshots its nodes when opened, and was
        # opened at 1.2s with 0 nodes while the data landed at ~10s.
        click_after = str(state.get("click_after_count") or "")
        if click and click_after:
            ms = int(state.get("ready_timeout_ms") or 0) or 30000
            try:
                await page.wait_for_function(_READY_COUNT_JS, arg=click_after, timeout=ms)
            except Exception as exc:
                raise PanelReadinessError(f"click_after:{click_after}", ms,
                                          type(exc).__name__) from exc
        if click:
            try:
                await page.locator(click).first.click(timeout=10000)
            except Exception as exc:
                raise PanelReadinessError(f"click:{click}", 10000,
                                          type(exc).__name__) from exc
        await _run_steps(page, state.get("steps") or [])
        readiness = await _wait_ready(page, settle_ms, state)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        await page.screenshot(path=abspath, full_page=bool(full_page), type="png")
        return {"ok": True, "readiness": readiness}
    except PanelReadinessError as e:
        message = str(e)
        log.warning("operator/docs: shoot %s failed: %s", url, message)
        return {"ok": False, "failed_condition": e.condition, "error": message,
                "readiness": e.evidence}
    except Exception as e:
        log.warning("operator/docs: shoot %s failed: %s", url, e)
        return {"ok": False, "failed_condition": "capture", "error": str(e),
                "readiness": locals().get("readiness", [])}
