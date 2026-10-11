"""What a recorded decision saw, kept so a student can learn it (four-minds FM-0.2, the payload rule).

A ledger row holds `state_hash` and a `state_ref`, never the state (ROADMAP §5.1). That keeps the
ledger small and shareable, but a hash cannot train anything: a student for `loop.intent` needs the
goal text, one for `loop.step_verify` the step, its success criterion and the result it judged. The
rendered inputs therefore go to a separate training store, keyed by decision_id so a training set
is one join of decision, inputs and outcome.

The rules, from the roadmap: per surface and only where enabled (default loop on, chat off),
redacted for credentials and personal content, bounded, kept 90 days, purgeable.

Pure: no I/O. The credential redactor is passed in (the runtime uses log_setup.redact, the same
masking the log file gets).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, Optional, Set

# surface -> stored by default. The owner turns a surface on or off (VERA_TRAINING_STATE_SURFACES).
# discovery: the drift guard's page verdicts (public web excerpts), labelled for the FM-7.5 200-page set
# intel: the intelligence gates' signals (public web titles, excerpts, topic names), rated in /decide/labels (FM-13.6)
SURFACE_DEFAULTS: Dict[str, bool] = {"loop": True, "chat": False, "intel": True, "memory": False,
                                     "discovery": True}
RETENTION_DAYS = 90
MAX_FIELD = 4000          # characters kept of any one string
MAX_LIST = 30             # items kept of any one list
MAX_TOTAL = 40000         # bytes of the whole rendered state (JSON)
LONG_FIELDS = {"evidence": 16000, "evidence_system": 16000}   # a model's whole prompt and rules (the live verifier's)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Phone numbers: international (+44 ...) or national with a leading 0 (07700 900123, 020 7946 0958).
# Deliberately narrow, so dates (2026-10-08), versions and ids pass untouched.
_PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ -]?\(?\d{1,4}\)?|\(?0\d{2,4}\)?)[ -]?\d{3,4}[ -]?\d{3,4}(?!\w)")


def enabled_surfaces(setting: Optional[str] = None) -> Set[str]:
    """The surfaces whose decision inputs are stored. None or blank: the defaults (loop only);
    "none": nothing; otherwise a comma list ("loop,chat")."""
    s = (setting or "").strip().lower()
    if not s:
        return {k for k, on in SURFACE_DEFAULTS.items() if on}
    if s in ("none", "off", "0", "false"):
        return set()
    return {p.strip() for p in s.split(",") if p.strip()}


def redact_text(text: str, cred: Optional[Callable[[str], str]] = None) -> str:
    """Credentials (via `cred`), then e-mail addresses and phone numbers."""
    out = str(text)
    if cred is not None:
        try:
            out = cred(out)
        except Exception:
            pass
    if "@" in out:
        out = _EMAIL.sub("<email>", out)
    return _PHONE.sub("<phone>", out)


def _render(v: Any, cred, limit: int) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        s = redact_text(v, cred)
        return s if len(s) <= limit else s[:limit] + "…"
    if isinstance(v, dict):
        return {str(k)[:60]: _render(x, cred, limit) for k, x in list(v.items())[:MAX_LIST]}
    if isinstance(v, (list, tuple, set)):
        return [_render(x, cred, limit) for x in list(v)[:MAX_LIST]]
    return _render(str(v), cred, limit)


def render(inputs: Dict[str, Any], cred: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """The inputs as stored: redacted, every string and list bounded, the whole under MAX_TOTAL
    (strings are shortened further until it fits). A LONG_FIELDS string (a model's whole prompt, so a
    student can be shown exactly what the model saw) keeps its own larger bound and its share of the total."""
    inputs = dict(inputs or {})
    long = {k: _render(inputs.pop(k), cred, LONG_FIELDS[k]) for k in list(inputs)
            if k in LONG_FIELDS and isinstance(inputs[k], str)}
    budget = MAX_TOTAL - sum(len(json.dumps(v)) for v in long.values())
    limit = MAX_FIELD
    out = _render(inputs, cred, limit)
    while len(json.dumps(out, default=str)) > budget and limit > 100:
        limit //= 2
        out = _render(out, None, limit)
    out.update(long)
    return out


def state_row(decision: Dict[str, Any], rendered: Dict[str, Any]) -> Dict[str, Any]:
    return {"decision_id": decision["decision_id"], "question_id": decision.get("question_id"),
            "surface": decision.get("surface") or "", "session_id": decision.get("session_id") or "",
            "state": rendered}


def wanted(decision: Dict[str, Any], surfaces: Iterable[str]) -> bool:
    return bool(decision) and str(decision.get("surface") or "") in set(surfaces)
