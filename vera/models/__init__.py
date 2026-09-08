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
from .inference_contracts import (
    InferenceArtifact, InferenceCancellation, InferenceContractConflict,
    InferenceEvent, InferenceProvider, InferenceRequest, InferenceTranscript,
    InferenceValue, consume_inference, inference_event_from_dict,
    inference_request_from_dict, inference_value_from_dict)
from .onnx_inference_adapter import LegacyONNXInferenceProvider, LegacyONNXRunner
from .openai_inference_adapter import (
    OpenAICompatibleInferenceProvider, OpenAICompatibleTransport)
from .inference_registry import (
    InferenceProviderDescriptor, InferenceProviderRegistry)
from .inference_health import (
    InferenceProviderHealth, inference_provider_health_from_dict)
from .inference_conformance import (
    InferenceConformanceExpectation, InferenceConformanceReport,
    evaluate_inference_conformance, inference_conformance_expectation_from_dict)
from .ollama_inference_adapter import (
    LegacyOllamaInferenceProvider, LegacyOllamaRunner)
from .legacy_prediction_adapter import (
    LegacyBatchPredictionProvider, LegacyPredictionRunner)
from .inference_deployment import (
    InferenceDeployment, InferenceDeploymentObservation,
    InferenceDeploymentStoreCorrupt, SQLiteInferenceDeploymentRegistry,
    define_inference_deployment, inference_deployment_from_dict,
    inference_deployment_observation_from_dict)
from .ml_workshop_inference_adapter import (
    LegacyMLWorkshopInferenceProvider, LegacyMLWorkshopRunner)
from .native_tensor_inference_adapter import (
    NativeTensorRunner, PyTorchInferenceProvider, TensorFlowInferenceProvider)

__all__ = ["AdmittedModelActivation", "LegacyModelCapabilityBinding", "ModelActivationReceipt",
           "ModelAdmissionReceipt", "ModelDeploymentTarget",
           "ModelPackageAdmissionRejected", "ModelTrustPolicy", "ONNXImportReceipt",
           "EvalProvider", "EvaluationReport", "EvaluationRequest", "MetricResult",
           "LifecycleContractConflict",
           "InferenceArtifact", "InferenceCancellation", "InferenceContractConflict",
           "InferenceEvent", "InferenceProvider", "InferenceRequest",
           "InferenceProviderDescriptor", "InferenceProviderRegistry",
           "InferenceProviderHealth", "InferenceConformanceExpectation",
           "InferenceConformanceReport", "evaluate_inference_conformance",
           "inference_provider_health_from_dict",
           "inference_conformance_expectation_from_dict",
           "InferenceTranscript", "InferenceValue",
           "LegacyONNXInferenceProvider", "LegacyONNXRunner",
           "LegacyOllamaInferenceProvider", "LegacyOllamaRunner",
           "LegacyBatchPredictionProvider", "LegacyPredictionRunner",
           "InferenceDeployment", "InferenceDeploymentObservation",
           "InferenceDeploymentStoreCorrupt",
           "SQLiteInferenceDeploymentRegistry", "define_inference_deployment",
           "inference_deployment_from_dict",
           "inference_deployment_observation_from_dict",
           "LegacyMLWorkshopInferenceProvider", "LegacyMLWorkshopRunner",
           "NativeTensorRunner", "PyTorchInferenceProvider",
           "TensorFlowInferenceProvider",
           "OpenAICompatibleInferenceProvider", "OpenAICompatibleTransport",
           "PromptMessage", "PromptPackage", "ProviderProfile", "TrainingRequest",
           "TrainingRun", "TrainingRuntime",
           "DeterministicScalarEvalProvider", "ScalarCaseObservation",
           "ScalarEvaluationFixture", "ScalarMetricPolicy",
           "SQLiteModelPackageRegistry", "inspect_and_register_onnx",
           "evaluate_model_admission", "evaluation_report_from_dict",
           "evaluation_request_from_dict", "legacy_onnx_bindings",
           "consume_inference", "inference_event_from_dict",
           "inference_request_from_dict", "inference_value_from_dict",
           "prompt_package_from_dict", "training_request_from_dict",
           "training_run_from_dict"]
