"""Say what a chat turn is waiting for, without ever claiming it is waiting for nothing.

A reply that takes ninety seconds and a reply that has hung look identical from
the chat window. Usually the answer is mundane — a loop step is generating on
the same node, or the model has to be loaded — and the user could have known
that the whole time.

The hard constraint is honesty about what Vera can see. `in_use` and the ollama
request log only count requests **Vera itself** made. The CPU nodes are shared
with n8n, the sandboxes and each other, and on 2026-09-20 an entire outage went
eight hours unreported precisely because every Vera-side signal said a node was
free while nothing could generate on it. So:

  * "N jobs ahead" is only ever said about jobs Vera started and can name;
  * a runner that is busy while Vera has nothing in flight is reported as
    exactly that — work Vera did not start — not as an idle node;
  * when nothing is known, the result is `clear`, which renders NOTHING. It
    never says "no queue", because an empty `in_use` is not evidence of an
    empty node.

Pure: no I/O, no clock, no globals. The caller collects the facts and re-calls
this every few seconds while the turn waits, so ages advance and the line
changes as the queue drains.
"""
from typing import Any, Dict, List, Optional

#: Below this, a wait is not worth a line — the turn is about to start anyway.
MIN_AGE_S = 1.0


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _age(seconds: float) -> str:
    """'4m', '35s' — short enough to sit in a status line."""
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}"


def needs_model_load(resident_model: str, resident_ctx: int,
                     requested_model: str, requested_ctx: int) -> bool:
    """Will ollama have to (re)load a runner to serve this request?

    ollama keys a runner by (model, num_ctx), so either differing means a load.
    Nothing resident at all means a cold load. This is the same arithmetic that
    drove the reload thrash — here it is used to TELL the user, rather than to
    avoid it (chat_ctx_core does that).
    """
    if not requested_model:
        return False
    if not resident_model:
        return True                      # nothing loaded: cold start
    if resident_model != requested_model:
        # Tag-insensitive: `x:latest` and `x` are the same runner to ollama.
        if resident_model.split(":")[0] != requested_model.split(":")[0]:
            return True
    if resident_ctx and requested_ctx and resident_ctx != requested_ctx:
        return True
    return False


def describe_wait(node: str,
                  ahead: Optional[List[Dict[str, Any]]] = None,
                  *,
                  resident_model: str = "",
                  resident_ctx: int = 0,
                  requested_model: str = "",
                  requested_ctx: int = 0,
                  runner_busy: Optional[bool] = None) -> Dict[str, Any]:
    """What this turn is waiting for.

    `ahead` is Vera's own in-flight requests on `node`, this turn excluded, each
    {job_type, caller, model, age_s}. `runner_busy` is the node's own answer
    about whether its runner is computing — None when we could not ask, which
    must never be read as "not busy".

    Returns {state, node, ahead, will_load, text}. `state` is one of:
      queued          — named jobs Vera started are ahead of this one
      busy_elsewhere  — the runner is working and none of it is Vera's
      loading         — nothing in the way, but the model must be loaded
      clear           — nothing known. `text` is "" and the caller emits nothing.
    """
    rows = [r for r in (ahead or []) if isinstance(r, dict)]
    rows = [r for r in rows if float(r.get("age_s") or 0) >= MIN_AGE_S]
    rows.sort(key=lambda r: -float(r.get("age_s") or 0))

    will_load = needs_model_load(resident_model, resident_ctx,
                                 requested_model, requested_ctx)

    out: Dict[str, Any] = {"state": "clear", "node": node, "ahead": rows,
                           "will_load": will_load, "text": ""}

    if rows:
        out["state"] = "queued"
        first = rows[0]
        what = str(first.get("job_type") or first.get("caller") or "a job")
        detail = f"{what}, {_age(float(first.get('age_s') or 0))}"
        if len(rows) > 1:
            detail += f" +{len(rows) - 1} more"
        out["text"] = (f"Queued behind {len(rows)} "
                       f"{_plural(len(rows), 'job', 'jobs')} on {node} ({detail})")
        if will_load:
            out["text"] += " — then loading the model"
        return out

    if runner_busy is True:
        # Vera has nothing in flight here, yet the runner is computing. Name it
        # for what it is rather than pretending the node is free.
        out["state"] = "busy_elsewhere"
        out["text"] = (f"Waiting for {node} — it is busy with work Vera did "
                       f"not start")
        return out

    if will_load:
        out["state"] = "loading"
        ctx = f" ({requested_ctx} ctx)" if requested_ctx else ""
        out["text"] = f"Loading {requested_model or 'the model'} on {node}{ctx}…"
        return out

    # Nothing known to be in the way. Deliberately NOT "no queue": an empty
    # in_use is not evidence of an empty node.
    return out


def should_emit(status: Optional[Dict[str, Any]]) -> bool:
    """Is there something to say? `clear` says nothing at all."""
    return bool(status) and status.get("state") != "clear" and bool(status.get("text"))
