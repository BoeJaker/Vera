#!/usr/bin/env python3
"""Replay the NLI claim check (compute-roles P3) on saved loop runs, beside the LLM answer check.

For every saved run (probe_layout/ev/<sid>.jsonl) that ran the answer check:
  evidence  = the run's info.seek / web.research / web.fetch results (tool_done previews; a clipped
              preview is read leniently),
  claims A  = the claims the LLM answer check quoted in its fixes (it judged them wrong/unsupported),
  claims B  = the checkable claims in the answer as delivered (probe json `final`, after the check).
Each claim is paired with its best evidence windows (vera.dag.claim_nli_core) and scored by the NLI
model the nodes serve (DeBERTa-v3 mnli-fever-anli), run here on the CPU.

Reads only; writes ~/loop-census/nli_replay/<stamp>.json. Run it in a census gap (it takes the CPU).

  nli_replay.py [--ev-dir DIR] [--limit N] [--model ID] [--threads 2]
"""
import argparse
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..")))
from vera.dag import claim_nli_core as C  # noqa: E402

EVIDENCE_TOOLS = ("info.seek", "web.research", "web.fetch")
DATA = os.path.expanduser("~/loop-census")
MODEL = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"  # pragma: allowlist secret
_JSTR = r'"((?:[^"\\]|\\.)*)'           # a JSON string body, possibly cut off at the end


def _lenient_items(preview: str):
    """Items from a tool result preview: parsed JSON when whole, else the string fields read with
    a regex (previews are clipped mid-JSON)."""
    try:
        from vera.dag import evidence_ledger_core as L
        got = L.items_from(json.loads(preview))
        if got:
            return got
    except Exception:
        pass
    items, cur = [], {}
    for m in re.finditer(r'"(title|url|published|text|snippet|content)"\s*:\s*' + _JSTR, preview or ""):
        k, v = m.group(1), m.group(2)
        try:
            v = json.loads('"%s"' % v)
        except Exception:
            v = v.replace('\\"', '"').replace("\\n", "\n")
        if k in cur and k in ("title", "url"):
            items.append(cur)
            cur = {}
        cur["text" if k in ("snippet", "content") and "text" not in cur else k] = v
    if cur:
        items.append(cur)
    out = []
    for k, it in enumerate(i for i in items if i.get("text") or i.get("title")):
        it.setdefault("n", k + 1)
        out.append(it)
    return out


