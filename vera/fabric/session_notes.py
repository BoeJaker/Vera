"""
session_notes.py  —  Per-scope agent memory files ("session notebooks")
=======================================================================
A small, structured markdown file the agent/LLM maintains for each working
context so it can steer itself across turns: remember goals, user
instructions, key facts/decisions, mistakes to avoid, and next steps.

One note per (scope, ref_id):

    scope       ref_id
    ─────       ──────
    chat        chat session_id
    agent       agent name
    project     dream project id
    notebook    notebook id
    workspace   IDE workspace / root path slug
    dream       dream id

The note is deliberately SMALL (hard char cap) and SECTIONED so a model can
edit it surgically with notes.section_set / notes.append instead of
re-writing the whole file. `notes.context` returns a system-prompt fragment
(the note + optional editing instructions) for injection via
context.assemble or the chat stream endpoint.

Persistence: SQLite (source of truth) + revision history + emit_event for
live UI refresh. A reusable web component <vera-session-notes> is served at
/ui/vera-notes.js so every panel (chat context area, agent editor, dream,
IDE, notebooks) can display and edit the same note.

Capabilities:
  notes.get          — read a note (+parsed sections)
  notes.set          — replace the whole note
  notes.section_set  — replace ONE section's body
  notes.append       — append a bullet line to a section
  notes.remove_line  — prune lines matching a substring
  notes.clear        — delete a note
  notes.list         — list notes (per scope or all)
  notes.context      — system-prompt fragment for injection
  notes.revisions    — recent revision history for a note
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from Vera.vera.capability_orchestration import (
    APP,
    capability,
    emit_event,
    now_iso,
)

log = logging.getLogger("vera.session_notes")

# ─────────────────────────────────────────────────────────────────────────────
# STORE
# ─────────────────────────────────────────────────────────────────────────────
_SQLITE_PATH = Path(__file__).parent / "vera_session_notes.db"

# Known scopes — unknown scopes are allowed (forward-compat) but normalised.
KNOWN_SCOPES = ("chat", "agent", "project", "notebook", "workspace", "dream")

# Hard cap — notes must stay succinct to be useful as injected context.
MAX_NOTE_CHARS = 6000
MAX_REVISIONS  = 20

# Canonical section skeleton. section_set/append create missing sections in
# this order so notes stay uniformly structured for both LLM and UI.
SECTIONS = [
    "Goals",
    "User Instructions & Preferences",
    "Key Facts & Decisions",
    "Mistakes To Avoid",
    "Next Steps",
]

_TEMPLATE = "# Session Memory\n" + "".join(f"\n## {s}\n" for s in SECTIONS)


def _sqlite_init():
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=10, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_notes (
            scope       TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            content     TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'user',
            PRIMARY KEY (scope, ref_id)
        );
        CREATE TABLE IF NOT EXISTS session_note_revisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            content     TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'user'
        );
        CREATE INDEX IF NOT EXISTS idx_note_rev ON session_note_revisions(scope, ref_id, id);
    """)
    conn.commit()
    return conn


try:
    _c = _sqlite_init(); _c.close()
    log.info("session_notes: SQLite ready at %s", _SQLITE_PATH)
except Exception as e:
    log.warning("session_notes: SQLite init failed: %s", e)


def _conn():
    return sqlite3.connect(str(_SQLITE_PATH), timeout=5, check_same_thread=False)


def _norm(scope: str, ref_id: str) -> tuple:
    return (scope or "chat").strip().lower()[:32], (ref_id or "").strip()[:200]


def _db_get(scope: str, ref_id: str) -> Optional[dict]:
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT content, updated_at, updated_by FROM session_notes "
            "WHERE scope=? AND ref_id=?", (scope, ref_id)).fetchone()
        if not r:
            return None
        return {"scope": scope, "ref_id": ref_id, "content": r[0],
                "updated_at": r[1], "updated_by": r[2]}
    finally:
        conn.close()


