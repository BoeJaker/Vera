"""
agent_loop_output_capabilities.py
==================================
Registers the <vera-agent-loop-output> custom element with Vera's caps /
panels / elements system so it can be re-used anywhere — chat UI, the
capability_orchestration sub-panels, dream panel, or any future panel.

What this gives you
───────────────────
1. **GET /ui/elements/agent_loop_output.js** — serves the element definition
   as a standalone JS include. Any panel can `<script src=…>` it once and
   then drop `<vera-agent-loop-output>` anywhere it needs an agentic-loop
   stream renderer.

2. **register_ui("agent-loop-output", mode="inject", …)** — registers the
   element as an "injectable" UI panel so it appears in the panels menu /
   media sub-switcher just like other inject elements (see
   cap_hub_capabilities.py for the established pattern: cap-list,
   cap-search, activity-track, etc.). The HTML body is just the script
   include + the custom-element tag.

3. The element supports multiple instances on the same page. Each instance
   has its own shadow DOM, its own state, and its own optional bound
   stream — so the capability_orchestrator panel and any of its sub-panels
   can each spawn one without interference.

The element source itself lives in agent_loop_output_element.js next to
this file. We read it from disk on startup (and re-read on every request
in dev mode) so iterating on the JS doesn't require a server restart.

Public API on the element (see the JS file's header for the full doc):
  el.appendEvent(ev)        — feed one parsed SSE event
  el.bindStream(url, body)  — fetch /POST and stream events into the element
  el.reset()                — clear everything
  el.abort()                — cancel a bound stream
  el.getResult()            — last result/done payload
  el.setSessionId(sid)      — used for HITL respond callbacks
  el.setApiBase(url)        — override API base
  el.setShowThinking(bool)
  el.setHitlEndpoint(url)

Events dispatched by the element:
  alo:cycle-start, alo:tool-call, alo:tool-done, alo:hitl-request,
  alo:hitl-resolved, alo:done, alo:final, alo:error
"""

from __future__ import annotations

import logging

from Vera.Orchestration.capability_orchestration import (
    register_ui,
)

log = logging.getLogger("vera.agent_loop_output")


# ─────────────────────────────────────────────────────────────────────────────
# NOTE: The JS file is served via @APP.get("/ui/elements/agent_loop_output.js")
# in ui_capabilities.py, following the same pattern as vera-ui.js and
# vera-graph.js. The old @capability route didn't work because the capability
# pipeline doesn't properly serve raw JS responses.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Register as an injectable UI element
# ─────────────────────────────────────────────────────────────────────────────
# The HTML body is just the script include plus the custom-element tag. The
# script tag has a stable id so multiple register_ui injects on the same page
# only load it once (the harness de-dupes by id).
#
# `mode="inject"` — appears in the panels menu / media sub-switcher (the same
# treatment as cap-list, cap-search, etc.). Hosts that want to embed the
# element directly (chat UI, capability_orchestration sub-panels) can also
# just drop the <script src> and the tag without going through register_ui.
# ─────────────────────────────────────────────────────────────────────────────

_ELEMENT_SCRIPT_INCLUDE = (
    '<script id="vera-agent-loop-output-js-include" '
    'src="/ui/elements/agent_loop_output.js"></script>'
)

_INJECT_HTML = (
    f"{_ELEMENT_SCRIPT_INCLUDE}\n"
    '<vera-agent-loop-output style="display:block;width:100%;height:100%"></vera-agent-loop-output>'
)

register_ui(
    panel_id="agent-loop-output",
    label="Agent Loop Output",
    icon="↻",
    mode="inject",
    tab_order=204,
    html=_INJECT_HTML,
    ui_caps=[],
)

log.info(
    "agent-loop-output: registered as injectable element "
    "(<vera-agent-loop-output>); JS at /ui/elements/agent_loop_output.js"
)