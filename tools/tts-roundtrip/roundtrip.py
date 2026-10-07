#!/usr/bin/env python3
"""Round-trip check of a media node's speech: render each corpus line with the
node's TTS, transcribe it back with the same node's Whisper, and score it.

    python3 tools/tts-roundtrip/roundtrip.py --node http://192.168.0.246:8765 [--out DIR] [--only ID,ID]

Per line: the words heard vs the words expected (word error rate after both go
through the same text normaliser, since Whisper writes "108 to 300" for "one
hundred and eight to three hundred"), and the silent gaps in the audio (>=150 ms),
so a pacing change shows as numbers, not an impression. One request at a time:
the nodes are shared, so keep runs short and never alongside a census.

Corpus: corpus.jsonl beside this file, one {"id", "text", "expect"[, "gaps"]} per
line - "expect" is the spoken form, "gaps" the number of pauses a reader leaves.
Stdlib only (runs on the Vera host or any box that reaches the node).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
import uuid
import wave
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "edge"))
import tts_text_core as tn  # noqa: E402  - the same normaliser the node runs


def _post_json(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post_wav(url: str, wav: bytes, timeout: float) -> dict:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nen\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gaps(wav: bytes, min_s: float = 0.15, quiet_db: float = -40.0) -> list:
    """Silent runs inside the audio (leading and trailing silence excluded), in seconds."""
    with wave.open(io.BytesIO(wav)) as w:
        sr, n, width = w.getframerate(), w.getnframes(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2:
        return []
    pcm = array("h", raw)
    frame = max(1, sr // 100)                                    # 10 ms frames
    loud = []
    for i in range(0, len(pcm) - frame + 1, frame):
        seg = pcm[i:i + frame]
        loud.append((sum(x * x for x in seg) / frame) ** 0.5)
    if not loud or max(loud) == 0:
        return []
    floor = max(loud) * 10 ** (quiet_db / 20)
    quiet = [v <= floor for v in loud]
    first = next((i for i, q in enumerate(quiet) if not q), 0)
    last = len(quiet) - next((i for i, q in enumerate(reversed(quiet)) if not q), 0)
    out, run = [], 0
    for q in quiet[first:last]:
        if q:
            run += 1
        else:
            if run * 0.01 >= min_s:
                out.append(round(run * 0.01, 2))
            run = 0
    return out


def words(text: str) -> list:
    t = tn.normalize(text).lower().replace("-", " ")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return [w for w in t.split() if w != "and"]               # "one hundred (and) eight": both are right


def wer(ref: list, hyp: list) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)] / max(1, len(ref))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True, help="media server base URL, e.g. http://192.168.0.246:8765")
    ap.add_argument("--corpus", default=os.path.join(HERE, "corpus.jsonl"))
    ap.add_argument("--out", default="", help="directory for report.json and the rendered wavs")
    ap.add_argument("--only", default="", help="comma-separated ids")
    ap.add_argument("--voice", default="")
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()

    only = {x for x in a.only.split(",") if x}
    lines = [json.loads(x) for x in open(a.corpus, encoding="utf-8") if x.strip() and not x.startswith("#")]
    lines = [x for x in lines if not only or x["id"] in only]
    cfg = {}
    try:
        with urllib.request.urlopen(a.node.rstrip("/") + "/tts/config", timeout=10) as r:
            cfg = json.load(r)
    except Exception as e:  # noqa: BLE001 - an older node without the route
        cfg = {"error": str(e)}
    if a.out:
        os.makedirs(a.out, exist_ok=True)

    rows = []
    for x in lines:
        body = {"text": x["text"]}
        if a.voice:
            body["voice"] = a.voice
        t0 = time.time()
        try:
            r = _post_json(a.node.rstrip("/") + "/tts", body, a.timeout)
            wav = base64.b64decode(r["audio_b64"])
            t1 = time.time()
            heard = _post_wav(a.node.rstrip("/") + "/stt", wav, a.timeout).get("text", "")
        except Exception as e:  # noqa: BLE001
            rows.append({"id": x["id"], "error": f"{type(e).__name__}: {e}"[:200]})
            print(f"{x['id']:<14} ERROR {e}", flush=True)
            continue
        ref, hyp = words(x["expect"]), words(heard)
        g = gaps(wav)
        with wave.open(io.BytesIO(wav)) as w:
            dur = w.getnframes() / w.getframerate()
        row = {"id": x["id"], "text": x["text"], "expect": x["expect"], "heard": heard.strip(),
               "wer": round(wer(ref, hyp), 3), "gaps": g, "gaps_expected": x.get("gaps"),
               "audio_s": round(dur, 2), "render_s": round(t1 - t0, 2)}
        rows.append(row)
        if a.out:
            open(os.path.join(a.out, f"{x['id']}.wav"), "wb").write(wav)
        print(f"{x['id']:<14} wer {row['wer']:.2f}  gaps {len(g)}{'/' + str(x['gaps']) if 'gaps' in x else ''} "
              f"{g}  heard: {row['heard'][:90]}", flush=True)

    ok = [r for r in rows if "wer" in r]
    summary = {"node": a.node, "config": cfg, "lines": len(rows), "errors": len(rows) - len(ok),
               "mean_wer": round(sum(r["wer"] for r in ok) / max(1, len(ok)), 3),
               "exact": sum(1 for r in ok if r["wer"] == 0),
               "pacing_hits": sum(1 for r in ok if r.get("gaps_expected") is not None
                                  and len(r["gaps"]) >= r["gaps_expected"]),
               "pacing_lines": sum(1 for r in ok if r.get("gaps_expected") is not None),
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    print(json.dumps(summary))
    if a.out:
        json.dump({"summary": summary, "rows": rows}, open(os.path.join(a.out, "report.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
