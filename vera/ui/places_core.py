"""Where a reading lives in the UI - one table, so a drill-through never guesses.

A PLACE is a name for a page the shell can open: a top-level tab, a pane (and
sub-tab) of the Estate or Models panel, a view of a panel that hosts other
panels (Automations), or an element of the Elements switcher. The dashboard's
records name a place with `open`; a record without one takes the place its
SOURCE maps to (the rules below, first match wins), so a hand-made widget over
`backup.status` opens Estate > Storage without anyone wiring it.

Pure data plus three functions, so it tests without booting Vera
(tests/test_ui_places.py). `ui.places` serves it; the shell's openPlace()
consumes the resolved spec:

  {"panel": id}                     switchTab('auto-' + id)
  {"panel": id, "pane", "sub"}      … then vera:estate:open to that panel
  {"panel": id, "nav": item}        … then vera:panel:action nav_select
  {"tab": name}                     a static tab of the shell (dashboard, media)
  {"tab": "media", "media": id}     … then the element of the switcher

The panel ids are the registry's (ui.panels); test_ui_places pins them to the
ids the modules register, and the Estate panes/subs to the Estate panel's
markup, so a renamed pane fails a test instead of a click.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ESTATE_PANEL = "workers-ollama"        # the Estate tab (its Models view is the `models` tab)
MODELS_PANEL = "models"

# ── the places ────────────────────────────────────────────────────────────────
PLACES: Dict[str, Dict[str, str]] = {
    # the Estate tab and its panes
    "estate":                    {"panel": ESTATE_PANEL, "pane": "overview"},
    "estate/map":                {"panel": ESTATE_PANEL, "pane": "estate"},
    "estate/observe":            {"panel": ESTATE_PANEL, "pane": "observe", "sub": "events"},
    "estate/perf":               {"panel": ESTATE_PANEL, "pane": "observe", "sub": "perf"},
    "estate/ops":                {"panel": ESTATE_PANEL, "pane": "ops"},
    "estate/machines":           {"panel": ESTATE_PANEL, "pane": "machines"},
    "estate/workers":            {"panel": ESTATE_PANEL, "pane": "workers"},
    "estate/proxmox":            {"panel": ESTATE_PANEL, "pane": "proxmox"},
    "estate/docker":             {"panel": ESTATE_PANEL, "pane": "docker"},
    "estate/sandbox":            {"panel": ESTATE_PANEL, "pane": "sandbox"},
    "estate/connections":        {"panel": ESTATE_PANEL, "pane": "connections"},
    "estate/remote":             {"panel": ESTATE_PANEL, "pane": "remote"},
    "estate/storage":            {"panel": ESTATE_PANEL, "pane": "storage"},
    "estate/network":            {"panel": ESTATE_PANEL, "pane": "network", "sub": "graph"},
    "estate/network/traffic":    {"panel": ESTATE_PANEL, "pane": "network", "sub": "ops"},
    "estate/network/netctl":     {"panel": ESTATE_PANEL, "pane": "network", "sub": "netctl"},
    "estate/trust":              {"panel": ESTATE_PANEL, "pane": "provision", "sub": "security"},
    "estate/trust/secrets":      {"panel": ESTATE_PANEL, "pane": "provision", "sub": "secrets"},
    "estate/trust/certs":        {"panel": ESTATE_PANEL, "pane": "provision", "sub": "certs"},
    "estate/trust/registration": {"panel": ESTATE_PANEL, "pane": "provision", "sub": "registration"},
    "estate/trust/identity":     {"panel": ESTATE_PANEL, "pane": "provision", "sub": "identity"},
    "estate/trust/enroll":       {"panel": ESTATE_PANEL, "pane": "provision", "sub": "enroll"},
    "estate/trust/mesh":         {"panel": ESTATE_PANEL, "pane": "provision", "sub": "mesh"},
    "estate/build":              {"panel": ESTATE_PANEL, "pane": "build"},
    "estate/foundry":            {"panel": ESTATE_PANEL, "pane": "foundry"},
    "estate/software":           {"panel": ESTATE_PANEL, "pane": "software"},
    "estate/integrations":       {"panel": ESTATE_PANEL, "pane": "integrations"},
    "estate/platforms":          {"panel": ESTATE_PANEL, "pane": "platforms"},
    # the Models tab (the same panel, its models view)
    "models":                    {"panel": MODELS_PANEL, "pane": "ollama"},
    "models/routing":            {"panel": MODELS_PANEL, "pane": "modelrouting"},
    "models/mimic":              {"panel": MODELS_PANEL, "pane": "mimic"},
    "models/vllm":               {"panel": MODELS_PANEL, "pane": "vllm"},
    "models/api":                {"panel": MODELS_PANEL, "pane": "api"},
    # top-level tabs
    "dashboard":                 {"tab": "dashboard"},
    "agents":                    {"panel": "agents-skills-ontologies"},
    "capabilities":              {"panel": "cap-hub"},
    "chat":                      {"panel": "chat2"},
    "fabric":                    {"panel": "fabric-panel"},
    "activity":                  {"panel": "activity"},
    "ide":                       {"panel": "ide-panel"},
    "exec":                      {"panel": "exec-panel"},
    "research":                  {"panel": "research-panel"},
    "notebook":                  {"panel": "notebook-panel"},
    "workspaces":                {"panel": "workspaces"},
    "galaxy":                    {"panel": "memory-galaxy-panel"},
    "godseye":                   {"panel": "godseye"},
    "mesh":                      {"panel": "mesh"},
    "evolve":                    {"panel": "evolve"},
    "dream":                     {"panel": "dream-panel"},
    "markets":                   {"panel": "markets"},
    "business":                  {"panel": "business"},
    "comms":                     {"panel": "comms-panel"},
    "gallery":                   {"panel": "gallery"},
    "image-studio":              {"panel": "image-studio"},
    "canvas":                    {"panel": "canvas"},
    # the Automations tab hosts the flow builder, n8n, OpenClaw, the Operator and Home Assistant
    "automations":               {"panel": "automations", "nav": "overview"},
    "automations/actions":       {"panel": "automations", "nav": "actions"},
    "automations/flows":         {"panel": "automations", "nav": "flows"},
    "automations/n8n":           {"panel": "automations", "nav": "n8n"},
    "automations/openclaw":      {"panel": "automations", "nav": "openclaw"},
    "automations/operator":      {"panel": "automations", "nav": "operator"},
    "automations/home":          {"panel": "automations", "nav": "home"},
    # the Elements switcher (mode="inject" panels)
    "elements/monitor":          {"tab": "media", "media": "system-monitor"},
    "elements/perf":             {"tab": "media", "media": "perf-monitor"},
    "elements/calendar":         {"tab": "media", "media": "calendar-panel"},
    "elements/telegram":         {"tab": "media", "media": "telegram-panel"},
    "elements/worldview":        {"tab": "media", "media": "worldview"},
    "elements/events":           {"tab": "media", "media": "live-event-stream"},
    "elements/jobs":             {"tab": "media", "media": "job-stream"},
    "elements/syslog":           {"tab": "media", "media": "system-log"},
}

# ── which place a SOURCE opens, by capability name; first match wins ──────────
# A rule is (prefix, place). Order matters: the specific reading before its group.
SOURCE_RULES: List[Tuple[str, str]] = [
    ("sysmon.",             "estate/ops"),
    ("obs.health",          "estate/ops"),
    ("obs.node_temps",      "estate/ops"),
    ("obs.events",          "activity"),
    ("obs.scheduler",       "estate/workers"),
    ("obs.workers",         "estate/workers"),
    ("obs.diagnostics",     "estate/observe"),
    ("obs.",                "estate/observe"),
    ("perf.",               "estate/perf"),
    ("topology.",           "estate/map"),
    ("estate.",             "estate"),
    ("nodes.agent",         "estate/workers"),
    ("nodes.",              "estate/machines"),
    ("backup.",             "estate/storage"),
    ("vfs.",                "estate/storage"),
    ("pxstore.",            "estate/storage"),
    ("docker.",             "estate/docker"),
    ("proxmox.",            "estate/proxmox"),
    ("evolve.sandbox",      "estate/sandbox"),
    ("netmon.",             "estate/network/traffic"),
    ("netscan.",            "estate/network"),
    ("netsec.",             "estate/trust/mesh"),
    ("identity.",           "estate/trust/identity"),
    ("certs.",              "estate/trust/certs"),
    ("secrets.",            "estate/trust/secrets"),
    ("autoenroll.",         "estate/trust/enroll"),
    ("provision.",          "estate/software"),
    ("foundry.",            "estate/foundry"),
    ("ollama.route",        "models/routing"),
    ("ollama.routing",      "models/routing"),
    ("ollama.cap_routing",  "models/routing"),
    ("ollama.",             "models"),
    ("catalog.",            "models"),
    ("bench.",              "models"),
    ("vllm.",               "models/vllm"),
    ("providers.",          "models/api"),
    ("cluster.mimic",       "models/mimic"),
    ("cluster.",            "models"),
    ("memory.",             "fabric"),
    ("fabric.",             "fabric"),
    ("worldview.",          "fabric"),
    ("evolve.",             "evolve"),
    ("loops.",              "agents"),
    ("agent.",              "agents"),
    ("project.",            "agents"),
    ("workshop.",           "automations/flows"),
    ("dream.",              "dream"),
    ("background.",         "activity"),
    ("activity.",           "activity"),
    ("jobs.",               "estate/workers"),
    ("mesh.",               "mesh"),
    ("ide.",                "ide"),
    ("ha.",                 "automations/home"),
    ("n8n.",                "automations/n8n"),
    ("openclaw.",           "automations/openclaw"),
    ("cal.",                "elements/calendar"),
    ("tg.",                 "elements/telegram"),
    ("research.",           "research"),
    ("markets.",            "markets"),
    ("cap.",                "capabilities"),
]


def place_for(source: Any, explicit: Any = None) -> str:
    """The place a record opens: its own `open` when it names a known place,
    else the first rule its source matches, else ''."""
    if explicit and str(explicit) in PLACES:
        return str(explicit)
    s = str(source or "")
    for prefix, place in SOURCE_RULES:
        if s.startswith(prefix):
            return place
    return ""


def resolve(place: Any) -> Optional[Dict[str, str]]:
    """The shell's spec for a place, or None for a name the table does not know."""
    spec = PLACES.get(str(place or ""))
    return dict(spec) if spec else None


def unknown_places(records: Iterable[Mapping[str, Any]]) -> List[str]:
    """Every `open` on the records (and their children) that names no place -
    the layout test refuses a layout that has one."""
    bad: List[str] = []
    for r in records:
        if not isinstance(r, Mapping):
            continue
        o = r.get("open")
        if o and str(o) not in PLACES:
            bad.append(f"{r.get('id') or r.get('title') or '?'}: {o}")
        for c in r.get("children") or []:
            cr = c.get("record") if isinstance(c, Mapping) else None
            if isinstance(cr, Mapping) and cr.get("open") and str(cr["open"]) not in PLACES:
                bad.append(f"{r.get('id') or '?'}/{c.get('slot') or '?'}: {cr['open']}")
    return bad


def table() -> Dict[str, Any]:
    """What ui.places serves: the places, the rules, and the panels they need."""
    return {
        "places": {k: dict(v) for k, v in PLACES.items()},
        "rules": [list(r) for r in SOURCE_RULES],
        "panels": sorted({v["panel"] for v in PLACES.values() if v.get("panel")}),
        "estate_panel": ESTATE_PANEL,
    }
