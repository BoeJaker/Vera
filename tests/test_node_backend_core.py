"""A node switches between Ollama and vLLM in place, keeping its identity.

Pure decisions (vera/vllm/node_backend_core): which nodes are switched, how a pin
naming a switched node resolves, the vLLM unit environment (Ollama thread
parity on CPU), the on-node switch script, and when a switch refuses.
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.vllm import node_backend_core as bc  # noqa: E402

pytestmark = pytest.mark.critical

OLLAMA = {"gpu-250": {"enabled": True, "has_gpu": True},
          "cpu-246": {"enabled": False, "has_gpu": False},
          "cpu-247": {"enabled": True, "has_gpu": False}}
VLLM = {"vllm-cpu-246": {"gate_as": "cpu-246"}, "vllm-gpu-251": {"gate_as": ""}}


def test_switched_means_ollama_disabled_and_its_vllm_instance_gates_as_it():
    assert bc.switched_ids(OLLAMA, VLLM) == ["cpu-246"]
    # disabled but no vLLM instance: an operator disabled it, not a switch
    assert bc.switched_ids(OLLAMA, {}) == []
    # vLLM instance present but Ollama enabled: not switched
    assert bc.switched_ids(dict(OLLAMA, **{"cpu-246": {"enabled": True}}), VLLM) == []


def test_a_pin_naming_a_switched_node_reaches_its_vllm_instance():
    assert bc.resolve_pin("cpu-246", OLLAMA, VLLM) == "vllm:vllm-cpu-246"
    assert bc.resolve_pin("cpu-247", OLLAMA, VLLM) == "cpu-247"
    assert bc.resolve_pin("vllm:x", OLLAMA, VLLM) == "vllm:x"
    assert bc.resolve_pin("provider:openai", OLLAMA, VLLM) == "provider:openai"
    assert bc.resolve_pin("", OLLAMA, VLLM) == ""


def test_vllm_instance_id():
    assert bc.vllm_instance_id("cpu-246") == "vllm-cpu-246"


def test_env_carries_model_aliases_and_ollama_thread_parity_on_cpu():
    env = bc.env_file("RedHatAI/Qwen3.5-9B-quantized.w8a8",
                      aliases=["qwen3.5:9b", "RedHatAI/Qwen3.5-9B-quantized.w8a8"], threads=6)
    assert "VLLM_MODEL=RedHatAI/Qwen3.5-9B-quantized.w8a8\n" in env
    assert ("VLLM_EXTRA_ARGS=--served-model-name RedHatAI/Qwen3.5-9B-quantized.w8a8 qwen3.5:9b"
            in env)                          # the model once, then its aliases
    assert "OMP_NUM_THREADS=6" in env
    gpu = bc.env_file("m", threads=6, has_gpu=True)
    assert "OMP_NUM_THREADS" not in gpu


def _script(backend):
    return bc.switch_script(backend, vllm_service="vera-vllm_server",
                            env_path="/etc/default/vera-vllm", env_text="VLLM_MODEL=m\n",
                            public_port=11435, vllm_port=8000,
                            serve_path="/opt/vera-vllm/venv/serve.sh", wait_s=60)


def test_to_vllm_stops_ollama_and_its_tap_then_proves_vllm_answers():
    s = _script("vllm")
    assert "test -x /opt/vera-vllm/venv/serve.sh" in s
    assert "systemctl disable --now vera-ollama-tap@11435" in s
    assert "systemctl disable --now $U" in s and "ollama-vera" in s
    assert s.index("disable --now $U") < s.index("systemctl restart vera-vllm_server")
    assert "systemctl enable vera-vllm_server" in s
    assert "http://127.0.0.1:8000/v1/models" in s and bc.DONE in s


def test_to_ollama_restores_ollama_and_its_tap():
    s = _script("ollama")
    assert "systemctl disable --now vera-vllm_server" in s
    assert "systemctl enable $U" in s and "systemctl restart vera-ollama-tap@11435" in s
    assert "http://127.0.0.1:11435/api/tags" in s and bc.DONE in s


def test_bad_backend_is_refused():
    with pytest.raises(ValueError):
        _script("tgi")


@pytest.mark.parametrize("kw,action", [
    (dict(backend="vllm", vllm_present=True, switched=False, in_flight=0, force=False), "switch"),
    (dict(backend="vllm", vllm_present=False, switched=False, in_flight=0, force=False), "refuse"),
    (dict(backend="vllm", vllm_present=True, switched=False, in_flight=2, force=False), "refuse"),
    (dict(backend="vllm", vllm_present=True, switched=False, in_flight=2, force=True), "switch"),
    (dict(backend="vllm", vllm_present=True, switched=True, in_flight=0, force=False), "noop"),
    (dict(backend="ollama", vllm_present=True, switched=False, in_flight=0, force=False), "noop"),
    (dict(backend="ollama", vllm_present=True, switched=True, in_flight=0, force=False), "switch"),
])
def test_plan(kw, action):
    assert bc.plan(ollama_id="cpu-246", inst=OLLAMA["cpu-246"], **kw)["action"] == action


@pytest.mark.skipif(not shutil.which("bash") or os.name == "nt", reason="needs a POSIX bash")
@pytest.mark.parametrize("backend", ["vllm", "ollama"])
def test_switch_script_is_valid_bash(backend):
    r = subprocess.run(["bash", "-n"], input=_script(backend), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
