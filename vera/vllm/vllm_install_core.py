"""
vllm_install_core.py - one install recipe for a vLLM server, shared by every
provisioner (provision.install over SSH, pxstore.backend.provision_vllm in an LXC).

Pure: builds shell text only. No I/O, no app imports, so it is testable app-free.

Why a recipe at all (measured on PVE01, 2026-10-05):
  * GPU: the stock PyPI wheels are built for CUDA 13, which has no kernels for
    compute capability 7.0 (Tesla V100) - the server starts and every kernel
    fails with "no kernel image is available". vLLM's GitHub release ships a
    "+cu129" wheel; installed with torch from the cu126 index it keeps sm_70.
    Volta has no bf16, so serving needs --dtype half there.
  * CPU: the "+cpu" release wheel needs glibc >= 2.39 (Debian 13 / Ubuntu 24.04).
    Its default OpenMP binding (VLLM_CPU_OMP_THREADS_BIND=auto) left the model
    worker on ONE thread on an AVX2 host (4 tok/s on a 0.5B model); nobind +
    OMP_NUM_THREADS = cores-1 used them all (15 tok/s). bf16 has no native
    support without AVX-512 and adds a minutes-long compile to the first
    request after every start, so fp32 is the CPU default.

The script writes <venv>/vllm.env and <venv>/serve.sh; serve.sh sources the env
and appends $VLLM_SERVE_ARGS to `vllm serve`, so mode-specific settings live in
one file on the box.
"""
from __future__ import annotations

import json
import shlex
from typing import List, Optional

MODES = ("auto", "gpu", "cpu")
WHEEL_TAG = {"gpu": "cu129", "cpu": "cpu"}
TORCH_INDEX = {
    "gpu": "https://download.pytorch.org/whl/cu126",
    "cpu": "https://download.pytorch.org/whl/cpu",
}
CPU_MIN_GLIBC = "2.39"
DONE_MARKER = "VERA_PROVISION_DONE"


def normalise_mode(mode: str) -> str:
    m = (mode or "auto").strip().lower()
    if m not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    return m


def qpath(path: str) -> str:
    """Shell-quote a path, keeping a leading $HOME expandable."""
    if path.startswith("$HOME/"):
        return '"$HOME"/' + shlex.quote(path[len("$HOME/"):])
    return shlex.quote(path)


def system_deps_cmd(sudo: str = "") -> str:
    """apt step for what vLLM needs at RUN time: a C compiler (Triton builds its
    GPU launcher with gcc; torch.compile on CPU builds C++ kernels), curl for the
    install, and on CPU tcmalloc. A no-op on a box without apt-get."""
    s = sudo
    return ("if command -v apt-get >/dev/null 2>&1; then "
            f"{s}env DEBIAN_FRONTEND=noninteractive apt-get -qq update && "
            f"{s}env DEBIAN_FRONTEND=noninteractive apt-get -qq -y install "
            "curl ca-certificates build-essential >/dev/null; fi")


def cpu_threads(nproc: int) -> int:
    """OpenMP threads for the CPU worker: every core but one (the engine loop)."""
    return max(1, int(nproc) - 1)


