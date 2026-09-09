"""What an agent brings with it, written down where Vera can see it.

Vera already knows about its OWN skills (`skills.py`), its own loop presets
(`loop_profiles.py`) and its own capabilities (`CAPABILITY_REGISTRY`). What it
has never known is the other half of the estate: the things an *external* agent
runs Vera WITH. A Claude Code session driving a census is using a `/loop` tool,
an `improve-vera-sandboxed` skill, a run_census.py harness and four throwaway
helper scripts, and none of that exists anywhere Vera can name - so a census row
records what was run and is silent on who ran it and with what.

This module is the record shape for that half, and nothing else. Pure: dicts in,
dicts out, no store, no clock, no network. The capability layer
(`registry_capabilities.py`) owns persistence; everything here is testable
without any of it.

FIVE KINDS, because they are genuinely different things and collapsing them
loses the distinction that makes the registry worth having:

  skill      a body of instructions an agent follows (a Claude Code SKILL.md,
             a Vera `skills.*` record)
  tool       something an agent invokes (a slash command, an MCP tool)
  loop       a repeating operating pattern (`/loop`, a census programme, a
             Vera loop profile)
  technique  a way of working that is not itself executable - "prove the code
             path ran before claiming a fix", "mutation-test the guard"
  os         an "LLM operating system": a whole harness that hosts agents
             (Claude Code, Codex, a Vera dream director)

CROSS-COMPATIBILITY IS A PROJECTION, NOT A COPY. `to_vera_skill` renders an
entry in the shape `skills.create` accepts, and `from_vera_skill` reads one
back. Neither store is authoritative over the other and neither is mutated
here - a registry entry that came from a Vera skill keeps `source.origin`
saying so, which is what stops a round-trip from laundering provenance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

#: The five kinds. Order is display order.
KINDS = ("skill", "tool", "loop", "technique", "os")

#: Where an entry came from. `vera` means it mirrors something Vera already
#: owns; the others are external harnesses that drive Vera from outside.
ORIGINS = ("claude-code", "codex", "vera", "user", "other")

#: Vera `skills.py` accepts only these `type` values. `to_vera_skill` has to
#: land on one of them or `skills.create` rejects the projection.
_VERA_SKILL_TYPES = ("system_prompt", "few_shot", "chain_of_thought", "persona",
                     "tool_hint", "output_format", "delivery_channel", "custom")

#: How each registry kind renders into a Vera skill `type`. A technique is
#: guidance an agent applies while working, which is what `tool_hint` means;
#: everything else that is not literally a prompt body is `custom` rather than
#: being forced into a shape that misdescribes it.
_KIND_TO_SKILL_TYPE = {
    "skill": "system_prompt",
    "technique": "tool_hint",
    "tool": "custom",
    "loop": "custom",
    "os": "custom",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"[a-z0-9]+")

#: A registry id is used as a Redis key segment and a Vera skill id, so it is
#: held to the same shape both accept.
MAX_ID = 96
MAX_NAME = 120
MAX_SUMMARY = 400


def slug(text: Any) -> str:
    return _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")[:MAX_ID]


def entry_id(kind: str, name: str) -> str:
    """Ids are namespaced by kind, so a `loop` and a `skill` may share a name.

    They routinely do - the `/loop` tool and the loop it runs are both called
    "loop" by the person using them.
    """
    return "%s:%s" % (slug(kind) or "other", slug(name) or "unnamed")


# â”€â”€ helper scripts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# A helper is a throwaway script an agent wrote mid-task. They are the least
# durable thing in a session and the most annoying to lose, because the next
# agent re-derives them from scratch. A helper with no purpose recorded is not
# worth keeping: the file alone does not say why it existed.

def helper_problems(helper: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(helper, dict):
        return ["helper is not an object"]
    if not str(helper.get("name") or "").strip():
        out.append("helper has no name")
    if not str(helper.get("purpose") or "").strip():
        out.append("helper %r has no purpose - the file alone does not say why "
                   "it existed" % (helper.get("name") or "?"))
    return out


def clean_helpers(helpers: Any) -> List[Dict[str, str]]:
    """Keep the helpers that say what they are for; drop the rest.

    Dropping is deliberate. A half-recorded helper reads as coverage and is
    worse than an absent one, exactly as a check that asserts nothing is worse
    than no check.
    """
    out: List[Dict[str, str]] = []
    for h in (helpers or []):
        if helper_problems(h):
            continue
        out.append({"name": str(h.get("name") or "").strip()[:MAX_NAME],
                    "purpose": str(h.get("purpose") or "").strip()[:MAX_SUMMARY],
                    "path": str(h.get("path") or "").strip()[:512]})
    return out


# â”€â”€ the record â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def normalise(entry: Any, *, now: str = "") -> Dict[str, Any]:
    """A registry entry with every field present and bounded.

    Never raises: a caller handing in nonsense gets a well-formed empty-ish
    record and `problems()` says what is wrong with it. Validation and coercion
    are kept apart on purpose so the capability layer can refuse a bad write
    while still having something printable to refuse it WITH.
    """
    e = entry if isinstance(entry, dict) else {}
    kind = str(e.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = "technique" if kind else ""
    name = str(e.get("name") or "").strip()[:MAX_NAME]
    owner = e.get("owner") if isinstance(e.get("owner"), dict) else {}
    source = e.get("source") if isinstance(e.get("source"), dict) else {}
    interop = e.get("interop") if isinstance(e.get("interop"), dict) else {}
    origin = str(source.get("origin") or "").strip().lower()
    if origin not in ORIGINS:
        origin = "other" if origin else ""

    out: Dict[str, Any] = {
        "id": str(e.get("id") or "").strip()[:MAX_ID] or entry_id(kind, name),
        "kind": kind,
        "name": name,
        "version": str(e.get("version") or "").strip()[:32],
        "summary": str(e.get("summary") or "").strip()[:MAX_SUMMARY],
        "body": str(e.get("body") or ""),
        "owner": {
            # WHO is operating, not which model answered. A census row wants
            # "claude" and the session that drove it.
            "agent": str(owner.get("agent") or "").strip()[:64],
            "session": str(owner.get("session") or "").strip()[:64],
        },
        "source": {
            "origin": origin,
            "path": str(source.get("path") or "").strip()[:512],
            "repo": str(source.get("repo") or "").strip()[:128],
            "commit": str(source.get("commit") or "").strip()[:64],
        },
        "interop": {
            # The three ways something in here can also be reached from inside
            # Vera. All optional; an entry may be reachable no other way than
            # by the agent that owns it, which is itself worth recording.
            "cap": str(interop.get("cap") or "").strip()[:128],
            "mcp_tool": str(interop.get("mcp_tool") or "").strip()[:128],
            "protocol": str(interop.get("protocol") or "").strip()[:64],
        },
        "tags": [str(t).strip()[:48] for t in (e.get("tags") or []) if str(t).strip()][:24],
        "applies_to": [str(t).strip()[:64] for t in (e.get("applies_to") or [])
                       if str(t).strip()][:24],
        "helpers": clean_helpers(e.get("helpers")),
        "created_at": str(e.get("created_at") or now or ""),
        "updated_at": str(now or e.get("updated_at") or ""),
    }
    return out


def problems(entry: Any) -> List[str]:
    """Everything wrong with this entry, in plain words.

    A registry whose entries do not say what they are is a list of names, and
    a list of names is what the estate already had.
    """
    out: List[str] = []
    if not isinstance(entry, dict):
        return ["not an entry object"]
    kind = str(entry.get("kind") or "").strip().lower()
    if not kind:
        out.append("entry has no kind (one of: %s)" % ", ".join(KINDS))
    elif kind not in KINDS:
        out.append("unknown kind %r (one of: %s)" % (kind, ", ".join(KINDS)))
    if not str(entry.get("name") or "").strip():
        out.append("entry has no name")
    if not str(entry.get("summary") or "").strip():
        out.append("entry has no summary - a registry of names is what the "
                   "estate already had")
    src = entry.get("source")
    origin = str((src or {}).get("origin") or "").strip().lower() if isinstance(src, dict) else ""
    if not origin:
        out.append("entry has no source.origin (one of: %s)" % ", ".join(ORIGINS))
    elif origin not in ORIGINS:
        out.append("unknown source.origin %r" % origin)
    for h in (entry.get("helpers") or []):
        out.extend(helper_problems(h))
    return out


def merge(existing: Any, incoming: Any, *, now: str = "") -> Dict[str, Any]:
    """Update an entry without losing what the update did not mention.

    `created_at` is the entry's, never the update's - an edit is not a
    re-registration, and a registry that forgets when something first appeared
    cannot answer the only question a timeline asks.
    """
    base = normalise(existing)
    incoming = incoming if isinstance(incoming, dict) else {}
    merged = dict(base)
    for k, v in incoming.items():
        if v in (None, "", [], {}):
            continue
        if k in ("owner", "source", "interop") and isinstance(v, dict):
            sub = dict(base.get(k) or {})
            sub.update({kk: vv for kk, vv in v.items() if vv not in (None, "")})
            merged[k] = sub
        else:
            merged[k] = v
    merged["id"] = base["id"] or merged.get("id")
    out = normalise(merged, now=now)
    out["created_at"] = base.get("created_at") or out["created_at"]
    return out


# â”€â”€ search â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _terms(query: Any) -> List[str]:
    return _WORD_RE.findall(str(query or "").lower())


def score(entry: Any, query: Any) -> int:
    """How well one entry answers one query. 0 means it does not.

    Weighted so a name match beats a body match: searching "loop" should
    surface the `/loop` tool before every entry whose prose mentions a loop.
    """
    e = normalise(entry)
    terms = _terms(query)
    if not terms:
        return 1
    name = e["name"].lower()
    idl = e["id"].lower()
    tags = " ".join(e["tags"]).lower()
    applies = " ".join(e["applies_to"]).lower()
    summary = e["summary"].lower()
    body = e["body"].lower()
    total = 0
    for t in terms:
        hit = 0
        if t in name or t in idl:
            hit = 8
        elif t in tags or t in applies:
            hit = 5
        elif t in summary:
            hit = 3
        elif t in body:
            hit = 1
        if not hit:
            # EVERY term must land somewhere. Without this an unrelated entry
            # scores on one common word ("the census") and the result set stops
            # meaning anything.
            return 0
        total += hit
    return total


def search(entries: Iterable[Any], query: Any = "", *, kind: str = "",
           owner: str = "", tag: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Ranked matches, best first. Filters are ANDed with the query."""
    kind = str(kind or "").strip().lower()
    owner = str(owner or "").strip().lower()
    tag = str(tag or "").strip().lower()
    scored: List[tuple] = []
    for raw in (entries or []):
        e = normalise(raw)
        if kind and e["kind"] != kind:
            continue
        if owner and e["owner"]["agent"].lower() != owner:
            continue
        if tag and tag not in [t.lower() for t in e["tags"]]:
            continue
        s = score(e, query)
        if s <= 0:
            continue
        # Ties break on name so the order is stable across calls - an unstable
        # listing looks like the registry changed when it did not.
        scored.append((-s, e["name"].lower(), e))
    scored.sort(key=lambda r: (r[0], r[1]))
    return [e for _, _, e in scored[:max(1, int(limit or 50))]]


