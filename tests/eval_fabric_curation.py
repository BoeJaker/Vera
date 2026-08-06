"""
eval_fabric_curation.py — live end-to-end evaluation of the curated-dataset layer
=================================================================================

NOT a pytest (needs the live Vera instance with the curation module loaded).
Run it against a running Vera to prove the two motivating scenarios end to end:

  1. Pokedex   — upsert gen-1 twice → no duplicates; a partial re-fetch tops up
                 missing fields (gap-fill) rather than duplicating or clobbering.
  2. Markets   — append a price series, append newer points (idempotent on the
                 overlap), then serve a date range with memory.select.

Usage (from the repo root, inside the Vera environment):
    python tests/eval_fabric_curation.py            # in-process via the registry
    VERA_BASE=https://llm.int:8999 python tests/eval_fabric_curation.py   # HTTP

Exit code 0 = all scenarios passed.
"""

import asyncio
import json
import os
import sys

BASE = os.environ.get("VERA_BASE", "")


async def _via_registry(cap: str, **kw):
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    c = CAPABILITY_REGISTRY.get(cap)
    if not c or not c.get("func"):
        raise RuntimeError(f"cap not registered: {cap} (is the module loaded?)")
    return await c["func"](**kw)


def _http_call(cap: str, method: str, path: str, **kw):
    import httpx
    url = BASE.rstrip("/") + path
    # Bulk keyed upserts embed every row — allow for a cold embed model.
    timeout = float(os.environ.get("VERA_TIMEOUT", "240"))
    with httpx.Client(verify=False, timeout=timeout) as client:
        if method == "GET":
            r = client.get(url, params=kw)
        else:
            r = client.post(url, json=kw)
        r.raise_for_status()
        return r.json()


# (cap, method, path) for the HTTP path.
_ROUTES = {
    "fabric.upsert":         ("POST", "/fabric/upsert"),
    "fabric.schema.declare": ("POST", "/fabric/schema/declare"),
    "fabric.schema.get":     ("GET",  "/fabric/schema/get"),
    "fabric.validate":       ("POST", "/fabric/validate"),
    "memory.select":         ("POST", "/memory/select"),
    "fabric.delete_dataset": ("POST", "/fabric/delete"),
    "fabric.identify":       ("POST", "/fabric/identify"),
    "fabric.gaps":           ("POST", "/fabric/gaps"),
    "fabric.gaps.attempt":   ("POST", "/fabric/gaps/attempt"),
    "fabric.gaps.resolve":   ("POST", "/fabric/gaps/resolve"),
    "fabric.fuse":           ("POST", "/fabric/fuse"),
    "fabric.fuse.refresh":   ("POST", "/fabric/fuse/refresh"),
    "fabric.datasets.tag":   ("POST", "/fabric/datasets/tag"),
    "context.for_agent":     ("POST", "/context/for_agent"),
}


async def call(cap: str, **kw):
    if BASE:
        method, path = _ROUTES[cap]
        return _http_call(cap, method, path, **kw)
    return await _via_registry(cap, **kw)


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        raise AssertionError(name)


async def scenario_pokedex():
    print("\n[1] Pokedex — idempotent upsert + gap-fill")
    ds = "eval.pokedex.gen1"
    dex = [{"id": i, "name": f"mon{i}", "type": "normal"} for i in range(1, 152)]
    await call("fabric.schema.declare", dataset_id=ds,
               schema={"id": {"type": "integer", "required": True},
                       "name": {"type": "string", "required": True},
                       "type": {"type": "string"}},
               key="id", kind="table", trust=0.9)
    r1 = await call("fabric.upsert", dataset_id=ds,
                    rows=json.dumps(dex), key="id", mode="merge")
    check("first ingest counts 151", r1.get("record_count") == 151, str(r1.get("record_count")))
    r2 = await call("fabric.upsert", dataset_id=ds,
                    rows=json.dumps(dex), key="id", mode="merge")
    check("re-ingest does NOT duplicate", r2.get("record_count") == 151, str(r2.get("record_count")))
    check("re-ingest reports 0 new", r2.get("new") == 0, str(r2.get("new")))
    # Partial re-fetch that adds a field to one mon → gap-fill, no new row.
    r3 = await call("fabric.upsert", dataset_id=ds,
                    rows=json.dumps([{"id": 25, "weight_kg": 6.0}]),
                    key="id", mode="merge")
    check("gap-fill adds no rows", r3.get("record_count") == 151, str(r3.get("record_count")))
    sel = await call("memory.select", dataset_id=ds,
                     where=json.dumps([{"field": "id", "op": "eq", "value": 25}]))
    row = (sel.get("rows") or [{}])[0].get("data", {})
    check("gap-fill kept name AND added weight",
          row.get("name") == "mon25" and row.get("weight_kg") == 6.0, json.dumps(row))
    v = await call("fabric.validate", dataset_id=ds)
    check("validate: clean, high quality", v.get("ok") and v.get("quality_score") == 1.0,
          f"q={v.get('quality_score')} trust={v.get('trust')}")


