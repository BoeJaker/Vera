"""vera.operator.missions — applications built on the operator.

A *mission* is a higher-level task the operator performs end-to-end. The first
is ``documentation`` (screenshot Vera's own UI + regenerate the docs); the
registry is open for future missions (web RPA, QA flows, driving a VM …).

Missions are resolved lazily so importing this package stays dependency-light
(the documentation mission pulls in ``httpx`` only when actually run). Each
mission coroutine takes ``(params: dict, ctx: dict)`` and returns a result dict,
where ``ctx`` provides ``call_cap`` (in-process cap dispatch), ``emit`` (event
emitter) and ``repo_root``.
"""

from __future__ import annotations

from typing import Any, Dict

# name → one-line description (kept import-light; the coroutine is loaded lazily)
_MISSIONS: Dict[str, str] = {
    "documentation": "Screenshot every Vera UI panel (seeded) and regenerate the "
                     "documentation set + gallery.",
}


def list_missions() -> Dict[str, str]:
    return dict(_MISSIONS)


async def run_mission(name: str, params: Dict[str, Any],
                      ctx: Dict[str, Any]) -> Dict[str, Any]:
    if name == "documentation":
        from .documentation import run_documentation_mission
        return await run_documentation_mission(params or {}, ctx or {})
    if name not in _MISSIONS:
        return {"error": f"unknown mission '{name}'. Available: {', '.join(_MISSIONS)}"}
    return {"error": f"mission '{name}' has no runner yet"}
