"""
platform_core.py — pure logic for the platform configuration controller
=======================================================================

One place to set the facts that several platforms each want their own copy of —
home and work coordinates, a timezone, an API key — and then push those facts
into n8n, Home Assistant and anything else, instead of retyping them per tool
and letting them drift.

Three record kinds, kept deliberately separate:

  values    Non-secret shared facts. `home_coords`, `work_coords`, `timezone`.
            Readable in the UI, safe in logs.
  secrets   Reusable credentials, sealed at rest and redacted on output. One
            record can be referenced by several platforms, or each platform can
            hold its own — the reference model makes that a choice, not a
            rewrite.
  platforms A target (homeassistant, n8n, …) whose fields are literals or
            references of the form `@value:<key>` / `@secret:<key>`.

The reference indirection is the whole point: change `home_coords` once and
every platform field bound to it changes with it.

Everything here is pure — no I/O, no app import — so it is unit-testable
without booting Vera (`tests/test_platform_core.py` imports it as
`vera.platforms.platform_core`).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

VALUE_REF = "@value:"
SECRET_REF = "@secret:"

# A coordinate pair as a human types it: "52.234091,0.123136" (space optional).
_COORD_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


# ═════════════════════════════════════════════════════════════════════════════
#  Keys and coordinates
# ═════════════════════════════════════════════════════════════════════════════

def normalise_key(raw: str) -> str:
    """Registry keys are lowercase snake so a reference is unambiguous."""
    k = re.sub(r"[^a-z0-9_]+", "_", (raw or "").strip().lower()).strip("_")
    return k[:64]


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def parse_coords(raw: str) -> Tuple[float, float]:
    """Parse "lat,lon" and range-check it.

    Range-checking matters more than it looks: a transposed pair still parses,
    but 0.12,52.23 puts "home" in the Gulf of Guinea and every downstream
    travel-time sensor silently computes nonsense.
    """
    m = _COORD_RE.match(raw or "")
    if not m:
        raise ValueError(
            "coordinates must look like '52.234091,0.123136' (lat,lon)")
    lat, lon = float(m.group(1)), float(m.group(2))
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude {lat} is out of range (-90..90)")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude {lon} is out of range (-180..180)")
    return lat, lon


def format_coords(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


# ═════════════════════════════════════════════════════════════════════════════
#  Reference resolution
# ═════════════════════════════════════════════════════════════════════════════

def is_ref(v: Any) -> bool:
    return isinstance(v, str) and (v.startswith(VALUE_REF)
                                   or v.startswith(SECRET_REF))


def ref_target(v: str) -> Tuple[str, str]:
    """('value'|'secret', key) for a reference string."""
    if v.startswith(VALUE_REF):
        return "value", v[len(VALUE_REF):].strip()
    if v.startswith(SECRET_REF):
        return "secret", v[len(SECRET_REF):].strip()
    raise ValueError(f"not a reference: {v!r}")


def resolve_fields(fields: Dict[str, Any], values: Dict[str, Any],
                   secrets: Dict[str, Any]) -> Dict[str, Any]:
    """Expand `@value:`/`@secret:` references into their current values.

    Returns {resolved, missing, secret_fields}. A missing reference is reported
    rather than silently becoming an empty string — a blank API key that looks
    configured is worse than one that is obviously absent.
    """
    resolved: Dict[str, Any] = {}
    missing: List[Dict[str, str]] = []
    secret_fields: List[str] = []

    for name, raw in (fields or {}).items():
        if not is_ref(raw):
            resolved[name] = raw
            continue
        kind, key = ref_target(raw)
        src = values if kind == "value" else secrets
        if kind == "secret":
            secret_fields.append(name)
        if key in src and src[key] not in (None, ""):
            resolved[name] = src[key]
        else:
            resolved[name] = ""
            missing.append({"field": name, "kind": kind, "key": key})

    return {"resolved": resolved, "missing": missing,
            "secret_fields": sorted(secret_fields)}


def redact_fields(fields: Dict[str, Any],
                  secret_fields: List[str]) -> Dict[str, Any]:
    out = dict(fields or {})
    for f in secret_fields or []:
        if out.get(f):
            out[f] = "••••••••"
    return out


def referencing_platforms(platforms: List[Dict[str, Any]], kind: str,
                          key: str) -> List[str]:
    """Which platforms use this value/secret — so a change shows its blast
    radius before it is made, and a delete can refuse to orphan a field."""
    want = (VALUE_REF if kind == "value" else SECRET_REF) + key
    out = []
    for p in platforms or []:
        for v in (p.get("fields") or {}).values():
            if v == want:
                out.append(p.get("id", ""))
                break
    return [p for p in out if p]


# ═════════════════════════════════════════════════════════════════════════════
#  Home Assistant payload builders
# ═════════════════════════════════════════════════════════════════════════════

def ha_core_config_payload(lat: float, lon: float, *, elevation: int = 0,
                           timezone: str = "", unit_system: str = "metric",
                           currency: str = "") -> Dict[str, Any]:
    """Body for HA's `POST /api/config/core/update`.

    This is what actually moves the 'home' the sun, presence and travel-time
    integrations all key off — editing the zone entity alone does not.
    """
    body: Dict[str, Any] = {"latitude": lat, "longitude": lon,
                            "elevation": int(elevation),
                            "unit_system": unit_system}
    if timezone:
        body["time_zone"] = timezone
    if currency:
        body["currency"] = currency
    return body


def waze_flow_data(origin: str, destination: str, *, name: str = "",
                   region: str = "gb") -> Dict[str, Any]:
    """Second-step payload for HA's Waze Travel Time config-entry flow.

    The integration is fiddly by hand because the origin/destination fields
    accept several shapes and silently fail on the wrong one. Raw
    "lat,lon" is the shape that always works, so coordinates are passed
    through verbatim after validation rather than being reformatted.
    """
    parse_coords(origin)
    parse_coords(destination)
    data: Dict[str, Any] = {"origin": origin.strip(),
                            "destination": destination.strip(),
                            "region": (region or "gb").lower()}
    if name:
        data["name"] = name
    return data


def waze_pair(home: str, work: str) -> List[Dict[str, Any]]:
    """The two commute sensors people actually want: out and back."""
    return [
        waze_flow_data(home, work, name="Home to work"),
        waze_flow_data(work, home, name="Work to home"),
    ]


# ═════════════════════════════════════════════════════════════════════════════
#  Platform definitions
# ═════════════════════════════════════════════════════════════════════════════

def field_spec(key: str, label: str, *, secret: bool = False,
               required: bool = False, hint: str = "",
               kind: str = "text") -> Dict[str, Any]:
    return {"key": key, "label": label, "secret": secret,
            "required": required, "hint": hint, "kind": kind}


# What each supported platform expects. `default` may itself be a reference,
# which is how a new platform picks up the shared facts automatically.
PLATFORM_SPECS: Dict[str, Dict[str, Any]] = {
    "homeassistant": {
        "label": "Home Assistant",
        "icon": "H",
        "docs": "https://www.home-assistant.io/docs/",
        "fields": [
            field_spec("base_url", "Base URL", required=True,
                       hint="e.g. http://192.168.0.96:8123"),
            field_spec("token", "Long-lived access token", secret=True,
                       required=True,
                       hint="Profile > Security > Long-lived access tokens"),
            field_spec("home_coords", "Home coordinates",
                       hint="lat,lon — usually @value:home_coords"),
            field_spec("work_coords", "Work coordinates",
                       hint="lat,lon — usually @value:work_coords"),
            field_spec("timezone", "Timezone", hint="e.g. Europe/London"),
            field_spec("waze_region", "Waze region",
                       hint="gb, us, na, eu, il — gb for the UK"),
        ],
        "defaults": {
            "home_coords": VALUE_REF + "home_coords",
            "work_coords": VALUE_REF + "work_coords",
            "timezone": VALUE_REF + "timezone",
            "waze_region": "gb",
        },
        "actions": ["ha.core_location", "ha.waze_travel_time"],
    },
    "n8n": {
        "label": "n8n",
        "icon": "n",
        "docs": "https://docs.n8n.io/api/",
        "fields": [
            field_spec("base_url", "Base URL", required=True,
                       hint="e.g. http://192.168.0.93"),
            field_spec("api_key", "Public API key", secret=True, required=True,
                       hint="Settings > n8n API"),
            field_spec("timezone", "Timezone", hint="e.g. Europe/London"),
        ],
        "defaults": {"timezone": VALUE_REF + "timezone"},
        "actions": ["n8n.ping"],
    },
}


def platform_spec(kind: str) -> Optional[Dict[str, Any]]:
    return PLATFORM_SPECS.get((kind or "").strip().lower())


def secret_field_keys(kind: str) -> List[str]:
    spec = platform_spec(kind) or {}
    return [f["key"] for f in spec.get("fields", []) if f.get("secret")]


def new_platform(kind: str, platform_id: str = "",
                 label: str = "") -> Dict[str, Any]:
    """A blank record pre-wired to the shared values, so a new platform starts
    already knowing where home is."""
    spec = platform_spec(kind)
    if not spec:
        raise ValueError(f"unknown platform kind: {kind!r} "
                         f"(known: {', '.join(sorted(PLATFORM_SPECS))})")
    pid = normalise_key(platform_id or kind)
    fields = {f["key"]: "" for f in spec["fields"]}
    fields.update(spec.get("defaults") or {})
    return {"id": pid, "kind": kind, "label": label or spec["label"],
            "enabled": True, "fields": fields, "status": "unconfigured"}


def config_completeness(kind: str, resolved: Dict[str, Any]) -> Dict[str, Any]:
    """Which required fields are still empty after resolution."""
    spec = platform_spec(kind) or {}
    missing = [f["label"] for f in spec.get("fields", [])
               if f.get("required") and not (resolved or {}).get(f["key"])]
    return {"configured": not missing, "missing_required": missing}
