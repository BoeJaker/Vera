"""Which datasets are worth a vector, and which are just machine chatter.

An embedding is not free. On this estate they are pinned to the CPU nodes by the
`embedding` routing rule (deny_gpu — deliberately, see the memory/embed notes),
and those nodes are LXC containers sharing one host. So every row embedded is
CPU taken from every other row, and from anything else that needs those nodes.

Measured 2026-09-20 over a 3.3-hour window: of 11,755 seconds of embedding,
**6,946 (59%) were Home Assistant entity rows** — 80 embeds of text like

    binary_sensor.boejaker360_in_game binary_sensor off Boejaker360 2026-09-20T…

pushed in by an n8n `ha-sync` workflow into `vera.ha.entities`. A vector over
that is close to worthless: the rows are machine state, they are re-ingested
wholesale on a timer, and nobody searches them by meaning. Meanwhile the same
node was taking 113–236s to embed a single line of real research text.

`EMBED_EXCLUDED_DATASETS` already existed for exactly this, and `ingest_dataset`
never consulted it — it was read only by the backfill. So a producer could
declare itself excluded and still be embedded on every ingest. This module is
the decision, made pure so it is testable and so the ingest path and the
backfill cannot drift apart.

Excluded rows are still STORED and still searchable by text; they simply carry
no vector. Naming such a dataset explicitly in a backfill still embeds it — that
is how you catch up deliberately.
"""
from fnmatch import fnmatchcase
from typing import Iterable, List, Optional, Set

#: Patterns excluded unless the operator says otherwise. Home Assistant state
#: is the case this was built for; `*.states` catches the same shape from any
#: other producer that adopts the convention.
DEFAULT_EXCLUDED_PATTERNS: tuple = ("vera.ha.*", "*.ha.entities", "*.ha.states")


def parse_patterns(raw: Optional[str],
                   default: Iterable[str] = DEFAULT_EXCLUDED_PATTERNS) -> List[str]:
    """Patterns from a comma/whitespace separated env value.

    An unset value takes the defaults. An explicitly EMPTY value means "exclude
    nothing" — that is the operator switching the policy off, and it must not
    silently fall back to the defaults, or turning it off would be impossible.
    """
    if raw is None:
        return list(default)
    parts = [p.strip() for p in raw.replace("\n", ",").replace(" ", ",").split(",")]
    return [p for p in parts if p]


def embedding_excluded(dataset_id: str,
                       exact: Optional[Set[str]] = None,
                       patterns: Optional[Iterable[str]] = None) -> bool:
    """Should rows going into `dataset_id` be stored WITHOUT a vector?

    `exact` is the runtime set producers register themselves in
    (EMBED_EXCLUDED_DATASETS); `patterns` are operator-configured globs. Either
    one excluding is enough.

    Case-sensitive: dataset ids are identifiers here, not prose, and matching
    `Vera.HA.*` against `vera.ha.entities` would be a coincidence rather than an
    intent.
    """
    did = (dataset_id or "").strip()
    if not did:
        return False
    if exact and did in exact:
        return True
    for pat in (patterns or ()):
        if pat and fnmatchcase(did, pat):
            return True
    return False
