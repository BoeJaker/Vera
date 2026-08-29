"""Portable model lifecycle contracts."""

from .onnx_import import ONNXImportReceipt, inspect_and_register_onnx
from .model_package_store import (
    AdmittedModelActivation, ModelActivationReceipt, SQLiteModelPackageRegistry)
from .legacy_binding import LegacyModelCapabilityBinding, legacy_onnx_bindings
from .admission import (
    ModelAdmissionReceipt, ModelDeploymentTarget, ModelPackageAdmissionRejected,
    ModelTrustPolicy, evaluate_model_admission)
from .training_contracts import (
    EvalProvider, EvaluationReport, EvaluationRequest, MetricResult,
    LifecycleContractConflict, PromptMessage, PromptPackage, ProviderProfile,
    TrainingRequest, TrainingRun, TrainingRuntime, evaluation_report_from_dict,
    evaluation_request_from_dict, prompt_package_from_dict,
    training_request_from_dict, training_run_from_dict)
from .deterministic_evaluation import (
    DeterministicScalarEvalProvider, ScalarCaseObservation,
    ScalarEvaluationFixture, ScalarMetricPolicy)

__all__ = ["AdmittedModelActivation", "LegacyModelCapabilityBinding", "ModelActivationReceipt",
           "ModelAdmissionReceipt", "ModelDeploymentTarget",
           "ModelPackageAdmissionRejected", "ModelTrustPolicy", "ONNXImportReceipt",
           "EvalProvider", "EvaluationReport", "EvaluationRequest", "MetricResult",
           "LifecycleContractConflict",
           "PromptMessage", "PromptPackage", "ProviderProfile", "TrainingRequest",
           "TrainingRun", "TrainingRuntime",
           "DeterministicScalarEvalProvider", "ScalarCaseObservation",
           "ScalarEvaluationFixture", "ScalarMetricPolicy",
           "SQLiteModelPackageRegistry", "inspect_and_register_onnx",
           "evaluate_model_admission", "evaluation_report_from_dict",
           "evaluation_request_from_dict", "legacy_onnx_bindings",
           "prompt_package_from_dict", "training_request_from_dict",
           "training_run_from_dict"]
