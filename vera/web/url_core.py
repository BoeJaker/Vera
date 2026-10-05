"""URLs an agent typed, and pages that are only navigation. Pure.

From the 4 Oct 2026 census (strategic-vector-db-report, broad research-report):

* `https://weav iate.io/docs` - a space inside the host. The direct fetch failed, the reader
  proxy answered HTTP 422 with a JSON error, and web.fetch returned that error as the page,
  ok:true. `normalize_url` removes whitespace from the host and encodes it in the path.
* `/valkey.io/blog/2024-10-03-valkey-launch.html` - a URL without its scheme. The loop's
  path guard read it as an invented absolute FILE path and refused the call three times.
  `normalize_url` gives a scheme-less host its https://.
* qdrant.tech/documentation came back as "Develop Deploy Ecosystem Learn API Reference ..."
  - the menu of a page rendered by JavaScript - and counted as fetched documentation.
  `looks_nav_only` says when a page's text is only navigation.
"""

from __future__ import annotations

import re

#: Extensions that end a FILE name, never a host - "/report.md" stays a path.
_FILE_EXTS = frozenset({
    "md", "txt", "py", "js", "ts", "json", "csv", "tsv", "html", "htm", "css", "sh", "yaml",
    "yml", "toml", "xml", "pdf", "png", "jpg", "jpeg", "gif", "svg", "log", "ini", "cfg",
    "sql", "db", "zip", "gz", "tar", "rs", "go", "java", "rb", "php", "c", "h", "cpp", "ipynb",
})
_HOST = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)(?::\d{1,5})?$")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", re.S)


def _hostlike(h: str) -> bool:
    m = _HOST.match(h or "")
    if not m:
        return False
    tld = m.group(1).rsplit(".", 1)[-1].lower()
    return tld.isalpha() and 2 <= len(tld) <= 24 and tld not in _FILE_EXTS


def _split_host(rest: str):
    """(host[:port], the rest from the first / ? or #)."""
    m = re.search(r"[/?#]", rest)
    return (rest, "") if not m else (rest[:m.start()], rest[m.start():])


def normalize_url(url: str) -> str:
    """A URL an agent typed, made fetchable; anything else is returned unchanged
    (stripped). Never raises."""
    s = str(url or "").strip().strip("<>\"'`").strip()
    if not s:
        return s
    m = _SCHEME.match(s)
    if m:
        scheme, rest = m.group(1), m.group(2)
        host, tail = _split_host(rest)
        return "%s://%s%s" % (scheme, re.sub(r"\s+", "", host), re.sub(r"\s", "%20", tail.strip()))
    if s.startswith("//"):
        host, sep, path = s[2:].partition("/")
        host = re.sub(r"\s+", "", host)
        if _hostlike(host):
            return "https://%s%s%s" % (host, sep, re.sub(r"\s", "%20", path))
        return s
    # scheme-less: "valkey.io/blog/x" or "/valkey.io/blog/x" (the leading-slash form only
    # with a path after the host, so a lone "/notes.md"-like name is never a URL)
    lead = s.startswith("/")
    body = s.lstrip("/")
    host, sep, path = body.partition("/")
    host = re.sub(r"\s+", "", host)
    if _hostlike(host) and (sep or not lead):
        return "https://%s%s%s" % (host, sep, re.sub(r"\s", "%20", path))
    return s


def looks_nav_only(text: str) -> bool:
    """True when a page's text is (almost) only menu items - short lines, hardly a sentence:
    what a JavaScript-rendered page gives a plain fetch."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    prose_words = sum(len(ln.split()) for ln in lines if len(ln.split()) >= 8)
    short = sum(1 for ln in lines if len(ln.split()) <= 4)
    return prose_words < 60 and short >= 0.8 * len(lines)
