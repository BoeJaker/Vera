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

import shlex

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
             dtype_expr: str = "") -> str:
    """The vllm.env body for a concrete mode ('gpu' | 'cpu').

    Box-dependent values (core count, the GPU's dtype flag) are shell
    expressions; the install script writes this through an unquoted heredoc,
    so they are expanded on the box when the file is written.
    """
    if mode == "gpu":
        return ("# vLLM (gpu). Volta (cc 7.x) has no bf16 -> --dtype half.\n"
                "VLLM_USE_FLASHINFER_SAMPLER=0\n"
                'VLLM_SERVE_ARGS="--gpu-memory-utilization %s --max-model-len %d%s"\n'
                % (float(gpu_util), int(max_model_len), dtype_expr))
    if mode == "cpu":
        return ("# vLLM (cpu). auto OMP binding ran the worker on one thread; nobind +\n"
                "# OMP_NUM_THREADS uses the cores. fp32: bf16 is emulated without AVX-512.\n"
                "VLLM_CPU_OMP_THREADS_BIND=nobind\n"
                "OMP_NUM_THREADS=$(( %s > 1 ? %s - 1 : 1 ))\n"
                "VLLM_CPU_KVCACHE_SPACE=%d\n"
                'VLLM_SERVE_ARGS="--dtype float32 --max-model-len %d"\n'
                % (nproc_expr, nproc_expr, int(cpu_kv_gb), int(max_model_len)))
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
if [ "$MODE" = cpu ]; then
  GL=$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0.0)
  if [ "$(printf '%s\n' @GLIBC@ "$GL" | sort -V | head -1)" != @GLIBC@ ]; then
    echo "vllm cpu wheel needs glibc >= @GLIBC@; this host has $GL (use Debian 13 / Ubuntu 24.04)" >&2
    exit 3
  fi
fi
UVD="${VENV%/}-uv"
UV="$UVD/uv"
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
                   done_marker: str = DONE_MARKER) -> str:
    """Bash that installs vLLM into `venv` for `mode` and writes vllm.env + serve.sh.

    mode      'auto' picks gpu when an NVIDIA device answers, else cpu.
    version   a vLLM release (e.g. '0.31.0'); blank = the latest release.
    pip_spec  an explicit requirement or wheel URL; skips the release lookup.
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
        "@GLIBC@": CPU_MIN_GLIBC,
        "@TAG_GPU@": WHEEL_TAG["gpu"], "@TAG_CPU@": WHEEL_TAG["cpu"],
        "@IDX_GPU@": TORCH_INDEX["gpu"], "@IDX_CPU@": TORCH_INDEX["cpu"],
        "@GPU_ENV@": env_body("gpu", max_model_len=max_model_len, gpu_util=gpu_util,
                              dtype_expr="$DT"),
        "@CPU_ENV@": env_body("cpu", max_model_len=max_model_len, cpu_kv_gb=cpu_kv_gb),
        "@DONE@": q(done_marker),
    }
    out = _SCRIPT
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


def serve_command(venv: str, model: str, port: int, extra: str = "") -> str:
    """The command a unit or nohup launcher runs (serve.sh carries the mode's env)."""
    cmd = f"{qpath(venv.rstrip('/') + '/serve.sh')} {shlex.quote(model)} {int(port)}"
    return cmd + (f" {extra}" if extra else "")
