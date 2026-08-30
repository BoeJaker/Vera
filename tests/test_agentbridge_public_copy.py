"""Editorial guardrails for Agent Bridge's user-facing copy."""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SURFACES = (
    ROOT / "vera/agentbridges/agentbridge_catalog_panel.html",
    ROOT / "vera/agentbridges/agentbridge_capabilities.py",
    ROOT / "documentation/23-integrations.md",
    ROOT / "documentation/36-agent-runtimes-providers.md",
    ROOT / "documentation/46-interoperability-foundations.md",
)
INTERNAL_PACKAGE_ID = re.compile(r"\b(?:W\d+-\d+|LIB-?\d+)\b", re.IGNORECASE)


@pytest.mark.critical
def test_agentbridge_public_copy_does_not_expose_internal_package_ids():
    leaked = {}
    for path in PUBLIC_SURFACES:
        matches = sorted(set(INTERNAL_PACKAGE_ID.findall(path.read_text(encoding="utf-8"))))
        if matches:
            leaked[str(path.relative_to(ROOT))] = matches

    assert not leaked, f"internal delivery-plan identifiers leaked into public copy: {leaked}"
