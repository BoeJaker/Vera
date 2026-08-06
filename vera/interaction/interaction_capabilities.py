"""
interaction_capabilities.py — live "Estate" infrastructure map
==============================================================
An SVG map of the **backend estate outside Vera** — the Docker stack, the
Proxmox stack, and how every host is wired into the provisioning + security
systems (FreeIPA directory/CA, OpenBao vault, WireGuard mesh). Hosts sit on the
left with their security posture as badges (🔐 cert SSH · 🕸️ on mesh · 🆔 in
FreeIPA); the managers/security fabric sit on the right; edges show which
systems each host is enrolled into. It composes the fast provisioning caps
(proxmox.cluster.list, workers.docker.hosts, enroll.ssh.host.list,
netsec.mesh.members, identity.host.list/status, openbao.seal.status) client-side,
with an optional "Deep scan" for the slow full guest/container list.

It is *alive*: the panel polls `/events` and animates each real infrastructure
event as a pulse to the system that produced it. No new backend state.

Rendered as an embedded **sub-tab of Workers & Ollama** (mode="element", not a
top-level tab) — the panel is served at /interaction/panel and iframed there.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

from Vera.vera.capability_orchestration import APP, register_ui

_HERE = Path(__file__).parent


@APP.get("/interaction/panel", include_in_schema=False)
async def _interaction_panel():
    p = _HERE / "interaction_panel.html"
    return HTMLResponse(
        p.read_text(encoding="utf-8") if p.exists()
        else "<p style='color:red'>interaction_panel.html not found</p>"
    )


register_ui(
    "interaction-map",
    "Estate",
    "🛰️",
    """<div style="height:100%;display:flex;flex-direction:column">
  <iframe src="/interaction/panel"
          style="flex:1;border:none;width:100%;height:100%;background:var(--bg0,#0d0f12)"
          title="Estate — infrastructure, provisioning & security"></iframe>
</div>""",
    "",
    ui_caps=["obs.events", "proxmox.cluster.list", "workers.docker.hosts",
             "netsec.mesh.members", "identity.host.list"],
    mode="element",      # embedded as a Workers & Ollama sub-tab, not a top-level tab
)
