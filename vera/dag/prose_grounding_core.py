"""Which install/build commands in a written document need a manifest that is not there. Pure.

The check exists for a README that describes the run's own project: on 1 Aug 2026 one told
the reader to `npm install` a Node backend that was never built. It was applied to EVERY
document, and on 4 Oct 2026 it failed a research report on vector databases for saying
`pip install` - describing third-party software, not this project (strategic-vector-db-
report, which then ran out of time).

Two narrowings:
  * only a PROJECT document is checked: README / INSTALL / SETUP / CONTRIBUTING /
    GETTING_STARTED / USAGE files, or a task that asks for setup / install / run / build
    instructions;
  * installing a NAMED package (`pip install qdrant-client`, `npm install express`,
    `go get x`) never needs a manifest. Installing or building the project itself does:
    a bare `npm install`, `npm ci`, `npm run x`, `pip install -r requirements.txt`,
    `pip install .`, `poetry install`, `cargo build`, `go build`, `mvn package`...
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence

MANIFESTS = {
    "npm": "package.json", "yarn": "package.json", "pnpm": "package.json",
    "pip": "requirements.txt", "pip3": "requirements.txt",
    "poetry": "pyproject.toml", "cargo": "Cargo.toml",
    "bundle": "Gemfile", "composer": "composer.json",
    "mvn": "pom.xml", "gradle": "build.gradle", "go": "go.mod",
}
#: Alternatives that satisfy the same ecosystem.
ALSO_OK = {
    "pip": ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"),
    "pip3": ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"),
    "gradle": ("build.gradle", "build.gradle.kts"),
}
CMD_RE = re.compile(
    r"`{0,3}\s*(npm|yarn|pnpm|pip3?|poetry|cargo|bundle|composer|mvn|gradle|go)\s+"
    r"(install|ci|build|run|get|add|require|package|test)\b([^`\n]*)", re.I)
#: Verbs whose argument is a package to fetch from a registry.
_NAMED_VERBS = {"install", "add", "get", "require"}
_PROJECT_DOC_NAME = re.compile(r"^(readme|install|installation|setup|contributing|getting[_-]?started|usage)\b",
                               re.I)
_PROJECT_DOC_TASK = re.compile(
    r"\b(readme|setup (guide|instructions)|install(ation)? (guide|instructions|steps)|"
    r"how to (install|run|build|set up|setup)|build instructions|run instructions|getting started)\b",
    re.I)


def is_project_doc(path: str, task: str = "") -> bool:
    """Does this document describe the run's OWN project (so its install commands must match
    the files really there)?"""
    base = os.path.basename(str(path or "")).strip()
    return bool(_PROJECT_DOC_NAME.match(base) or _PROJECT_DOC_TASK.search(str(task or "")))


def _names_a_package(verb: str, rest: str) -> bool:
    if verb.lower() not in _NAMED_VERBS:
        return False
    toks = rest.replace("`", " ").split()
    i = 0
    while i < len(toks):
        tk = toks[i]
        if tk in ("-r", "--requirement", "-c", "--constraint"):
            return False                               # installs from a file of the project
        if tk in ("-e", "--editable"):
            i += 1
            continue
        if tk.startswith("-"):
            i += 1
            continue
        if tk in (".", "./") or tk.startswith(("./", "../")) or tk.endswith((".txt", ".toml", ".cfg")):
            return False                               # the project itself
        return bool(re.match(r"^[A-Za-z0-9@][\w.@/\-\[\],<>=!~^*:+]*$", tk))
    return False                                       # bare: the project's dependencies


def ungrounded_install_refs(text: str, real_files: Optional[Sequence[str]]) -> List[str]:
    """Install/build commands that install or build the PROJECT through a manifest that is
    not in `real_files`. None (listing unknown) flags nothing."""
    if real_files is None:
        return []
    names = {os.path.basename(f).lower() for f in real_files if isinstance(f, str)}
    out: List[str] = []
    for m in CMD_RE.finditer(text or ""):
        tool, verb, rest = m.group(1).lower(), m.group(2), m.group(3)
        if _names_a_package(verb, rest):
            continue
        ok = ALSO_OK.get(tool, (MANIFESTS.get(tool, ""),))
        if any(o and o.lower() in names for o in ok):
            continue
        out.append(m.group(0).strip().strip("`").strip()[:120])
    return out
