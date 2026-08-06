"""Model-download bookkeeping across a Vera restart.

A pull is a streaming ``/api/pull`` request owned by *this* Vera process, so a
restart kills the transfer. What used to survive was the Redis mirror, still
claiming ``downloading`` with a frozen speed and ETA — a phantom row the UI
counted as active and offered no way to cancel or dismiss. These tests pin the
staleness rules that retire such records.

Pure-unit: the helpers are loaded straight out of the module source so the suite
does not need fastapi/redis installed.
"""

import asyncio
import os
import re
import types
from datetime import datetime, timedelta, timezone

import pytest

_MOD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "vera", "catalog", "catalog_capabilities.py")


def _load(first: str, last: str, **extra) -> dict:
    """Exec one slice of the module's source without importing the whole stack."""
    src = open(_MOD, encoding="utf-8").read()
    preamble = ("from __future__ import annotations\n"
                "from datetime import datetime, timezone\n"
                "from typing import Optional, Tuple, List, Dict\n"
                "def now_iso():\n"
                "    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')\n")
    ns: dict = dict(extra)
    exec(compile(preamble + src[src.index(first):src.index(last)], _MOD, "exec"), ns)
    return ns


def _load_stale(**extra):
    """The staleness slice. It reads env/hostname for the owner tag and the wall
    clock for the process-start comparison, so those have to come along."""
    import socket as _socket
    import time as _time
    kw = dict(os=os, socket=_socket, time=_time)
    kw.update(extra)
    return _load("_ACTIVE_STATES = (", "async def _pull_all_redis", **kw)


@pytest.fixture(scope="module")
def helpers():
    """The pull staleness helpers."""
    return _load_stale()


@pytest.fixture(scope="module")
def resume():
    """The auto-resume policy helpers."""
    import os as _os
    return _load("PULL_AUTORESUME = (", "async def _resume_pull(",
                 os=_os, re=re, log=None)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def _rec(state="downloading", age_s=1, **kw) -> dict:
    r = {"id": "cpu-246-1785683435-1", "model": "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:Q8_K_XL",
         "instance_id": "cpu-246", "state": state, "error": "",
         "started_at": _ago(hours=2), "updated_at": _ago(seconds=age_s),
         "speed_bps": 10515344, "eta_s": 2052}
    r.update(kw)
    return r


# ── staleness ───────────────────────────────────────────────────────────────
def test_live_pull_is_not_stale(helpers):
    assert not helpers["_pull_is_stale"](_rec(age_s=1))


def test_slow_manifest_fetch_is_not_stale(helpers):
    """No bytes yet is normal for a big repo — don't reap a pull that's starting."""
    assert not helpers["_pull_is_stale"](_rec(state="starting", age_s=90))


def test_orphan_from_a_restart_is_stale(helpers):
    assert helpers["_pull_is_stale"](_rec(age_s=3600))


def test_verifying_orphan_is_stale(helpers):
    assert helpers["_pull_is_stale"](_rec(state="verifying", age_s=3600))


@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "interrupted"])
def test_terminal_records_are_never_stale(helpers, state):
    """Staleness is about a missing owner, not about age — finished is finished."""
    assert not helpers["_pull_is_stale"](_rec(state=state, updated_at="2020-01-01T00:00:00Z"))


def test_unreadable_timestamp_counts_as_dead(helpers):
    assert helpers["_pull_is_stale"](_rec(updated_at="not-a-date", started_at=""))


# ── retirement ──────────────────────────────────────────────────────────────
def test_interrupting_zeroes_the_fake_live_rate(helpers):
    """The frozen speed/ETA were the misleading part: they read as current."""
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600))
    assert rec["state"] == "interrupted"
    assert rec["ok"] is False
    assert rec["speed_bps"] == 0 and rec["eta_s"] is None
    assert rec["finished_at"] and rec["error"]


def test_interrupted_record_is_not_reaped_again(helpers):
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600))
    assert not helpers["_pull_is_stale"](rec)


def test_interrupting_keeps_a_real_error(helpers):
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600, error="no space left on device"))
    assert rec["error"] == "no space left on device"


