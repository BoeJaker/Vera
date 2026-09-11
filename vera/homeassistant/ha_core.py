"""
ha_core.py — pure logic for the Home Assistant integration
==========================================================

No app, no network, no Redis: everything here is a function over plain data so
it can be unit-tested without booting Vera (`tests/test_ha_core.py` imports it
as `vera.homeassistant.ha_core`).

Three jobs, and each exists because of a fault seen on the real instance:

  URL normalisation   A Home Assistant integration on this estate was saved
                      with the host and port but no scheme, and the HTTP
                      client it feeds rejected every request with "No
                      connection adapters were found for '192.168.0.94:8096'".
                      Vera writes the base URL for its own HA connection the
                      same way, so a missing scheme is repaired here once
                      rather than being discovered later as a dead integration.

  Entity resolution   Speech and chat name things the way a person does — "the
                      bedside lamp", "kitchen" — not `switch.bedside_lamp_
                      socket_1`. Ranking a spoken phrase against entity_id AND
                      friendly_name is what makes `ha.set`/`ha.scene` usable
                      from a sentence instead of from a copied identifier.

  Call safety         A service call is the one thing here that changes the
                      physical world. Which (domain, service) pairs are
                      dangerous is a judgement, so it is stated once, as data,
                      and enforced at the boundary rather than re-decided at
                      each call site.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  URLs
# ─────────────────────────────────────────────────────────────────────────────

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalise_base(url: str, *, default_scheme: str = "http") -> str:
    """A base URL an HTTP client will actually accept.

    `requests`/`httpx` do not guess a scheme: given `192.168.0.94:8096` they
    raise rather than assume, which is how an otherwise correct integration
    ends up loaded-but-dead. A bare host:port is therefore given a scheme
    instead of being passed through to fail later.
    """
    out = (url or "").strip()
    if not out:
        return ""
    if not _SCHEME_RE.match(out):
        out = f"{default_scheme}://{out}"
    return out.rstrip("/")


def api_url(base: str, path: str) -> str:
    """Join a base and an API path without the double slash HA 404s on."""
    return normalise_base(base) + "/" + (path or "").lstrip("/")


# ─────────────────────────────────────────────────────────────────────────────
#  Entities
# ─────────────────────────────────────────────────────────────────────────────

def split_entity(entity_id: str) -> Tuple[str, str]:
    """('light.kitchen') -> ('light', 'kitchen'). Domain is '' if malformed."""
    eid = (entity_id or "").strip()
    if "." not in eid:
        return "", eid
    domain, _, object_id = eid.partition(".")
    return domain, object_id


def is_entity_id(value: str) -> bool:
    domain, object_id = split_entity(value)
    return bool(domain and object_id)


def friendly_name(entity: Dict[str, Any]) -> str:
    """The name a person would use, falling back to a readable object_id."""
    attrs = entity.get("attributes") or {}
    name = str(attrs.get("friendly_name") or "").strip()
    if name:
        return name
    _, object_id = split_entity(str(entity.get("entity_id") or ""))
    return object_id.replace("_", " ").strip()


#: Domains that hold no state of their own, so `unknown` is their resting
#: value rather than a fault. A scene is not "unreachable" because nobody has
#: activated it yet, and counting 18 of them as broken hides the devices that
#: genuinely are.
STATELESS_DOMAINS = frozenset({
    "scene", "button", "input_button", "event", "notify", "tts", "stt",
    "conversation", "script",
})


def is_available(entity: Dict[str, Any]) -> bool:
    """Whether HA can currently reach the thing behind this entity.

    `unavailable` always means unreachable. `unknown` usually does too — but
    for a stateless domain it is the normal resting value, so treating it as a
    fault would report a working house as half-broken.
    """
    state = str(entity.get("state") or "").lower()
    if state == "unavailable":
        return False
    domain, _ = split_entity(str(entity.get("entity_id") or ""))
    if state in ("unknown", "none", ""):
        return domain in STATELESS_DOMAINS
    return True


_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(value: str) -> List[str]:
    return _WORD_RE.findall((value or "").lower())


def score_match(query: str, entity: Dict[str, Any]) -> int:
    """How well `query` names `entity`. 0 means no match at all.

    Deliberately word-based rather than a substring test: "bedside lamp"
    should find `switch.bedside_lamp_socket_1`, whose id contains neither the
    phrase nor a space. Exact identity still outranks everything so a caller
    that already knows the entity_id is never second-guessed.
    """
    q = (query or "").strip().lower()
    if not q:
        return 0
    eid = str(entity.get("entity_id") or "").lower()
    if q == eid:
        return 1000
    name = friendly_name(entity).lower()
    if q == name:
        return 900

    q_words = _words(q)
    if not q_words:
        return 0
    hay_words = set(_words(eid)) | set(_words(name))
    hit = sum(1 for w in q_words if w in hay_words)
    if not hit:
        return 0

    # Every word of the query accounted for is a far stronger signal than a
    # partial overlap, which is usually a coincidence on a shared word.
    score = int(300 * hit / len(q_words))
    if hit == len(q_words):
        score += 300
    if q in name or q in eid:
        score += 120
    # Prefer the shorter of two equally-matching names: "Gaming" over
    # "Interior CCTV Indicator light" for the query "gaming".
    score += max(0, 40 - len(hay_words))
    return score


def find_entities(states: Sequence[Dict[str, Any]], query: str = "",
                  domain: str = "", limit: int = 20,
                  available_only: bool = False) -> List[Dict[str, Any]]:
    """Rank entities against a human phrase. Empty query = plain filter."""
    domain = (domain or "").strip().lower()
    rows: List[Tuple[int, Dict[str, Any]]] = []
    for e in states or []:
        eid = str(e.get("entity_id") or "")
        if not eid:
            continue
        if domain and split_entity(eid)[0] != domain:
            continue
        if available_only and not is_available(e):
            continue
        if query:
            score = score_match(query, e)
            if score <= 0:
                continue
        else:
            score = 1
        rows.append((score, e))

    rows.sort(key=lambda r: (-r[0], str(r[1].get("entity_id"))))
    out = []
    for score, e in rows[:max(1, int(limit or 20))]:
        out.append({
            "entity_id": e.get("entity_id"),
            "name": friendly_name(e),
            "state": e.get("state"),
            "domain": split_entity(str(e.get("entity_id") or ""))[0],
            "available": is_available(e),
            "score": score,
        })
    return out


def resolve_entity(states: Sequence[Dict[str, Any]], target: str,
                   domain: str = "") -> Dict[str, Any]:
    """Turn a phrase or an entity_id into ONE entity, or explain why not.

    Returns {entity_id, name, ...} on success, or {error, candidates} when the
    phrase is ambiguous — an ambiguous match must never be silently resolved
    to the first row when the effect is switching something physical on.
    """
    target = (target or "").strip()
    if not target:
        return {"error": "no entity given"}

    if is_entity_id(target):
        for e in states or []:
            if str(e.get("entity_id") or "").lower() == target.lower():
                return {"entity_id": e.get("entity_id"),
                        "name": friendly_name(e), "state": e.get("state"),
                        "available": is_available(e), "exact": True}
        return {"error": f"no such entity: {target}"}

    matches = find_entities(states, target, domain=domain, limit=6)
    if not matches:
        return {"error": f"nothing matches {target!r}"}
    if len(matches) > 1 and matches[0]["score"] == matches[1]["score"]:
        return {"error": f"{target!r} is ambiguous",
                "candidates": [m["entity_id"] for m in matches[:6]]}
    top = dict(matches[0])
    top["exact"] = False
    return top


def summarise(states: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts per domain plus what is unreachable — the health view."""
    by_domain: Dict[str, int] = {}
    unavailable: List[str] = []
    for e in states or []:
        eid = str(e.get("entity_id") or "")
        if not eid:
            continue
        d = split_entity(eid)[0]
        by_domain[d] = by_domain.get(d, 0) + 1
        if not is_available(e):
            unavailable.append(eid)
    return {
        "total": sum(by_domain.values()),
        "domains": dict(sorted(by_domain.items(),
                               key=lambda kv: (-kv[1], kv[0]))),
        "unavailable_count": len(unavailable),
        "unavailable": sorted(unavailable)[:50],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Service calls
# ─────────────────────────────────────────────────────────────────────────────

#: Domains whose on/off is expressed as a domain-specific service rather than
#: the generic homeassistant.turn_on. Anything absent falls back to the
#: generic service, which HA routes correctly for every switchable domain.
_ACTION_SERVICES: Dict[str, Dict[str, str]] = {
    "scene": {"on": "turn_on", "off": "turn_on", "toggle": "turn_on"},
    "script": {"on": "turn_on", "off": "turn_off", "toggle": "turn_on"},
    "cover": {"on": "open_cover", "off": "close_cover",
              "toggle": "toggle"},
    "lock": {"on": "lock", "off": "unlock", "toggle": "open"},
    "media_player": {"on": "turn_on", "off": "turn_off",
                     "toggle": "media_play_pause"},
}

ACTIONS = ("on", "off", "toggle")

#: (domain, service) pairs that are refused without an explicit confirm.
#: Physical security and anything that opens the house: a misheard phrase from
#: a voice turn must not be able to unlock a door.
RISKY: Tuple[Tuple[str, str], ...] = (
    ("lock", "unlock"),
    ("lock", "open"),
    ("alarm_control_panel", "alarm_disarm"),
    ("cover", "open_cover"),
    ("homeassistant", "stop"),
    ("homeassistant", "restart"),
    ("hassio", "host_reboot"),
    ("hassio", "host_shutdown"),
    ("backup", "create"),
)


def is_risky(domain: str, service: str) -> bool:
    return ((domain or "").lower(), (service or "").lower()) in RISKY


def service_for_action(domain: str, action: str) -> str:
    """The HA service implementing on/off/toggle for a domain."""
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}, got {action!r}")
    table = _ACTION_SERVICES.get((domain or "").lower())
    if table:
        return table[action]
    return {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action]


