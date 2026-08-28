"""Portable model lifecycle contracts."""

from .onnx_import import ONNXImportReceipt, inspect_and_register_onnx
from .model_package_store import (
    AdmittedModelActivation, ModelActivationReceipt, SQLiteModelPackageRegistry)
from .legacy_binding import LegacyModelCapabilityBinding, legacy_onnx_bindings
from .admission import (
    ModelAdmissionReceipt, ModelDeploymentTarget, ModelPackageAdmissionRejected,
    ModelTrustPolicy, evaluate_model_admission)

__all__ = ["AdmittedModelActivation", "LegacyModelCapabilityBinding", "ModelActivationReceipt",
           "ModelAdmissionReceipt", "ModelDeploymentTarget",
           "ModelPackageAdmissionRejected", "ModelTrustPolicy", "ONNXImportReceipt",
           "SQLiteModelPackageRegistry", "inspect_and_register_onnx",
           "evaluate_model_admission", "legacy_onnx_bindings"]
