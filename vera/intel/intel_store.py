"""Intelligence - the store.

One SQLite file outside the source tree (state_paths) holding topics, signals,
snapshots, reports, actions and deliveries. Each row keeps its whole record as
JSON plus the few columns the timeline and lists filter on, so the record shape
can grow without migrations. Pure stdlib; the path is passed in, so tests run it
against a temporary file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics   (id TEXT PRIMARY KEY, status TEXT, updated TEXT, rec TEXT);
CREATE TABLE IF NOT EXISTS signals  (id TEXT PRIMARY KEY, topic_id TEXT, seen TEXT, score REAL,
                                     enriched INTEGER, reported INTEGER DEFAULT 0, rec TEXT);
CREATE INDEX IF NOT EXISTS ix_sig_topic_seen ON signals(topic_id, seen);
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, topic_id TEXT, ts TEXT, rec TEXT);
CREATE INDEX IF NOT EXISTS ix_snap_topic_ts ON snapshots(topic_id, ts);
CREATE TABLE IF NOT EXISTS reports  (id TEXT PRIMARY KEY, ts TEXT, title TEXT, topic_ids TEXT, rec TEXT, html TEXT);
CREATE INDEX IF NOT EXISTS ix_rep_ts ON reports(ts);
CREATE TABLE IF NOT EXISTS actions  (token TEXT PRIMARY KEY, report_id TEXT, topic_id TEXT, status TEXT,
                                     created TEXT, rec TEXT);
CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT, channel TEXT,
                                      ts TEXT, ok INTEGER, rec TEXT);
CREATE TABLE IF NOT EXISTS kv       (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, ts TEXT, question_id TEXT, ref_kind TEXT,
                                     ref_id TEXT, answer TEXT, rec TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_ref ON decisions(ref_kind, ref_id);
CREATE INDEX IF NOT EXISTS ix_dec_q_ts ON decisions(question_id, ts);
CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT, ts TEXT, rec TEXT);
CREATE INDEX IF NOT EXISTS ix_out_dec ON outcomes(decision_id);
"""


class IntelStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        """Open the store, retrying a transient 'unable to open database file'
        (seen on prod under load); a persistent failure still raises."""
        last: Optional[Exception] = None
        for attempt in range(4):
            try:
                c = sqlite3.connect(self.path, timeout=15)
                c.execute("PRAGMA journal_mode=WAL")
                return c
            except sqlite3.OperationalError as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise last  # type: ignore[misc]

    # ── kv (config, cursors, last-run marks) ──
    def get_kv(self, k: str, default: Any = None) -> Any:
        with self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_kv(self, k: str, v: Any) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # ── topics ──
    def put_topic(self, t: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO topics(id,status,updated,rec) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "status=excluded.status, updated=excluded.updated, rec=excluded.rec",
                      (t["id"], t.get("status", "active"), t.get("updated", ""), json.dumps(t)))
        return t

    def patch_topic(self, tid: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Re-read the topic and change only `fields` - so a long background step
        never writes back a stale copy over an edit (or a delete) made meanwhile."""
        with self._lock, self._conn() as c:
            r = c.execute("SELECT rec FROM topics WHERE id=?", (tid,)).fetchone()
            if not r:
                return None
            t = json.loads(r[0])
            t.update(fields)
            c.execute("UPDATE topics SET status=?, rec=? WHERE id=?", (t.get("status", "active"), json.dumps(t), tid))
        return t

    def get_topic(self, tid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT rec FROM topics WHERE id=?", (tid,)).fetchone()
        return json.loads(r[0]) if r else None

    def topics(self, status: str = "") -> List[Dict[str, Any]]:
        q, a = "SELECT rec FROM topics", ()
        if status:
            q, a = q + " WHERE status=?", (status,)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY updated DESC", a).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_topic(self, tid: str, *, purge: bool = False) -> bool:
        with self._lock, self._conn() as c:
            n = c.execute("DELETE FROM topics WHERE id=?", (tid,)).rowcount
            if purge:
                c.execute("DELETE FROM signals WHERE topic_id=?", (tid,))
                c.execute("DELETE FROM snapshots WHERE topic_id=?", (tid,))
        return bool(n)

    # ── signals ──
    def has_signal(self, sid: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM signals WHERE id=?", (sid,)).fetchone() is not None

    def put_signal(self, s: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO signals(id,topic_id,seen,score,enriched,reported,rec) VALUES(?,?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET seen=excluded.seen, score=excluded.score, enriched=excluded.enriched, "
                      "rec=excluded.rec",   # the indexed columns always match the record
                      (s["id"], s.get("topic_id", ""), s.get("seen", ""), s.get("score"),
                       1 if s.get("enriched") else 0, 1 if s.get("reported") else 0, json.dumps(s)))

    def get_signal(self, sid: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT rec FROM signals WHERE id=?", (sid,)).fetchone()
        return json.loads(r[0]) if r else None

    def signals(self, topic_id: str = "", *, since: str = "", until: str = "", limit: int = 200,
                min_score: Optional[float] = None, enriched: Optional[bool] = None,
                unreported: bool = False, order: str = "seen") -> List[Dict[str, Any]]:
        q = "SELECT rec FROM signals WHERE 1=1"
        a: List[Any] = []
        if topic_id:
            q += " AND topic_id=?"; a.append(topic_id)
        if since:
            q += " AND seen>=?"; a.append(since)
        if until:
            q += " AND seen<=?"; a.append(until)
        if min_score is not None:
            q += " AND score>=?"; a.append(min_score)
        if enriched is not None:
            q += " AND enriched=?"; a.append(1 if enriched else 0)
        if unreported:
            q += " AND reported=0"
        q += " ORDER BY " + ("score DESC, seen DESC" if order == "score" else "seen DESC") + " LIMIT ?"
        a.append(int(limit))
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute(q, a).fetchall()]

    def mark_reported(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("UPDATE signals SET reported=1 WHERE id=?", [(i,) for i in ids])

    def count_signals(self, *, since: str = "", topic_id: str = "") -> int:
        q, a = "SELECT COUNT(*) FROM signals WHERE 1=1", []
        if since:
            q += " AND seen>=?"; a.append(since)
        if topic_id:
            q += " AND topic_id=?"; a.append(topic_id)
        with self._conn() as c:
            return int(c.execute(q, a).fetchone()[0])

    # ── snapshots ──
    def put_snapshot(self, s: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO snapshots(id,topic_id,ts,rec) VALUES(?,?,?,?)",
                      (s["id"], s.get("topic_id", ""), s.get("ts", ""), json.dumps(s)))

    def snapshots(self, topic_id: str = "", *, since: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        q, a = "SELECT rec FROM snapshots WHERE 1=1", []
        if topic_id:
            q += " AND topic_id=?"; a.append(topic_id)
        if since:
            q += " AND ts>=?"; a.append(since)
        q += " ORDER BY ts DESC LIMIT ?"; a.append(int(limit))
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute(q, a).fetchall()]

    def latest_snapshot(self, topic_id: str) -> Optional[Dict[str, Any]]:
        s = self.snapshots(topic_id, limit=1)
        return s[0] if s else None

    # ── reports ──
    def put_report(self, doc: Dict[str, Any], html: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO reports(id,ts,title,topic_ids,rec,html) VALUES(?,?,?,?,?,?)",
                      (doc["id"], doc.get("ts", ""), doc.get("title", ""), ",".join(doc.get("topic_ids") or []),
                       json.dumps(doc), html))

    def get_report(self, rid: str, *, with_html: bool = False) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT rec, html FROM reports WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        d = json.loads(r[0])
        if with_html:
            d["_html"] = r[1]
        return d

    def reports(self, *, topic_id: str = "", since: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        q, a = "SELECT id, ts, title, topic_ids, rec FROM reports WHERE 1=1", []
        if topic_id:
            q += " AND (','||topic_ids||',') LIKE ?"; a.append(f"%,{topic_id},%")
        if since:
            q += " AND ts>=?"; a.append(since)
        q += " ORDER BY ts DESC LIMIT ?"; a.append(int(limit))
        with self._conn() as c:
            rows = c.execute(q, a).fetchall()
        out = []
        for rid, ts, title, tids, rec in rows:
            d = json.loads(rec)
            out.append({"id": rid, "ts": ts, "title": title, "topic_ids": [x for x in tids.split(",") if x],
                        "counts": d.get("counts", {}), "delivered": d.get("delivered", [])})
        return out

    def count_reports(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM reports").fetchone()[0])

    def record_family_counts(self) -> Dict[str, int]:
        """Exact content-free counts without materialising stored JSON records."""
        tables = ("topics", "signals", "snapshots", "reports", "actions",
                  "deliveries", "decisions", "outcomes", "kv")
        with self._conn() as c:
            return {table: int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in tables}

    def update_report(self, rid: str, **fields: Any) -> None:
        d = self.get_report(rid)
        if not d:
            return
        d.update(fields)
        with self._lock, self._conn() as c:
            c.execute("UPDATE reports SET rec=? WHERE id=?", (json.dumps(d), rid))

    # ── actions ──
    def put_action(self, a: Dict[str, Any], *, report_id: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO actions(token,report_id,topic_id,status,created,rec) VALUES(?,?,?,?,?,?)",
                      (a["token"], report_id or a.get("report_id", ""), a.get("topic_id", ""),
                       a.get("status", "offered"), a.get("created", ""), json.dumps(a)))

    def get_action(self, token: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT rec FROM actions WHERE token=?", (token,)).fetchone()
        return json.loads(r[0]) if r else None

    def claim_action(self, token: str) -> bool:
        """offered (or failed) -> running exactly once, so a double press (or a
        retried Telegram link) never runs the same action twice; a failure may be retried."""
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE actions SET status='running' WHERE token=? AND status IN ('offered','failed')",
                          (token,)).rowcount
        return n == 1

    def finish_action(self, token: str, status: str, result: Dict[str, Any]) -> None:
        a = self.get_action(token) or {"token": token}
        from datetime import datetime, timezone
        a.update({"status": status, "result": result,
                  "finished": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        with self._lock, self._conn() as c:
            c.execute("UPDATE actions SET status=?, rec=? WHERE token=?", (status, json.dumps(a), token))

    def actions(self, *, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        q, a = "SELECT rec FROM actions WHERE 1=1", []
        if status:
            q += " AND status=?"; a.append(status)
        q += " ORDER BY created DESC LIMIT ?"; a.append(int(limit))
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute(q, a).fetchall()]

    # ── decisions and outcomes (four-minds Decision/Outcome v0, kept verbatim) ──
    def put_decision(self, d: Dict[str, Any]) -> None:
        ref = d.get("state_ref") or {}
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO decisions(decision_id,ts,question_id,ref_kind,ref_id,answer,rec) "
                      "VALUES(?,?,?,?,?,?,?)", (d["decision_id"], d.get("ts", ""), d.get("question_id", ""),
                                                ref.get("kind", ""), ref.get("id", ""), str(d.get("answer")),
                                                json.dumps(d)))

    def decisions_for(self, ref_kind: str, ref_id: str) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT rec FROM decisions WHERE ref_kind=? AND ref_id=? ORDER BY ts",
                             (ref_kind, ref_id)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_outcome(self, o: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO outcomes(decision_id,ts,rec) VALUES(?,?,?)",
                      (o["decision_id"], o.get("ts", ""), json.dumps(o)))

    def decisions(self, *, question_id: str = "", since: str = "", limit: int = 500,
                  with_outcomes: bool = True) -> List[Dict[str, Any]]:
        q, a = "SELECT rec FROM decisions WHERE 1=1", []
        if question_id:
            q += " AND question_id=?"; a.append(question_id)
        if since:
            q += " AND ts>=?"; a.append(since)
        q += " ORDER BY ts DESC LIMIT ?"; a.append(int(limit))
        with self._conn() as c:
            rows = [json.loads(r[0]) for r in c.execute(q, a).fetchall()]
            if with_outcomes and rows:
                ids = [r["decision_id"] for r in rows]
                outs: Dict[str, List[Dict[str, Any]]] = {}
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    for (rec,) in c.execute("SELECT rec FROM outcomes WHERE decision_id IN (%s)"
                                            % ",".join("?" * len(chunk)), chunk).fetchall():
                        o = json.loads(rec)
                        outs.setdefault(o["decision_id"], []).append(o)
                for r in rows:
                    r["outcomes"] = outs.get(r["decision_id"], [])
        return rows

    def outcomes_since(self, since: str = "", limit: int = 5000) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [json.loads(r[0]) for r in c.execute(
                "SELECT rec FROM outcomes WHERE ts>=? ORDER BY ts LIMIT ?", (since, int(limit))).fetchall()]

    def prune_decisions(self, before: str) -> int:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM outcomes WHERE decision_id IN (SELECT decision_id FROM decisions WHERE ts<?)",
                      (before,))
            return c.execute("DELETE FROM decisions WHERE ts<?", (before,)).rowcount

    # ── deliveries ──
    def log_delivery(self, report_id: str, channel: str, ts: str, ok: bool, rec: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO deliveries(report_id,channel,ts,ok,rec) VALUES(?,?,?,?,?)",
                      (report_id, channel, ts, 1 if ok else 0, json.dumps(rec)[:4000]))

    def deliveries(self, *, report_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        q, a = "SELECT report_id, channel, ts, ok, rec FROM deliveries WHERE 1=1", []
        if report_id:
            q += " AND report_id=?"; a.append(report_id)
        q += " ORDER BY id DESC LIMIT ?"; a.append(int(limit))
        with self._conn() as c:
            return [{"report_id": r[0], "channel": r[1], "ts": r[2], "ok": bool(r[3]), "detail": json.loads(r[4] or "{}")}
                    for r in c.execute(q, a).fetchall()]

    def successful_delivery_channels(self, report_ids: Iterable[str]) -> Dict[str, List[str]]:
        """Return bounded channel evidence for the selected reports only.

        Grouping in SQLite avoids loading an unbounded delivery history merely
        to learn whether Fabric or Memory ever accepted a report.
        """
        ids = sorted({str(item or "") for item in report_ids if str(item or "")})
        out: Dict[str, set[str]] = {}
        with self._conn() as c:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                if not chunk:
                    continue
                rows = c.execute(
                    "SELECT report_id, channel FROM deliveries WHERE ok=1 "
                    "AND channel IN ('fabric','memory') AND report_id IN (%s) "
                    "GROUP BY report_id, channel" % ",".join("?" * len(chunk)),
                    chunk,
                ).fetchall()
                for report_id, channel in rows:
                    out.setdefault(str(report_id), set()).add(str(channel))
        return {report_id: sorted(channels) for report_id, channels in out.items()}
