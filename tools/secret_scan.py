#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
#  Vera — secret_scan.py
#  Dependency-free secret scanner. Catches credentials before they reach git.
#
#  Modes:
#    --staged   Scan only files staged for commit (used by the pre-commit hook).
#    --all      Scan every git-tracked file (used by CI).
#    <paths…>   Scan the given files/dirs explicitly.
#
#  Exit code is non-zero when a likely secret is found, which aborts the commit
#  (pre-commit) or fails the CI job.
#
#  Suppressing a false positive:
#    • Add  # pragma: allowlist secret   (or  gitleaks:allow ) to the line.
#    • Add a regex to .secretscanignore at the repo root (one per line).
# ═══════════════════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252 and choke on box/check glyphs; force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Named detection rules ──────────────────────────────────────────────────────
#  Each rule is (name, compiled-regex). Keep these specific; broad rules belong
#  in the entropy pass below.
RULES: list[tuple[str, re.Pattern]] = [
    ("AWS Access Key ID", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b")),
    ("AWS Secret Access Key", re.compile(r"(?i)aws.{0,20}?(?:secret|key).{0,20}?['\"]([A-Za-z0-9/+=]{40})['\"]")),
    ("GitHub Token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Slack Webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("Google OAuth Client Secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe Secret Key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("Telegram Bot Token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{32,}\b")),
    ("OpenAI / Anthropic Key", re.compile(r"\b(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{20,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Private Key Block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("Generic Secret Assignment", re.compile(
        r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"private[_-]?key)\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]")),
]

# ── Inline / value allowlist ───────────────────────────────────────────────────
INLINE_ALLOW = re.compile(r"pragma:\s*allowlist secret|gitleaks:\s*allow", re.IGNORECASE)

# Obvious placeholders / non-secrets — skipped even when a rule or entropy fires.
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"x{3,}|\*{3,}|\.{3,}|<[^>]+>|\{\{?[^}]+\}?\}|\$\{?[a-z_]+\}?|"          # masks, templates, ${VAR}
    r"changeme|example|placeholder|dummy|sample|test|fake|your[_-].*|"        # words
    r"none|null|true|false|redacted|os\.environ.*|getenv.*"
    r")$"
)

# Files/dirs we never scan (binaries, generated, vendored, docs of placeholders).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "lib",
             "chroma_store", "chroma_db", "archive", "deprecated", "Deprecated"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
               ".woff", ".woff2", ".ttf", ".eot", ".zip", ".gz", ".tar", ".db",
               ".sqlite", ".sqlite3", ".pyc", ".pyo", ".pyd", ".lock", ".min.js"}
# *.example / *.sample files hold deliberate placeholder values.
SKIP_NAME_RE = re.compile(r"\.(example|sample|template)$|^\.env\.example$", re.IGNORECASE)

MAX_BYTES = 2_000_000  # skip files larger than ~2 MB


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {c: s.count(c) for c in set(s)}
    return -sum((n / len(s)) * math.log2(n / len(s)) for n in freq.values())


# High-entropy token in a quoted string or after = / : — the catch-all for keys
# that don't match a named vendor format.
ENTROPY_CANDIDATE = re.compile(r"['\"]([A-Za-z0-9+/=_\-]{20,})['\"]")


def load_user_allowlist() -> list[re.Pattern]:
    f = REPO_ROOT / ".secretscanignore"
    pats: list[re.Pattern] = []
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    pats.append(re.compile(line))
                except re.error:
                    pass
    return pats


def is_allowlisted(value: str, user_allow: list[re.Pattern]) -> bool:
    if PLACEHOLDER.match(value):
        return True
    return any(p.search(value) for p in user_allow)


def scan_text(text: str, user_allow: list[re.Pattern]) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if INLINE_ALLOW.search(line):
            continue
        stripped = line.strip()

        matched_here = False
        for name, rule in RULES:
            m = rule.search(line)
            if not m:
                continue
            captured = m.group(m.lastindex) if m.lastindex else m.group(0)
            if is_allowlisted(captured, user_allow):
                continue
            findings.append((lineno, name, captured[:60]))
            matched_here = True
        if matched_here:
            continue

        # Entropy pass — skip comment-only lines to cut doc/example noise.
        if stripped.startswith(("#", "//", "*", "<!--")):
            continue
        for m in ENTROPY_CANDIDATE.finditer(line):
            token = m.group(1)
            if is_allowlisted(token, user_allow):
                continue
            if len(token) < 24 or shannon_entropy(token) < 4.0:
                continue
            # Distinguish real keys from URL paths / CLI flags / slugs, which are
            # also long and varied. A credential is either mixed-case+digit
            # (base64-style) or a long pure-hex string; paths/flags are not.
            has_lower = any(c.islower() for c in token)
            has_upper = any(c.isupper() for c in token)
            has_digit = any(c.isdigit() for c in token)
            is_hex = re.fullmatch(r"[0-9a-fA-F]{32,}", token) is not None
            if (has_lower and has_upper and has_digit) or is_hex:
                findings.append((lineno, "High-entropy string", token[:60]))
    return findings


def git_files(staged: bool) -> list[Path]:
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def expand_paths(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(f for f in path.rglob("*") if f.is_file())
        elif path.is_file():
            files.append(path)
    return files


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.suffix.lower() in SKIP_SUFFIX:
        return True
    if SKIP_NAME_RE.search(path.name):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan for leaked secrets.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="scan files staged for commit")
    g.add_argument("--all", action="store_true", help="scan all git-tracked files")
    ap.add_argument("paths", nargs="*", help="explicit files/dirs to scan")
    args = ap.parse_args()

    if args.paths:
        files = expand_paths(args.paths)
    else:
        files = git_files(staged=args.staged or not args.all)

    user_allow = load_user_allowlist()
    total = 0
    for f in files:
        if should_skip(f) or not f.exists():
            continue
        try:
            if f.stat().st_size > MAX_BYTES:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for lineno, name, snippet in scan_text(text, user_allow):
            rel = f.relative_to(REPO_ROOT) if REPO_ROOT in f.parents or f == REPO_ROOT else f
            print(f"  {rel}:{lineno}: {name}: {snippet}")
            total += 1

    if total:
        print(f"\n✖ {total} potential secret(s) found. Commit blocked.")
        print("  • Real secret?  Remove it and use .env / vera/security/secrets.py.")
        print("  • False alarm?  Add '# pragma: allowlist secret' to the line,")
        print("    or a regex to .secretscanignore at the repo root.")
        return 1
    print("✔ No secrets detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
