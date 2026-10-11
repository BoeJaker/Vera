"""intel's modes as switchboard switches (four-minds FM-13.3; mapping agreed with the intelligence unit, 11 Oct 2026).

intel's own config stays the one truth. A switch overrides its config key only when something SET it (a census arm,
a session, the global layer); at its default it changes nothing, so the registered defaults below only describe
today's behaviour. The mapping:

  intel.nlp_triage      on | off              -> "nlp" (off = keyword/heuristic triage, no nlp.* calls)
  intel.brief           llm | extractive      -> "brief_synthesize": the night brief's MoE top line + follow-ups;
                                                 extractive keeps the per-topic assessments and drops the
                                                 cross-topic pass (not a no-LLM mode)
  intel.auto_research   off | on              -> "auto_research"
  intel.material_alerts on | off              -> "alerts" (decisions are still recorded; only the send stops)
  intel.calibrated_gate shadow | on | off     -> "calibrated_gate" (on bites only once a topic's curve is usable);
                                                 off also stops computing p_shadow ("calibration_off")

Pure: no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping

SWITCHES = ("intel.nlp_triage", "intel.brief", "intel.auto_research", "intel.material_alerts",
            "intel.calibrated_gate")
#: seconds a read of the effective modes is reused - config() runs for every signal in triage
TTL_S = 15.0

_ON = {"on": True, "off": False}


def overrides(modes: Mapping[str, Any], source: Mapping[str, Any]) -> Dict[str, Any]:
    """intel config keys to override, from the effective switch modes and the layer that set each one; a switch at
    its default (source 'default' or missing) contributes nothing."""
    out: Dict[str, Any] = {}
    for sid in SWITCHES:
        if str((source or {}).get(sid) or "default") == "default":
            continue
        mode = str((modes or {}).get(sid) or "")
        if sid == "intel.nlp_triage" and mode in _ON:
            out["nlp"] = _ON[mode]
        elif sid == "intel.brief" and mode in ("llm", "extractive"):
            out["brief_synthesize"] = mode == "llm"
        elif sid == "intel.auto_research" and mode in _ON:
            out["auto_research"] = _ON[mode]
        elif sid == "intel.material_alerts" and mode in _ON:
            out["alerts"] = _ON[mode]
        elif sid == "intel.calibrated_gate" and mode in ("shadow", "on", "off"):
            out["calibrated_gate"] = mode == "on"
            out["calibration_off"] = mode == "off"
    return out
