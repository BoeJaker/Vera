import copy

import pytest

from vera.execution.run_protocol import ArtifactRef
from vera.models.distributed_training_runtime import (
    CheckpointContract, DistributedRuntimeProfile, DistributedTopology,
    plan_accelerate_training)
from vera.models.training_contracts import TrainingRequest


pytestmark = pytest.mark.critical


def request():
    return TrainingRequest(
        "dataset_rev_7", "fine_tune", "model_base_1", "prompt_1",
        (("epochs", 3), ("learning_rate", 0.0001)))


def profile():
    return DistributedRuntimeProfile(
        "accelerate_runtime_1", "1.11.0", "2.9.1", "c" * 64,
        ("cpu", "cuda"), ("single", "ddp", "fsdp", "deepspeed"),
        ("fp32", "fp16", "bf16"),
        ("OMP_NUM_THREADS", "TOKENIZERS_PARALLELISM"), 4, 8)


def entrypoint():
    return ArtifactRef(
        "entrypoint_1", "training.entrypoint", "artifact://private/train.py",
        "a" * 64, "text/x-python", 120)


def checkpoint(resume=False):
    artifact = (ArtifactRef(
        "checkpoint_4", "training.checkpoint", "artifact://private/ckpt",
        "b" * 64, "application/octet-stream", 2048) if resume else None)
    return CheckpointContract(100, 3, artifact)


def test_plan_is_deterministic_payload_safe_and_preserves_authority():
    topology = DistributedTopology("cuda", "fsdp", "bf16", 2, 4, 1, "rdzv_1")
    kwargs = dict(
        request=request(), profile=profile(), topology=topology,
        checkpoint=checkpoint(True), entrypoint_module="jobs.train",
        entrypoint_artifact=entrypoint(),
        environment={"OMP_NUM_THREADS": 4, "TOKENIZERS_PARALLELISM": False},
        application_arguments=("--dataset-ref", "dataset_rev_7"))
    first = plan_accelerate_training(**kwargs)
    second = plan_accelerate_training(**kwargs)
    assert first == second
    value = first.to_dict()
    assert value["plan_id"].startswith("atplan_")
    assert value["training_request_id"] == request().training_request_id
    assert value["runtime_profile_id"] == profile().profile_id
    assert value["topology"]["world_size"] == 8
    assert "--use_fsdp" in value["command"]
    assert value["command"][-4:] == [
        "jobs.train", "--", "--dataset-ref", "dataset_rev_7"]
    assert value["effect"] == "none"
    assert value["runtime_invoked"] is False
    assert value["hardware_allocated"] is False
    assert value["activation_authority"] is False
    text = repr(value)
    assert "artifact://private" not in text
    assert "0.0001" not in text
    assert "OMP_NUM_THREADS': 4" not in text


def test_plan_identity_changes_with_every_execution_relevant_projection():
    base = dict(
        request=request(), profile=profile(),
        topology=DistributedTopology("cuda", "ddp", "fp16", 1, 2),
        checkpoint=checkpoint(), entrypoint_module="jobs.train",
        entrypoint_artifact=entrypoint())
    first = plan_accelerate_training(**base)
    assert first.plan_id != plan_accelerate_training(
        **base, environment={"OMP_NUM_THREADS": 2}).plan_id
    altered = copy.copy(base)
    altered["checkpoint"] = CheckpointContract(101, 3)
    assert first.plan_id != plan_accelerate_training(**altered).plan_id


@pytest.mark.parametrize("topology", [
    lambda: DistributedTopology("cpu", "single", "fp32", 1, 2),
    lambda: DistributedTopology("cuda", "ddp", "fp16"),
    lambda: DistributedTopology("cpu", "fsdp", "fp32", 1, 2),
    lambda: DistributedTopology("mps", "ddp", "fp32", 1, 2),
    lambda: DistributedTopology("cuda", "ddp", "fp16", 2, 2, 0, ""),
])
def test_contradictory_topologies_fail_closed(topology):
    with pytest.raises(ValueError):
        topology()


def test_profile_capabilities_and_capacity_are_enforced():
    small = DistributedRuntimeProfile(
        "cpu_runtime", "1.11.0", "2.9.1", "c" * 64,
        ("cpu",), ("single",), ("fp32",), (), 1, 1)
    with pytest.raises(ValueError, match="device"):
        plan_accelerate_training(
            request=request(), profile=small,
            topology=DistributedTopology("cuda", "ddp", "fp16", 1, 2),
            checkpoint=checkpoint(), entrypoint_module="jobs.train",
            entrypoint_artifact=entrypoint())


