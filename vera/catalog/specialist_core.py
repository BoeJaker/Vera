"""Specialist (non-LLM) models across the estate: NLP on the nodes, STT/TTS and
diffusion on the GPU media server, entity NER on the host - pure shaping for
`specialist.status`, testable without booting Vera.

Consumers import uppercase (Vera.vera.catalog.specialist_core); tests import
lowercase so pytest binds to the worktree copy. Nothing here imports Vera: the
version comparison is passed in (components_core.compare_versions).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List

#: The families the Specialist view shows, in display order.
FAMILIES = ("nlp", "media", "host_ner")

#: The media node detail the heartbeat keeps (capability_orchestration's
#: _ping_media_instance); `services` carries which of stt/tts/imagegen it serves.
MEDIA_DETAIL_KEYS = ("tts_engine", "gpu", "cuda", "device", "sample_rate", "sd_device",
                     "model_store")


def nlp_node_row(node: Dict[str, Any], host_version: Dict[str, Any],
                 compare: Callable[[Dict, Dict], Dict]) -> Dict[str, Any]:
    """One NLP node as the view shows it: its deployed nlp_server version against
    the host's, and each task's model with whether it is in the store and loaded.

    `modified` beats every other state: a node whose files no longer match its
    own version record is running code nobody deployed."""
    comp = node.get("component") or {}
    cmp = compare(host_version, comp)
    state = "modified" if comp.get("intact") is False else cmp["state"]
    tasks = {}
    for task, row in sorted((node.get("tasks") or {}).items()):
        tasks[task] = {"model": row.get("model", ""), "present": bool(row.get("present")),
                       "loaded": bool(row.get("loaded")),
                       "package": ((row.get("model_package") or {}).get("version") or "")}
    return {"node_id": str(node.get("node_id") or ""), "url": node.get("nlp_url", ""),
            "version": comp.get("version", ""), "state": state,
            "changed": list(comp.get("changed") or cmp.get("changed") or []),
            "threads": node.get("threads"), "tasks": tasks,
            "missing": [t for t, r in tasks.items() if not r["present"]]}


def media_node_row(iid: str, inst: Dict[str, Any]) -> Dict[str, Any]:
    detail = inst.get("detail") or {}
    return {"instance_id": iid, "label": inst.get("label", iid),
            "url": inst.get("url", ""), "status": inst.get("status", "unknown"),
            "has_gpu": bool(inst.get("has_gpu")), "enabled": inst.get("enabled", True),
            "services": list(inst.get("services") or []),
            "in_use": int(inst.get("in_use") or 0),
            "serves": {k: detail.get(k) for k in MEDIA_DETAIL_KEYS if k in detail}}


def registry_rows(default_models: Dict[str, str], task_kind: Dict[str, str]) -> List[Dict]:
    """The NLP model registry every node serves from (nlp_dispatch_core)."""
    return [{"task": t, "model": m, "kind": task_kind.get(t, "")}
            for t, m in default_models.items()]


# ── the store mount on each node ──────────────────────────────────────────────
#: The store on the Proxmox host, and where every node sees it (read-only).
STORE_HOST_PATH = "/tank_sdh/vera-store/models"
STORE_NODE_PATH = "/opt/vera-store/models"


def store_mount_plan(pct_config: str, source: str = STORE_HOST_PATH,
                     target: str = STORE_NODE_PATH) -> Dict[str, Any]:
    """From `pct config <vmid>` text: is the store already mounted at `target`,
    and if not, the pct set that adds it read-only on the first free mpN.

    A mount of the same source at the target that is WRITABLE is reported, not
    rewritten: only the builder may write the store, and a node found with a
    writable mount is a finding for the operator, not something to paper over."""
    used = set()
    for line in (pct_config or "").splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not (key.startswith("mp") and key[2:].isdigit()):
            continue
        used.add(int(key[2:]))
        opts = dict(p.split("=", 1) for p in val.split(",")[1:] if "=" in p)
        if opts.get("mp") == target:
            ro = opts.get("ro") in ("1", "true")
            return {"state": "mounted" if ro else "writable", "key": key,
                    "source": val.split(",", 1)[0], "ro": ro, "cmd": ""}
    idx = next(i for i in range(256) if i not in used)
    return {"state": "missing", "key": f"mp{idx}", "ro": True,
            "cmd": f"-mp{idx} {source},mp={target},ro=1"}


# ── models on a node's OWN disk (not in the shared store) ────────────────────
# Found on gpu-250 (2026-09-28): SD 1.5 (three copies across users' HF caches),
# IP-Adapter, ControlNet-openpose, Whisper base (four copies), Kokoro, Coqui
# tacotron2 and rembg's u2net/isnet - all in per-user caches, none in the store.
_HF_HUBS = ("/.cache/huggingface/hub", "/root/.cache/huggingface/hub",
            "/home/*/.cache/huggingface/hub")
_WHISPER_DIRS = ("/.cache/whisper", "/root/.cache/whisper", "/home/*/.cache/whisper",
                 "/home/*/*/cache/whisper")
_KOKORO_GLOBS = ("/home/*/*/kokoro-v1.0.onnx", "/opt/*/kokoro-v1.0.onnx",
                 "/opt/*/*/kokoro-v1.0.onnx")
_U2NET_DIRS = ("/.u2net", "/root/.u2net")
_COQUI_DIRS = ("/opt/model-cache/tts/tts", "/home/*/*/cache/tts")


def node_cache_probe_cmd() -> str:
    """Shell that prints `kind<TAB>MB<TAB>path` for every model in the known
    per-node caches. Read-only; globs that match nothing print nothing."""
    parts = []
    for hub in _HF_HUBS:
        parts.append(f'for d in {hub}/models--*; do [ -d "$d" ] && '
                     f'printf "hf\\t%s\\t%s\\n" "$(du -sm "$d" | cut -f1)" "$d"; done')
    for w in _WHISPER_DIRS:
        parts.append(f'for f in {w}/*.pt; do [ -f "$f" ] && '
                     f'printf "whisper\\t%s\\t%s\\n" "$(du -sm "$f" | cut -f1)" "$f"; done')
    for g in _KOKORO_GLOBS:
        parts.append(f'for f in {g}; do [ -f "$f" ] && '
                     f'printf "kokoro\\t%s\\t%s\\n" "$(du -sm "$f" | cut -f1)" "$f"; done')
    for u in _U2NET_DIRS:
        parts.append(f'for f in {u}/*.onnx; do [ -f "$f" ] && '
                     f'printf "rembg\\t%s\\t%s\\n" "$(du -sm "$f" | cut -f1)" "$f"; done')
    for c in _COQUI_DIRS:
        parts.append(f'for d in {c}/tts_models--*; do [ -d "$d" ] && '
                     f'printf "coqui\\t%s\\t%s\\n" "$(du -sm "$d" | cut -f1)" "$d"; done')
    return "; ".join(parts) + "; true"


def node_cache_prune_script(dry_run: bool = True, store: str = STORE_NODE_PATH,
                            root: str = "") -> str:
    """Shell that removes a node's OWN copy of a model only when the shared
    store (mounted read-only at `store`) holds the same thing:

      single files (whisper .pt, kokoro, rembg .onnx) - byte-identical (cmp)
      directories (HF snapshots, Coqui models)       - every file present in
                                                        the store copy, same size

    Prints `PRUNE|WOULD|KEEP <TAB> kind <TAB> MB|reason <TAB> path`. Refuses
    to do anything when the store is not mounted (then the node's copy is the
    only copy). Never writes under the store."""
    dry = "1" if dry_run else "0"
    r = root.rstrip("/")                 # a test runs it over a fake filesystem
    hubs = " ".join(r + h for h in _HF_HUBS)
    whisper = " ".join(f"{r}{d}/*.pt" for d in _WHISPER_DIRS)
    kokoro = " ".join(r + g for g in _KOKORO_GLOBS) + " " + " ".join(
        r + g.replace("kokoro-v1.0.onnx", "voices-v1.0.bin") for g in _KOKORO_GLOBS)
    u2net = " ".join(f"{r}{d}/*.onnx" for d in _U2NET_DIRS)
    coqui = " ".join(f"{r}{d}/tts_models--* {r}{d}/vocoder_models--*" for d in _COQUI_DIRS)
    return f"""S={store}; DRY={dry}