def env_body(mode: str, *, max_model_len: int = 8192, gpu_util: float = 0.85,
             cpu_kv_gb: int = 2, nproc_expr: str = "$(nproc)",
             dtype_expr: str = "", cpu_reserve: int = 1, cpu_threads: int = 0) -> str:
    """The vllm.env body for a concrete mode ('gpu' | 'cpu').

    Box-dependent values (core count, the GPU's dtype flag) are shell
    expressions; the install script writes this through an unquoted heredoc,
    so they are expanded on the box when the file is written.

    cpu_reserve: on a dedicated box, cores left out of vLLM's OpenMP pool (1 =
    the engine loop).
    cpu_threads: a fixed thread count instead - on a fleet node, the same count
    its Ollama runners use (node_threads_core, LLAMA_ARG_THREADS), written as a
    DEFAULT so the unit's environment (nodes.backend.switch writes the node's own
    num_thread there) overrides it.
    """
    r = max(1, int(cpu_reserve))
    if int(cpu_threads or 0) > 0:
        # "\$" survives the install script's unquoted heredoc as a literal "$"
        omp = "OMP_NUM_THREADS=\\${OMP_NUM_THREADS:-%d}\n" % int(cpu_threads)
    else:
        omp = "OMP_NUM_THREADS=$(( %s > %d ? %s - %d : 1 ))\n" % (nproc_expr, r, nproc_expr, r)
    if mode == "gpu":
        return ("# vLLM (gpu). Volta (cc 7.x) has no bf16 -> --dtype half.\n"
                "VLLM_USE_FLASHINFER_SAMPLER=0\n"
                'VLLM_SERVE_ARGS="--gpu-memory-utilization %s --max-model-len %d%s"\n'
                % (float(gpu_util), int(max_model_len), dtype_expr))
    if mode == "cpu":
        return ("# vLLM (cpu). auto OMP binding ran the worker on one thread; nobind +\n"
                "# OMP_NUM_THREADS uses the cores. fp32: bf16 is emulated without AVX-512.\n"
                "VLLM_CPU_OMP_THREADS_BIND=nobind\n"
                + omp +
                "VLLM_CPU_KVCACHE_SPACE=%d\n"
                'VLLM_SERVE_ARGS="--dtype float32 --max-model-len %d"\n'
                # On a container that also has a GPU (a GPU node's CPU slot), Triton
                # picks its NVIDIA driver from libcuda and calls into the CPU-only
                # torch: "Torch not compiled with CUDA enabled" while loading the
                # model's linear-attention kernels (gpu-250-cpu, 5 Oct 2026).
                "TRITON_DEFAULT_BACKEND=cpu\n"
                % (int(cpu_kv_gb), int(max_model_len)))
    raise ValueError(f"env_body needs a concrete mode, not {mode!r}")


