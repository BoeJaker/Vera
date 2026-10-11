"""Bounded, content-free inventory of compatibility-path consumers.

The caller explicitly supplies roots.  The scanner never imports or executes
their contents and never claims that declared roots represent every external
consumer.  Results are evidence for a per-path review, not removal authority.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

SCHEMA = "vera.compatibility-consumer-inventory/v1"
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_BYTES = 64_000_000
_LABEL = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SYMBOL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{1,127}$")
_SUFFIXES = {
    ".py", ".pyi", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".sh", ".ps1",
}
_SKIP_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    leaf = symbol.rsplit(".", 1)[-1]
    alternatives = sorted({re.escape(symbol), re.escape(leaf)}, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9_])")


def collect_compatibility_consumer_inventory(
    roots: Mapping[str, str | Path],
    symbols: Sequence[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Scan explicitly declared roots and return path/digest/line evidence only."""
    if not roots:
        raise ValueError("at least one explicitly declared root is required")
    if not 1 <= int(max_files) <= DEFAULT_MAX_FILES:
        raise ValueError(f"max_files must be between 1 and {DEFAULT_MAX_FILES}")
    if not 1 <= int(max_bytes) <= DEFAULT_MAX_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {DEFAULT_MAX_BYTES}")
    ordered_symbols = tuple(sorted(set(str(item) for item in symbols)))
    if not ordered_symbols or any(not _SYMBOL.fullmatch(item) for item in ordered_symbols):
        raise ValueError("symbols must be unique bounded dotted identifiers")
    patterns = {symbol: _symbol_pattern(symbol) for symbol in ordered_symbols}
    root_results: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0

    for label, raw_root in sorted(roots.items()):
        if not _LABEL.fullmatch(str(label)):
            raise ValueError("root labels must be bounded identifiers")
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise ValueError(f"declared root is unavailable: {label}")
        matches = {symbol: [] for symbol in ordered_symbols}
        scanned_files = 0
        scanned_bytes = 0
        skipped_symlinks = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                skipped_symlinks += 1
                continue
            if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part in _SKIP_PARTS for part in relative.parts):
                continue
            size = path.stat().st_size
            if total_files + 1 > max_files or total_bytes + size > max_bytes:
                raise ValueError("consumer inventory bounds exceeded")
            raw = path.read_bytes()
            total_files += 1
            total_bytes += len(raw)
            scanned_files += 1
            scanned_bytes += len(raw)
            text = raw.decode("utf-8", errors="replace")
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            for symbol, pattern in patterns.items():
                lines = [index for index, line in enumerate(text.splitlines(), 1)
                         if pattern.search(line)]
                if lines:
                    matches[symbol].append({
                        "relative_path": relative.as_posix(),
                        "line_numbers": lines,
                        "content_digest": digest,
                    })
        root_results.append({
            "label": label,
            "root_identity": _identity("croot_", {"label": label}),
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "skipped_symlinks": skipped_symlinks,
            "matches": matches,
            "scan_complete": True,
        })

    payload = {
        "schema": SCHEMA,
        "symbols": list(ordered_symbols),
        "declared_roots": root_results,
        "scanned_files": total_files,
        "scanned_bytes": total_bytes,
        "scope": "declared_roots_only",
        "coverage_exclusions": ["symlink_targets", "unsupported_file_types"],
        "global_external_coverage": False,
        "removal_authority": False,
        "executes": False,
        "imports": False,
        "mutates": False,
    }
    return {"inventory_id": _identity("ccinv_", payload), **payload}
