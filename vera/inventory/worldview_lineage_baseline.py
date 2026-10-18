"""Source-bound inventory for Vera's three Worldview product lineages.

The inventory never imports the inventoried modules.  It distinguishes the
JEPA model, the unserved non-JEPA Intelligence Platform, and the integrated
Godseye successor before any data bridge or shared UI is allowed to imply that
they have the same authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Sequence


SCHEMA = "vera.worldview-lineage-baseline/v1"
_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_STATES = {"live", "default_on", "opt_in", "ui_only", "offline", "unserved",
           "test_only", "source_only"}
_KINDS = {"model", "consumer", "ui", "capability", "feed", "persistence",
          "portable_export", "lifecycle", "comparison"}
_DIRECTIONS = {"produces", "consumes", "exposes", "adapts", "persists"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _identity(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class WorldviewLineage:
    lineage_id: str
    label: str
    authority: str
    source: str
    source_assertion: str
    runtime_state: str
    successor_of: str = ""

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.lineage_id):
            raise ValueError("lineage_id must be a bounded identifier")
        if not self.label or len(self.label) > 128:
            raise ValueError("lineage label must be present and bounded")
        if not _ID.fullmatch(self.authority):
            raise ValueError("authority must be a bounded identifier")
        if self.runtime_state not in _STATES:
            raise ValueError("unsupported runtime state")
        if self.successor_of and not _ID.fullmatch(self.successor_of):
            raise ValueError("successor_of must be a bounded lineage identifier")
        _source_path(self.source)
        if not self.source_assertion or len(self.source_assertion) > 256:
            raise ValueError("source_assertion must be present and bounded")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id, "label": self.label,
            "authority": self.authority, "source": self.source,
            "runtime_state": self.runtime_state,
            "successor_of": self.successor_of,
        }


@dataclass(frozen=True, slots=True)
class WorldviewSurface:
    surface_id: str
    lineage_id: str
    kind: str
    direction: str
    runtime_state: str
    source: str
    source_assertion: str
    contract: str
    target: str = ""

    def __post_init__(self) -> None:
        for name in ("surface_id", "lineage_id", "contract"):
            if not _ID.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a bounded identifier")
        if self.target and not _ID.fullmatch(self.target):
            raise ValueError("target must be a bounded identifier")
        if self.kind not in _KINDS or self.direction not in _DIRECTIONS:
            raise ValueError("unsupported surface kind or direction")
        if self.runtime_state not in _STATES:
            raise ValueError("unsupported runtime state")
        _source_path(self.source)
        if not self.source_assertion or len(self.source_assertion) > 256:
            raise ValueError("source_assertion must be present and bounded")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id, "lineage_id": self.lineage_id,
            "kind": self.kind, "direction": self.direction,
            "runtime_state": self.runtime_state, "source": self.source,
            "contract": self.contract, "target": self.target,
        }


def _source_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix not in {
            ".py", ".html", ".js", ".json", ".md"}:
        raise ValueError("source must be a supported repository-relative file")
    return path


LINEAGES = (
    WorldviewLineage(
        "worldview.jepa", "JEPA Worldview", "jepa.model-and-retrieval",
        "vera/worldview/worldview_jepa.py", "class WorldView:", "live"),
    WorldviewLineage(
        "worldview.non-jepa", "Worldview Intelligence Platform",
        "non-jepa.browser-product", "vera/worldview/worldview.html",
        "WORLDVIEW — Intelligence Platform v3", "unserved"),
    WorldviewLineage(
        "godseye.integrated", "Godseye", "godseye.upstream-and-vera-integration",
        "documentation/11-worldview.md",
        "is the integrated successor intended to converge", "live",
        successor_of="worldview.non-jepa"),
)


SURFACES = (
    WorldviewSurface(
        "jepa.capabilities", "worldview.jepa", "capability", "exposes", "live",
        "vera/worldview/worldview_jepa.py", '"worldview.query",',
        "worldview.capability-family"),
    WorldviewSurface(
        "jepa.fabric-feed", "worldview.jepa", "feed", "consumes", "live",
        "vera/fabric/data_fabric.py", '"type": "fabric.ingested"',
        "fabric.ingested", "worldview.jepa"),
    WorldviewSurface(
        "jepa.context-recall", "worldview.jepa", "consumer", "consumes",
        "opt_in", "vera/fabric/context.py", "async def _recall_worldview",
        "worldview.query", "context.recall"),
    WorldviewSurface(
        "jepa.research", "worldview.jepa", "consumer", "consumes",
        "default_on", "vera/research/researcher_api.py",
        "async def gather_worldview", "worldview.query", "research.gather"),
    WorldviewSurface(
        "jepa.panel", "worldview.jepa", "ui", "exposes", "ui_only",
        "vera/worldview/worldview_jepa.py",
        '@APP.get("/ui/panels/worldview-panel"', "worldview.panel"),
    WorldviewSurface(
        "jepa.graph-panel", "worldview.jepa", "ui", "consumes", "ui_only",
        "vera/vera_graph_panel_worldview.js",
        "window.veraUI.Graph.registerPanel", "worldview.capability-family",
        "vera.graph"),
    WorldviewSurface(
        "jepa.retrieval-provenance", "worldview.jepa", "portable_export",
        "produces", "live", "vera/worldview/retrieval_provenance.py",
        "class JepaRetrievalProvenance", "vera.jepa-retrieval-provenance.v1",
        "fabric.retrieval-comparison"),
    WorldviewSurface(
        "jepa.evidence-envelope", "worldview.jepa", "portable_export",
        "produces", "test_only", "vera/worldview/evidence_provider.py",
        "class WorldviewEvidence", "vera.worldview-evidence.v1",
        "context.portable"),
    WorldviewSurface(
        "jepa.projection-manifest", "worldview.jepa", "portable_export",
        "produces", "test_only",
        "vera/worldview/worldview_projection_adapter.py",
        "class WorldviewProjectionManifest",
        "vera.worldview-projection-manifest.v1", "worldview.jepa"),
    WorldviewSurface(
        "jepa.shadow-comparison", "worldview.jepa", "comparison", "produces",
        "test_only", "vera/worldview/reranking_shadow.py",
        "def compare_reranking_shadow", "vera.worldview-reranking-shadow.v1",
        "context.portable"),
    WorldviewSurface(
        "non-jepa.page", "worldview.non-jepa", "ui", "exposes", "unserved",
        "vera/worldview/worldview.html",
        "WORLDVIEW — Intelligence Platform v3", "non-jepa.browser-page"),
    WorldviewSurface(
        "non-jepa.registration", "worldview.non-jepa", "lifecycle", "exposes",
        "source_only", "vera/capability_orchestration.py",
        '# @APP.get("/worldview/panel"', "non-jepa.commented-registration"),
    WorldviewSurface(
        "non-jepa.portable-dataset", "worldview.non-jepa", "portable_export",
        "adapts", "offline", "vera/godseye/portable_dataset.py",
        'source not in {"godseye", "worldview-non-jepa"}',
        "vera.godseye-portable-dataset.v1", "fabric.dataset"),
    WorldviewSurface(
        "godseye.lifecycle", "godseye.integrated", "lifecycle", "exposes",
        "live", "vera/godseye/godseye_capabilities.py",
        '"godseye.status", memory="off"', "godseye.lifecycle-family"),
    WorldviewSurface(
        "godseye.panel", "godseye.integrated", "ui", "exposes", "ui_only",
        "vera/godseye/godseye_capabilities.py", 'register_ui(\n    "godseye"',
        "godseye.panel"),
    WorldviewSurface(
        "godseye.cctv", "godseye.integrated", "capability", "produces", "live",
        "vera/godseye/godseye_capabilities.py",
        '"cctv.refresh", memory="on"', "godseye.cctv-native"),
    WorldviewSurface(
        "godseye.imagery", "godseye.integrated", "capability", "produces",
        "live", "vera/godseye/godseye_capabilities.py",
        '"imagery.search", memory="on"', "godseye.imagery-native"),
    WorldviewSurface(
        "godseye.buildings", "godseye.integrated", "capability", "produces",
        "live", "vera/godseye/godseye_capabilities.py",
        '"buildings.fetch", memory="on"', "godseye.buildings-native"),
    WorldviewSurface(
        "godseye.portable-dataset", "godseye.integrated", "portable_export",
        "adapts", "offline", "vera/godseye/portable_dataset.py",
        "def make_portable_dataset", "vera.godseye-portable-dataset.v1",
        "fabric.dataset"),
)


GAPS = (
    "non_jepa_worldview_page_is_not_served",
    "portable_geospatial_dataset_has_no_runtime_consumer",
    "jepa_does_not_consume_non_jepa_or_godseye_snapshots",
    "godseye_vendor_source_is_runtime_external_to_this_repository",
    "cross_lineage_value_and_contamination_are_not_measured",
)


def semantic_baseline(
        lineages: Sequence[WorldviewLineage] = LINEAGES,
        surfaces: Sequence[WorldviewSurface] = SURFACES) -> dict[str, Any]:
    lineages = tuple(sorted(lineages, key=lambda value: value.lineage_id))
    surfaces = tuple(sorted(surfaces, key=lambda value: value.surface_id))
    lineage_ids = [value.lineage_id for value in lineages]
    surface_ids = [value.surface_id for value in surfaces]
    if len(lineages) != 3 or len(lineage_ids) != len(set(lineage_ids)):
        raise ValueError("exactly three unique lineages are required")
    if not surfaces or len(surface_ids) != len(set(surface_ids)):
        raise ValueError("surfaces must be non-empty and uniquely identified")
    known = set(lineage_ids)
    if any(value.lineage_id not in known for value in surfaces):
        raise ValueError("surface references an unknown lineage")
    if any(value.successor_of and value.successor_of not in known
           for value in lineages):
        raise ValueError("successor references an unknown lineage")
    payload = {
        "schema": SCHEMA,
        "lineages": [value.semantic_dict() for value in lineages],
        "surfaces": [value.semantic_dict() for value in surfaces],
        "gaps": list(GAPS),
        "authority_shared": False,
        "runtime_changed": False,
    }
    return {"baseline_id": _identity("wvlb_", payload), **payload}


def _source_evidence(repo_root: Path, lineages: Sequence[WorldviewLineage],
                     surfaces: Sequence[WorldviewSurface]) -> list[dict[str, str]]:
    root = repo_root.resolve()
    entries = [(value.lineage_id, value.source, value.source_assertion)
               for value in lineages]
    entries += [(value.surface_id, value.source, value.source_assertion)
                for value in surfaces]
    evidence = []
    for identity, source, assertion in sorted(entries):
        path = (root / source).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"source is unavailable: {source}")
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise ValueError(f"source is too large: {source}")
        if assertion not in raw.decode("utf-8"):
            raise ValueError(f"source assertion missing for {identity}")
        evidence.append({
            "identity": identity,
            "source_path_digest": "sha256:" + hashlib.sha256(source.encode()).hexdigest(),
            "source_content_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "assertion_digest": "sha256:" + hashlib.sha256(assertion.encode()).hexdigest(),
        })
    return evidence


def build_worldview_lineage_baseline(
        repo_root: Path, lineages: Sequence[WorldviewLineage] = LINEAGES,
        surfaces: Sequence[WorldviewSurface] = SURFACES) -> dict[str, Any]:
    stable = semantic_baseline(lineages, surfaces)
    evidence = _source_evidence(Path(repo_root), lineages, surfaces)
    grouped: dict[str, list[str]] = {value.lineage_id: [] for value in lineages}
    for surface in sorted(surfaces, key=lambda value: value.surface_id):
        grouped[surface.lineage_id].append(surface.surface_id)
    return {
        **stable,
        "inventory_id": _identity(
            "wvli_", {"baseline_id": stable["baseline_id"],
                      "source_evidence": evidence}),
        "source_evidence": evidence,
        "surfaces_by_lineage": dict(sorted(grouped.items())),
        "counts": {"lineages": len(lineages), "surfaces": len(surfaces),
                   "live_or_default": sum(value.runtime_state in {"live", "default_on"}
                                          for value in surfaces),
                   "gaps": len(GAPS)},
        "constraints": {
            "imports_inventoried_subsystems": False,
            "invokes_capabilities": False,
            "contacts_sources": False,
            "contacts_models": False,
            "uses_accelerators": False,
            "mutates": False,
        },
    }