def run_inputs(path: str):
    ev = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    ac = [e for e in ev if e.get("type") == "agent_loop_v6.answer_check"]
    if not ac:
        return None
    items = []
    for e in ev:
        if e.get("type") == "agent_loop_v5.tool_done" and e.get("tool") in EVIDENCE_TOOLS and e.get("ok"):
            for it in _lenient_items(e.get("preview") or ""):
                it["e"] = len(items) + 1
                items.append(it)
    fixes = ac[-1].get("fixes") or ""
    flagged = []
    for q in re.findall(r"[\"“]([^\"”]{12,300})[\"”]", fixes):
        if len(q.split()) >= 3 and q not in flagged:
            flagged.append(q)
    sid = os.path.basename(path)[:-6]
    final = ""
    for pj in glob.glob(os.path.join(os.path.dirname(os.path.dirname(path)), "*.json")):
        try:
            d = json.load(open(pj, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and d.get("sid") == sid:
            final = d.get("final") or ""
            break
    if not final:
        for e in ev:
            if e.get("type") == "agent_loop_v6.deliverable":
                final = e.get("markdown") or final
    return {"sid": sid, "items": items, "flagged": flagged, "final": final, "fixes": fixes}


class NLI:
    def __init__(self, model_id: str, threads: int):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(max(1, threads))
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id).eval()
        self.labels = {i: l.lower() for i, l in self.model.config.id2label.items()}

    def score(self, pairs, batch: int = 8):
        out = []
        for k in range(0, len(pairs), batch):
            chunk = pairs[k:k + batch]
            enc = self.tok([p["premise"] for p in chunk], [p["hypothesis"] for p in chunk],
                           truncation="only_first", max_length=320, padding=True, return_tensors="pt")
            with self.torch.no_grad():
                probs = self.torch.softmax(self.model(**enc).logits, dim=-1).tolist()
            out += [{self.labels[i]: float(p) for i, p in enumerate(row)} for row in probs]
        return out


VERA = os.environ.get("VERA_URL", "https://127.0.0.1:8999")


def _rerank(query, docs):
    """nlp.rerank on a node (cross-encoder): document indices, best first; None when unavailable."""
    import ssl
    import urllib.request
    body = json.dumps({"name": "nlp.rerank", "arguments": {"query": query, "documents": docs}}).encode()
    req = urllib.request.Request(VERA + "/mcp/call", data=body, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as r:
            out = json.loads(r.read()).get("content") or {}
        return [int(x["index"]) for x in out.get("ranked") or []] or None
    except Exception:
        return None


def ranked_order(claims, items):
    """{claim i: [window index, best first]} from nlp.rerank over each claim's candidates."""
    wins = C.windows(items)
    order = {}
    for c in claims:
        cand = C.candidates(c, wins)
        if not cand:
            continue
        got = _rerank(c["text"], [wins[k]["text"] for k in cand])
        if got:
            order[c["i"]] = [cand[j] for j in got if j < len(cand)]
    return order


def _norm(s):
    return re.sub(r"^[^A-Za-z0-9]+", "", s or "")


def label_of(labels, sid, text):
    for key, lab in (labels.get(sid[:8]) or {}).items():
        if _norm(text).startswith(_norm(key)):
            return lab
    return None


def check(nli, claims, items, rerank=False):
    prs = C.pairs(claims, items, ranked=ranked_order(claims, items) if rerank else None)
    t = time.monotonic()
    scores = nli.score([{"premise": p["premise"], "hypothesis": claims[p["claim"]]["text"]} for p in prs]) if prs else []
    vs = C.verdicts(claims, prs, scores)
    return vs, len(prs), time.monotonic() - t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ev-dir", default=os.path.join(DATA, "probe_layout", "ev"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--rerank", action="store_true", help="pair claims with evidence via nlp.rerank")
    ap.add_argument("--labels", default="", help="hand labels JSON {sid8: {claim prefix: ok|bad}}")
    a = ap.parse_args()
    labels = json.load(open(a.labels, encoding="utf-8")) if a.labels else {}
    conf = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "unlabelled": 0}
    runs = [r for r in (run_inputs(p) for p in sorted(glob.glob(os.path.join(a.ev_dir, "*.jsonl")))) if r]
    if a.limit:
        runs = runs[:a.limit]
    print("runs with an answer check:", len(runs), flush=True)
    nli = NLI(a.model, a.threads)
    res, tot = [], {"A": [0, 0], "B": [0, 0], "pairs": 0, "secs": 0.0}
    for r in runs:
        ca = [{"i": k, "text": q[:C.CLAIM_CHARS], "cites": []} for k, q in enumerate(r["flagged"][:C.MAX_CLAIMS])]
        cb = C.claims(r["final"])
        va, na, sa = check(nli, ca, r["items"], a.rerank)
        vb, nb, sb = check(nli, cb, r["items"], a.rerank)
        if labels and r["items"]:
            for v in vb:
                lab = label_of(labels, r["sid"], v["text"])
                v["label"] = lab
                flag = v["verdict"] != "supported"
                if lab is None:
                    conf["unlabelled"] += 1
                else:
                    conf[("tp" if flag else "fn") if lab == "bad" else ("fp" if flag else "tn")] += 1
        tot["A"][0] += sum(v["verdict"] != "supported" for v in va); tot["A"][1] += len(va)
        tot["B"][0] += sum(v["verdict"] != "supported" for v in vb); tot["B"][1] += len(vb)
        tot["pairs"] += na + nb; tot["secs"] += sa + sb
        res.append({"sid": r["sid"], "evidence_items": len(r["items"]),
                    "llm_flagged": va, "delivered": vb, "llm_fixes": r["fixes"][:1500]})
        print("%s items=%d  LLM-flagged: %d/%d not supported  delivered: %d/%d not supported"
              % (r["sid"][:8], len(r["items"]), sum(v["verdict"] != "supported" for v in va), len(va),
                 sum(v["verdict"] != "supported" for v in vb), len(vb)), flush=True)
    summ = {"runs": len(runs), "model": a.model,
            "llm_flagged_not_supported": tot["A"], "delivered_not_supported": tot["B"],
            "pairs": tot["pairs"], "ms_per_pair": round(1000 * tot["secs"] / max(1, tot["pairs"]), 1),
            "rerank": a.rerank}
    if labels:
        tp, fp, fn = conf["tp"], conf["fp"], conf["fn"]
        summ["vs_labels"] = dict(conf, precision=round(tp / max(1, tp + fp), 2),
                                 recall=round(tp / max(1, tp + fn), 2))
    os.makedirs(os.path.join(DATA, "nli_replay"), exist_ok=True)
    outp = os.path.join(DATA, "nli_replay", time.strftime("%Y%m%d-%H%M%S") + ".json")
    json.dump({"summary": summ, "runs": res}, open(outp, "w"), indent=1)
    print(json.dumps(summ), "\n->", outp)


if __name__ == "__main__":
    main()
