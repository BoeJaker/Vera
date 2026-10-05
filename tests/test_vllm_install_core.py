"""One vLLM install recipe for every provisioner: V100-capable on GPU, a working CPU build."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.vllm import vllm_install_core as ic  # noqa: E402

pytestmark = pytest.mark.critical


def test_gpu_uses_the_release_cu129_wheel_with_cu126_torch():
    s = ic.install_script("/opt/vera-vllm", mode="gpu")
    assert "TAG=cu129" in s and "whl/cu126" in s
    assert "--index-strategy unsafe-best-match" in s
    # the stock PyPI wheel (CUDA 13, no sm_70) is never what gets installed
    assert "pip install vllm" not in s


def test_cpu_uses_the_cpu_wheel_and_checks_glibc():
    s = ic.install_script("/opt/vera-vllm", mode="cpu")
    assert "TAG=cpu" in s and "whl/cpu" in s
    assert ic.CPU_MIN_GLIBC in s and "exit 3" in s


def test_release_asset_url_matches_the_encoded_plus():
    s = ic.install_script("/opt/v", mode="cpu")
    assert "(\\+|%2B)" in s


def test_cpu_env_uses_nobind_threads_and_fp32():
    body = ic.env_body("cpu", nproc_expr="12")
    assert "VLLM_CPU_OMP_THREADS_BIND=nobind" in body
    assert "OMP_NUM_THREADS=$(( 12 > 1 ? 12 - 1 : 1 ))" in body
    assert "--dtype float32" in body


def test_gpu_env_takes_the_dtype_flag_from_the_box():
    body = ic.env_body("gpu", gpu_util=0.5, max_model_len=4096, dtype_expr="$DT")
    assert '"--gpu-memory-utilization 0.5 --max-model-len 4096$DT"' in body
    s = ic.install_script("/opt/v", mode="gpu")
    assert '[0-7].*) DT=" --dtype half"' in s


def test_explicit_spec_skips_the_release_lookup():
    s = ic.install_script("/opt/v", mode="gpu", pip_spec="vllm==0.9.2")
    assert "SPEC=vllm==0.9.2" in s


def test_done_marker_is_the_last_line():
    s = ic.install_script("/opt/v", done_marker="VLLM_PROVISION_OK")
    assert s.strip().splitlines()[-1] == "echo VLLM_PROVISION_OK"


def test_home_relative_venv_stays_expandable():
    assert ic.qpath("$HOME/.vera/vllm") == '"$HOME"/.vera/vllm'
    assert 'VENV="$HOME"/.vera/vllm' in ic.install_script("$HOME/.vera/vllm")
    assert ic.serve_command("$HOME/.vera/vllm", "Qwen/Q", 8001).startswith(
        '"$HOME"/.vera/vllm/serve.sh Qwen/Q 8001')


def test_bad_mode_is_refused():
    with pytest.raises(ValueError):
        ic.install_script("/opt/v", mode="tpu")


def test_system_deps_bring_a_compiler():
    assert "build-essential" in ic.system_deps_cmd("sudo ")


def test_cpu_threads_leave_one_core():
    assert ic.cpu_threads(12) == 11 and ic.cpu_threads(1) == 1


@pytest.mark.skipif(not shutil.which("bash") or os.name == "nt",
                    reason="needs a POSIX bash")
@pytest.mark.parametrize("mode", ["auto", "gpu", "cpu"])
def test_script_is_valid_bash(mode):
    s = ic.install_script("/opt/vera-vllm", mode=mode, version="0.31.0")
    r = subprocess.run(["bash", "-n"], input=s, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