# The script is assembled from plain (non f-string) templates so shell braces,
# backslashes and quotes stay literal; only @NAME@ tokens are substituted.
_SCRIPT = r'''set -euo pipefail
VENV=@VENV@
MODE=@MODE@
VER=@VER@
SPEC=@SPEC@
export UV_HTTP_TIMEOUT=600 UV_HTTP_RETRIES=8
if [ "$MODE" = auto ]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then MODE=gpu; else MODE=cpu; fi
fi
echo "vllm install mode=$MODE"
WHEEL_DIR=@WHEEL_DIR@
if [ -z "$SPEC" ] && [ "$MODE" = cpu ] && [ -n "$WHEEL_DIR" ]; then
  # a wheel built for the fleet (vllm-build-cpu-wheel.sh: Debian 12 glibc, AVX2 int4)
  SPEC=$(ls -1 "$WHEEL_DIR"/vllm-*.whl 2>/dev/null | sort -V | tail -1 || true)
  [ -n "$SPEC" ] && echo "vllm wheel from $WHEEL_DIR"
fi
if [ -z "$SPEC" ] && [ "$MODE" = cpu ]; then
  GL=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0.0)
  if [ "$(printf '%s\n' @GLIBC@ "$GL" | sort -V | head -1)" != @GLIBC@ ]; then
    echo "the released vllm cpu wheel needs glibc >= @GLIBC@; this host has $GL - build one with vllm-build-cpu-wheel.sh into the store's vllm/ family" >&2
    exit 3
  fi
fi
UVD="${VENV%/}-uv"
UV="$UVD/uv"
# uv's receipt, cache and Pythons live beside the venv, not in $HOME: a node
# whose /root the container cannot write (an unprivileged CT restored with a
# host-owned /root) failed the install on the receipt (5 Oct 2026).
export XDG_CONFIG_HOME="$UVD/config" UV_CACHE_DIR="$UVD/cache" UV_PYTHON_INSTALL_DIR="$UVD/python"
if [ ! -x "$UV" ]; then
  mkdir -p "$UVD"
  curl -fsSL https://astral.sh/uv/install.sh -o "$UVD/install.sh"
  UV_INSTALL_DIR="$UVD" UV_NO_MODIFY_PATH=1 sh "$UVD/install.sh" >/dev/null
fi
[ -x "$VENV/bin/python" ] || "$UV" venv --python 3.12 "$VENV"
if [ "$MODE" = gpu ]; then TAG=@TAG_GPU@; IDX=@IDX_GPU@; else TAG=@TAG_CPU@; IDX=@IDX_CPU@; fi
if [ -z "$SPEC" ]; then
  if [ -n "$VER" ]; then REL="tags/v$VER"; else REL=latest; fi
  curl -fsSL -o "$UVD/release.json" "https://api.github.com/repos/vllm-project/vllm/releases/$REL"
  # asset URLs encode the '+' of the local version as %2B
  SPEC=$(grep -oE "https://[^\"]*/vllm-[0-9.]+(\+|%2B)$TAG-[^\"]*x86_64\.whl" "$UVD/release.json" | sed -n 1p || true)
  [ -n "$SPEC" ] || { echo "no vllm +$TAG x86_64 wheel in release $REL" >&2; exit 4; }
fi
echo "vllm wheel: $SPEC"
VIRTUAL_ENV="$VENV" "$UV" pip install "$SPEC" --extra-index-url "$IDX" --index-strategy unsafe-best-match
"$VENV/bin/python" - "$MODE" <<'PY'
import sys, torch, vllm
print("vllm", vllm.__version__, "torch", torch.__version__)
if sys.argv[1] == "gpu":
    assert torch.cuda.is_available(), "CUDA not available"
    cap = torch.cuda.get_device_capability()
    assert "sm_%d%d" % cap in torch.cuda.get_arch_list(), "torch has no kernels for this GPU"
    a = torch.randn(256, 256, device="cuda", dtype=torch.float16)
    (a @ a).sum().item()
    print("gpu ok", torch.cuda.get_device_name(), cap)
PY
if [ "$MODE" = gpu ]; then
  CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)
  DT=""
  case "$CC" in [0-7].*) DT=" --dtype half" ;; esac
  cat > "$VENV/vllm.env" <<EOF
@GPU_ENV@EOF
else
  cat > "$VENV/vllm.env" <<EOF
@CPU_ENV@EOF
fi
cat > "$VENV/serve.sh" <<'EOF'
#!/bin/sh
# usage: serve.sh <model> <port> [extra vllm args...] - sources vllm.env, then execs vllm serve
D=$(dirname "$0")
set -a; . "$D/vllm.env"; set +a
M=$1; P=$2; shift 2
exec "$D/bin/vllm" serve "$M" --host 0.0.0.0 --port "$P" $VLLM_SERVE_ARGS "$@"
EOF
chmod +x "$VENV/serve.sh"
"$UV" cache prune >/dev/null 2>&1 || true
echo @DONE@
'''