def test_interrupted_is_not_an_active_state(helpers):
    """Drives the UI: active rows get Cancel, the rest get Resume + Dismiss."""
    assert "interrupted" not in helpers["_ACTIVE_STATES"]


def test_progress_so_far_is_preserved_for_resume(helpers):
    """Ollama keeps the partial blob; the record must keep the byte counts that
    tell the operator resuming is worth it."""
    rec = helpers["_pull_mark_interrupted"](
        _rec(age_s=3600, completed_bytes=16880784751, total_bytes=38451182560, pct=43.9))
    assert rec["completed_bytes"] == 16880784751 and rec["pct"] == 43.9


# ── registry preflight ──────────────────────────────────────────────────────
# Real case this guards: unsloth/Qwen3.6-35B-A3B-GGUF:Q8_K_XL serves a valid
# manifest whose 638-byte config blob 404s. Ollama fetches the config last, so
# 39 GB downloaded, hit 100%, then died with Go's bare os.ErrNotExist ("file
# does not exist"), leaving no manifest and an invisible, unresumable model.
_QWEN = "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:Q8_K_XL"
_MANIFEST = {
    "layers": [
        {"digest": "sha256:" + "b7" * 32, "mediaType": "application/vnd.ollama.image.model",
         "size": 38451182560},
        {"digest": "sha256:" + "f4" * 32, "mediaType": "application/vnd.ollama.image.template",
         "size": 182},
        {"digest": "sha256:" + "89" * 32, "mediaType": "application/vnd.ollama.image.projector",
         "size": 899283680},
        {"digest": "sha256:" + "66" * 32, "mediaType": "application/vnd.ollama.image.params",
         "size": 71},
    ],
    "config": {"digest": "sha256:" + "96" * 32,
               "mediaType": "application/vnd.docker.container.image.v1+json", "size": 638},
}
_CONFIG_DIGEST = _MANIFEST["config"]["digest"]


def _fake_httpx(manifest_status=200, manifest=None, blob_codes=None, blob_raises=()):
    """An httpx stand-in so the preflight is testable without the network."""
    blob_codes = blob_codes or {}

    class Resp:
        def __init__(self, status_code, payload=None):
            self.status_code, self._p = status_code, payload

        def json(self):
            return self._p

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return Resp(manifest_status, manifest if manifest is not None else _MANIFEST)

        async def head(self, url):
            digest = url.rsplit("/", 1)[-1]
            if digest in blob_raises:
                raise OSError("connection reset")
            return Resp(blob_codes.get(digest, 200))

    return types.SimpleNamespace(Timeout=lambda **kw: None, AsyncClient=Client)


def _preflight(**kw) -> dict:
    return _load("_HF_REF_RE = re.compile", "async def _pull_persist",
                 re=re, asyncio=asyncio, httpx=_fake_httpx(**kw),
                 HF_HOST="https://huggingface.co", _ssl=lambda: True)


