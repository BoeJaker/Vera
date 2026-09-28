"""Every node serves the same non-LLM models, from the shared store.

2026-09-28: the store held only the NLP set; SD 1.5 (three copies), IP-Adapter,
ControlNet, Whisper, Kokoro, Coqui and rembg lived in gpu-250's own caches, the
CPU nodes served Kokoro only, ran an older GPU_inference.py, and the
gpu_inference component deployed to a layout no node used - so the nodes had
been installed by hand and drifted.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "edge"))

import media_store_core as ms  # noqa: E402
import Vera.vera.provisioning.components_capabilities as components  # noqa: E402
from Vera.vera.provisioning.components_core import media_env_profile  # noqa: E402
from Vera.vera.catalog import specialist_core as sc  # noqa: E402

pytestmark = pytest.mark.critical

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ── where the media server finds a model ──────────────────────────────────────
def _store(tmp_path):
    s = tmp_path / "models"
    (s / "sd" / "runwayml__stable-diffusion-v1-5").mkdir(parents=True)
    (s / "sd" / "runwayml__stable-diffusion-v1-5" / "model_index.json").write_text("{}")
    (s / "sd" / "h94__IP-Adapter" / "models").mkdir(parents=True)
    (s / "sd" / "half__copied.part").mkdir(parents=True)       # a copy in flight
    (s / "whisper").mkdir()
    (s / "whisper" / "base.pt").write_bytes(b"x")
    (s / "tts" / "kokoro-v1.0").mkdir(parents=True)
    for f in ms.KOKORO_FILES:
        (s / "tts" / "kokoro-v1.0" / f).write_bytes(b"x")
    (s / "tts" / "coqui" / "tts" / "tts_models--en--ljspeech--tacotron2-DDC").mkdir(parents=True)
    (s / "rembg").mkdir()
    (s / "rembg" / "u2net.onnx").write_bytes(b"x")
    return str(s)


def test_a_model_in_the_store_loads_from_the_store(tmp_path):
    r = _store(tmp_path)
    assert ms.hf_source("runwayml/stable-diffusion-v1-5", "sd", r) == \
        os.path.join(r, "sd", "runwayml__stable-diffusion-v1-5")
    assert ms.hf_source("h94/IP-Adapter", "sd", r).endswith("h94__IP-Adapter")
    assert ms.whisper_root("base", r) == os.path.join(r, "whisper")
    assert ms.kokoro_dir(r).endswith("kokoro-v1.0")
    assert ms.coqui_home("tts_models/en/ljspeech/tacotron2-DDC", r) == os.path.join(r, "tts", "coqui")  # pragma: allowlist secret
    assert ms.rembg_home("u2net", r) == os.path.join(r, "rembg")


def test_a_model_the_store_lacks_keeps_its_old_source(tmp_path):
    # never "download into the store": it is read-only on every serving node
    r = _store(tmp_path)
    assert ms.hf_source("thibaud/controlnet-openpose-sdxl-1.0", "sd", r) == \
        "thibaud/controlnet-openpose-sdxl-1.0"
    assert ms.hf_source("half/copied", "sd", r) == "half/copied"
    assert ms.whisper_root("large-v3", r) is None
    assert ms.rembg_home("isnet-anime", r) is None
    # an operator's absolute path is theirs
    assert ms.hf_source("/data/my-sd", "sd", r) == "/data/my-sd"
    # no store mounted at all: everything as before
    assert ms.resolve_all({"sd": "a/b", "whisper": "base"}, str(tmp_path / "nope")) == {
        "store": None, "sd": None, "controlnet": None, "ipadapter": None, "whisper": None,
        "kokoro": None, "coqui": None, "rembg": None}


def test_the_server_resolves_every_load_through_the_store():
    src = open(os.path.join(_ROOT, "edge", "GPU_inference.py"), encoding="utf-8").read()
    assert "SDPipeline.from_pretrained(SD_MODEL_ID" not in src
    assert "P.from_pretrained(\n        SD_MODEL_ID" not in src
    assert "ControlNetModel.from_pretrained(cn_id," not in src
    assert "load_ip_adapter(IPADAPTER_REPO" not in src
    assert src.count("download_root=_whisper_root()") == 2
    assert '"model_store":      _store_report()' in src


# ── the node profile ──────────────────────────────────────────────────────────
def test_every_node_serves_every_model_on_its_own_device():
    for gpu, dev in ((True, "cuda"), (False, "cpu")):
        env = dict(ln.split("=", 1) for ln in media_env_profile(gpu).splitlines()
                   if ln and not ln.startswith("#"))
        assert env["ENABLE_WHISPER"] == env["ENABLE_TTS"] == env["ENABLE_SD"] == "1"
        assert env["SD_DEVICE"] == dev
        assert env["VERA_MODEL_STORE"] == "/opt/vera-store/models"
        assert env["SERVER_PORT"] == "8765"


# ── the store mount ───────────────────────────────────────────────────────────
def test_store_mount_plan():
    cfg = ("cores: 12\nmp0: /tank_sdh/vera-store/models/ollama,mp=/root/.ollama/models,ro=1\n"
           "mp1: /tank_sdh/vera-store/models/nlp,mp=/opt/nlp-models,ro=1\n")
    p = sc.store_mount_plan(cfg)
    assert p["state"] == "missing" and p["key"] == "mp2"
    assert p["cmd"] == "-mp2 /tank_sdh/vera-store/models,mp=/opt/vera-store/models,ro=1"
    done = cfg + "mp2: /tank_sdh/vera-store/models,mp=/opt/vera-store/models,ro=1\n"
    assert sc.store_mount_plan(done)["state"] == "mounted"
    # a writable mount on a serving node is reported, never "fixed" silently
    rw = cfg + "mp5: /tank_sdh/vera-store/models,mp=/opt/vera-store/models\n"
    assert sc.store_mount_plan(rw)["state"] == "writable"
    # gaps are reused, existing keys never
    assert sc.store_mount_plan("mp0: a,mp=/x\nmp2: b,mp=/y\n")["key"] == "mp1"


# ── deploying the media server into the layout the nodes run ─────────────────
async def _async(v):
    return v


@pytest.mark.asyncio
async def test_media_server_deploys_into_its_real_layout(monkeypatch):
    calls = []

    async def host(_hid):
        return {"id": "h", "host": "192.0.2.10", "user": "root"}

    async def ssh(_hid, cmd, timeout=0, **_kw):
        calls.append(cmd)
        if "VERA_PRECHECK_OK" in cmd:
            return {"ok": True, "stdout": "VERA_PRECHECK_OK", "stderr": ""}
        if "VERA_HEALTH" in cmd:
            return {"ok": True, "stdout": 'VERA_HEALTH {"status": "ok", "model_store": {"sd": "/x"}}',
                    "stderr": ""}
        return {"ok": True, "stdout": "VERA_DEPS_DONE\nVERA_OPTIONAL_FAILED=realesrgan>=0.3.0\n"
                                      "VERA_LAUNCHED service=gpu-inference", "stderr": ""}

    streamed = []

    async def runner(_hid, cmd, timeout=0, input=None):
        streamed.append(cmd)
        return {"ok": True, "stdout": "VERA_PUSHED", "stderr": ""}

    monkeypatch.setattr(components, "_host_rec", host)
    monkeypatch.setattr(components, "_ssh", ssh)
    monkeypatch.setattr(components, "_ssh_stored_with_input", lambda: runner)
    monkeypatch.setattr(components, "_node_has_gpu", lambda _a: False)
    monkeypatch.setattr(components, "observe_infrastructure_effect", lambda **k: {"mode": k["mode"]})
    monkeypatch.setattr(components, "emit_event", lambda *_a: _async(None))

    res = await components.cap_deploy.__wrapped__(host_id="h", component="gpu_inference",
                                                  install_deps=True, launch=True)
    assert res["ok"], res
    allc = "\n".join(calls)
    d = "/home/Servers/StableDiffustionWhisper"
    # the 144 KB server goes over stdin, the small core inline
    assert any(f"> {d}/GPU_inference.py.part" in c for c in streamed)
    assert f"> {d}/media_store_core.py" in allc
    assert f"chmod 755 {d}/start.sh" in allc
    assert "~/.vera/edge" not in allc and "$HOME/.vera/edge" not in allc
    # the node's existing venv is kept; a CPU node gets the CPU torch build
    assert f'[ -x "{d}/env/bin/python" ] || python3 -m venv "{d}/env"' in allc
    assert "https://download.pytorch.org/whl/cpu" in allc and "cu121" not in allc
    # an optional tier failing is reported, not fatal
    assert res["optional_failed"] == ["realesrgan>=0.3.0"]
    # the profile, the unit, a restart (not enable --now), and a health wait
    assert "/etc/default/vera-inference.pre-vera" in allc
    assert "/etc/systemd/system/gpu-inference.service" in allc
    assert "systemctl restart gpu-inference" in allc and "enable --now" not in allc
    assert res["health"]["model_store"] == {"sd": "/x"}
    assert res["version"].startswith("code-")


@pytest.mark.asyncio
async def test_a_file_too_big_for_a_command_line_goes_over_stdin(monkeypatch):
    # GPU_inference.py is 144 KB; inline as base64 it broke the 128 KB limit on
    # one argument: "/bin/bash: Argument list too long" (cpu-247, 2026-09-28)
    cmds, streamed = [], []

    async def host(_hid):
        return {"id": "h", "host": "192.0.2.10", "user": "root"}

    async def ssh(_hid, cmd, timeout=0, **_kw):
        cmds.append(cmd)
        return {"ok": True, "stdout": "VERA_PRECHECK_OK VERA_DEPS_DONE", "stderr": ""}

    async def runner(_hid, cmd, timeout=0, input=None):
        streamed.append((cmd, len(input or "")))
        return {"ok": True, "stdout": "VERA_PUSHED", "stderr": ""}

    big = b"x" * (144 * 1024)
    monkeypatch.setattr(components, "_host_rec", host)
    monkeypatch.setattr(components, "_ssh", ssh)
    monkeypatch.setattr(components, "_ssh_stored_with_input", lambda: runner)
    monkeypatch.setattr(components, "_read_local",
                        lambda rel: big if rel.endswith("GPU_inference.py") else b"small")
    monkeypatch.setattr(components, "_node_has_gpu", lambda _a: True)
    monkeypatch.setattr(components, "observe_infrastructure_effect", lambda **k: {"mode": k["mode"]})
    monkeypatch.setattr(components, "emit_event", lambda *_a: _async(None))

    res = await components.cap_deploy.__wrapped__(host_id="h", component="gpu_inference",
                                                  install_deps=False, launch=False)
    assert res["ok"], res
    assert len(streamed) == 1 and streamed[0][0].endswith("VERA_PUSHED")
    assert "GPU_inference.py" in streamed[0][0]
    assert streamed[0][1] > 128 * 1024           # the payload went on stdin...
    assert max(len(c) for c in cmds) < 100 * 1024  # ...and no command carries it


def test_requirements_never_downgrade_what_the_store_models_need():
    # numpy<2 made pip downgrade numpy and kokoro-onnx with it on cpu-247;
    # kokoro-onnx 0.3.3 cannot read voices-v1.0.bin, so TTS fell back to Coqui
    reqs = {}
    for ln in open(os.path.join(_ROOT, "edge", "requirements.txt"), encoding="utf-8"):
        ln = ln.split("#", 1)[0].strip()
        if ln:
            name = ln.split(">", 1)[0].split("<", 1)[0].split("=", 1)[0].strip().lower()
            reqs[name] = ln
    assert "<" not in reqs["numpy"], reqs["numpy"]
    floor = reqs["kokoro-onnx"].split(">=", 1)[1].split(",", 1)[0]
    assert tuple(int(x) for x in floor.split(".")) >= (0, 3, 4), reqs["kokoro-onnx"]


def test_the_media_server_version_covers_its_unit_and_core():
    comp = components._COMPONENTS["gpu_inference"]
    files = components._shipped_files(comp)
    names = [n for n, _c in files]
    assert {"GPU_inference.py", "media_store_core.py", "start.sh", "<unit>", "<deps>"} <= set(names)
    assert comp["sync"] and comp["service"] == "gpu-inference"
