"""A decorator binds to whatever def comes next - including the wrong one.

Inserting a helper immediately above a function is normally harmless. Above a
DECORATED function it silently re-points the registration:

    @capability("evolve.sandbox.prune", ...)
    def _probe_worktree(path): ...        # <- silently BECOMES the capability

    async def evolve_sandbox_prune(...):  # <- silently stops being one

That is valid Python. It parses, it imports, the call site still reads
correctly, and the module-level source still contains every string a
substring-based guard would look for. I shipped it on 2026-08-30 and
`evolve.sandbox.prune` started returning `_probe_worktree() missing 1 required
positional argument: 'path'` in prod - a fix for a destructive bug that broke
the very capability it was fixing.

The invariant that catches it: a capability's registered NAME should correspond
to the function it decorates. Across 2289 registered capabilities there are
exactly 16 benign abbreviations (`serve_loom_panel_js` for
`ui.graph_panels.loom_js`, `cap_fabric_delete` for `fabric.delete_dataset`),
frozen below. A decorator that has drifted onto an unrelated function does not
resemble its name at all, so it fails.

A stricter-sounding rule - "no private helper carries @capability" - was tried
first and is simply FALSE here: `_echo`, `_health`, `_ui_panel_specialist` and
`_caps_specialist` are legitimately private-named capabilities.
"""
import ast
import os

_VERA = os.path.join(os.path.dirname(__file__), "..", "vera")

#: (capability name, function name) pairs that do not match by shape but are
#: correct. Frozen 2026-08-30 from a full scan. A NEW entry here should be a
#: deliberate decision, not a way to silence this test.
KNOWN_ABBREVIATIONS = {
    ("ui.graph_panels.loom_js", "serve_loom_panel_js"),
    ("ui.graph_panels.example_js", "serve_example_panel_js"),
    ("ui.graph_panels.worldview_js", "serve_worldview_panel_js"),
    ("ui.graph_panels.api_js", "serve_api_panel_js"),
    ("ui.graph_panels.discover_js", "serve_discover_panel_js"),
    # Same convention as the five above: the capability is `<panel>_js` and the
    # function is `serve_<panel>_panel_js`. Checked by reading vera_graph_panels.py
    # — the decorator sits directly on serve_explode_panel_js, which serves
    # vera_graph_panel_explode.js. Added 2026-09-20 as a deliberate entry, not to
    # silence the test: renaming the function to satisfy _resembles() would make
    # it the only one of six siblings not following the house pattern.
    ("ui.graph_panels.explode_js", "serve_explode_panel_js"),
    ("integration.panel.html", "cap_panel"),
    ("netmon.snapshot", "cap_netmon"),
    ("netscan.dork.search", "cap_netscan_dork"),
    ("workshop.dag_to_cap_preview", "cap_workshop_dag_preview"),
    ("identity.migrate.panel.html", "cap_migrate_panel"),
    ("markets.trader.config.set", "cap_trader_config"),
    ("ui.elements.live_event_stream_js", "serve_les_js"),
    ("ui.elements.system_log_js", "serve_sl_js"),
    ("fabric.entity_graph.ner_labels", "cap_ner_set_labels"),
    ("fabric.delete_dataset", "cap_fabric_delete"),
    ("fabric.link_datasets", "cap_fabric_link"),
}


def _modules():
    for root, _dirs, files in os.walk(_VERA):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _capability_names(node):
    for d in node.decorator_list:
        if not isinstance(d, ast.Call):
            continue
        f = d.func
        nm = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else None)
        if nm == "capability" and d.args and isinstance(d.args[0], ast.Constant) \
                and isinstance(d.args[0].value, str):
            yield d.args[0].value


def _resembles(cap_name, func_name):
    fn = func_name.lstrip("_")
    flat = cap_name.replace(".", "_").replace("-", "_")
    last = cap_name.split(".")[-1].replace("-", "_")
    return (fn == flat or fn.endswith(flat) or last in fn
            or fn.replace("_", "") in flat.replace("_", "")
            or flat.replace("_", "") in fn.replace("_", ""))


def _scan():
    pairs = []
    for path in _modules():
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for cap in _capability_names(n):
                    pairs.append((cap, n.name, os.path.basename(path), n.lineno))
    return pairs


def test_no_capability_is_bound_to_an_unrelated_function():
    offenders = [
        "%s:%d  @capability(%r) <- def %s" % (mod, line, cap, fn)
        for cap, fn, mod, line in _scan()
        if not _resembles(cap, fn) and (cap, fn) not in KNOWN_ABBREVIATIONS
    ]
    assert not offenders, (
        "a decorator appears to have bound to the wrong function:\n"
        + "\n".join(offenders))


def test_the_prune_capability_is_still_bound_to_the_prune():
    """The exact regression, pinned by name."""
    with open(os.path.join(_VERA, "evolve", "evolve_capabilities.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in ("evolve_sandbox_prune", "_probe_worktree")}
    assert found["evolve_sandbox_prune"].decorator_list, \
        "evolve_sandbox_prune lost its @capability"
    assert not found["_probe_worktree"].decorator_list, \
        "_probe_worktree stole a decorator"


def test_the_scan_is_not_vacuous():
    """If the scan stops finding capabilities, the guard is worthless."""
    assert len(_scan()) > 2000


def test_the_known_abbreviations_are_all_still_real():
    """A stale allowlist entry hides a future drift onto that same name."""
    live = {(cap, fn) for cap, fn, _m, _l in _scan()}
    assert KNOWN_ABBREVIATIONS <= live, (
        "allowlisted pairs no longer exist and should be removed: %s"
        % sorted(KNOWN_ABBREVIATIONS - live))
