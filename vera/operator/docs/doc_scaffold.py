"""doc_scaffold.py — managed auto-blocks inside documentation markdown.

The operator regenerates only the regions between HTML-comment markers, leaving
authored prose untouched (the same idea scaffold.py uses for its managed
blocks):

    <!-- VERA:AUTO:screenshots START -->
    …regenerated screenshots…
    <!-- VERA:AUTO:screenshots END -->

Two blocks per doc: ``screenshots`` (the in-action gallery) and ``capabilities``
(a reference table built from the live registry). Everything here is pure string
manipulation → unit-testable.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _start(name: str) -> str:
    return f"<!-- VERA:AUTO:{name} START -->"


def _end(name: str) -> str:
    return f"<!-- VERA:AUTO:{name} END -->"


def upsert_block(text: str, name: str, content: str,
                 heading: str = "") -> str:
    """Insert or replace the ``name`` auto-block. Appends (with optional
    ``heading``) if the block is absent."""
    text = text or ""
    block = f"{_start(name)}\n{content.rstrip()}\n{_end(name)}"
    pattern = re.compile(
        re.escape(_start(name)) + r".*?" + re.escape(_end(name)), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text)
    prefix = text.rstrip()
    head = f"\n\n{heading}\n" if heading else "\n\n"
    return f"{prefix}{head}\n{block}\n"


def render_screenshots_block(shots: List[Dict[str, Any]]) -> str:
    """``shots`` = [{label, rel_path, caption, mode}]. Renders a gallery."""
    if not shots:
        return "_No screenshots captured yet — run `docs.build` (or " \
               "`operator.mission.run documentation`)._"
    lines: List[str] = []
    for s in shots:
        label = s.get("label") or s.get("panel_id") or "panel"
        rel = s.get("rel_path") or ""
        cap = s.get("caption") or label
        mode = s.get("mode") or "seeded"
        lines.append(f"#### {label}")
        lines.append("")
        lines.append(f"![{cap}]({rel})")
        lines.append("")
        lines.append(f"*{cap}  ·  captured `{mode}`*")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_caps_block(caps: List[Dict[str, Any]]) -> str:
    """``caps`` = [{name, method, path, description}] → a markdown table."""
    if not caps:
        return "_No capabilities resolved for this domain._"
    rows = ["| Capability | HTTP | Description |", "|---|---|---|"]
    for c in caps:
        name = c.get("name", "")
        method = c.get("method") or ""
        path = c.get("path") or ""
        http = f"`{method} {path}`" if path else "—"
        desc = (c.get("description") or "").replace("|", "\\|")
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 200:
            desc = desc[:197] + "…"
        rows.append(f"| `{name}` | {http} | {desc} |")
    return "\n".join(rows)


_SCAFFOLD = """# {number} · {title}

<!-- Authored prose lives outside the VERA:AUTO blocks and is preserved across
     regenerations. Fill in the overview, concepts and walkthrough here. -->

_Overview of the {title} domain. (Draft — author me.)_

## Screenshots

{screenshots}

## Capabilities

{capabilities}
"""


def build_doc(existing: Optional[str], *, number: str, title: str,
              screenshots_content: str, caps_content: str) -> str:
    """Return the updated markdown for a domain doc.

    • If ``existing`` is provided, only the two auto-blocks are refreshed.
    • Otherwise a fresh scaffold is created with placeholder prose.
    """
    if existing and existing.strip():
        out = upsert_block(existing, "screenshots", screenshots_content,
                           heading="## Screenshots")
        out = upsert_block(out, "capabilities", caps_content,
                           heading="## Capabilities")
        return out
    shots = f"{_start('screenshots')}\n{screenshots_content.rstrip()}\n{_end('screenshots')}"
    caps = f"{_start('capabilities')}\n{caps_content.rstrip()}\n{_end('capabilities')}"
    return _SCAFFOLD.format(number=number, title=title,
                            screenshots=shots, capabilities=caps)
