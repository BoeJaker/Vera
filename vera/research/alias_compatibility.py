"""Compatibility metadata for the legacy research convenience capabilities.

The aliases remain callable, but their behaviour is defined here instead of
being repeated independently in each wrapper.  This keeps request projection
deterministic and makes misleading names visible to discovery clients.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchAlias:
    name: str
    mode: str
    output_mode: str
    replacement: str = "research.run"
    direct_search: bool = False

    def request(self, *, query: str, project_id: str = "", context: str = "",
                context_mode: str = "fresh") -> dict[str, str]:
        return {
            "query": query,
            "mode": self.mode,
            "output_mode": self.output_mode,
            "project_id": project_id,
            "context": context,
            "context_mode": context_mode or "fresh",
        }


_ALIASES = (
    ResearchAlias("research.report", "single", "report"),
    ResearchAlias("research.parallel", "parallel", "report"),
    ResearchAlias("research.deep", "deep", "report"),
    ResearchAlias("research.code", "deep", "code"),
    ResearchAlias("research.guide", "single", "guide"),
    ResearchAlias("research.filestore", "deep", "filestore"),
    # Despite its historical name this is not a direct search operation.  It
    # queues the same synthesized report as research.report.
    ResearchAlias("research.quick_search", "single", "report",
                  replacement="research.report", direct_search=False),
)

RESEARCH_ALIASES = {item.name: item for item in _ALIASES}


def research_alias(name: str) -> ResearchAlias:
    """Return a declared alias or fail instead of guessing its semantics."""
    try:
        return RESEARCH_ALIASES[name]
    except KeyError as exc:
        raise ValueError(f"unknown research compatibility alias: {name}") from exc

