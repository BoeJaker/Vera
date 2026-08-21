"""gallery.py — the generated visual index (documentation/GALLERY.md).

Renders a cover-image grid: one card per domain linking to its doc, plus a
capability-count summary. Pure string builder over the manifest the
documentation mission produces.
"""

from __future__ import annotations

from typing import Any, Dict, List

OUTPUT_FILE = "GALLERY.md"

_HEADER = """# Vera — Visual gallery

Auto-generated gallery. Each card links to the authored write-up for that domain;
screenshots are captured by the **operator** (`operator.mission.run
documentation`) against a loop-lab sandbox and refreshed in place.

> Regenerate: `python tools/vera-docgen/docgen.py run`  ·  or the capability
> `docs.build`. Only the images and reference tables are regenerated — authored
> prose is preserved.
"""


def build_gallery(entries: List[Dict[str, Any]], *, generated_at: str = "",
                  total_caps: int = 0) -> str:
    """``entries`` = [{slug, title, doc, cover_rel, shot_count, cap_count}]
    in document order."""
    parts: List[str] = [_HEADER]
    if generated_at:
        parts.append(f"*Last generated: {generated_at}*")
    shot_total = sum(int(e.get("shot_count") or 0) for e in entries)
    parts.append(
        f"**{len(entries)} domains · {shot_total} screenshots · "
        f"{total_caps} capabilities**\n")

    parts.append("| Domain | Preview | Capabilities |")
    parts.append("|---|---|---|")
    for e in entries:
        title = e.get("title", e.get("slug", ""))
        doc = e.get("doc", "")
        link = f"[**{title}**]({doc})"
        cover = e.get("cover_rel") or ""
        img = f'<img src="{cover}" width="280">' if cover else "_(no screenshot)_"
        preview = f"[{img}]({doc})" if cover else img
        caps = e.get("cap_count") or 0
        parts.append(f"| {link} | {preview} | {caps} |")
    parts.append("")
    return "\n".join(parts)
