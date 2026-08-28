"""Portable model lifecycle contracts."""

from .onnx_import import ONNXImportReceipt, inspect_and_register_onnx
from .model_package_store import ModelActivationReceipt, SQLiteModelPackageRegistry
from .legacy_binding import LegacyModelCapabilityBinding, legacy_onnx_bindings

__all__ = ["LegacyModelCapabilityBinding", "ModelActivationReceipt", "ONNXImportReceipt",
           "SQLiteModelPackageRegistry", "inspect_and_register_onnx",
           "legacy_onnx_bindings"]