def call_domain_for(entity_domain: str, service: str) -> str:
    """Which domain the service lives under.

    turn_on/turn_off/toggle exist both on the entity's own domain and on the
    `homeassistant` domain. Using the entity's domain keeps domain-specific
    options (brightness, colour) valid, and every switchable core domain
    implements them, so there is no need to special-case.
    """
    return (entity_domain or "homeassistant").lower()


def build_service_call(entity_id: str, action: str = "",
                       service: str = "",
                       data: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """Assemble {domain, service, payload} for one entity.

    Either `action` (on/off/toggle) or an explicit `service` must be given.
    """
    domain, _ = split_entity(entity_id)
    if not domain:
        raise ValueError(f"not an entity_id: {entity_id!r}")
    if service:
        svc = service.strip()
        # 'light.turn_on' and 'turn_on' are both natural to write.
        if "." in svc:
            call_dom, _, svc = svc.partition(".")
        else:
            call_dom = call_domain_for(domain, svc)
    elif action:
        svc = service_for_action(domain, action)
        call_dom = call_domain_for(domain, svc)
    else:
        raise ValueError("give either action or service")

    payload: Dict[str, Any] = {"entity_id": entity_id}
    for k, v in (data or {}).items():
        if k != "entity_id" and v is not None:
            payload[k] = v
    return {"domain": call_dom, "service": svc, "payload": payload,
            "risky": is_risky(call_dom, svc)}


def changed_entity_ids(result: Any) -> List[str]:
    """HA answers a service call with the states it changed.

    An empty list is the normal answer for a call that changed nothing (a
    light already on), so it is reported rather than treated as failure.
    """
    if not isinstance(result, list):
        return []
    out = []
    for row in result:
        if isinstance(row, dict) and row.get("entity_id"):
            out.append(str(row["entity_id"]))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Notifications
# ─────────────────────────────────────────────────────────────────────────────

def notify_service(target: str) -> Tuple[str, str]:
    """Split a notify target into (domain, service).

    Accepts 'notify.mobile_app_x', 'mobile_app_x' and the bare device slug,
    because HA exposes the same destination under all three spellings
    depending on the integration's age.
    """
    t = (target or "").strip()
    if not t:
        return "notify", "notify"
    if t.startswith("notify."):
        return "notify", t.split(".", 1)[1]
    if "." in t:
        domain, _, svc = t.partition(".")
        return domain, svc
    return "notify", t


def notify_payload(message: str, title: str = "",
                   data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"message": str(message or "")}
    if title:
        out["title"] = str(title)
    if data:
        out["data"] = data
    return out
