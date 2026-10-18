"""chat.corpus.run / chat.corpus.last: the weekly chat corpus run (four-minds §5.4; judged in chat_corpus_core).

The run is a browser driving the real chat page (tools/chat-corpus/run_chat_corpus.py, Playwright), so it runs
beside this process on the host, never inside it: chat.corpus.run starts it detached with this interpreter and
returns at once; chat.corpus.last reads the newest run's summary. A Loop Lab schedule (kind cap) calls
chat.corpus.run weekly, outside the census windows and the owner's evening hours. Prod only: a dev sandbox has no
chat traffic worth measuring and must not drive prod's page.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import capability
from Vera.vera.config import cfg

log = logging.getLogger("vera.minds.chat_corpus")
RUNNER = Path(__file__).resolve().parents[2] / "tools" / "chat-corpus" / "run_chat_corpus.py"


def _out_dir() -> Path:
    try:
        from Vera.vera.state_paths import state_dir
        return Path(state_dir("chat_corpus"))
    except Exception:                    # pragma: no cover - state_paths always present on prod
        return Path(os.path.expanduser("~/chat-corpus"))


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _target() -> str:
    return f"{'https' if cfg.TLS_ENABLED else 'http'}://127.0.0.1:{cfg.ORCHESTRATOR_PORT}"


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "chat.corpus.run", http_method="POST", http_path="/chat/corpus/run", http_tags=["minds", "chat"],
    memory="off",
    description="Start the chat corpus run (four-minds §5.4): the owner-approved chat cases typed into the real chat "
                "page by a headless browser on the host, each judged by deterministic checks (route, acquire, "
                "mentions, canvas, ttft). Effects are stubbed (a reminder is not booked, a loop is not started) and "
                "each case's chat-corpus- session is deleted after. Refuses while a census runs, while a run is "
                "going, and in a dev sandbox. Returns at once. Inputs: only (str - e.g. '1-6,27'; empty = all), "
                "gap_s (float=8), force (bool - run beside a census). Output: {ok, pid, out} | {ok:false, error}.",
)
async def cap_chat_corpus_run(only: str = "", gap_s: float = 8.0, force: bool = False,
                              trace_id=None) -> Dict[str, Any]:
    if getattr(_orch, "is_dev_sandbox", lambda: False)():
        return {"ok": False, "error": "prod only: a dev sandbox does not run the chat corpus"}
    if not RUNNER.is_file():
        return {"ok": False, "error": f"runner missing: {RUNNER}"}
    out = _out_dir()
    out.mkdir(parents=True, exist_ok=True)
    lock = out / "running.json"
    if lock.is_file():
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
        except Exception:
            held = {}
        if _alive(held.get("pid")):
            return {"ok": False, "error": f"a run is going (pid {held.get('pid')}, run {held.get('run_id')})"}
    cmd = [sys.executable, str(RUNNER), "--target", _target(), "--out", str(out), "--gap", str(float(gap_s or 0))]
    if only:
        cmd += ["--only", str(only)]
    if force:
        cmd.append("--force")
    try:
        with open(out / "runner.log", "ab") as logf:
            p = subprocess.Popen(cmd, cwd=str(RUNNER.parents[2]), stdin=subprocess.DEVNULL, stdout=logf,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        return {"ok": False, "error": f"could not start the runner: {e}"}
    log.info("chat corpus run started: pid %s only=%r", p.pid, only)
    return {"ok": True, "pid": p.pid, "out": str(out), "log": str(out / "runner.log")}


@capability(
    "chat.corpus.last", http_method="GET", http_path="/chat/corpus/last", http_tags=["minds", "chat"],
    contract=_MC.contract("chat.corpus.last", effects=["read", "filesystem"], idempotency="idempotent", filesystem="state_dir"),
    memory="off", silent=True,
    description="The newest chat corpus run: {run, summary, results?} (results with full=true), plus running "
                "{pid, run_id} while one is going. Input: full (bool=false).",
)
async def cap_chat_corpus_last(full: bool = False, trace_id=None) -> Dict[str, Any]:
    out = _out_dir()
    res: Dict[str, Any] = {}
    lock = out / "running.json"
    if lock.is_file():
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
            if _alive(held.get("pid")):
                res["running"] = held
        except Exception:
            pass
    try:
        run_id = (out / "latest.txt").read_text(encoding="utf-8").strip()
        doc = json.loads((out / run_id / "results.json").read_text(encoding="utf-8"))
    except Exception:
        return dict(res, ok=True, run=None, note="no finished run yet")
    res.update(ok=True, run=doc.get("run"), summary=doc.get("summary"))
    if full:
        res["results"] = doc.get("results")
    return res
