"""
worker_placement_core.py — where a task may run, and what a node worker is (no app imports)

A Vera worker on a compute node is a full Vera process joined to the shared
task stream. Two things made that unsafe before this module existed:

  * **Any worker took any task.** There is one consumer group on `vera:tasks`,
    and the only refusal was "I do not have that cap". A node has every cap
    loaded, so it would happily run `evolve.*` against an estate it cannot see,
    `docker.*`/`exec.*` with credentials stored only on the host, or a
    `*.config.set` that changes ITS in-memory copy of some setting while the
    host's stays as it was. That is the class of failure behind 2026-08-31,
    when a foundry worker on another machine reaped the sandbox pool for a day
    (see evolve/estate_role.py).

  * **A worker ran the whole ambient scheduler.** Every periodic job, plus
    one-time startup hooks that start pollers and loops: `telegram_startup` (a
    second getUpdates loop on the same bot token), `email_startup`,
    `dream_startup`, `fabric_startup`'s auto-pull of every fabric source,
    `cluster_startup`'s proxy/affinity loops, `job_persist_startup` reclaiming
    OTHER processes' pending stream entries.

Why an ALLOW-list and not a list of host-bound namespaces: a deny-list fails
open, and it leaks in two ways that were found while writing this. A
harmless-looking cap can keep local state the host never sees (`research.run`
returns a job id whose status lives in the process that ran it; `web.fetch`
ingests into the fabric's host-local SQLite). And a composing cap runs its
children IN-PROCESS (`dag.run` executing an `evolve.*` step), where no placement
check applies. So a namespace is node-safe only once it has been checked to use
shared backends or external services alone. Everything else goes to a stream
only the host reads. `VERA_WORKER_NODE_OK` widens the list without a release;
`VERA_WORKER_HOST_ONLY` narrows it.

Pure: names in, decisions out. Tested without a cluster.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, Optional, Tuple

#: The stream every worker reads (unchanged name - old processes keep working).
TASK_STREAM = "vera:tasks"
#: Tasks only the Vera host may run. Node workers never read it.
HOST_TASK_STREAM = "vera:tasks:host"

#: Namespaces checked to be safe on a node worker, with what makes them so.
NODE_SAFE: Dict[str, str] = {
    "llm":    "generation through the shared node registry and the cross-process GPU gate",
    "nlp":    "calls the node NLP servers over HTTP",
    "text":   "pure string transforms (text.generate goes through llm)",
    "math":   "pure computation",
    "http":   "stateless outbound requests",
    "memory": "Postgres / Neo4j / Chroma, shared by every process",
    "echo":   "diagnostic",
}

#: Final name segments that change process-local settings. Such a cap is
#: host-only even inside a node-safe namespace (`nlp.config.set` flips
#: `nlp.local` in the process that runs it).
MUTATOR_SEGMENTS = frozenset({
    "set", "config", "configure", "enable", "disable", "reset", "reload",
    "register", "unregister", "save", "delete",
})

#: One-time startup hooks a worker runs. Only hooks that load state a node-safe
#: cap needs; anything that starts a poller, a promoter, a proxy or a sweep, or
#: loads from a host-local store, is absent on purpose.
WORKER_STARTUP_HOOKS = frozenset({
    "worker_metrics",        # reports this worker's own CPU/RAM into its registration
    "memory_startup",        # memory backends (its promoter is worker-gated in memory.py)
    "memory_hooks_startup",  # in-process memory-mode wrappers, so a cap records
                             # memory on a worker the way it does on the host
})

#: Both routes a startup takes - schedule(..., interval=STARTUP_INTERVAL) and
#: capability_orchestration.start_at_import() - are judged by the same name.

#: Interval the scheduler uses for one-time startup hooks.
STARTUP_INTERVAL = 999999


def _names(v: Optional[Iterable[str]]) -> Tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = v.split(",")
    return tuple(s.strip().strip(".") for s in v if str(s).strip().strip("."))


def namespace(cap_name: str) -> str:
    return str(cap_name or "").split(".", 1)[0]


def placement(cap_name: str, *, node_ok: Iterable[str] = (),
              host_only: Iterable[str] = ()) -> Tuple[str, str]:
    """('any' | 'host', reason).

    `host_only` (namespaces or exact cap names) wins over everything - it is
    the operator's brake. `node_ok` then admits a namespace or one exact cap
    the vetted list does not (an exact name also overrides the mutator rule)."""
    name = str(cap_name or "")
    ns = namespace(name)
    brake = set(_names(host_only))
    if name in brake or ns in brake:
        return "host", "listed in VERA_WORKER_HOST_ONLY"
    ok = set(_names(node_ok))
    if name in ok:
        return "any", "admitted by VERA_WORKER_NODE_OK"
    last = name.rsplit(".", 1)[-1] if "." in name else ""
    if last in MUTATOR_SEGMENTS or ".config." in name:
        return "host", "changes process-local settings"
    if ns in ok:
        return "any", "admitted by VERA_WORKER_NODE_OK"
    if ns in NODE_SAFE:
        return "any", NODE_SAFE[ns]
    return "host", "not vetted as node-safe"


# ── task classes: what a node worker is FOR ───────────────────────────────────
#: The classes a node can be given (user, 2026-09-28: "task classes per node").
#: Each vetted namespace belongs to exactly one class. A class with no
#: namespaces is real but empty: nothing has been vetted into it yet, and the
#: UI says so rather than implying the node will get such work.
CLASSES: Dict[str, Dict[str, object]] = {
    "general": {"label": "General",
                "desc": "LLM calls, text, math, HTTP and memory - shared stores and the "
                        "cross-process GPU gate only",
                "namespaces": ("llm", "text", "math", "http", "memory", "echo")},
    "nlp":     {"label": "NLP",
                "desc": "NER, classification, zero-shot, QA, embeddings and rerank "
                        "through the node NLP servers",
                "namespaces": ("nlp",)},
    "cpu_compute": {"label": "CPU compute",
                    "desc": "parsing, transforms, test runs - nothing vetted into this "
                            "class yet",
                    "namespaces": ()},
    "media":   {"label": "Media (GPU)",
                "desc": "diffusion, STT, TTS - served by the node's gpu_inference "
                        "server directly; no worker tasks in this class yet",
                "namespaces": ()},
}

#: Defaults when a node has no roles set. A CPU node takes the vetted work; the
#: GPU node's worker takes none - its cores stay with the V100 runner and the
#: gpu_inference server, which do its media work without the task stream.
DEFAULT_CLASSES_CPU = ("general", "nlp")
DEFAULT_CLASSES_GPU = ()

#: One stream per class; a node worker reads only the classes it has.
CLASS_STREAM = "vera:tasks:cls:{cls}"

#: Redis hash host_id -> JSON list of classes, set from the Workers UI. A node
#: worker re-reads its entry every 30 s, so a change applies without a restart.
ROLES_KEY = "vera:node_workers:roles"


def class_stream(cls: str) -> str:
    return CLASS_STREAM.format(cls=cls)


def class_of(cap_name: str, env: Optional[Dict[str, str]] = None) -> str:
    """The class a node-safe cap belongs to; '' for a host-bound one. A cap an
    operator admitted with VERA_WORKER_NODE_OK that no class names is
    `general`."""
    where, _ = placement_from_env(cap_name, env)
    if where != "any":
        return ""
    ns = namespace(cap_name)
    for cls, spec in CLASSES.items():
        if ns in spec["namespaces"]:
            return cls
    return "general"


def default_classes(has_gpu: bool) -> Tuple[str, ...]:
    return DEFAULT_CLASSES_GPU if has_gpu else DEFAULT_CLASSES_CPU


def clean_classes(classes: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Known classes only, in catalogue order, no duplicates."""
    want = set(_names(classes)) if classes is not None else set()
    return tuple(c for c in CLASSES if c in want)