def test_environment_is_allowlisted_hashed_and_never_retained():
    plan = plan_accelerate_training(
        request=request(), profile=profile(),
        topology=DistributedTopology("cpu", "single", "fp32"),
        checkpoint=checkpoint(), entrypoint_module="jobs.train",
        entrypoint_artifact=entrypoint(),
        environment={"OMP_NUM_THREADS": 7})
    assert plan.to_dict()["environment"] == [{
        "name": "OMP_NUM_THREADS",
        "value_digest": plan.to_dict()["environment"][0]["value_digest"],
    }]
    assert set(plan.to_dict()["environment"][0]) == {"name", "value_digest"}
    for unsafe in ({"HF_TOKEN": "secret"}, {"PATH": "/tmp"}):
        with pytest.raises(ValueError, match="explicitly allowed"):
            plan_accelerate_training(
                request=request(), profile=profile(),
                topology=DistributedTopology("cpu", "single", "fp32"),
                checkpoint=checkpoint(), entrypoint_module="jobs.train",
                entrypoint_artifact=entrypoint(), environment=unsafe)


def test_secret_like_environment_cannot_even_enter_profile_allowlist():
    with pytest.raises(ValueError, match="unsafe"):
        DistributedRuntimeProfile(
            "runtime", "1.0", "2.0", "c" * 64, ("cpu",),
            ("single",), ("fp32",), ("API_KEY",))


@pytest.mark.parametrize("argument", ["--api-key", "TOKEN=value", "line\nbreak"])
def test_unsafe_application_arguments_are_rejected(argument):
    with pytest.raises(ValueError):
        plan_accelerate_training(
            request=request(), profile=profile(),
            topology=DistributedTopology("cpu", "single", "fp32"),
            checkpoint=checkpoint(), entrypoint_module="jobs.train",
            entrypoint_artifact=entrypoint(), application_arguments=(argument,))


@pytest.mark.parametrize("module", ["/tmp/train.py", "jobs.train;rm", "jobs..train"])
def test_entrypoint_is_a_module_not_a_shell_or_path(module):
    with pytest.raises(ValueError, match="dotted Python module"):
        plan_accelerate_training(
            request=request(), profile=profile(),
            topology=DistributedTopology("cpu", "single", "fp32"),
            checkpoint=checkpoint(), entrypoint_module=module,
            entrypoint_artifact=entrypoint())


def test_entrypoint_and_resume_artifacts_require_digest_and_size():
    for artifact in (
        ArtifactRef("entry", "training.entrypoint", "x", "bad", "text/x-python", 1),
        ArtifactRef("entry", "training.entrypoint", "x", "a" * 64, "text/x-python"),
    ):
        with pytest.raises(ValueError):
            plan_accelerate_training(
                request=request(), profile=profile(),
                topology=DistributedTopology("cpu", "single", "fp32"),
                checkpoint=checkpoint(), entrypoint_module="jobs.train",
                entrypoint_artifact=artifact)
    with pytest.raises(ValueError):
        CheckpointContract(100, resume_from=ArtifactRef(
            "ckpt", "training.checkpoint", "x", "bad", size_bytes=1))


def test_runtime_versions_revision_and_artifact_roles_are_pinned():
    for adapter_version, source_revision in (("latest", "c" * 64),
                                             ("1.11.0", "latest")):
        with pytest.raises(ValueError):
            DistributedRuntimeProfile(
                "runtime", adapter_version, "2.9.1", source_revision,
                ("cpu",), ("single",), ("fp32",))
    with pytest.raises(ValueError, match="training.entrypoint"):
        plan_accelerate_training(
            request=request(), profile=profile(),
            topology=DistributedTopology("cpu", "single", "fp32"),
            checkpoint=checkpoint(), entrypoint_module="jobs.train",
            entrypoint_artifact=ArtifactRef(
                "script", "text", "x", "a" * 64, "text/x-python", 1))
    with pytest.raises(ValueError, match="training checkpoint"):
        CheckpointContract(100, resume_from=ArtifactRef(
            "weights", "model", "x", "b" * 64,
            "application/octet-stream", 1))


def test_profile_and_inputs_are_not_mutated():
    env = {"OMP_NUM_THREADS": 4}
    args = ["--epochs", "3"]
    before_env, before_args = copy.deepcopy(env), copy.deepcopy(args)
    plan_accelerate_training(
        request=request(), profile=profile(),
        topology=DistributedTopology("cpu", "single", "fp32"),
        checkpoint=checkpoint(), entrypoint_module="jobs.train",
        entrypoint_artifact=entrypoint(), environment=env,
        application_arguments=args)
    assert env == before_env
    assert args == before_args
