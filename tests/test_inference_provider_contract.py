import asyncio

import pytest

from vera.models import (
    InferenceArtifact, InferenceContractConflict, InferenceEvent,
    InferenceProvider, InferenceRequest, InferenceValue, ProviderProfile,
    consume_inference, inference_event_from_dict, inference_request_from_dict,
)

pytestmark = pytest.mark.critical


def request(*, stream=True, max_output_bytes=100):
    return InferenceRequest(
        "mpkg_abc", "generate", "messages/v1", "text/v1",
        (InferenceValue.from_json("prompt", {"text": "hello"}),),
        parameters=(("temperature", 0),), stream=stream,
        max_output_bytes=max_output_bytes)


class Provider:
    def __init__(self, events, *, profile=None):
        self.events = events
        self._profile = profile or ProviderProfile(
            "provider", "inference", ("generate",))

    def profile(self):
        return self._profile

    async def infer(self, value, *, cancellation=None):
        for event in self.events:
            yield event


def event(value, sequence, kind, *, output=None, provider_id="provider",
          model_package_id=None, request_id=None, error_code="", usage=()):
    return InferenceEvent(
        request_id or value.request_id,
        model_package_id or value.model_package_id, provider_id, sequence, kind,
        output=output, error_code=error_code, usage=usage)


def test_request_is_canonical_content_addressed_and_immutable_at_boundaries():
    left = request()
    right = InferenceRequest(
        "mpkg_abc", "generate", "messages/v1", "text/v1",
        (InferenceValue.from_json("prompt", {"text": "hello"}),),
        parameters=(("temperature", 0),), stream=True, max_output_bytes=100)
    assert left.request_id == right.request_id
    assert left.inputs[0].json_data == '{"text":"hello"}'
    assert left.to_dict()["inputs"][0]["value"] == {"text": "hello"}
    assert inference_request_from_dict(left.to_dict()) == left
    changed = left.to_dict()
    changed["task"] = "embed"
    with pytest.raises(InferenceContractConflict, match="identity"):
        inference_request_from_dict(changed)


def test_values_require_exactly_one_bounded_inline_or_verified_artifact_payload():
    artifact = InferenceArtifact(
        "artifact://result", "a" * 64, 10, "application/octet-stream")
    assert InferenceValue("tensor", "application/octet-stream",
                          artifact=artifact).size_bytes == 10
    with pytest.raises(ValueError, match="exactly one"):
        InferenceValue("x", "application/json")
    with pytest.raises(ValueError, match="canonical JSON"):
        InferenceValue("x", "application/json", '{"b": 2, "a": 1}')
    with pytest.raises(ValueError, match="sha256"):
        InferenceArtifact("artifact://result", "bad", 1,
                          "application/octet-stream")


@pytest.mark.asyncio
async def test_consumer_validates_and_collects_one_complete_stream():
    value = request()
    first = InferenceValue.from_json("text", "hel", media_type="text/plain")
    second = InferenceValue.from_json("text", "lo", media_type="text/plain")
    result = await consume_inference(Provider([
        event(value, 0, "output", output=first),
        event(value, 1, "output", output=second),
        event(value, 2, "completed", usage=(("output_tokens", 2),)),
    ]), value)
    assert result.outputs == (first, second)
    assert (result.status, result.usage) == (
        "completed", (("output_tokens", 2),))
    assert isinstance(Provider([]), InferenceProvider)
    assert inference_event_from_dict(
        event(value, 0, "output", output=first).to_dict()).output == first


@pytest.mark.asyncio
@pytest.mark.parametrize("events, message", [
    (lambda value: [event(value, 1, "completed")], "sequence"),
    (lambda value: [event(value, 0, "completed"),
                    event(value, 1, "completed")], "after a terminal"),
    (lambda value: [], "without a terminal"),
    (lambda value: [event(value, 0, "completed", provider_id="foreign")],
     "identity mismatch"),
])
async def test_consumer_fails_closed_on_malformed_stream(events, message):
    value = request()
    with pytest.raises(InferenceContractConflict, match=message):
        await consume_inference(Provider(events(value)), value)


@pytest.mark.asyncio
async def test_output_budget_counts_inline_and_artifact_sizes():
    value = request(max_output_bytes=5)
    output = InferenceValue(
        "tensor", "application/octet-stream",
        artifact=InferenceArtifact("artifact://large", "b" * 64, 6,
                                   "application/octet-stream"))
    with pytest.raises(InferenceContractConflict, match="budget"):
        await consume_inference(Provider([
            event(value, 0, "output", output=output),
            event(value, 1, "completed")]), value)


@pytest.mark.asyncio
async def test_provider_profile_and_task_are_checked_before_dispatch():
    value = request()
    with pytest.raises(InferenceContractConflict, match="not inference"):
        await consume_inference(Provider([], profile=ProviderProfile(
            "provider", "evaluation", ("generate",))), value)
    with pytest.raises(InferenceContractConflict, match="requested task"):
        await consume_inference(Provider([], profile=ProviderProfile(
            "provider", "inference", ("embed",))), value)


@pytest.mark.asyncio
async def test_cancellation_remains_a_control_signal():
    class Cancel:
        calls = 0
        def checkpoint(self):
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError

    value = request()
    with pytest.raises(asyncio.CancelledError):
        await consume_inference(Provider([
            event(value, 0, "completed")]), value, cancellation=Cancel())


def test_terminal_and_usage_invariants_are_explicit():
    value = request()
    with pytest.raises(ValueError, match="requires an error"):
        event(value, 0, "failed")
    with pytest.raises(ValueError, match="only failed"):
        event(value, 0, "completed", error_code="bad")
    with pytest.raises(ValueError, match="non-negative"):
        event(value, 0, "completed", usage=(("tokens", -1),))
    with pytest.raises(ValueError, match="only terminal"):
        event(value, 0, "output", output=InferenceValue.from_json("x", 1),
              usage=(("tokens", 1),))
    with pytest.raises(TypeError, match="output value"):
        event(value, 0, "output", output="not-a-value")


def test_nested_or_duplicate_parameters_fail_closed():
    with pytest.raises(ValueError, match="JSON scalars"):
        InferenceRequest(
            "mpkg_abc", "generate", "messages/v1", "text/v1",
            (InferenceValue.from_json("prompt", "hello"),),
            parameters=(("nested", {"unsafe": "mutable"}),))
    with pytest.raises(ValueError, match="unique"):
        InferenceRequest(
            "mpkg_abc", "generate", "messages/v1", "text/v1",
            (InferenceValue.from_json("prompt", "hello"),),
            parameters=(("temperature", 0), ("temperature", 1)))
