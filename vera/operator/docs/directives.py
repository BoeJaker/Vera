"""directives.py — <!-- VERA:CAPTURE … --> markers in documentation.

Authors drop a capture directive right where an image belongs:

    <!-- VERA:CAPTURE panel="markets-studio" name="backtest" gif="true"
         steps="click_text Run backtest; gif_start; wait 3000; gif_stop backtest" -->

The harness navigates to the panel, runs the (deterministic) steps, captures a
still or a GIF, and inserts/refreshes the image in a paired managed block right
after the directive — so re-running is idempotent and never disturbs authored
prose. If ``steps`` is omitted it captures the panel as-loaded (or a default
scroll GIF when ``gif="true"``).

Everything here is pure string work (parse + insert); execution lives in the
``docs.capture`` capability, which reuses the tour step runner.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_DIRECTIVE = re.compile(r"<!--\s*VERA:CAPTURE\b(.*?)-->", re.DOTALL)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"' r"|(\w+)\s*=\s*'([^']*)'"
                   r"|(\w+)\s*=\s*(\S+)|(\w+)")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on", "")


def parse_attrs(s: str) -> Dict[str, Any]:
    """Parse ``key="v" key2='v2' flag`` into a dict (bare flag → True)."""
    out: Dict[str, Any] = {}
    for m in _ATTR.finditer(s or ""):
        if m.group(1) is not None:
            out[m.group(1)] = m.group(2)
        elif m.group(3) is not None:
            out[m.group(3)] = m.group(4)
        elif m.group(5) is not None:
            out[m.group(5)] = m.group(6)
        elif m.group(7) is not None:
            out[m.group(7)] = True
    return out


def parse_directives(md: str) -> List[Dict[str, Any]]:
    """Return [{attrs, start, end, raw}] for every capture directive, in order."""
    res = []
    for m in _DIRECTIVE.finditer(md or ""):
        res.append({"attrs": parse_attrs(m.group(1)), "start": m.start(),
                    "end": m.end(), "raw": m.group(0)})
    return res


def image_markdown(name: str, rel: str, gif: bool = False) -> str:
    cap = name.replace("-", " ").replace("_", " ")
    tag = " · animated" if gif else ""
    return f"![{cap}]({rel})\n\n*{cap}{tag}*"


def _cap_block(name: str, body: str) -> str:
    return (f"<!-- VERA:CAPTURED {name} -->\n{body.rstrip()}\n"
            f"<!-- /VERA:CAPTURED {name} -->")


def upsert_capture(md: str, name: str, image_md: str,
                   after_pos: Optional[int] = None) -> str:
    """Insert or replace the CAPTURED block for ``name``. If one already exists
    anywhere it is replaced in place; otherwise it is inserted at ``after_pos``
    (the end of the directive), or appended."""
    block = _cap_block(name, image_md)
    pat = re.compile(re.escape(f"<!-- VERA:CAPTURED {name} -->") + r".*?"
                     + re.escape(f"<!-- /VERA:CAPTURED {name} -->"), re.DOTALL)
    if pat.search(md):
        return pat.sub(lambda _m: block, md)
    if after_pos is None:
        return md.rstrip() + "\n\n" + block + "\n"
    return md[:after_pos] + "\n\n" + block + "\n" + md[after_pos:]


def directive_steps(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a tour step list for a directive: navigate to the panel, run the
    author's steps (or a sensible default capture)."""
    from ..tours import parse_steps  # local import; tours is a sibling
    name = str(attrs.get("name") or "capture")
    gif = _truthy(attrs.get("gif")) if "gif" in attrs else False
    steps: List[Dict[str, Any]] = []
    if attrs.get("panel"):
        steps.append({"do": "goto", "panel": str(attrs["panel"])})
        steps.append({"do": "wait", "ms": int(attrs.get("settle_ms", 1300) or 1300)})
    elif attrs.get("url"):
        steps.append({"do": "goto", "url": str(attrs["url"])})
        steps.append({"do": "wait", "ms": int(attrs.get("settle_ms", 1300) or 1300)})

    body = parse_steps(str(attrs["steps"])) if attrs.get("steps") else []
    has_capture = any(s.get("do") in ("shot", "gif_stop") for s in body)
    if not has_capture:
        if gif:
            body += parse_steps(
                f"gif_start; scroll 450; wait 900; scroll 450; wait 900; gif_stop {name} 800")
        else:
            body.append({"do": "shot", "name": name})
    steps += body
    return steps