def _run(coro):
    """Drive a coroutine on a private loop.

    Not asyncio.run(): on 3.8 it leaves the thread with no current event loop,
    which breaks any later test whose imports call get_event_loop()."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _check(model, **kw):
    ns = _preflight(**kw)
    return _run(ns["_hf_check_objects"](model)), ns


@pytest.mark.parametrize("ref,want", [
    ("hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:Q8_K_XL", ("unsloth/Qwen3.6-35B-A3B-GGUF", "Q8_K_XL")),
    ("huggingface.co/a/b:Q4_K_M", ("a/b", "Q4_K_M")),
    ("hf.co/a/b", ("a/b", "latest")),
    ("qwen2.5:7b", None),
    ("library/mistral:7b", None),
])
def test_only_hugging_face_refs_are_preflightable(ref, want):
    assert _preflight()["_hf_ref"](ref) == want


def test_complete_model_is_allowed():
    chk, _ = _check(_QWEN)
    assert chk["missing"] == [] and chk["checked"]


def test_missing_config_blob_is_caught():
    chk, ns = _check(_QWEN, blob_codes={_CONFIG_DIGEST: 404})
    assert [o["kind"] for o in chk["missing"]] == ["config"]
    msg = ns["_hf_broken_msg"](_QWEN, chk)
    assert "config" in msg and _CONFIG_DIGEST[7:19] in msg
    assert "file does not exist" in msg          # ties it to what the user saw


def test_unreadable_manifest_does_not_block_the_pull():
    """400/401 (bad quant, unknown repo) — Ollama fails on those in a second
    without moving data, so the guard must stay out of the way."""
    for status in (400, 401, 500):
        chk, _ = _check(_QWEN, manifest_status=status)
        assert chk["missing"] == [] and not chk["checked"]


def test_unreachable_blob_does_not_count_as_missing():
    """A network blip must never masquerade as a broken upstream model."""
    chk, _ = _check(_QWEN, blob_raises={_MANIFEST["layers"][0]["digest"]})
    assert chk["missing"] == [] and not chk["checked"]


def test_a_real_404_still_reports_alongside_an_unreachable_object():
    chk, _ = _check(_QWEN, blob_codes={_CONFIG_DIGEST: 404},
                    blob_raises={_MANIFEST["layers"][0]["digest"]})
    assert [o["kind"] for o in chk["missing"]] == ["config"]


def test_non_hugging_face_ref_is_skipped():
    chk, _ = _check("qwen2.5:7b")
    assert chk["missing"] == [] and not chk["checked"]


def test_manifest_without_a_config_is_handled():
    man = {"layers": _MANIFEST["layers"]}
    chk, _ = _check(_QWEN, manifest=man)
    assert chk["missing"] == [] and chk["checked"]


# ── auto-resume policy ──────────────────────────────────────────────────────
# Every Vera restart kills every in-flight pull, because the /api/pull stream
# belongs to the Vera process. Twelve multi-GB downloads died that way in one
# afternoon. They come back on their own now — but only the ones that should.
def test_interrupted_downloads_resume(resume):
    assert resume["_pull_is_resumable"]({"state": "interrupted"})


def test_operator_cancelled_downloads_do_not_come_back(resume):
    """Pressing Cancel means stop wanting this model. A restart must not
    override that decision."""
    assert not resume["_pull_is_resumable"]({"state": "cancelled"})


def test_failed_downloads_do_not_retry_themselves(resume):
    """'failed' is a rejected pull — a broken upstream quant, a bad tag.
    Retrying reproduces the failure; it needs a human, not a loop."""
    assert not resume["_pull_is_resumable"]({"state": "failed"})


@pytest.mark.parametrize("state", ["downloading", "starting", "verifying", "done"])
def test_live_and_finished_downloads_are_not_resumed(resume, state):
    assert not resume["_pull_is_resumable"]({"state": state})


def test_resume_is_bounded(resume):
    """A restart with a large backlog must not stampede the uplink, and stale
    abandoned transfers should not silently restart days later."""
    assert 0 < resume["PULL_RESUME_MAX"] <= 32
    assert resume["PULL_RESUME_MAX_AGE_S"] <= 7 * 86400


def test_hydrate_does_not_latch_before_redis_exists():
    """The regression that made auto-resume a no-op.

    Redis connects about a second after the HTTP server, and the Downloads tab
    polls immediately. _hydrate() used to set its "done" flag *before* checking
    for Redis, so a single poll landing in that window permanently skipped the
    node restore, the orphan reap and the download auto-resume — for the entire
    life of the process. Twelve interrupted downloads sat untouched overnight."""
    ns = _load("async def _hydrate() -> None:", "async def _hydrate_from(r)")
    ns["_HYDRATED"] = {"v": False, "busy": False}
    calls = []
    redis_up = {"v": False}
    ns["_redis"] = lambda: ("redis" if redis_up["v"] else None)

    async def _fake_hydrate_from(r):
        calls.append(r)
    ns["_hydrate_from"] = _fake_hydrate_from

    # Poll arrives while Redis is still coming up.
    _run(ns["_hydrate"]())
    assert calls == [], "hydrated with no Redis"
    assert ns["_HYDRATED"]["v"] is False, "latched before Redis was available"

    # Redis is up; the next poll must actually hydrate.
    redis_up["v"] = True
    _run(ns["_hydrate"]())
    assert calls == ["redis"], "never retried after Redis came up"
    assert ns["_HYDRATED"]["v"] is True

    # And it stays a one-shot thereafter.
    _run(ns["_hydrate"]())
    assert calls == ["redis"], "hydrated more than once"


def test_hydrate_retries_after_a_failure():
    """A throwing hydrate must not latch either, or one bad read disables the
    reap and auto-resume until the next restart."""
    ns = _load("async def _hydrate() -> None:", "async def _hydrate_from(r)")
    ns["_HYDRATED"] = {"v": False, "busy": False}
    ns["_redis"] = lambda: "redis"
    boom = {"v": True}

    async def _fake_hydrate_from(r):
        if boom["v"]:
            raise RuntimeError("redis read failed")
    ns["_hydrate_from"] = _fake_hydrate_from

    with pytest.raises(RuntimeError):
        _run(ns["_hydrate"]())
    assert ns["_HYDRATED"]["v"] is False
    assert ns["_HYDRATED"]["busy"] is False, "busy flag leaked; hydrate wedged forever"

    boom["v"] = False
    _run(ns["_hydrate"]())
    assert ns["_HYDRATED"]["v"] is True


def test_autoresume_is_on_by_default_and_can_be_switched_off(resume, monkeypatch):
    assert resume["PULL_AUTORESUME"] is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("VERA_PULL_AUTORESUME", off)
        ns = _load("PULL_AUTORESUME = (", "async def _resume_pull(", os=os, re=re)
        assert ns["PULL_AUTORESUME"] is False, off
    monkeypatch.setenv("VERA_PULL_AUTORESUME", "1")
    ns = _load("PULL_AUTORESUME = (", "async def _resume_pull(", os=os, re=re)
    assert ns["PULL_AUTORESUME"] is True


# ── rejected quant tags ─────────────────────────────────────────────────────
# HF's Ollama endpoint matches a tag against its own list of quantization
# schemes and rejects anything else with a flat 400 naming no alternatives — so
# every quant in the picker looks equally broken and you retry blind.
def test_quant_tag_parsed_from_gguf_filenames():
    ns = _load("_QUANT_TAG_RE = re.compile", "async def _hf_bad_tag_msg", re=re, os=os)
    rx, shard = ns["_QUANT_TAG_RE"], ns["_HF_SHARD_RE"]

    def tag_of(fn):
        stem = shard.sub("", os.path.basename(fn)[:-5])
        t = stem.rsplit("-", 1)[-1]
        return t if rx.match(t) else None

    assert tag_of("Qwen_Qwen3.6-35B-A3B-IQ2_XXS.gguf") == "IQ2_XXS"
    assert tag_of("Qwen3.6-35B-A3B-MXFP4_MOE.gguf") == "MXFP4_MOE"
    # 'UD-' is a publisher marker, not part of the tag HF serves.
    assert tag_of("Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf") == "Q8_K_XL"
    # Sharded weights collapse to the one quant they belong to.
    assert tag_of("Qwen3.6-35B-A3B-BF16-00001-of-00002.gguf") == "BF16"
    # A model name alone carries no quant — 'A3B' must not be mistaken for one.
    assert tag_of("Qwen3.6-35B-A3B.gguf") is None


# ── the restart race ────────────────────────────────────────────────────────
# Auto-resume runs at startup. It only picks up records marked stale, and stale
# meant "untouched for PULL_STALE_S". But the worker persists once a second right
# up to the instant it is killed, so at restart every record is ~1s old and looks
# alive: auto-resume found nothing, and _hydrate is one-shot so it never looked
# again. The heuristic raced the one event it exists to catch.
def _stale_ns(local_pulls=None, proc_age_s=5.0):
    import time as _t
    ns = _load_stale()
    ns["PULLS"] = local_pulls if local_pulls is not None else {}
    ns["PULL_OWNER"] = "vera-host"
    ns["_PROC_START"] = _t.time() - proc_age_s
    return ns


def _live_rec(**kw):
    """A record as it looks the instant its Vera is killed: written 1s ago."""
    r = {"id": "cpu-246-1-1", "state": "downloading", "owner": "vera-host",
         "updated_at": _ago(seconds=1), "started_at": _ago(hours=1)}
    r.update(kw)
    return r


def test_our_own_download_is_dead_the_moment_we_restart():
    """One second old, and still dead — nothing local is driving it."""
    ns = _stale_ns()
    assert ns["_pull_is_stale"](_live_rec())


def test_a_download_we_are_actually_running_is_not_touched():
    ns = _stale_ns(local_pulls={"cpu-246-1-1": {}})
    assert not ns["_pull_is_stale"](_live_rec())


def test_another_veras_live_download_is_left_alone():
    """No local task, but it is not ours — only silence may condemn it."""
    ns = _stale_ns()
    assert not ns["_pull_is_stale"](_live_rec(owner="other-vera"))
    assert ns["_pull_is_stale"](_live_rec(owner="other-vera",
                                          updated_at=_ago(seconds=3600)))


def test_records_from_a_build_without_owner_are_recovered_at_startup():
    """Downloads in flight when this fix ships have no owner recorded. The
    startup pass must still recognise them, or the upgrade itself strands them
    one final time."""
    ns = _stale_ns(proc_age_s=5.0)
    rec = _live_rec(updated_at=_ago(seconds=60))
    rec.pop("owner")
    assert ns["_predates_this_process"](rec), "legacy record not recovered"


def test_legacy_record_written_after_we_started_is_not_condemned():
    ns = _stale_ns(proc_age_s=3600.0)
    rec = _live_rec(updated_at=_ago(seconds=30))
    rec.pop("owner")
    assert not ns["_predates_this_process"](rec)


def test_predates_check_never_touches_an_owned_record():
    """It is a fallback for old records only — anything with an owner is judged
    by ownership, never by the blunt 'older than this process' rule."""
    ns = _stale_ns(proc_age_s=5.0)
    assert not ns["_predates_this_process"](_live_rec(updated_at=_ago(seconds=60)))


# ── transient vs permanent failure ──────────────────────────────────────────
# Ollama calls both "failed". A dropped connection 70% through a 20 GB pull is
# bad luck and should be picked back up; a rejected manifest is a verdict and
# retrying reproduces it forever. Nine real downloads died overnight on
# "max retries exceeded: EOF" and sat there because nothing told them apart.
def _fail(err, n=0):
    return {"state": "failed", "error": err, "resume_count": n}


@pytest.mark.parametrize("err", [
    "max retries exceeded: EOF",
    "max retries exceeded: stream error: stream ID 123; CANCEL; received from peer",
    "connection reset by peer",
    "read tcp: i/o timeout",
    "HTTP 503: upstream unavailable",
    "digest mismatch, file must be downloaded again",
])
def test_transport_failures_are_retried(resume, err):
    assert resume["_pull_is_resumable"](_fail(err))


@pytest.mark.parametrize("err", [
    "file does not exist",
    "'Q4_K_L' is not published for bartowski/x — that repo offers: Q4_K_M",
    "this quant is published incompletely on hugging face",
    "pull model manifest: 400: The specified tag is not a valid quantization scheme",
    "unauthorized",
    "no space left on device",
])
def test_verdicts_are_not_retried(resume, err):
    assert not resume["_pull_is_resumable"](_fail(err))


def test_a_permanent_message_wins_over_a_transient_word():
    """'not published' also contains no transport wording — but make sure a
    verdict that happens to mention a timeout is still treated as a verdict."""
    ns = _load("_TRANSIENT_RE = re.compile", "async def _resume_pull(", re=re, os=os)
    assert not ns["_pull_failure_is_transient"](
        {"error": "file does not exist (after timeout)"})


def test_retrying_gives_up_eventually(resume):
    """A model that keeps dropping must stop, not cycle forever."""
    n = resume["PULL_MAX_RETRIES"]
    assert resume["_pull_is_resumable"](_fail("EOF", n - 1))
    assert not resume["_pull_is_resumable"](_fail("EOF", n))


def test_an_unexplained_failure_is_left_alone(resume):
    """Unrecognised wording is not assumed to be worth retrying."""
    assert not resume["_pull_is_resumable"](_fail("something strange happened"))