def placement_from_env(cap_name: str, env: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    e = os.environ if env is None else env
    return placement(cap_name, node_ok=_names(e.get("VERA_WORKER_NODE_OK", "")),
                     host_only=_names(e.get("VERA_WORKER_HOST_ONLY", "")))


def stream_for(cap_name: str, env: Optional[Dict[str, str]] = None) -> str:
    """The stream a task for this cap is queued on: the host stream for a
    host-bound cap, else its class's stream."""
    cls = class_of(cap_name, env)
    return class_stream(cls) if cls else HOST_TASK_STREAM


def streams_to_read(*, is_worker: bool, classes: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """The host reads everything. A node worker reads the shared legacy stream
    (tasks queued by a process running older code) and its own classes' streams
    - never the host stream, never a class it was not given."""
    if not is_worker:
        return (TASK_STREAM, HOST_TASK_STREAM) + tuple(class_stream(c) for c in CLASSES)
    return (TASK_STREAM,) + tuple(class_stream(c) for c in clean_classes(classes))


def may_run_here(cap_name: str, *, is_worker: bool,
                 env: Optional[Dict[str, str]] = None,
                 classes: Optional[Iterable[str]] = None) -> Tuple[bool, str]:
    """Whether THIS process may execute the cap. A worker runs only node-safe
    caps in the classes it was given (`classes` None = no class restriction);
    anything else it meets is handed to where it belongs (handoff_stream)."""
    if not is_worker:
        return True, ""
    where, why = placement_from_env(cap_name, env)
    if where == "host":
        return False, why
    if classes is not None:
        cls = class_of(cap_name, env)
        if cls not in clean_classes(classes):
            return False, "class %r is not enabled on this node" % cls
    return True, ""


def handoff_stream(cap_name: str, env: Optional[Dict[str, str]] = None) -> str:
    """Where a worker puts a task it may not run: the host stream for a
    host-bound cap, else the class stream (which the host always reads, so the
    task cannot bounce between nodes)."""
    return stream_for(cap_name, env)


def scheduler_may_run(job_name: str, interval: float, *, is_worker: bool) -> bool:
    """A worker runs no periodic job and only the allow-listed startup hooks."""
    if not is_worker:
        return True
    return float(interval or 0) >= STARTUP_INTERVAL and job_name in WORKER_STARTUP_HOOKS


# ── threads ───────────────────────────────────────────────────────────────────
#: The BLAS/OpenMP pools a Python worker can spin up. Capped so a numpy or
#: onnx call inside a cap cannot take the cores the node's ollama runner and
#: NLP server are sized for.
THREAD_ENV_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                   "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")

#: A worker's default pool size. The process itself is one event loop (GIL);
#: two lets a vectorised call overlap I/O without competing with the runner.
DEFAULT_WORKER_THREADS = 2
#: systemd CPUWeight for the worker (every unit defaults to 100): under
#: contention the ollama runner and the NLP server get twice its share.
WORKER_CPU_WEIGHT = 50
WORKER_NICE = 5


def worker_thread_env(threads: int = DEFAULT_WORKER_THREADS) -> Dict[str, str]:
    n = str(max(1, int(threads or DEFAULT_WORKER_THREADS)))
    return {k: n for k in THREAD_ENV_VARS}
