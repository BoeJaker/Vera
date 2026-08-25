"""Deterministic adapters from canonical revisions to legacy read stores."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


def apply_sqlite_projection(path: str, envelope: dict[str, Any], *,
                            now: Callable[[], str]) -> dict[str, str]:
    """Upsert/remove one record while keeping legacy dataset counts exact."""
    record_id = envelope["record_id"]
    dataset_id = envelope["namespace"]
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT dataset_id FROM fabric_records WHERE id=?", (record_id,)).fetchone()
        if envelope["tombstone"]:
            conn.execute("DELETE FROM fabric_records WHERE id=?", (record_id,))
            if existing:
                conn.execute(
                    "UPDATE fabric_datasets SET record_count=MAX(record_count-1,0),"
                    "updated_at=? WHERE dataset_id=?", (now(), existing["dataset_id"]))
            state = "removed"
        else:
            inline = envelope["content"]["inline"]
            text = inline if isinstance(inline, str) else json.dumps(
                inline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            source = envelope.get("source") or {}
            metadata = envelope.get("metadata") or {}
            tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
            conn.execute(
                "INSERT INTO fabric_records "
                "(id,dataset_id,text,data,source_id,tags,created_at,synced_pg) "
                "VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET "
                "dataset_id=excluded.dataset_id,text=excluded.text,data=excluded.data,"
                "source_id=excluded.source_id,tags=excluded.tags,"
                "created_at=excluded.created_at,synced_pg=0",
                (record_id, dataset_id, text,
                 json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                 str(source.get("id") or source.get("url") or ""),
                 json.dumps(tags), envelope["created_at"]))
            conn.execute(
                "INSERT OR IGNORE INTO fabric_datasets "
                "(dataset_id,record_count,created_at,updated_at) VALUES (?,0,?,?)",
                (dataset_id, now(), now()))
            if not existing:
                conn.execute(
                    "UPDATE fabric_datasets SET record_count=record_count+1,updated_at=? "
                    "WHERE dataset_id=?", (now(), dataset_id))
            elif existing["dataset_id"] != dataset_id:
                conn.execute(
                    "UPDATE fabric_datasets SET record_count=MAX(record_count-1,0),"
                    "updated_at=? WHERE dataset_id=?", (now(), existing["dataset_id"]))
                conn.execute(
                    "UPDATE fabric_datasets SET record_count=record_count+1,updated_at=? "
                    "WHERE dataset_id=?", (now(), dataset_id))
            state = "applied"
        conn.commit()
        return {"projection": "sqlite", "state": state,
                "projection_revision": f"sqlite:{envelope['revision_id']}"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
