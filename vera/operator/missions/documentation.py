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


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "panel")).strip("-") or "panel"


async def _fetch_json(client: Any, url: str) -> Any:
    try:
        r = await client.get(url, timeout=15)
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

    docs_dir = os.path.join(repo_root, "documentation")
    assets_dir = os.path.join(docs_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    session = None
    if do_capture:
        try:
            session = await _be.start_session(base_url=base_url)
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

    try:
        for domain in domains:
            slug = domain["slug"]
            dpanels = by_domain.get(slug, [])
            # include explicitly-listed panel ids that exist live but weren't assigned here
            listed = {p.lower() for p in domain.get("panel_ids", [])}
            for p in panels:
                if str(p.get("id", "")).lower() in listed and p not in dpanels:
                    dpanels.append(p)
            caps = _caps_for_domain(domain, all_caps)
            shots: List[Dict[str, Any]] = []

            # seed
            seed_name = domain.get("seed")
            if do_capture and seed_name:
                await _emit("seed", f"[{slug}] seeding ({seed_name})")
                await _seeds.run_seed(seed_name, _make_call_target(base_url))

            # capture each panel
            dom_asset_dir = os.path.join(assets_dir, slug)
            for p in dpanels:
                pid = p.get("id", "")
                if not pid:
                    continue
                if panel_filter and pid not in panel_filter:
                    continue  # selective re-capture skips everything else
                mode = "seeded" if seed_name else "default"
                for state in _capture_states(domain, pid):
                    shot_id = _state_name(pid, state)
                    rel = f"assets/{slug}/{shot_id}.png"
                    abspath = os.path.join(docs_dir, rel)
                    label = state.get("label") or p.get("label") or pid
                    if do_capture and session is not None:
                        cap_url = panel_capture_url(base_url, p)
                        ok = await _shoot_panel(
                            session, cap_url["url"], abspath,
                            settle_ms=max(settle_ms, int(state.get("settle_ms", 0) or 0)),
                            full_page=bool(state.get("full_page", full_page)), state=state)
                        if not ok:
                            continue
                        total_shots += 1
                        await _emit("shot", f"[{slug}] {label}",
                                    domain=slug, panel_id=pid, shot_id=shot_id,
                                    label=label, rel=rel,
                                    url=f"/docs/asset?path={rel}", via=cap_url["via"])
                    elif not os.path.exists(abspath):
                        continue
                    os.makedirs(dom_asset_dir, exist_ok=True)
                    shots.append({"panel_id": shot_id, "source_panel_id": pid,
                                  "label": label, "rel_path": rel,
                                  "caption": state.get("caption") or label,
                                  "mode": mode, "via": (cap_url["via"] if do_capture else "")})

            # write doc auto-blocks
            number = domain["doc"].split("-")[0]
            doc_path = os.path.join(docs_dir, domain["doc"])
            if write_docs:
                existing = None
                if os.path.exists(doc_path):
                    with open(doc_path, "r", encoding="utf-8") as f:
                        existing = f.read()
                content = _scaffold.build_doc(
                    existing, number=number, title=domain["title"],
                    screenshots_content=_scaffold.render_screenshots_block(shots),
                    caps_content=_scaffold.render_caps_block(caps))
                with open(doc_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # Always merge into the existing manifest entry so a partial run (a
            # selective re-capture, or a domain that captured fewer panels this
            # time) never drops previously-recorded images.
            prev = dict(manifest_prev.get("domains", {}).get(slug, {}))
            prev_panels = {pp.get("id"): pp for pp in prev.get("panels", [])}
            for s in shots:
                prev_panels[s["panel_id"]] = {
                    "id": s["panel_id"], "label": s["label"], "shot": s["rel_path"],
                    "mode": s["mode"], "via": s.get("via", "")}
            manifest["domains"][slug] = {
                "doc": domain["doc"], "title": domain["title"],
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
        if not info:
            continue
        pnls = info.get("panels", [])
        gallery_entries.append({
            "slug": d["slug"], "title": info.get("title", d["title"]),
            "doc": info.get("doc", d["doc"]),
            "cover_rel": pnls[0]["shot"] if pnls else "",
            "shot_count": len(pnls), "cap_count": info.get("cap_count", 0)})

    # gallery + manifest
    if write_docs:
        gal = _gallery.build_gallery(gallery_entries, generated_at=_iso(),
                                     total_caps=len(all_caps))
        with open(os.path.join(docs_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(gal)
    with open(os.path.join(assets_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    await _emit("done", f"documentation built: {total_shots} screenshots across "
                        f"{len(domains)} domains")
    return {"ok": True, "base_url": base_url, "target_kind": resolved.get("kind"),
            "domains": len(domains), "panels_discovered": len(panels),
            "screenshots": total_shots, "capabilities": len(all_caps),
            "captured": do_capture, "capture_note": capture_note,
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


def panel_capture_url(base_url: str, panel: Dict[str, Any]) -> Dict[str, str]:
    """Where to actually screenshot a panel.

    Most tab panels register an ``<iframe src="/xxx/panel">`` — that src is the
    REAL panel route, which renders with full fidelity. Screenshot it directly
    rather than the ``/ui/panel/window`` wrapper (whose degraded shim is why
    several panels came out wrong). Inline/element panels have no iframe, so we
    fall back to the panel window. Returns {url, via}."""
    base = base_url.rstrip("/")
    html = panel.get("html") or ""
    m = _IFRAME_SRC.search(html)
    if m:
        src = m.group(1)
        if src.startswith("http"):
            return {"url": src, "via": "route"}
        return {"url": base + "/" + src.lstrip("/"), "via": "route"}
    return {"url": f"{base}/ui/panel/window?id={panel.get('id')}", "via": "window"}


async def _wait_ready(page, settle_ms: int, state: Optional[Dict[str, Any]] = None) -> None:
    """Give a panel time to actually render: best-effort network-idle, then wait
    for real content, then a settle delay for charts / async fetches / WS data."""
    state = state or {}
    try:
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass  # WS-backed panels never go idle — that's fine
    try:
        # Wait until the body has meaningful rendered content (not a blank shell).
        await page.wait_for_function(
            "() => { const b=document.body; if(!b) return false; "
            "const text=(b.innerText||'').replace(/loading[….]*/ig,'').trim(); "
            "return b.scrollHeight > 120 && (text.length > 40 "
            "|| b.querySelector('canvas,svg,img[src],table tbody tr')); }",
            timeout=10000)
    except Exception:
        pass
    ready_selector = str(state.get("ready_selector") or "")
    if ready_selector:
        try:
            await page.wait_for_selector(ready_selector, state="visible", timeout=15000)
        except Exception:
            pass
    ready_text = str(state.get("ready_text") or "")
    if ready_text:
        try:
            await page.wait_for_function(
                """sel => { const e=document.querySelector(sel);
                if(!e) return false; const t=(e.innerText||e.textContent||'').trim();
                return t.length > 2 && !/^(loading|starting|—|[.]{3})/i.test(t); }""",
                ready_text, timeout=20000)
        except Exception:
            pass
    try:
        await page.evaluate("""async () => {
          if (document.fonts && document.fonts.ready) await document.fonts.ready;
          const imgs=[...document.images].filter(i => !i.complete);
          await Promise.all(imgs.map(i => new Promise(r => {
            i.addEventListener('load',r,{once:true}); i.addEventListener('error',r,{once:true});
            setTimeout(r,3000);
          })));
        }""")
    except Exception:
        pass
    try:
        await page.wait_for_timeout(max(300, int(settle_ms)))
    except Exception:
        pass


async def _shoot_panel(session, url: str, abspath: str, *, settle_ms: int = 1400,
                       full_page: bool = False,
                       state: Optional[Dict[str, Any]] = None) -> bool:
    """Navigate to a panel's real URL and save a screenshot. Returns success."""
    page = session.page
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        state = state or {}
        click = str(state.get("click") or "")
        if click:
            await page.locator(click).first.click(timeout=10000)
        await _wait_ready(page, settle_ms, state)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        await page.screenshot(path=abspath, full_page=bool(full_page), type="png")
        return True
    except Exception as e:
        log.warning("operator/docs: shoot %s failed: %s", url, e)
        return False
