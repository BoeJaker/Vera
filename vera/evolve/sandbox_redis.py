"""A sandbox gets its own Redis, so it can have real state without prod's.

Dev sandboxes were configured with `REDIS_URL=redis://host.docker.internal:6379/<db>`
— prod's Redis SERVER, on an isolated DB number. That never worked: redis-server
binds 127.0.0.1 only, while a container resolves host.docker.internal to the
docker bridge (172.17.0.1), so every sandbox has run Redis-less since the
feature existed.

Running Redis-less is not neutral. `evolve.tasks` silently falls back to the 14
in-code defaults instead of the 33 real ones, suite scoreboards persist nowhere,
and `evolve.sandbox.snapshot` — which exists precisely to seed a sandbox from
prod one-way — has nothing to write into. A sandbox is not a faithful place to
test the thing it is meant to test.

The fix is NOT to expose prod's Redis to every container. Isolation here is
deliberate: a sandbox must not be able to pollute prod's state. So each sandbox
gets its OWN redis server as a sidecar, and isolation stops being a DB-number
convention and becomes a property of the topology — a sandbox cannot reach
prod's Redis because it is not addressable from the container at all.

    prod        native process   127.0.0.1:6379            (unchanged)
    sandbox     sidecar          <container>-redis:6379/0  (private, disposable)

Seeding stays ONE-WAY: `evolve.sandbox.snapshot` copies prod's Loop Lab config,
benchmark tasks, routing overrides and fabric DB INTO the sandbox. Nothing goes
back.

THE GPU GATE IS THE ONE THING THAT MUST NOT SILENTLY CHANGE
`_ensure_coord_redis` derives the coordination endpoint from REDIS_URL's own
authority. Point the data at a sidecar and the gate follows it there — so each
sandbox would get its own private capacity-1 gate that LOOKS like the shared
"one big queue" and is not one. Today the gate is already a no-op in a sandbox
(no Redis at all → COORD_REDIS stays None), so an explicit no-op preserves
today's behaviour; a per-sandbox gate would be a new and misleading one.
`coord_setting` returns that explicit disable, and the seam
(VERA_COORD_REDIS_URL) is what a genuinely shared coordination endpoint would
use later.

Pure: names and URLs in, names and URLs out.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

#: Small, fast, and thrown away with the sandbox. No persistence is configured
#: on purpose — a sandbox's Redis is scratch, and a stale dump surviving a
#: rebuild would reintroduce exactly the confusion this removes.
SIDECAR_IMAGE = "redis:7-alpine"

#: The value that turns coordination OFF explicitly. Chosen over an empty
#: string because compose cannot express "unset", and "" would be
#: indistinguishable from "not configured".
COORD_OFF = "off"

_AUTHORITY_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/?#]+)")


def sidecar_name(container_name: str) -> str:
    """The redis service/container paired with a sandbox container."""
    return "%s-redis" % str(container_name or "vera-dev").strip()


def data_url(container_name: str) -> str:
    """What the sandbox should use as REDIS_URL.

    DB 0 on a private server, not DB N on a shared one: the isolation is the
    server, so the DB number carries no safety meaning any more and pretending
    otherwise invites someone to "simplify" it back.
    """
    return "redis://%s:6379/0" % sidecar_name(container_name)


def authority_of(url: str) -> str:
    """host:port of a redis URL, or "" if it does not parse."""
    m = _AUTHORITY_RE.match(str(url or "").strip())
    return m.group(1) if m else ""


def reaches_same_server(a: str, b: str) -> bool:
    """True when two redis URLs address the same server, whatever DB each names.

    The safety question is 'can this sandbox touch prod's data', and a DB
    number is not an answer to it — a process that can reach the server can
    SELECT any DB on it.
    """
    aa, bb = authority_of(a), authority_of(b)
    return bool(aa) and aa == bb


def pollution_risk(sandbox_url: str, prod_url: str) -> str:
    """Why this sandbox could write prod's Redis, or "" when it cannot."""
    if not authority_of(sandbox_url):
        return "sandbox redis url is unreadable: %r" % (sandbox_url,)
    if reaches_same_server(sandbox_url, prod_url):
        return ("sandbox shares prod's redis server (%s) — a DB number does not "
                "contain it" % authority_of(sandbox_url))
    return ""


def coord_setting() -> str:
    """What a sandbox's VERA_COORD_REDIS_URL must be.

    Explicitly off. See the module docstring: with a private data Redis the
    derived coordination endpoint would become the sandbox's own server, giving
    it a private capacity-1 gate wearing the shared gate's name.
    """
    return COORD_OFF


def coord_url_from(env_value: Optional[str], derived: str) -> Optional[str]:
    """Resolve the coordination endpoint.

    An explicit env value wins; the sentinel disables coordination entirely
    (None); anything else falls back to whatever the caller derived, which is
    prod's existing behaviour and must stay untouched.
    """
    v = (env_value or "").strip()
    if not v:
        return derived
    if v.lower() == COORD_OFF:
        return None
    return v


def sidecar_service_yaml(container_name: str, network: str) -> str:
    """The compose fragment for a sandbox's private redis.

    `--save ""` and `--appendonly no` keep it in memory: this data is scratch,
    and a dump surviving a rebuild would resurrect a state nobody chose.
    Bound to the compose network only — no published port, so it is
    unreachable from the host and from anything not on that network.
    """
    return (
        "  %s:\n"
        "    image: %s\n"
        "    container_name: %s\n"
        "    command: [\"redis-server\", \"--save\", \"\", \"--appendonly\", \"no\"]\n"
        "    restart: \"no\"\n"
        "    networks:\n"
        "      - %s\n"
        % (sidecar_name(container_name), SIDECAR_IMAGE,
           sidecar_name(container_name), network)
    )