[ -d "$S/nlp" ] || {{ printf "ABORT\\tstore\\tnot mounted\\t%s\\n" "$S"; exit 0; }}
mb() {{ du -sm "$1" 2>/dev/null | cut -f1; }}
act() {{ k=$1; p=$2; m=$(mb "$p"); if [ "$DRY" = 1 ]; then printf "WOULD\\t%s\\t%s\\t%s\\n" "$k" "$m" "$p"; else rm -rf -- "$p" && printf "PRUNE\\t%s\\t%s\\t%s\\n" "$k" "$m" "$p"; fi; }}
keep() {{ printf "KEEP\\t%s\\t%s\\t%s\\n" "$1" "$2" "$3"; }}
same_tree() {{ src=$1; dst=$2; [ -d "$dst" ] || return 1
  n=0; for f in $(cd "$src" && find -L . -type f); do n=$((n+1))
    [ -f "$dst/$f" ] || return 1
    [ "$(stat -Lc %s "$src/$f")" = "$(stat -c %s "$dst/$f")" ] || return 1
  done; [ $n -gt 0 ]; }}
for m in $(for h in {hubs}; do ls -d $h/models--* 2>/dev/null; done); do
  slug=$(basename $m | sed 's/^models--//; s/--/__/g'); ok=1; snaps=0
  for s in $m/snapshots/*; do [ -d "$s" ] || continue; snaps=$((snaps+1)); same_tree "$s" "$S/sd/$slug" || same_tree "$s" "$S/hf/$slug" || ok=0; done
  if [ $snaps -gt 0 ] && [ $ok = 1 ]; then act hf "$m"; else keep hf "not in the store (or differs)" "$m"; fi
done
for f in {whisper} {kokoro} {u2net}; do [ -f "$f" ] || continue
  b=$(basename "$f"); case "$f" in *.pt) t="$S/whisper/$b"; k=whisper;; *kokoro*|*voices-v1.0.bin) t="$S/tts/kokoro-v1.0/$b"; k=kokoro;; *) t="$S/rembg/$b"; k=rembg;; esac
  if [ -f "$t" ] && cmp -s "$f" "$t"; then act $k "$f"; else keep $k "not in the store (or differs)" "$f"; fi
done
for d in {coqui}; do [ -d "$d" ] || continue
  if same_tree "$d" "$S/tts/coqui/tts/$(basename $d)"; then act coqui "$d"; else keep coqui "not in the store (or differs)" "$d"; fi
done
true"""


def parse_prune(stdout: str) -> Dict[str, Any]:
    rows = []
    for line in (stdout or "").splitlines():
        bits = line.split("\t")
        if len(bits) == 4 and bits[0] in ("PRUNE", "WOULD", "KEEP", "ABORT"):
            rows.append({"action": bits[0].lower(), "kind": bits[1], "detail": bits[2],
                         "path": bits[3]})
    freed = sum(int(r["detail"]) for r in rows
                if r["action"] in ("prune", "would") and r["detail"].isdigit())
    return {"rows": rows, "mb": freed,
            "aborted": any(r["action"] == "abort" for r in rows)}


def _model_name(kind: str, path: str) -> str:
    base = path.rstrip("/").rsplit("/", 1)[-1]
    if kind == "hf" and base.startswith("models--"):
        return base[len("models--"):].replace("--", "/")
    if kind == "whisper" and base.endswith(".pt"):
        return base[:-3]
    if kind == "coqui" and base.startswith("tts_models--"):
        return base[len("tts_models--"):].replace("--", "/")
    return base


def parse_node_cache(stdout: str) -> List[Dict[str, Any]]:
    """Rows grouped by model: the copies a node holds of each, and their size.
    Several copies of one model (one per user cache) are common and worth
    seeing - each is disk the shared store would make unnecessary."""
    by: Dict[tuple, Dict[str, Any]] = {}
    for line in (stdout or "").splitlines():
        bits = line.split("\t")
        if len(bits) != 3:
            continue
        kind, mb, path = bits
        try:
            size = int(mb)
        except ValueError:
            continue
        key = (kind, _model_name(kind, path))
        row = by.setdefault(key, {"kind": kind, "model": key[1], "size_mb": 0, "paths": []})
        row["paths"].append(path)
        row["size_mb"] += size
    return sorted(by.values(), key=lambda r: (r["kind"], r["model"]))


def summarize(nlp_rows: Iterable[Dict], media_rows: Iterable[Dict]) -> Dict[str, int]:
    nlp_rows, media_rows = list(nlp_rows), list(media_rows)
    states: Dict[str, int] = {}
    for r in nlp_rows:
        states[r["state"]] = states.get(r["state"], 0) + 1
    return {"nlp_nodes": len(nlp_rows),
            "nlp_current": states.get("current", 0),
            "nlp_not_current": len(nlp_rows) - states.get("current", 0),
            "nlp_missing_models": sum(1 for r in nlp_rows if r["missing"]),
            "media_nodes": len(media_rows),
            "media_online": sum(1 for r in media_rows if r["status"] == "online")}
