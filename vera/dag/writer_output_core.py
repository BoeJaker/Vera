"""A writer reply that is its own planning notes is not the document.

Probe 3 Oct (7c5bf52c, strategic-vector-db-report): prose.author saved version 1 of
report.md as 5,212 bytes of the model's planning - "*   **Content:** A structured
multi-section Markdown report ...", "The prompt says ...", "I must treat this as ...",
"3. Drafting Plan", ending mid-sentence at "Start with:" - the reasoning ran out of tokens
before any document. The verifier rightly called it "only a high-level outline", and the
run spent the rest of its 30 minutes re-fetching and rewriting. Pure.
"""

from __future__ import annotations

import re

_MARKERS = re.compile(
    r"\bthe (?:prompt|task|user|request) (?:says|asks|wants|requires|is asking)\b"
    r"|\bI (?:must|will|need to|should|'ll)\b|\bI'll\b|\blet me\b"
    r"|\bdrafting plan\b|\banaly[sz]e the (?:request|goal|task|prompt)\b|\bthinking process\b"
    r"|\bkey aspects to cover\b|\*\*\s*(?:constraint|formatting|grounding|content)s?\s*:?\s*\*\*",
    re.IGNORECASE)


def looks_like_planning(text: str) -> bool:
    """True when `text` reads as the writer planning the document rather than the document:
    at least three planning markers, and no markdown title among its first lines."""
    t = text or ""
    if not t.strip():
        return False
    head = [ln for ln in t.splitlines() if ln.strip()][:5]
    if any(re.match(r"^\s*#{1,3}\s+\S", ln) for ln in head):
        return False
    return len(_MARKERS.findall(t)) >= 3


RETRY_NOTE = ("\n\nYOUR PREVIOUS REPLY WAS YOUR PLANNING NOTES, NOT THE DOCUMENT - it was not "
              "saved. Do not plan, analyse or restate the task. Output ONLY the finished "
              "document, in one fenced block, starting with its title.")