def install_script(venv: str, *, mode: str = "auto", version: str = "",
                   pip_spec: str = "", max_model_len: int = 8192,
                   gpu_util: float = 0.85, cpu_kv_gb: int = 2,
                   done_marker: str = DONE_MARKER, wheel_dir: str = "",
                   cpu_reserve: int = 1, cpu_threads: int = 0) -> str:
    """Bash that installs vLLM into `venv` for `mode` and writes vllm.env + serve.sh.

    mode      'auto' picks gpu when an NVIDIA device answers, else cpu.
    version   a vLLM release (e.g. '0.31.0'); blank = the latest release.
    pip_spec  an explicit requirement or wheel URL; skips the release lookup.
    wheel_dir (cpu) the newest vllm-*.whl in this directory wins over the release
              wheel - e.g. the shared store's vllm/ family, holding a wheel built
              for the fleet's Debian 12 nodes with the AVX2 int4 kernel.
    cpu_reserve / cpu_threads (cpu) vLLM's OpenMP thread count (see env_body).
    Prints `done_marker` last; any failure exits non-zero before it.
    """
    mode = normalise_mode(mode)

    def q(s: str) -> str:
        return shlex.quote(s) if s else "''"

    subs = {
        "@VENV@": qpath(venv) if venv else "''",
        "@MODE@": q(mode),
        "@VER@": q(version.strip()),
        "@SPEC@": q(pip_spec.strip()),
        "@WHEEL_DIR@": q(wheel_dir.strip()),
        "@GLIBC@": CPU_MIN_GLIBC,
        "@TAG_GPU@": WHEEL_TAG["gpu"], "@TAG_CPU@": WHEEL_TAG["cpu"],
        "@IDX_GPU@": TORCH_INDEX["gpu"], "@IDX_CPU@": TORCH_INDEX["cpu"],
        "@GPU_ENV@": env_body("gpu", max_model_len=max_model_len, gpu_util=gpu_util,
                              dtype_expr="$DT"),
        "@CPU_ENV@": env_body("cpu", max_model_len=max_model_len, cpu_kv_gb=cpu_kv_gb,
                              cpu_reserve=cpu_reserve, cpu_threads=cpu_threads),
        "@DONE@": q(done_marker),
    }
    out = _SCRIPT
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


def local_serve_argv(model: str, *, vllm_bin: str, port: int, host: str = "0.0.0.0",
                     tensor_parallel_size: int = 1, gpu_memory_utilization: float = 0.9,
                     dtype: str = "auto", cpu_offload_gb: float = 0, quantization: str = "",
                     max_model_len: int = 0, spec_model: str = "", spec_tokens: int = 5,
                     enable_lora: bool = False, max_loras: int = 4,
                     enable_prefix_caching: bool = True, enable_chunked_prefill: bool = True,
                     max_num_batched_tokens: int = 0, api_key: str = "",
                     served_model_name: str = "", extra_args: Optional[List[str]] = None) -> List[str]:
    """`vllm serve` as current vLLM takes it: the model is positional, and a
    draft model goes in --speculative-config (--speculative-model and
    --num-speculative-tokens are gone)."""
    argv = [vllm_bin, "serve", model, "--host", host, "--port", str(int(port)),
            "--tensor-parallel-size", str(int(tensor_parallel_size or 1)),
            "--gpu-memory-utilization", str(gpu_memory_utilization or 0.9),
            "--dtype", dtype or "auto"]
    if cpu_offload_gb and float(cpu_offload_gb) > 0:
        argv += ["--cpu-offload-gb", str(cpu_offload_gb)]
    if quantization:
        argv += ["--quantization", quantization]
    if max_model_len:
        argv += ["--max-model-len", str(int(max_model_len))]
    if spec_model:
        argv += ["--speculative-config", json.dumps(
            {"model": spec_model, "num_speculative_tokens": int(spec_tokens or 5)})]
    if enable_lora:
        argv += ["--enable-lora", "--max-loras", str(int(max_loras or 4))]
    if enable_prefix_caching:
        argv += ["--enable-prefix-caching"]
    if enable_chunked_prefill:
        argv += ["--enable-chunked-prefill"]
    if max_num_batched_tokens:
        argv += ["--max-num-batched-tokens", str(int(max_num_batched_tokens))]
    if api_key:
        argv += ["--api-key", api_key]
    if served_model_name:
        argv += ["--served-model-name", served_model_name]
    for a in extra_args or []:
        argv += shlex.split(str(a))
    return argv


def redact_argv(argv: List[str]) -> str:
    """The argv as one line for a log or an event, the API key masked."""
    out, hide = [], False
    for a in argv:
        out.append("***" if hide else a)
        hide = a == "--api-key"
    return " ".join(shlex.quote(a) for a in out)


def serve_command(venv: str, model: str, port: int, extra: str = "") -> str:
    """The command a unit or nohup launcher runs (serve.sh carries the mode's env)."""
    cmd = f"{qpath(venv.rstrip('/') + '/serve.sh')} {shlex.quote(model)} {int(port)}"
    return cmd + (f" {extra}" if extra else "")
