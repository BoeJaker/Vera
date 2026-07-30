"""perception.py — hybrid observation (accessibility/DOM refs + screenshot).

Every observation returns BOTH:
  • a list of interactive **elements** with stable *refs* (``e1``, ``e2`` …),
    roles, accessible names and bounding boxes — so the thinker can act on
    ``ref="e12"`` reliably instead of guessing pixel coordinates; and
  • a **screenshot** path — so vision models (and opaque canvas / VM surfaces)
    still work.

The DOM scan runs in-page (``page.evaluate``) and tags each interactive element
with ``data-vera-ref`` so acts resolve a ref back to a locator deterministically
(``[data-vera-ref="e12"]``) regardless of DOM reshuffles between calls.

``build_observation`` is a pure function over the scan output → unit-testable
with no browser. ``observe_page`` wires it to a live Playwright page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("vera.operator.perception")

# Interactive things worth offering as click/type targets. Kept broad but
# bounded so the ref list stays small enough for an LLM context.
_SCAN_JS = r"""
(maxEls) => {
  const SEL = [
    'a[href]','button','input','select','textarea','summary','label',
    '[role=button]','[role=link]','[role=tab]','[role=menuitem]',
    '[role=checkbox]','[role=radio]','[role=switch]','[role=option]',
    '[onclick]','[contenteditable=""]','[contenteditable=true]','[tabindex]'
  ].join(',');
  const seen = new Set();
  const out = [];
  let i = 0;
  const nodes = document.querySelectorAll(SEL);
  for (const el of nodes) {
    if (out.length >= maxEls) break;
    if (seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = r.width > 1 && r.height > 1 &&
      style.visibility !== 'hidden' && style.display !== 'none' &&
      style.opacity !== '0' &&
      r.bottom > 0 && r.right > 0 &&
      r.top < (window.innerHeight + 400) && r.left < (window.innerWidth + 400);
    if (!visible) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let role = el.getAttribute('role') || '';
    if (!role) {
      if (tag === 'a') role = 'link';
      else if (tag === 'button') role = 'button';
      else if (tag === 'select') role = 'combobox';
      else if (tag === 'textarea') role = 'textbox';
      else if (tag === 'input') role = (['checkbox','radio','range','submit','button'].includes(type) ? type : 'textbox');
      else role = tag;
    }
    const name = (
      el.getAttribute('aria-label') ||
      (el.innerText || '').trim() ||
      el.getAttribute('placeholder') ||
      el.getAttribute('value') ||
      el.getAttribute('alt') ||
      el.getAttribute('title') ||
      el.getAttribute('name') ||
      ''
    ).replace(/\s+/g, ' ').trim().slice(0, 120);
    const ref = 'e' + (++i);
    el.setAttribute('data-vera-ref', ref);
    out.push({
      ref, role, name, tag, type,
      value: (el.value != null ? String(el.value).slice(0, 80) : ''),
      enabled: !(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]
    });
  }
  const text = (document.body ? document.body.innerText : '')
    .replace(/\s+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim().slice(0, 4000);
  return { url: location.href, title: document.title || '', text, elements: out };
}
"""


@dataclass
class Element:
    ref: str
    role: str = ""
    name: str = ""
    tag: str = ""
    type: str = ""
    value: str = ""
    enabled: bool = True
    bbox: List[int] = field(default_factory=lambda: [0, 0, 0, 0])

    def one_line(self) -> str:
        nm = f' "{self.name}"' if self.name else ""
        val = f" ={self.value!r}" if self.value else ""
        dis = "" if self.enabled else " (disabled)"
        return f'{self.ref}: {self.role}{nm}{val}{dis}'


@dataclass
class Observation:
    url: str = ""
    title: str = ""
    text: str = ""
    screenshot_path: str = ""
    elements: List[Element] = field(default_factory=list)

    def ref_map(self) -> Dict[str, Dict[str, Any]]:
        """ref → locator info that ``actions`` uses to resolve targets."""
        return {
            e.ref: {"selector": f'[data-vera-ref="{e.ref}"]', "role": e.role,
                    "name": e.name, "bbox": e.bbox, "tag": e.tag}
            for e in self.elements
        }

    def compact(self, max_elements: int = 60) -> str:
        """A token-lean rendering for the thinker prompt."""
        head = f"URL: {self.url}\nTITLE: {self.title}\n"
        els = self.elements[:max_elements]
        listing = "\n".join(e.one_line() for e in els)
        more = "" if len(self.elements) <= max_elements else \
            f"\n… (+{len(self.elements) - max_elements} more elements)"
        txt = self.text[:1200]
        return f"{head}\nINTERACTIVE ELEMENTS:\n{listing}{more}\n\nVISIBLE TEXT:\n{txt}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url, "title": self.title,
            "screenshot": self.screenshot_path,
            "element_count": len(self.elements),
            "elements": [e.__dict__ for e in self.elements],
            "text": self.text,
        }


def build_observation(scan: Dict[str, Any], screenshot_path: str = "") -> Observation:
    """Pure: turn a raw ``_SCAN_JS`` result into an :class:`Observation`."""
    scan = scan or {}
    els: List[Element] = []
    for raw in (scan.get("elements") or []):
        if not isinstance(raw, dict) or not raw.get("ref"):
            continue
        bbox = raw.get("bbox") or [0, 0, 0, 0]
        els.append(Element(
            ref=str(raw.get("ref")), role=str(raw.get("role") or ""),
            name=str(raw.get("name") or ""), tag=str(raw.get("tag") or ""),
            type=str(raw.get("type") or ""), value=str(raw.get("value") or ""),
            enabled=bool(raw.get("enabled", True)),
            bbox=[int(x) for x in (bbox + [0, 0, 0, 0])[:4]],
        ))
    return Observation(
        url=str(scan.get("url") or ""), title=str(scan.get("title") or ""),
        text=str(scan.get("text") or ""), screenshot_path=screenshot_path or "",
        elements=els,
    )


async def observe_page(page: Any, screenshot_path: str = "",
                       max_elements: int = 120,
                       full_page: bool = False,
                       settle_ms: int = 350) -> Observation:
    """Run the hybrid scan against a live Playwright ``page``.

    Screenshot first-or-after doesn't matter much; we scan the DOM then shoot so
    the ``data-vera-ref`` tags don't appear visually (they're attributes only).
    """
    try:
        await page.wait_for_timeout(settle_ms)
    except Exception:
        pass
    scan: Dict[str, Any] = {}
    try:
        scan = await page.evaluate(_SCAN_JS, max_elements)
    except Exception as e:
        log.warning("operator: DOM scan failed: %s", e)
        try:
            scan = {"url": page.url, "title": await page.title(), "elements": [], "text": ""}
        except Exception:
            scan = {"elements": []}
    if screenshot_path:
        try:
            await page.screenshot(path=screenshot_path, full_page=full_page, type="png")
        except Exception as e:
            log.warning("operator: screenshot failed: %s", e)
            screenshot_path = ""
    return build_observation(scan, screenshot_path)
