"""
ha_estate.py — projecting Vera's service registry into Home Assistant
=====================================================================

Vera already discovers the estate: `integration.discover` walks the Docker
hosts and records what is running, on which port, of which kind. Home
Assistant, meanwhile, knows about the house — lights, sockets, a phone — and
nothing at all about the forty-odd services the house is actually running.

This module plans the bridge: one Home Assistant entity per registered
service, so the estate appears next to the lights, can be put on a dashboard,
and can drive an automation ("tell me if Jellyfin goes down while I am out").

Planning is separated from pushing on purpose. Everything here is a pure
function over the registry and over what Home Assistant already holds, so what
*would* change can be shown before anything does.

Two properties the plan has to guarantee
────────────────────────────────────────

  Never touch a real device.  Entities this creates carry `source:
                              "vera-estate"`. Sync and clear only ever consider
                              entities carrying that tag, so a bug here cannot
                              delete a light. The tag is the safety boundary,
                              which is why it is checked rather than a name
                              prefix that a user could coincidentally pick.

  Do not mirror churn.        Most of what the registry holds at any moment is
                              ephemeral: a dozen `vera-dev-feat-*` sandboxes
                              that exist for an afternoon. Mirroring those
                              would fill Home Assistant with entities that are
                              permanently stale within a day, so they are
                              skipped unless explicitly asked for.

A caveat this module cannot design away: entities pushed through HA's
`/api/states` are not backed by a config entry, so Home Assistant forgets them
when it restarts. They are a live mirror, not a registration, and they need
re-syncing — which is what makes a scheduled sync part of the design rather
than a nicety.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Stamped on every entity this creates. The ownership boundary: nothing
#: without this tag is ever updated or removed by an estate sync.
SOURCE_TAG = "vera-estate"

ENTITY_DOMAIN = "binary_sensor"

#: Name fragments that mark a container as short-lived. Loop Lab spawns one
#: sandbox per branch and tears it down on merge, so these are the majority of
#: the registry at any moment and the least worth mirroring.
EPHEMERAL_MARKERS = ("vera-dev-", "sbxw-", "vera-sandbox-", "-worktree-")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, fallback: str = "service") -> str:
    out = _SLUG_RE.sub("_", (value or "").strip().lower()).strip("_")
    return out or fallback


def is_ephemeral(label: str) -> bool:
    """Whether this looks like a container that will not exist tomorrow."""
    low = (label or "").lower()
    return any(m in low for m in EPHEMERAL_MARKERS)


def entity_ids_for(integrations: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Map each integration id to a stable, unique HA entity_id.

    A label alone is not unique — the registry holds three `traefik` rows and
    two `neo4j`, one per published port — so a repeated label is disambiguated
    by port. A unique label keeps the short, readable form, because most of
    them are unique and `binary_sensor.estate_jellyfin_8096` reads worse than
    `binary_sensor.estate_jellyfin` for no gain.
    """
    counts: Dict[str, int] = {}
    for it in integrations:
        s = slugify(str(it.get("label") or it.get("id") or ""))
        counts[s] = counts.get(s, 0) + 1

    out: Dict[str, str] = {}
    for it in integrations:
        key = str(it.get("id") or "")
        if not key:
            continue
        s = slugify(str(it.get("label") or it.get("id") or ""))
        if counts.get(s, 0) > 1 and it.get("port"):
            s = f"{s}_{it['port']}"
        out[key] = f"{ENTITY_DOMAIN}.estate_{s}"
    return out


def build_entity(integration: Dict[str, Any], entity_id: str,
                 reachable: Optional[bool] = None,
                 checked_at: str = "") -> Dict[str, Any]:
    """The HA state payload for one service.

    `device_class: connectivity` is what makes Home Assistant render this as
    Connected/Disconnected rather than On/Off, which is what it means.
    """
    label = str(integration.get("label") or integration.get("id") or "service")
    kind = str(integration.get("kind") or "generic")
    attrs: Dict[str, Any] = {
        "friendly_name": label,
        "device_class": "connectivity",
        "source": SOURCE_TAG,
        "kind": kind,
        "integration_id": str(integration.get("id") or ""),
    }
    for field in ("base_url", "host", "port", "scheme"):
        val = integration.get(field)
        if val not in (None, ""):
            attrs[field] = val
    if checked_at:
        attrs["checked_at"] = checked_at

    if reachable is None:
        state = "unknown"
    else:
        state = "on" if reachable else "off"
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def owned_entity_ids(states: Sequence[Dict[str, Any]]) -> List[str]:
    """Entity ids in HA that a previous estate sync created.

    Identified by the source tag, never by name: an entity is ours because we
    stamped it, not because it happens to be called estate_something.
    """
    out = []
    for e in states or []:
        attrs = e.get("attributes") or {}
        if str(attrs.get("source") or "") == SOURCE_TAG:
            eid = str(e.get("entity_id") or "")
            if eid:
                out.append(eid)
    return sorted(out)


def plan_sync(integrations: Sequence[Dict[str, Any]],
              ha_states: Sequence[Dict[str, Any]],
              include_ephemeral: bool = False,
              prune: bool = True) -> Dict[str, Any]:
    """What an estate sync would do, without doing any of it.

    Returns create/update/remove/skipped, each a list of {entity_id, label}.
    `remove` is only ever populated from entities carrying the source tag, so
    a real device can never appear in it.
    """
    wanted = [it for it in integrations
              if include_ephemeral or not is_ephemeral(
                  str(it.get("label") or it.get("id") or ""))]
    skipped = [{"entity_id": "", "label": str(it.get("label") or it.get("id")),
                "reason": "ephemeral sandbox"}
               for it in integrations if it not in wanted]

    ids = entity_ids_for(wanted)
    existing = set(owned_entity_ids(ha_states))

    create, update = [], []
    for it in wanted:
        eid = ids.get(str(it.get("id") or ""))
        if not eid:
            continue
        row = {"entity_id": eid,
               "label": str(it.get("label") or it.get("id")),
               "kind": str(it.get("kind") or "generic")}
        (update if eid in existing else create).append(row)

    planned_ids = {r["entity_id"] for r in create} | {r["entity_id"] for r in update}
    remove = ([{"entity_id": e, "label": "", "reason": "no longer registered"}
               for e in sorted(existing - planned_ids)] if prune else [])

    return {
        "create": sorted(create, key=lambda r: r["entity_id"]),
        "update": sorted(update, key=lambda r: r["entity_id"]),
        "remove": remove,
        "skipped": sorted(skipped, key=lambda r: r["label"]),
        "counts": {"create": len(create), "update": len(update),
                   "remove": len(remove), "skipped": len(skipped)},
    }


def probe_targets(integrations: Sequence[Dict[str, Any]]
                  ) -> List[Tuple[str, str]]:
    """(integration_id, url) pairs worth an HTTP reachability check."""
    out = []
    for it in integrations:
        url = str(it.get("base_url") or "").strip()
        key = str(it.get("id") or "")
        if url and key:
            out.append((key, url))
    return out


def summarise_plan(plan: Dict[str, Any]) -> str:
    """One line a human can read in a toast or a log."""
    c = plan.get("counts") or {}
    return (f"{c.get('create', 0)} to add, {c.get('update', 0)} to refresh, "
            f"{c.get('remove', 0)} to remove, {c.get('skipped', 0)} skipped")