# â”€â”€ projections â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def to_vera_skill(entry: Any) -> Dict[str, Any]:
    """Render an entry in the shape `skills.create` accepts.

    The projection is lossy by design - a Vera skill has no place to put
    `source` or `helpers` - so the tags carry the provenance that would
    otherwise vanish, and `registry:<id>` makes the round trip findable.
    """
    e = normalise(entry)
    tags = list(e["tags"])
    for extra in ("registry:%s" % e["id"], "kind:%s" % (e["kind"] or "unknown")):
        if extra not in tags:
            tags.append(extra)
    if e["source"]["origin"]:
        t = "origin:%s" % e["source"]["origin"]
        if t not in tags:
            tags.append(t)
    body = e["body"] or e["summary"]
    if e["helpers"]:
        body = body.rstrip() + "\n\nHelper scripts:\n" + "\n".join(
            "- %s - %s" % (h["name"], h["purpose"]) for h in e["helpers"])
    return {
        "id": "registry-%s" % slug(e["id"]),
        "name": e["name"],
        "description": e["summary"],
        "type": _KIND_TO_SKILL_TYPE.get(e["kind"], "custom"),
        "content": body,
        "tags": tags,
        "enabled": True,
    }


def from_vera_skill(skill: Any) -> Dict[str, Any]:
    """Read a Vera skill back as a registry entry.

    `source.origin` is `vera` unless the skill's own tags say where it really
    came from. That is the whole point: a round trip must not launder a Claude
    Code skill into a Vera-native one.
    """
    s = skill if isinstance(skill, dict) else {}
    tags = [str(t) for t in (s.get("tags") or [])]
    kind, origin = "", ""
    for t in tags:
        if t.startswith("kind:") and t[5:] in KINDS:
            kind = t[5:]
        elif t.startswith("origin:") and t[7:] in ORIGINS:
            origin = t[7:]
    return normalise({
        "id": next((t[9:] for t in tags if t.startswith("registry:")), "") or "",
        "kind": kind or "skill",
        "name": s.get("name") or "",
        "summary": s.get("description") or "",
        "body": s.get("content") or "",
        "tags": [t for t in tags
                 if not t.startswith(("registry:", "kind:", "origin:"))],
        "source": {"origin": origin or "vera"},
    })


def to_interop(entry: Any) -> Dict[str, Any]:
    """The entry as the interoperability layer describes things.

    Shaped to `documentation/46-interoperability-foundations.md`'s five
    boundaries so a registry entry can be answered for at each one rather than
    being a private note this subsystem alone understands.
    """
    e = normalise(entry)
    reachable = [v for v in (e["interop"]["cap"], e["interop"]["mcp_tool"]) if v]
    return {
        "schema": "vera.agent-registry-entry/v1",
        "id": e["id"],
        "kind": e["kind"],
        "name": e["name"],
        "contract": {"summary": e["summary"], "version": e["version"] or "unversioned"},
        "resolution": {"reachable_as": reachable,
                       "protocol": e["interop"]["protocol"] or "none",
                       # An entry nothing inside Vera can invoke is not broken;
                       # it is an external technique. Say which, do not imply.
                       "external_only": not reachable},
        "policy": {"owner": e["owner"]["agent"] or "unattributed",
                   "origin": e["source"]["origin"] or "unknown"},
        "evidence": {"source_path": e["source"]["path"],
                     "repo": e["source"]["repo"], "commit": e["source"]["commit"],
                     "helpers": e["helpers"]},
    }
