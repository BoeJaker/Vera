import pytest

from vera.models import (
    InferenceContractConflict, InferenceEvent, InferenceProviderRegistry,
    InferenceRequest, InferenceValue, ProviderProfile)

pytestmark = pytest.mark.critical


class Provider:
    def __init__(self, provider_id, tasks=("generate",), kind="inference"):
        self._profile = ProviderProfile(provider_id, kind, tasks)

    def profile(self):
        return self._profile

    async def infer(self, request, *, cancellation=None):
        yield InferenceEvent(
            request.request_id, request.model_package_id,
            self._profile.provider_id, 0, "completed")


def request(package="mpkg_one", task="generate"):
    return InferenceRequest(
        package, task, "text/v1", "text/v1",
        (InferenceValue.from_json("prompt", "hello"),))


def test_registry_lists_immutable_serializable_descriptors_in_stable_order():
    registry = InferenceProviderRegistry()
    registry.register(Provider("vllm:b"), package_ids=("mpkg_two", "mpkg_one"),
                      placements=("gpu", "local"), state="ready")
    registry.register(Provider("onnx:a", ("predict",)),
                      package_ids=("mpkg_onnx",), state="unknown")
    descriptors = registry.list()
    assert [item.provider_id for item in descriptors] == ["onnx:a", "vllm:b"]
    assert descriptors[1].to_dict() == {
        "schema": "vera.inference-provider-descriptor/v1",
        "provider_id": "vllm:b", "package_ids": ["mpkg_one", "mpkg_two"],
        "tasks": ["generate"], "placements": ["gpu", "local"],
        "state": "ready", "revision": 1}
    assert registry.get("missing") is None


def test_candidate_discovery_filters_package_task_placement_and_readiness():
    registry = InferenceProviderRegistry()
    registry.register(Provider("gpu"), package_ids=("mpkg_one",),
                      placements=("gpu",), state="ready")
    registry.register(Provider("cpu"), package_ids=("mpkg_one",),
                      placements=("cpu",), state="ready")
    registry.register(Provider("down"), package_ids=("mpkg_one",),
                      placements=("gpu",), state="unavailable")
    registry.register(Provider("other-task", ("embed",)),
                      package_ids=("mpkg_one",), placements=("gpu",), state="ready")
    assert [item.provider_id for item in registry.candidates(request())] == ["cpu", "gpu"]
    assert [item.provider_id for item in registry.candidates(
        request(), placements=("gpu",))] == ["gpu"]
    assert [item.provider_id for item in registry.candidates(
        request(), placements=("gpu",), include_unavailable=True)] == ["down", "gpu"]
    assert registry.candidates(request("mpkg_missing")) == ()


def test_resolution_is_explicit_and_never_falls_back_to_another_candidate():
    registry = InferenceProviderRegistry()
    first = Provider("first")
    second = Provider("second")
    registry.register(first, package_ids=("mpkg_one",), state="unavailable")
    registry.register(second, package_ids=("mpkg_one",), state="ready")
    with pytest.raises(InferenceContractConflict, match="eligible"):
        registry.resolve(request(), "first")
    assert registry.resolve(request(), "first", require_ready=False) is first
    assert registry.resolve(request(), "second") is second
    with pytest.raises(KeyError, match="not registered"):
        registry.resolve(request(), "missing")


def test_registration_updates_and_removal_require_compare_and_set_revision():
    registry = InferenceProviderRegistry()
    first = registry.register(
        Provider("provider"), package_ids=("mpkg_one",), state="unknown")
    assert first.revision == 1
    with pytest.raises(InferenceContractConflict, match="revision"):
        registry.register(Provider("provider"), package_ids=("mpkg_one",),
                          state="ready")
    second = registry.register(
        Provider("provider"), package_ids=("mpkg_one",), state="ready",
        expected_revision=1)
    assert second.revision == 2
    with pytest.raises(InferenceContractConflict, match="revision"):
        registry.remove("provider", expected_revision=1)
    registry.remove("provider", expected_revision=2)
    assert registry.list() == ()


def test_registry_rejects_non_inference_profiles_and_invalid_descriptors():
    registry = InferenceProviderRegistry()
    with pytest.raises(InferenceContractConflict, match="not inference"):
        registry.register(Provider("eval", kind="evaluation"),
                          package_ids=("mpkg_one",))
    with pytest.raises(ValueError, match="package_ids"):
        registry.register(Provider("empty"), package_ids=())
    with pytest.raises(ValueError, match="state"):
        registry.register(Provider("bad-state"), package_ids=("mpkg_one",),
                          state="healthy")
    with pytest.raises(TypeError, match="InferenceProvider"):
        registry.register(object(), package_ids=("mpkg_one",))