async def scenario_markets():
    print("\n[2] Markets — append series + range select")
    ds = "eval.market.btcusd"
    await call("fabric.schema.declare", dataset_id=ds,
               schema={"symbol": {"type": "string", "required": True},
                       "date": {"type": "string", "required": True},
                       "close": {"type": "number"}},
               key="symbol,date", kind="timeseries", trust=0.85)
    jan = [{"symbol": "BTC", "date": f"2026-01-{d:02d}", "close": 40000 + d}
           for d in range(1, 11)]
    await call("fabric.upsert", dataset_id=ds, rows=json.dumps(jan),
               key="symbol,date", mode="append")
    # New points overlapping the last day (idempotent on the overlap).
    more = [{"symbol": "BTC", "date": f"2026-01-{d:02d}", "close": 40000 + d}
            for d in range(10, 16)]
    r = await call("fabric.upsert", dataset_id=ds, rows=json.dumps(more),
                   key="symbol,date", mode="append")
    check("series length is 15 (overlap dedup)", r.get("record_count") == 15,
          str(r.get("record_count")))
    rng = await call("memory.select", dataset_id=ds,
                     where=json.dumps([{"field": "date", "op": "gte", "value": "2026-01-05"},
                                       {"field": "date", "op": "lte", "value": "2026-01-08"}]),
                     sort=json.dumps([{"field": "date", "dir": "asc"}]))
    dates = [row["data"]["date"] for row in rng.get("rows", [])]
    check("range select returns Jan 5-8 in order",
          dates == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"], str(dates))


async def scenario_identify_gaps():
    print("\n[3] Identify + gaps + noise/backoff suppression")
    ds = "eval.pokedex.gen1"     # created by scenario 1 (ids 1..151)
    # identify: we already have it → recommend reuse.
    idr = await call("fabric.identify", subject="pokedex gen1",
                     expected_fields="id,name")
    best = idr.get("best") or {}
    check("identify finds the existing pokedex", best.get("dataset_id") == ds,
          f"{best.get('dataset_id')} score={best.get('score')}")
    check("recommendation is reuse", idr.get("recommendation") == "reuse",
          str(idr.get("recommendation")))
    # Reset ledger for these keys so the scenario is re-runnable (a prior run
    # left 153=noise / 154=unfillable).
    for k in ("152", "153", "154", "155"):
        await call("fabric.gaps.resolve", dataset_id=ds,
                   gap={"type": "key", "ref": k}, status="open", reason="eval reset")
    # gaps: expect ids 1..155 → 152,153,154,155 missing.
    g = await call("fabric.gaps", dataset_id=ds, key_field="id",
                   key_range=json.dumps({"min": 1, "max": 155}))
    missing = sorted(int(x["ref"]) for x in g.get("actionable", []) if x["type"] == "key")
    check("gaps: 152-155 actionable", missing == [152, 153, 154, 155], str(missing))
    # Mark 153 as noise, and burn 154 through the attempt cap → both suppressed.
    await call("fabric.gaps.resolve", dataset_id=ds,
               gap={"type": "key", "ref": "153"}, status="noise", reason="not a real mon")
    for _ in range(4):
        await call("fabric.gaps.attempt", dataset_id=ds,
                   gaps=json.dumps([{"type": "key", "ref": "154"}]), outcome="failed")
    g2 = await call("fabric.gaps", dataset_id=ds, key_field="id",
                    key_range=json.dumps({"min": 1, "max": 155}))
    act2 = sorted(int(x["ref"]) for x in g2.get("actionable", []) if x["type"] == "key")
    supp = {x["gap"]["ref"]: x["reason"] for x in g2.get("suppressed", [])}
    check("153 (noise) no longer re-triggers", "153" not in [str(a) for a in act2]
          and supp.get("153") == "noise", str(supp))
    check("154 suppressed after attempt cap",
          supp.get("154") in ("unfillable", "attempts_exhausted"), str(supp))
    check("152,155 still actionable", act2 == [152, 155], str(act2))


async def scenario_fuse():
    print("\n[4] Fusion — row join → ephemeral dataset + recipe")
    a, b, into = "eval.fuse.left", "eval.fuse.right", "eval.fuse.out"
    await call("fabric.upsert", dataset_id=a, key="id", mode="replace",
               rows=json.dumps([{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]))
    await call("fabric.upsert", dataset_id=b, key="id", mode="replace",
               rows=json.dumps([{"id": 1, "score": 10}, {"id": 3, "score": 30}]))
    r = await call("fabric.fuse", left=a, right=b, on="id", how="inner", into=into)
    check("inner fuse yields 1 matched row", r.get("rows") == 1, str(r.get("rows")))
    sel = await call("memory.select", dataset_id=into)
    row = (sel.get("rows") or [{}])[0].get("data", {})
    check("fused row has both sides", row.get("name") == "alpha" and row.get("score") == 10,
          json.dumps(row))
    rr = await call("fabric.fuse.refresh", into=into)
    check("refresh re-runs the recipe", rr.get("refreshed") and rr.get("rows") == 1,
          str(rr.get("rows")))
    for ds in (a, b, into):
        try: await call("fabric.delete_dataset", dataset_id=ds)
        except Exception: pass


async def scenario_context():
    print("\n[5] context.for_agent — trust-ranked datasets above memories")
    ds = "eval.ctx.dex"
    await call("fabric.schema.declare", dataset_id=ds,
               schema={"id": {"type": "integer", "required": True},
                       "name": {"type": "string", "required": True}},
               key="id", kind="table", trust=0.9)
    await call("fabric.upsert", dataset_id=ds, key="id", mode="merge",
               rows=json.dumps([{"id": 1, "name": "bulbasaur"},
                                {"id": 4, "name": "charmander"}]))
    # Tag it for the fabric family so an agent scoped to that family finds it.
    await call("fabric.datasets.tag", dataset_id=ds, tags="family:fabric", action="add")
    ctx = await call("context.for_agent", query="pokemon bulbasaur",
                     agent="fabric-librarian", profile="fabric-discovery",
                     slice_rows=3)
    dsets = ctx.get("datasets", [])
    ids = [d["dataset_id"] for d in dsets]
    check("context surfaces the curated dataset", ds in ids, str(ids))
    d = next((x for x in dsets if x["dataset_id"] == ds), {})
    check("dataset carries its declared schema", "name" in (d.get("schema") or {}),
          json.dumps(d.get("schema")))
    check("dataset carries a data slice", bool(d.get("slice")), json.dumps(d.get("slice"))[:120])
    check("curated trust is high (0.9)", d.get("trust") == 0.9, str(d.get("trust")))
    check("prompt marks datasets authoritative",
          "AUTHORITATIVE DATASETS" in (ctx.get("prompt") or ""), (ctx.get("prompt") or "")[:80])
    await call("fabric.delete_dataset", dataset_id=ds)


async def main():
    try:
        await scenario_pokedex()
        await scenario_markets()
        await scenario_identify_gaps()
        await scenario_fuse()
        await scenario_context()
    except Exception as e:
        print(f"\nFAILED: {e!r}")
        return 1
    # Best-effort cleanup of the eval datasets' vectors.
    for ds in ("eval.pokedex.gen1", "eval.market.btcusd"):
        try:
            await call("fabric.delete_dataset", dataset_id=ds)
        except Exception:
            pass
    print("\nAll scenarios passed ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