def _db_save(scope: str, ref_id: str, content: str, updated_by: str) -> dict:
    content = (content or "").strip()[:MAX_NOTE_CHARS]
    now = now_iso()
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_notes (scope, ref_id, content, updated_at, updated_by) "
            "VALUES (?,?,?,?,?)", (scope, ref_id, content, now, updated_by))
        conn.execute(
            "INSERT INTO session_note_revisions (scope, ref_id, content, updated_at, updated_by) "
            "VALUES (?,?,?,?,?)", (scope, ref_id, content, now, updated_by))
        # Trim revision history
        conn.execute(
            "DELETE FROM session_note_revisions WHERE scope=? AND ref_id=? AND id NOT IN "
            "(SELECT id FROM session_note_revisions WHERE scope=? AND ref_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (scope, ref_id, scope, ref_id, MAX_REVISIONS))
        conn.commit()
    finally:
        conn.close()
    return {"scope": scope, "ref_id": ref_id, "content": content,
            "updated_at": now, "updated_by": updated_by}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION PARSING / EDITING
# ─────────────────────────────────────────────────────────────────────────────
_SEC_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _parse_sections(content: str) -> Dict[str, str]:
    """Split a note into {section_title: body}. Text before the first ## goes
    under the '' key (usually just the '# Session Memory' heading)."""
    out: Dict[str, str] = {}
    if not content:
        return out
    matches = list(_SEC_RE.finditer(content))
    if not matches:
        out[""] = content
        return out
    if matches[0].start() > 0:
        out[""] = content[:matches[0].start()].strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        out[m.group(1)] = content[m.end():end].strip()
    return out


def _canonical_section(name: str) -> str:
    """Fuzzy-match a section name against the skeleton (case/punct tolerant)."""
    key = re.sub(r"[^a-z]", "", (name or "").lower())
    for s in SECTIONS:
        if re.sub(r"[^a-z]", "", s.lower()) == key:
            return s
    # partial match ("mistakes", "next", "goals", "facts", "instructions")
    for s in SECTIONS:
        if key and key in re.sub(r"[^a-z]", "", s.lower()):
            return s
    return (name or "Notes").strip()[:60]


def _rebuild(sections: Dict[str, str]) -> str:
    """Reassemble a note from a sections dict, keeping skeleton order first."""
    parts = ["# Session Memory"]
    done = set()
    for s in SECTIONS:
        if s in sections:
            body = sections[s].strip()
            parts.append(f"\n## {s}" + (f"\n{body}" if body else ""))
            done.add(s)
    for s, body in sections.items():
        if s in done or s == "":
            continue
        body = (body or "").strip()
        parts.append(f"\n## {s}" + (f"\n{body}" if body else ""))
    return "\n".join(parts).strip()[:MAX_NOTE_CHARS]


def _empty_note() -> str:
    return _TEMPLATE.strip()


async def _emit_updated(scope: str, ref_id: str, updated_by: str):
    try:
        await emit_event({"type": "notes.updated", "scope": scope,
                          "ref_id": ref_id, "updated_by": updated_by})
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC HELPERS (used by context.assemble / chat stream — not caps)
# ─────────────────────────────────────────────────────────────────────────────

def get_note_content(scope: str, ref_id: str) -> str:
    """Raw note content or '' — synchronous, safe for hot paths."""
    scope, ref_id = _norm(scope, ref_id)
    if not ref_id:
        return ""
    try:
        rec = _db_get(scope, ref_id)
        return (rec or {}).get("content", "") or ""
    except Exception as e:
        log.debug("get_note_content: %s", e)
        return ""


_EDIT_INSTRUCTIONS = (
    "You may UPDATE this memory when you learn something durable (a goal, a "
    "user preference/instruction, a decision, a mistake you should not repeat, "
    "or the next step). Keep it SUCCINCT — short bullet lines, no prose, no "
    "duplicates; prune anything stale. Edit with the notes.* capabilities:\n"
    "  notes.append(scope=\"{scope}\", ref_id=\"{ref_id}\", section=\"Key Facts & Decisions\", text=\"…\")\n"
    "  notes.section_set(scope=\"{scope}\", ref_id=\"{ref_id}\", section=\"Next Steps\", content=\"- …\")\n"
    "  notes.remove_line(scope=\"{scope}\", ref_id=\"{ref_id}\", match=\"<substring>\")"
)


def build_notes_context(pairs: List[tuple], include_instructions: bool = True) -> str:
    """Build the injectable fragment for one or more (scope, ref_id) notes.
    Empty/missing notes are skipped. Returns '' when nothing exists."""
    blocks = []
    first_pair = None
    for scope, ref_id in pairs:
        scope, ref_id = _norm(scope, ref_id)
        content = get_note_content(scope, ref_id)
        # A pristine template with no user content is not worth injecting.
        if not content or content == _empty_note():
            continue
        if first_pair is None:
            first_pair = (scope, ref_id)
        label = f"{scope}:{ref_id}" if scope != "chat" else "this chat session"
        blocks.append(f"### Memory ({label})\n{content}")
    if not blocks:
        return ""
    frag = "## Session memory (agent-maintained — trust and follow it)\n" + "\n\n".join(blocks)
    if include_instructions and first_pair:
        frag += "\n\n" + _EDIT_INSTRUCTIONS.format(scope=first_pair[0], ref_id=first_pair[1])
    return frag


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────

@capability(
    "notes.get", memory="off", silent=True,
    http_method="GET", http_path="/notes/get", http_tags=["notes"],
    description=(
        "Read the session memory note for a scope. Inputs: scope "
        "(chat|agent|project|notebook|workspace|dream), ref_id (session/agent/"
        "project id). Output: {content, sections, exists, updated_at}."
    ),
)
async def notes_get(scope: str, ref_id: str, trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    if not ref_id:
        return {"error": "ref_id required"}
    rec = _db_get(scope, ref_id)
    if not rec:
        return {"scope": scope, "ref_id": ref_id, "exists": False,
                "content": "", "sections": {}, "template": _empty_note()}
    return {**rec, "exists": True, "sections": _parse_sections(rec["content"])}


@capability(
    "notes.set", memory="off",
    http_method="POST", http_path="/notes/set", http_tags=["notes"],
    description=(
        "Replace the ENTIRE session memory note for a scope. Prefer "
        "notes.append / notes.section_set for small edits. Inputs: scope, "
        "ref_id, content (markdown, ## sections), updated_by (agent|user)."
    ),
)
async def notes_set(scope: str, ref_id: str, content: str = "",
                    updated_by: str = "agent", trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    if not ref_id:
        return {"error": "ref_id required"}
    rec = _db_save(scope, ref_id, content, updated_by or "agent")
    await _emit_updated(scope, ref_id, updated_by)
    return {**rec, "chars": len(rec["content"]), "max_chars": MAX_NOTE_CHARS}


@capability(
    "notes.section_set", memory="off",
    http_method="POST", http_path="/notes/section_set", http_tags=["notes"],
    description=(
        "Replace ONE section of the session memory note (creates the note/"
        "section if missing). Inputs: scope, ref_id, section (e.g. 'Goals', "
        "'Next Steps', 'Mistakes To Avoid'), content (the new section body)."
    ),
)
async def notes_section_set(scope: str, ref_id: str, section: str,
                            content: str = "", updated_by: str = "agent",
                            trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    if not ref_id:
        return {"error": "ref_id required"}
    existing = _db_get(scope, ref_id)
    secs = _parse_sections(existing["content"] if existing else _empty_note())
    sec = _canonical_section(section)
    secs[sec] = (content or "").strip()
    rec = _db_save(scope, ref_id, _rebuild(secs), updated_by or "agent")
    await _emit_updated(scope, ref_id, updated_by)
    return {"scope": scope, "ref_id": ref_id, "section": sec,
            "chars": len(rec["content"]), "saved": True}


@capability(
    "notes.append", memory="off",
    http_method="POST", http_path="/notes/append", http_tags=["notes"],
    description=(
        "Append ONE short bullet line to a section of the session memory note "
        "(creates note/section if missing; skips exact duplicates). Inputs: "
        "scope, ref_id, section, text (one succinct line, no leading dash needed)."
    ),
)
async def notes_append(scope: str, ref_id: str, section: str, text: str,
                       updated_by: str = "agent", trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    if not ref_id:
        return {"error": "ref_id required"}
    line = (text or "").strip().lstrip("-•* ").strip()
    if not line:
        return {"error": "text required"}
    line = "- " + line[:300]
    existing = _db_get(scope, ref_id)
    secs = _parse_sections(existing["content"] if existing else _empty_note())
    sec = _canonical_section(section)
    body = secs.get(sec, "")
    if line.lower() in [l.strip().lower() for l in body.splitlines()]:
        return {"scope": scope, "ref_id": ref_id, "section": sec,
                "saved": False, "duplicate": True}
    secs[sec] = (body + "\n" + line).strip()
    rec = _db_save(scope, ref_id, _rebuild(secs), updated_by or "agent")
    await _emit_updated(scope, ref_id, updated_by)
    return {"scope": scope, "ref_id": ref_id, "section": sec,
            "chars": len(rec["content"]), "saved": True}


@capability(
    "notes.remove_line", memory="off",
    http_method="POST", http_path="/notes/remove_line", http_tags=["notes"],
    description=(
        "Prune the session memory note: remove all lines containing a "
        "substring (case-insensitive). Inputs: scope, ref_id, match, "
        "section (optional — restrict to one section)."
    ),
)
async def notes_remove_line(scope: str, ref_id: str, match: str,
                            section: str = "", updated_by: str = "agent",
                            trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    existing = _db_get(scope, ref_id)
    if not existing:
        return {"error": "note not found"}
    needle = (match or "").strip().lower()
    if not needle:
        return {"error": "match required"}
    secs = _parse_sections(existing["content"])
    target = _canonical_section(section) if section else None
    removed = 0
    for name, body in list(secs.items()):
        if target and name != target:
            continue
        kept = []
        for l in body.splitlines():
            if needle in l.lower() and l.strip().startswith(("-", "•", "*")):
                removed += 1
            else:
                kept.append(l)
        secs[name] = "\n".join(kept).strip()
    if removed:
        _db_save(scope, ref_id, _rebuild(secs), updated_by or "agent")
        await _emit_updated(scope, ref_id, updated_by)
    return {"scope": scope, "ref_id": ref_id, "removed": removed}


@capability(
    "notes.clear", memory="off",
    http_method="POST", http_path="/notes/clear", http_tags=["notes"],
    description="Delete the session memory note for a scope. Inputs: scope, ref_id.",
)
async def notes_clear(scope: str, ref_id: str, trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM session_notes WHERE scope=? AND ref_id=?",
                           (scope, ref_id))
        conn.commit()
        deleted = cur.rowcount > 0
    finally:
        conn.close()
    await _emit_updated(scope, ref_id, "user")
    return {"deleted": deleted}


@capability(
    "notes.list", memory="off", silent=True,
    http_method="GET", http_path="/notes/list", http_tags=["notes"],
    description="List session memory notes. Inputs: scope (optional filter).",
)
async def notes_list(scope: str = "", trace_id=None):
    conn = _conn()
    try:
        if scope:
            rows = conn.execute(
                "SELECT scope, ref_id, updated_at, updated_by, length(content) "
                "FROM session_notes WHERE scope=? ORDER BY updated_at DESC",
                (scope.strip().lower(),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT scope, ref_id, updated_at, updated_by, length(content) "
                "FROM session_notes ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()
    return {"notes": [{"scope": r[0], "ref_id": r[1], "updated_at": r[2],
                       "updated_by": r[3], "chars": r[4]} for r in rows],
            "count": len(rows)}


@capability(
    "notes.revisions", memory="off", silent=True,
    http_method="GET", http_path="/notes/revisions", http_tags=["notes"],
    description="Recent revision history for a note. Inputs: scope, ref_id, limit.",
)
async def notes_revisions(scope: str, ref_id: str, limit: int = 10, trace_id=None):
    scope, ref_id = _norm(scope, ref_id)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, content, updated_at, updated_by FROM session_note_revisions "
            "WHERE scope=? AND ref_id=? ORDER BY id DESC LIMIT ?",
            (scope, ref_id, max(1, min(int(limit or 10), MAX_REVISIONS)))).fetchall()
    finally:
        conn.close()
    return {"revisions": [{"id": r[0], "content": r[1], "updated_at": r[2],
                           "updated_by": r[3]} for r in rows]}


@capability(
    "notes.context", memory="off", silent=True,
    http_method="POST", http_path="/notes/context", http_tags=["notes"],
    description=(
        "Build a system-prompt fragment from one or more session memory notes. "
        "Inputs: scope, ref_id (primary note), extra (optional JSON list of "
        "[scope, ref_id] pairs, e.g. the agent-level note), "
        "include_instructions (bool default true — tell the model HOW to edit). "
        "Output: {fragment} — empty when no note content exists."
    ),
)
async def notes_context(scope: str, ref_id: str, extra: str = "",
                        include_instructions: bool = True, trace_id=None):
    pairs = [(scope, ref_id)]
    if extra:
        try:
            more = json.loads(extra) if isinstance(extra, str) else extra
            for p in (more or []):
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    pairs.append((p[0], p[1]))
        except Exception:
            pass
    frag = build_notes_context(pairs, include_instructions=bool(include_instructions))
    return {"fragment": frag, "length": len(frag)}


# ─────────────────────────────────────────────────────────────────────────────
# REUSABLE UI ELEMENT — <vera-session-notes scope="chat" ref-id="...">
# ─────────────────────────────────────────────────────────────────────────────
_NOTES_ELEMENT_JS = r"""
/* <vera-session-notes> — shared session-memory viewer/editor.
   Attributes: scope, ref-id, title (optional), compact (optional).
   Re-fetches when ref-id changes; Save persists via /notes/set. */
(function(){
if(customElements.get('vera-session-notes')) return;
class VeraSessionNotes extends HTMLElement{
  static get observedAttributes(){return ['scope','ref-id'];}
  constructor(){super();this._editing=false;this._note=null;}
  connectedCallback(){this._render();this.refresh();}
  attributeChangedCallback(n,o,v){if(o!==v&&this.isConnected)this.refresh();}
  get _base(){return this.getAttribute('base')||window.__VERA_BASE__||window.location.origin;}
  get _scope(){return this.getAttribute('scope')||'chat';}
  get _ref(){return this.getAttribute('ref-id')||'';}
  async refresh(){
    if(!this._ref){this._note=null;this._paint();return;}
    try{
      const r=await fetch(this._base+'/notes/get?scope='+encodeURIComponent(this._scope)+'&ref_id='+encodeURIComponent(this._ref));
      this._note=r.ok?await r.json():null;
    }catch(e){this._note=null;}
    this._paint();
  }
  async save(){
    const ta=this.querySelector('textarea');if(!ta)return;
    try{
      await fetch(this._base+'/notes/set',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({scope:this._scope,ref_id:this._ref,content:ta.value,updated_by:'user'})});
      this._editing=false;this.refresh();
      this.dispatchEvent(new CustomEvent('notes-saved',{bubbles:true,detail:{scope:this._scope,ref_id:this._ref}}));
    }catch(e){}
  }
  async clearNote(){
    if(!confirm('Clear this memory note?'))return;
    try{await fetch(this._base+'/notes/clear',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scope:this._scope,ref_id:this._ref})});}catch(e){}
    this._editing=false;this.refresh();
  }
  _esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  _md(s){
    return this._esc(s)
      .replace(/^# (.+)$/gm,'')
      .replace(/^## (.+)$/gm,'<div class="vsn-sec">$1</div>')
      .replace(/^- (.+)$/gm,'<div class="vsn-li">$1</div>')
      .replace(/\n{2,}/g,'\n').replace(/\n/g,'');
  }
  _render(){
    this.innerHTML=`<style>
      vera-session-notes{display:block;font-family:var(--mono,monospace);font-size:10px}
      vera-session-notes .vsn-head{display:flex;align-items:center;gap:4px;margin-bottom:3px}
      vera-session-notes .vsn-title{font-weight:600;font-size:9.5px;color:var(--acc,#5a9e8f);flex:1;letter-spacing:.4px;text-transform:uppercase}
      vera-session-notes .vsn-meta{font-size:8px;color:var(--dim2,#888)}
      vera-session-notes .vsn-btn{background:none;border:1px solid var(--border,#444);border-radius:3px;color:var(--dim2,#aaa);cursor:pointer;font-size:8.5px;padding:1px 6px}
      vera-session-notes .vsn-btn:hover{color:var(--text,#eee);border-color:var(--acc,#5a9e8f)}
      vera-session-notes .vsn-body{background:var(--bg0,#111);border:1px solid var(--border,#333);border-radius:3px;padding:5px 7px;max-height:220px;overflow-y:auto;line-height:1.55;color:var(--text,#ddd)}
      vera-session-notes .vsn-sec{color:var(--acc2,#8fb87a);font-weight:600;margin-top:4px;font-size:9px;text-transform:uppercase;letter-spacing:.3px}
      vera-session-notes .vsn-li{padding-left:8px;position:relative}
      vera-session-notes .vsn-li:before{content:'·';position:absolute;left:1px;color:var(--dim,#777)}
      vera-session-notes textarea{width:100%;min-height:140px;background:var(--bg0,#111);border:1px solid var(--border,#333);border-radius:3px;color:var(--text,#ddd);font-family:inherit;font-size:9.5px;padding:5px;box-sizing:border-box;resize:vertical}
      vera-session-notes .vsn-empty{color:var(--dim,#777);font-style:italic;padding:4px 2px}
    </style>
    <div class="vsn-head">
      <span class="vsn-title"></span><span class="vsn-meta"></span>
      <button class="vsn-btn" data-a="edit" title="Edit">✎</button>
      <button class="vsn-btn" data-a="refresh" title="Refresh">↻</button>
      <button class="vsn-btn" data-a="clear" title="Clear note">✕</button>
    </div>
    <div class="vsn-content"></div>`;
    this.addEventListener('click',ev=>{
      const b=ev.target.closest('.vsn-btn');if(!b)return;
      const a=b.dataset.a;
      if(a==='refresh')this.refresh();
      else if(a==='clear')this.clearNote();
      else if(a==='edit'){this._editing=!this._editing;this._paint();}
      else if(a==='save')this.save();
      else if(a==='cancel'){this._editing=false;this._paint();}
    });
  }
  _paint(){
    const t=this.querySelector('.vsn-title'),m=this.querySelector('.vsn-meta'),c=this.querySelector('.vsn-content');
    if(!c)return;
    t.textContent=this.getAttribute('title')||('Memory — '+this._scope);
    const n=this._note;
    m.textContent=n?.exists?((n.updated_by||'')+' · '+String(n.updated_at||'').slice(0,16).replace('T',' ')):'';
    if(this._editing){
      const cur=(n?.exists&&n.content)?n.content:(n?.template||'# Session Memory');
      c.innerHTML='<textarea></textarea><div style="display:flex;gap:4px;margin-top:3px;justify-content:flex-end">'
        +'<button class="vsn-btn" data-a="cancel">Cancel</button>'
        +'<button class="vsn-btn" data-a="save" style="color:var(--ok,#8fb87a);border-color:var(--ok,#8fb87a)">Save</button></div>';
      c.querySelector('textarea').value=cur;
      return;
    }
    if(!this._ref){c.innerHTML='<div class="vsn-empty">no session</div>';return;}
    if(!n||!n.exists||!n.content){c.innerHTML='<div class="vsn-empty">No memory yet — the agent (or you, via ✎) can add goals, facts, mistakes to avoid…</div>';return;}
    c.innerHTML='<div class="vsn-body">'+this._md(n.content)+'</div>';
  }
}
customElements.define('vera-session-notes',VeraSessionNotes);
})();
"""

from fastapi.responses import Response


@APP.get("/ui/vera-notes.js", include_in_schema=False)
async def _serve_vera_notes_js():
    return Response(content=_NOTES_ELEMENT_JS, media_type="application/javascript")


log.info("session_notes: registered (notes.* caps + /ui/vera-notes.js)")
