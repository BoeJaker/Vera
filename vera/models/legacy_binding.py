"""Non-executing bindings from legacy capability identities to ModelPackages."""

from __future__ import annotations

from dataclasses import dataclass

from .model_package import _identifier


@dataclass(frozen=True)
class LegacyModelCapabilityBinding:
    capability: str
    selector: str
    package_id: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", _identifier(
            self.capability, "legacy capability"))
        if self.selector:
            object.__setattr__(self, "selector", _identifier(
                self.selector, "legacy selector"))
        object.__setattr__(self, "package_id", _identifier(
            self.package_id, "package ID"))
        object.__setattr__(self, "source", _identifier(
            self.source, "binding source"))

    @property
    def legacy_identity(self) -> str:
        return self.capability if not self.selector else f"{self.capability}#{self.selector}"

    def to_dict(self) -> dict:
        return {**self.__dict__, "legacy_identity": self.legacy_identity}


def legacy_onnx_bindings(package_id: str, slug: str, *,
                         source: str = "ml-onnx-manifest/v1") -> tuple[LegacyModelCapabilityBinding, ...]:
    """Describe both legacy invocation identities for one exported ONNX artifact."""
    slug = _identifier(slug, "legacy ONNX slug")
    return (
        LegacyModelCapabilityBinding("ml.onnx.run", slug, package_id, source),
        LegacyModelCapabilityBinding(f"ml.onnx.model.{slug}", "", package_id, source),
    )
