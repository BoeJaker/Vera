"""Firmware bake + Arduino build path.

The panel configures a node ONCE (board pin map, display, SD, Wi-Fi, whether to
reclaim the USB pins) and every delivery route — source download, MicroPython
REPL push, and the server-side Arduino compile — is supposed to apply the same
options. These tests pin that contract, including the anchors the baker rewrites,
because a rename in a firmware file would otherwise make baking silently no-op
and ship a .bin with the wrong pin map.
"""

import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FW = os.path.join(_ROOT, "vera", "mesh", "firmware")
_INO = os.path.join(_FW, "arduino", "vera_mesh_node.ino")
_MPY = os.path.join(_FW, "micropython", "main.py")


@pytest.fixture(scope="module")
def mesh():
    """mesh_capabilities imports cleanly without the runtime (it guards), which is
    what lets the pure bake helpers be tested anywhere."""
    try:
        from Vera.vera.mesh import mesh_capabilities as m
        return m
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"mesh_capabilities unavailable: {e}")


class _StubBoards:
    """Stands in for mesh_boards_capabilities so pin-map baking is testable
    without pulling in the orchestrator."""
    PROFILE = {
        "id": "test-s3", "chip": "esp32s3",
        "io": {"tft": {"rst": 5, "cs": 6, "dc": 7, "wr": 1, "rd": 2,
                       "d": [21, 46, 18, 17, 19, 20, 3, 14]},
               "sd": {"clk": 12, "miso": 13, "mosi": 11, "cs": 10},
               "neopixel": 48},
    }

    @classmethod
    def board_io(cls, bid):
        return cls.PROFILE["io"] if bid == "test-s3" else None

    @classmethod
    def get_board(cls, bid):
        return cls.PROFILE if bid == "test-s3" else None


@pytest.fixture
def stub_boards(mesh, monkeypatch):
    monkeypatch.setattr(mesh, "_boards_mod", lambda: _StubBoards)
    return _StubBoards


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── The anchors the baker rewrites must exist in the real firmware ──────────

def test_arduino_sketch_has_bakeable_anchors():
    src = _read(_INO)
    for macro in ("USE_TFT_ILI9488_P8", "USE_SD_SPI", "TOOLKIT_CSI", "TFT_FREE_USB_PINS",
                  "TFT_P8_RST", "TFT_P8_WR", "TFT_P8_D4", "TFT_P8_D7", "SD_PIN_CLK"):
        assert re.search(r"(?m)^#define\s+%s\s+-?\d+" % macro, src), f"no bakeable #define {macro}"
    assert re.search(r"(?m)^int\s+neoPin\s*=\s*-?\d+", src)
    # The sketch must expose credential globals to bake into. Without them a
    # flashed-with-erase node has no NVS and no creds, so it never joins Wi-Fi.
    for name in ("WIFI_SSID", "WIFI_PASS"):
        assert re.search(r'(?m)^String\s+%s\s*=\s*"[^"]*"' % name, src), f"no bakeable {name}"


def test_micropython_has_bakeable_anchors():
    src = _read(_MPY)
    for name in ("TFT_ENABLED", "SD_ENABLED", "TFT_FREE_USB_PINS"):
        assert re.search(r"(?m)^%s\s*=\s*(?:True|False)" % name, src), f"no bakeable flag {name}"
    assert re.search(r"TFT_PINS\s*=\s*\{.*?\}", src, re.S)
    assert re.search(r"SD_PINS\s*=\s*\{.*?\}", src, re.S)
    assert re.search(r'(?m)^WIFI_SSID\s*=\s*"[^"]*"', src)


# ── Toggles ────────────────────────────────────────────────────────────────

def test_bake_no_params_is_a_no_op(mesh):
    src = _read(_INO)
    assert mesh._bake_firmware_options(src, "arduino", {}) == src
    mp = _read(_MPY)
    assert mesh._bake_firmware_options(mp, "micropython", {}) == mp


@pytest.mark.parametrize("on", [True, False])
def test_bake_arduino_toggles(mesh, on):
    out = mesh._bake_firmware_options(
        _read(_INO), "arduino",
        {"display": on, "sd": on, "csi": on, "usb_pins": on})
    want = "1" if on else "0"
    for macro in ("USE_TFT_ILI9488_P8", "USE_SD_SPI", "TOOLKIT_CSI", "TFT_FREE_USB_PINS"):
        assert re.search(r"(?m)^#define\s+%s\s+%s(?!\d)" % (macro, want), out), f"{macro} not baked to {want}"


@pytest.mark.parametrize("on", [True, False])
def test_bake_micropython_toggles(mesh, on):
    out = mesh._bake_firmware_options(
        _read(_MPY), "micropython", {"display": on, "sd": on, "usb_pins": on})
    want = "True" if on else "False"
    for name in ("TFT_ENABLED", "SD_ENABLED", "TFT_FREE_USB_PINS"):
        assert re.search(r"(?m)^%s\s*=\s*%s\b" % (name, want), out), f"{name} not baked to {want}"


def test_usb_pins_is_the_switch_that_frees_gpio_19_20(mesh):
    """The whole point of the option: MicroPython only drives an S3-Uno TFT when
    it is allowed to take GPIO19/20 off the USB PHY."""
    off = mesh._bake_firmware_options(_read(_MPY), "micropython", {"usb_pins": False})
    on = mesh._bake_firmware_options(_read(_MPY), "micropython", {"usb_pins": True})
    assert re.search(r"(?m)^TFT_FREE_USB_PINS\s*=\s*False", off)
    assert re.search(r"(?m)^TFT_FREE_USB_PINS\s*=\s*True", on)
    # and the firmware must default to OFF — it costs you the USB REPL
    assert re.search(r"(?m)^TFT_FREE_USB_PINS\s*=\s*False", _read(_MPY))


def test_bake_wifi_credentials(mesh):
    out = mesh._bake_firmware_options(
        _read(_MPY), "micropython", {"wifi_ssid": 'my"net', "wifi_pass": "p@ss"})
    assert re.search(r'(?m)^WIFI_SSID\s*=\s*"my\\"net"', out)
    assert re.search(r'(?m)^WIFI_PASS\s*=\s*"p@ss"', out)


@pytest.mark.parametrize("flavor,path,prefix", [
    ("micropython", _MPY, ""),
    ("arduino", _INO, "String "),
])
def test_wifi_bake_reaches_every_flavour(mesh, flavor, path, prefix):
    """Regression: the Arduino branch used to ignore wifi_ssid/wifi_pass entirely,
    so ticking Wi-Fi in the panel silently produced a node that never joined."""
    out = mesh._bake_firmware_options(
        _read(path), flavor, {"wifi_ssid": "HomeNet", "wifi_pass": "s3cret"})
    assert re.search(r'(?m)^%sWIFI_SSID\s*=\s*"HomeNet"' % prefix, out), f"{flavor}: ssid not baked"
    assert re.search(r'(?m)^%sWIFI_PASS\s*=\s*"s3cret"' % prefix, out), f"{flavor}: pass not baked"


# ── Board pin maps ─────────────────────────────────────────────────────────

def test_bake_arduino_board_pin_map(mesh, stub_boards):
    out = mesh._bake_firmware_options(_read(_INO), "arduino", {"board": "test-s3"})
    for macro, val in (("TFT_P8_RST", 5), ("TFT_P8_CS", 6), ("TFT_P8_DC", 7),
                       ("TFT_P8_WR", 1), ("TFT_P8_RD", 2), ("TFT_P8_D0", 21),
                       ("TFT_P8_D4", 19), ("TFT_P8_D7", 14), ("SD_PIN_CLK", 12)):
        assert re.search(r"(?m)^#define\s+%s\s+%d(?!\d)" % (macro, val), out), f"{macro} != {val}"
    assert re.search(r"(?m)^int\s+neoPin\s*=\s*48", out)


def test_bake_micropython_board_pin_map(mesh, stub_boards):
    out = mesh._bake_firmware_options(_read(_MPY), "micropython", {"board": "test-s3"})
    m = re.search(r"TFT_PINS = (\{.*?\})", out, re.S)
    assert m and '"d": [21, 46, 18, 17, 19, 20, 3, 14]' in m.group(1)
    assert re.search(r'SD_PINS = \{"clk": 12, "miso": 13, "mosi": 11, "cs": 10\}', out)


def test_unknown_board_leaves_pins_alone(mesh, stub_boards):
    src = _read(_INO)
    assert mesh._bake_firmware_options(src, "arduino", {"board": "nope"}) == src


# ── FQBN selection (needs the capability runtime) ───────────────────────────

def test_fqbn_follows_the_board_chip(mesh, stub_boards):
    if not getattr(mesh, "_CAP_AVAILABLE", False) or not hasattr(mesh, "_fqbn_for_board"):
        pytest.skip("capability runtime unavailable")
    fqbn = mesh._fqbn_for_board("test-s3")
    assert fqbn.startswith("esp32:esp32:esp32s3:"), fqbn
    # USB CDC must be OFF or GPIO19/20 stay owned by USB and the display is dead.
    assert "CDCOnBoot=default" in fqbn, fqbn
    # …and the app must fit: the default 1.3MB slot was already 90% full.
    assert "PartitionScheme=min_spiffs" in fqbn, fqbn
    assert mesh._fqbn_for_board("nope") == ""


def test_builder_candidates_cover_native_and_in_stack(monkeypatch):
    try:
        from Vera.vera.build import build_capabilities as b
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"build_capabilities unavailable: {e}")
    monkeypatch.delenv("VERA_BUILDER_URL", raising=False)
    cands = b.builder_candidates()
    # A native orchestrator can't resolve the compose DNS name, so a published
    # port must be tried too — this is what made can_build wrongly false.
    assert any("localhost" in c or "127.0.0.1" in c for c in cands)
    assert any("vera-builder" in c for c in cands)
    monkeypatch.setenv("VERA_BUILDER_URL", "http://elsewhere:9999/")
    assert b.builder_candidates() == ["http://elsewhere:9999"]


# ── Auto-OTA target selection ──────────────────────────────────────────────
# Picking the wrong artifact here pushes a MicroPython script at an Arduino node
# (or an image built for another chip), so the selection rules are pinned.

@pytest.fixture
def mesh_ui():
    try:
        from Vera.vera.mesh import mesh_ui_capabilities as m
        return m
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"mesh_ui_capabilities unavailable: {e}")


def test_runtime_detection(mesh_ui):
    assert mesh_ui.node_runtime("arduino", "") == "arduino"
    assert mesh_ui.node_runtime("micropython", "") == "micropython"
    # nodes flashed before `runtime` was reported fall back to the version suffix
    assert mesh_ui.node_runtime("", "1.3.0-mpy") == "micropython"
    assert mesh_ui.node_runtime("", "1.3.0-ino") == "arduino"
    # anything unrecognised must stay unknown so nothing gets pushed at it
    assert mesh_ui.node_runtime("", "1.1") == ""
    assert mesh_ui.node_runtime("", "") == ""


def test_served_fw_version_per_flavour(mesh_ui):
    mpy, ino = mesh_ui.served_fw_version("micropython"), mesh_ui.served_fw_version("arduino")
    assert mpy and ino, "both firmwares must declare FW_VERSION"
    assert mpy != ino, "each runtime needs its own version or they cross-trigger OTA"


def test_auto_ota_defaults_on_and_is_opt_out(mesh_ui):
    assert mesh_ui._auto_ota_enabled({}) is True          # no config at all
    assert mesh_ui._auto_ota_enabled({"ota": {}}) is True
    assert mesh_ui._auto_ota_enabled({"ota": {"auto": True}}) is True
    for off in (False, "false", "0", "off", "no"):
        assert mesh_ui._auto_ota_enabled({"ota": {"auto": off}}) is False, off


def test_arduino_bin_picker_ignores_foreign_and_mismatched_images(mesh_ui, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(mesh_ui, "_FW_BIN_DIR", str(tmp_path))

    def _bin(name, meta=None):
        (tmp_path / name).write_bytes(b"\xe9\x00")
        if meta is not None:
            (tmp_path / (name + ".json")).write_text(_json.dumps(meta))

    _bin("uploaded-by-hand.bin")                                   # no sidecar
    _bin("mpy.bin", {"runtime": "micropython", "chip": "esp32s3"})
    _bin("other-chip.bin", {"runtime": "arduino", "chip": "esp32c3", "fw_version": "9"})
    assert mesh_ui.newest_arduino_bin("esp32s3") is None, "must not pick a foreign/mismatched image"

    _bin("ours.bin", {"runtime": "arduino", "chip": "esp32s3", "fw_version": "1.4.0-ino"})
    got = mesh_ui.newest_arduino_bin("esp32s3")
    assert got and got["name"] == "ours.bin" and got["fw_version"] == "1.4.0-ino"
    # the node reports its chip as ESP.getChipModel() — that must match the sidecar
    assert mesh_ui.newest_arduino_bin("ESP32-S3")["name"] == "ours.bin"
    # with the chip unknown and two chips on offer, guessing would flash the wrong
    # image — refuse instead
    assert mesh_ui.newest_arduino_bin("") is None


def test_chip_normalisation(mesh_ui):
    for raw in ("ESP32-S3", "esp32s3", "ESP32S3 module with ESP32S3"):
        assert mesh_ui.normalise_chip(raw) == "esp32s3", raw
    assert mesh_ui.normalise_chip("ESP32-C3") == "esp32c3"
    assert mesh_ui.normalise_chip("ESP32") == "esp32"
    assert mesh_ui.normalise_chip("") == ""


# ── Server-driven UI parity ────────────────────────────────────────────────
# Vera pushes one widget schema to every display node. If a firmware quietly
# stops handling a widget or job type, screens render half-blank on that runtime
# only — which is exactly the kind of drift nobody notices until a node is on a
# wall. Pin both sides to the same contract.

_UI_WIDGETS = ("label", "rect", "hline", "button", "bar")
_UI_JOBS = ("ui_screen", "ui_clear", "touch_raw", "touch_cal")


def test_both_firmwares_render_every_widget_type():
    ino, mpy = _read(_INO), _read(_MPY)
    for w in _UI_WIDGETS:
        assert '"%s"' % w in ino, f"arduino firmware cannot render widget {w!r}"
        assert '"%s"' % w in mpy, f"micropython firmware cannot render widget {w!r}"


def test_both_firmwares_handle_every_ui_job():
    ino, mpy = _read(_INO), _read(_MPY)
    for j in _UI_JOBS:
        assert '"%s"' % j in ino, f"arduino firmware ignores job {j!r}"
        assert '"%s"' % j in mpy, f"micropython firmware ignores job {j!r}"


def test_both_firmwares_advertise_the_ui_module():
    # mesh.ui.* only targets nodes that say they can render.
    assert 'mods.add("ui")' in _read(_INO)
    assert '"ui"' in _read(_MPY)


def test_both_firmwares_report_touches_to_the_same_endpoint():
    ino, mpy = _read(_INO), _read(_MPY)
    for src, name in ((ino, "arduino"), (mpy, "micropython")):
        assert "/mesh/ui/event" in src, f"{name} never reports touches"
        assert '"ui_event"' in src, f"{name} uses the wrong event kind"


# ── Build progress reporting ───────────────────────────────────────────────
# A compile is ~90s and a first image build ~10min. Without a progress record the
# UI can only grey out a button, which is indistinguishable from a hang.

@pytest.fixture
def build_caps():
    try:
        from Vera.vera.build import build_capabilities as b
        return b
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"build_capabilities unavailable: {e}")


def test_job_lifecycle(build_caps):
    b = build_caps
    jid = b.job_start("test", "a label")
    v = b.job_view(jid)
    assert v["phase"] == "starting" and v["done"] is False and v["pct"] == 0
    b.job_phase(jid, "compiling", 40)
    b.job_log(jid, "hello")
    v = b.job_view(jid)
    assert v["phase"] == "compiling" and v["pct"] == 40 and v["log"] == ["hello"]
    assert v["elapsed_s"] >= 0
    b.job_done(jid, True, result={"name": "x.bin"})
    v = b.job_view(jid)
    assert v["done"] and v["ok"] and v["pct"] == 100 and v["result"]["name"] == "x.bin"


def test_docker_step_lines_become_a_percentage(build_caps):
    b = build_caps
    jid = b.job_start("test")
    b.job_log(jid, "Step 3/12 : RUN apt-get update")
    assert b.job_view(jid)["pct"] == 25
    b.job_log(jid, "Step 9/12 : RUN arduino-cli core install esp32:esp32")
    assert b.job_view(jid)["pct"] == 75
    # progress must never go backwards on unrelated chatter
    b.job_log(jid, "Removing intermediate container abc123")
    assert b.job_view(jid)["pct"] == 75
    # and never hits 100 until the job is actually marked done
    b.job_log(jid, "Step 12/12 : CMD [\"uvicorn\"]")
    assert b.job_view(jid)["pct"] == 99


def test_job_log_is_bounded_and_tailed(build_caps):
    b = build_caps
    jid = b.job_start("test")
    for i in range(b._JOB_LOG_MAX + 250):
        b.job_log(jid, f"line {i}")
    full = b.job_view(jid, tail=10_000)["log"]
    assert len(full) == b._JOB_LOG_MAX, "log must stay bounded — builds are chatty"
    assert full[-1] == f"line {b._JOB_LOG_MAX + 249}", "must keep the NEWEST lines"
    assert b.job_view(jid, tail=5)["log"] == full[-5:]


def test_unknown_job_reports_cleanly(build_caps):
    assert "error" in build_caps.job_view("nope-1234")


def test_failed_job_keeps_its_error(build_caps):
    b = build_caps
    jid = b.job_start("test")
    b.job_phase(jid, "building", 30)
    b.job_done(jid, False, error="docker build failed (rc=1)")
    v = b.job_view(jid)
    assert v["done"] and v["ok"] is False and "rc=1" in v["error"]
    assert v["pct"] == 30, "a failure should not claim 100%"


def test_run_streaming_feeds_the_log_as_output_appears(build_caps):
    """The whole point of streaming: lines must land in the job while the command
    is still running, not be dumped at the end."""
    import asyncio
    import sys
    b = build_caps
    jid = b.job_start("test")
    script = ("import sys,time\n"
              "for i in range(5):\n"
              "    print('tick %d' % i, flush=True)\n"
              "    time.sleep(0.15)\n")

    seen_midway = {}

    async def _watch():
        await asyncio.sleep(0.35)
        seen_midway["n"] = len(b.job_view(jid, tail=100)["log"])

    async def _go():
        return await asyncio.gather(
            b.run_streaming(jid, [sys.executable, "-c", script], timeout=30), _watch())

    rc, _ = asyncio.run(_go())
    assert rc == 0
    log = b.job_view(jid, tail=100)["log"]
    assert [l for l in log if l.startswith("tick")] == ["tick %d" % i for i in range(5)]
    assert 0 < seen_midway.get("n", 0) < 5, (
        "log should have been partially filled mid-run, got %r" % seen_midway)


def test_run_streaming_reports_a_missing_binary(build_caps):
    import asyncio
    b = build_caps
    jid = b.job_start("test")
    rc = asyncio.run(b.run_streaming(jid, ["definitely-not-a-real-binary-xyz"], timeout=10))
    assert rc == -1
    assert any("could not start" in l for l in b.job_view(jid)["log"])


def test_builder_up_forwards_the_container_name(build_caps, monkeypatch):
    """Regression: the capability-dispatch helper took the cap name as `name`,
    which collided with docker.run's container `name` kwarg and failed the whole
    bring-up with "got multiple values for argument 'name'"."""
    import asyncio
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY
    b = build_caps
    calls = {}

    async def _fake_rm(**kw):
        calls["rm"] = kw
        return {"ok": True}

    async def _fake_run(**kw):
        calls["run"] = kw
        return {"ok": True, "container_id": "abc123"}

    monkeypatch.setitem(CAPABILITY_REGISTRY, "docker.rm", {"func": _fake_rm})
    monkeypatch.setitem(CAPABILITY_REGISTRY, "docker.run", {"func": _fake_run})

    class _Proc:                       # `docker images -q` → image already present
        returncode = 0

        async def communicate(self):
            return (b"sha256:deadbeef\n", b"")

    async def _fake_exec(*a, **k):
        return _Proc()

    async def _fake_resolve(force=False):
        return "http://localhost:8785"

    async def _fake_health(path, timeout=15.0):
        return {"tools": {"arduino_cli": True}}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(b, "resolve_builder_url", _fake_resolve)
    monkeypatch.setattr(b, "builder_get", _fake_health)

    jid = b.job_start("test")
    res = asyncio.run(b._builder_up_job(jid, 8785, False, "", 60))

    assert res.get("ok"), res
    assert calls["rm"]["container"] == "vera-builder"
    assert calls["run"]["name"] == "vera-builder", "container name must reach docker.run"
    assert calls["run"]["ports"] == "8785:8080"
    assert "/opt/arduino" in calls["run"]["volumes"]
    assert b.job_view(jid)["done"] and b.job_view(jid)["ok"]


# ── Firmware stack safety ──────────────────────────────────────────────────
# StaticJsonDocument lives on the STACK. The job path nests four of them
# (poll -> runJob -> toolkitJob -> sendResult) and then calls HTTPClient, which
# overflowed the 8KB default loopTask stack and reset the board on every job.

def test_job_path_json_buffers_are_not_on_the_stack():
    src = _read(_INO)
    hot = ("d(16384)", "res(1536)", "res(3072)", "d(3072)")
    for buf in hot:
        assert "DynamicJsonDocument " + buf in src, f"{buf} must be heap-allocated"
    big = [int(m) for m in re.findall(r"StaticJsonDocument<(\d+)>", src)]
    assert not [n for n in big if n > 1024], (
        "no StaticJsonDocument on the job path may exceed 1KB of stack; found %r" % big)


def test_loop_task_stack_is_enlarged():
    src = _read(_INO)
    m = re.search(r"SET_LOOP_TASK_STACK_SIZE\(\s*(\d+)\s*\*\s*1024\s*\)", src)
    assert m, "the sketch must request a bigger loopTask stack"
    assert int(m.group(1)) >= 16, "8KB was not enough for the job path"


def test_reset_cause_is_reported():
    """With USB-CDC off there is no console, so a silent reboot is invisible
    unless the node says why it rebooted."""
    src = _read(_INO)
    assert "esp_reset_reason()" in src
    for cause in ("PANIC", "BROWNOUT", "task watchdog"):
        assert cause in src, f"reset cause {cause!r} not surfaced"
    assert 'd["reset_reason"]=bootReason' in src, "hello must carry the reset cause"
    assert 'res["reset_reason"]=bootReason' in src, "sysinfo must carry the reset cause"


def test_module_lookups_prefer_the_already_loaded_copy(mesh, monkeypatch):
    """Vera loads capability modules under a BARE name; a package import creates a
    SECOND module object with its own globals. A build job written to one copy's
    registry is invisible to build.progress reading the other's — which showed up
    as 'unknown job' right after a build was queued."""
    import sys as _sys
    import types
    # _builder_mod only exists once the capability runtime imported; _boards_mod
    # is module-level, so the check still bites in a bare environment.
    finders = [(mesh._boards_mod, "mesh_boards_capabilities")]
    if hasattr(mesh, "_builder_mod"):
        finders.append((mesh._builder_mod, "build_capabilities"))
    for finder, bare in finders:
        sentinel = types.ModuleType(bare)
        sentinel.__vera_test_marker__ = True
        monkeypatch.setitem(_sys.modules, bare, sentinel)
        assert finder() is sentinel, (
            f"{bare}: must reuse the loaded module, not re-import a second copy")


# ── V565 image wire format ─────────────────────────────────────────────────
# The node decodes nothing: it reads an 8-byte header then blits raw big-endian
# RGB565 rows. Byte order or geometry being wrong shows up as a garbled panel,
# which is miserable to debug on hardware — so pin the format here.

@pytest.fixture
def ui_mod():
    try:
        from Vera.vera.mesh import mesh_ui_capabilities as m
        # Pads point at capabilities living in sibling mesh modules; import them
        # so the name check judges the real registry rather than a partial one.
        for _sib in ("mesh_boards_capabilities", "mesh_toolkit_capabilities"):
            try:
                __import__(f"Vera.vera.mesh.{_sib}")
            except Exception:
                pass
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"mesh_ui_capabilities unavailable: {e}")
    if not m.pil_available():
        pytest.skip("Pillow not installed")
    return m


def _decode_header(blob):
    assert blob[:4] == b"V565", "magic must match the firmware's check"
    w = (blob[4] << 8) | blob[5]
    h = (blob[6] << 8) | blob[7]
    return w, h


def test_v565_header_and_length(ui_mod):
    from PIL import Image
    blob = ui_mod.encode_v565(Image.new("RGB", (32, 16), (0, 0, 0)), 480, 320)
    w, h = _decode_header(blob)
    assert (w, h) == (480, 320)
    assert len(blob) == 8 + 480 * 320 * 2, "must be exactly one RGB565 pixel per cell"


@pytest.mark.parametrize("rgb,expected", [
    ((255, 0, 0), 0xF800),          # red
    ((0, 255, 0), 0x07E0),          # green
    ((0, 0, 255), 0x001F),          # blue
    ((255, 255, 255), 0xFFFF),
    ((0, 0, 0), 0x0000),
])
def test_v565_pixel_encoding_is_big_endian_rgb565(ui_mod, rgb, expected):
    from PIL import Image
    blob = ui_mod.encode_v565(Image.new("RGB", (4, 4), rgb), 4, 4, fit="stretch")
    hi, lo = blob[8], blob[9]
    assert (hi << 8) | lo == expected, f"{rgb} → 0x{(hi << 8) | lo:04X}, want 0x{expected:04X}"


def test_v565_contain_letterboxes_without_distorting(ui_mod):
    from PIL import Image
    # A wide image into a square target: sides keep the background, centre is red.
    src = Image.new("RGB", (100, 10), (255, 0, 0))
    blob = ui_mod.encode_v565(src, 40, 40, fit="contain")
    w, h = _decode_header(blob)
    assert (w, h) == (40, 40)
    px = lambda x, y: (blob[8 + 2 * (y * w + x)] << 8) | blob[9 + 2 * (y * w + x)]
    assert px(20, 0) == 0x0000, "top edge should be letterbox background"
    assert px(20, 20) == 0xF800, "centre row should be the image"


def test_v565_cover_fills_every_pixel(ui_mod):
    from PIL import Image
    blob = ui_mod.encode_v565(Image.new("RGB", (100, 10), (0, 0, 255)), 40, 40, fit="cover")
    body = blob[8:]
    assert all((body[i] << 8 | body[i + 1]) == 0x001F for i in range(0, len(body), 2)), \
        "cover must crop to fill, leaving no background"


def test_stored_frames_are_pruned(ui_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_mod, "_IMG_DIR", str(tmp_path))
    monkeypatch.setattr(ui_mod, "_IMG_KEEP", 5)
    for i in range(12):
        ui_mod.store_v565(b"V565" + bytes([0, 1, 0, 1, 0, 0]), f"f{i}")
    left = list(tmp_path.glob("*.v565"))
    assert len(left) == 5, f"cache must stay bounded, found {len(left)}"


def test_frame_route_rejects_path_traversal(ui_mod):
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_ui_capabilities.py"))
    assert "os.path.basename" in src, "the image route must basename the request"
    assert 'safe.endswith(".v565")' in src, "and only ever serve rendered frames"


# ── V56A animation format ──────────────────────────────────────────────────

def test_v56a_header_and_frame_layout(ui_mod):
    from PIL import Image
    reds = [Image.new("RGB", (8, 8), (255, 0, 0)) for _ in range(3)]
    blob = ui_mod.encode_v56a(reds, 16, 16, fps=12, fit="stretch")
    assert blob[:4] == b"V56A"
    w = (blob[4] << 8) | blob[5]; h = (blob[6] << 8) | blob[7]
    n = (blob[8] << 8) | blob[9]; fps = (blob[10] << 8) | blob[11]
    assert (w, h, n, fps) == (16, 16, 3, 12)
    # 12-byte header then exactly n raw frames — the node indexes by offset, so
    # any padding or per-frame header would desync playback.
    assert len(blob) == 12 + 3 * 16 * 16 * 2


def test_v56a_frames_keep_their_order(ui_mod):
    from PIL import Image
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    blob = ui_mod.encode_v56a([Image.new("RGB", (4, 4), c) for c in cols],
                              4, 4, fps=5, fit="stretch")
    stride = 4 * 4 * 2
    for i, want in enumerate((0xF800, 0x07E0, 0x001F)):
        off = 12 + i * stride
        assert (blob[off] << 8) | blob[off + 1] == want, f"frame {i} out of order"


def test_v56a_rejects_empty_and_clamps_fps(ui_mod):
    from PIL import Image
    with pytest.raises(ValueError):
        ui_mod.encode_v56a([], 8, 8)
    blob = ui_mod.encode_v56a([Image.new("RGB", (4, 4))], 4, 4, fps=9999)
    assert ((blob[10] << 8) | blob[11]) == 60, "fps must be clamped to something drawable"


def test_sprite_sheet_slicing(ui_mod):
    from PIL import Image
    sheet = Image.new("RGB", (40, 20))
    frames = ui_mod._frames_from_image(sheet, cols=4, rows=2)
    assert len(frames) == 8
    assert frames[0].size == (10, 10)


def test_frame_count_is_capped(ui_mod):
    from PIL import Image
    many = [Image.new("RGB", (2, 2)) for _ in range(ui_mod._ANIM_MAX_FRAMES + 50)]
    blob = ui_mod.encode_v56a(many, 2, 2, fps=10)
    assert ((blob[8] << 8) | blob[9]) == ui_mod._ANIM_MAX_FRAMES


def test_firmware_frees_the_sequence_on_every_exit(ui_mod):
    """PSRAM held by a stale sequence is gone until reboot, so every path that
    leaves animation mode must release it."""
    src = _read(_INO)
    assert src.count("animFree()") >= 4, "ui_clear, ui_screen, stop and reload must all free"
    assert "ps_malloc" in src, "prefer PSRAM for frame storage"
    assert "animTick();" in src, "playback must be driven from loop(), not a blocking wait"


# ── App registry / follow-the-tab ──────────────────────────────────────────

def test_every_app_builds_a_renderable_screen(ui_mod):
    import asyncio
    for app_id, app in ui_mod.APPS.items():
        built = app["build"]("node-1", {})
        screen, mapping = asyncio.run(built) if asyncio.iscoroutine(built) else built
        assert isinstance(screen, dict) and isinstance(mapping, dict), app_id
        if screen.get("__status__"):
            continue
        assert screen.get("widgets"), f"{app_id} renders nothing"
        for w in screen["widgets"]:
            assert w.get("t") in ("label", "rect", "hline", "button", "bar", "image"), \
                f"{app_id} uses widget {w.get('t')!r} no firmware can draw"


def test_pad_buttons_all_map_to_a_capability(ui_mod):
    for app_id, app in ui_mod.APPS.items():
        if not app_id.startswith("pad:"):
            continue
        screen, mapping = app["build"]("node-1", {})
        actions = {w["action"] for w in screen["widgets"] if w.get("action")}
        for a in actions:
            assert a.startswith(("macro:", "app:", "nav:", "page:")), \
                f"{app_id}: odd action {a!r}"
            if a.startswith("macro:"):
                assert mapping.get(a, {}).get("cap"), f"{app_id}: {a} maps to nothing"


def test_self_targeting_pads_bind_the_node(ui_mod):
    """Buttons marked `self` act on the node you tapped, so node_id must be
    injected — otherwise the mesh pad would drive whatever node came first."""
    _, mapping = ui_mod.APPS["pad:mesh"]["build"]("node-42", {})
    targeted = [m for m in mapping.values() if m["cap"].startswith("mesh.")]
    assert targeted
    assert all(m["args"].get("node_id") == "node-42" for m in targeted)


def test_launcher_offers_every_other_app(ui_mod):
    screen, _ = ui_mod.APPS["launcher"]["build"]("n", {})
    offered = {w["action"].split(":", 1)[1] for w in screen["widgets"]
               if w.get("action", "").startswith("app:")}
    assert offered == set(ui_mod.APPS) - {"launcher"}


def test_pads_exist_for_the_mapped_panels(ui_mod):
    for panel in ui_mod.PANEL_PADS:
        assert "pad:" + panel in ui_mod.APPS


def test_follow_is_bound_to_an_opt_in_key():
    """The harness hook must stay inert until a node is explicitly bound, or
    every tab change would start pushing screens at someone's display."""
    src = _read(os.path.join(_ROOT, "vera", "capability_orchestration.html"))
    assert "vera_mesh_follow_node" in src
    assert "if (!node) return;" in src, "no bound node must mean no request at all"


# ── Macro pad behaviour ────────────────────────────────────────────────────

def test_pads_page_instead_of_overflowing_the_screen(ui_mod):
    """A 480x320 panel fits ~6 buttons. More than that used to be laid out off
    the bottom edge where they could never be tapped."""
    many = [{"label": f"b{i}", "cap": "mesh.sysinfo"} for i in range(14)]
    screen, mapping = ui_mod.build_pad_screen("n1", "Big", many, page=0)
    on_page = [w for w in screen["widgets"]
               if w.get("action", "").startswith("macro:")]
    assert len(on_page) == ui_mod._PAGE_SIZE
    assert all(w["y"] + w.get("h", 0) <= 320 for w in screen["widgets"]), "a button is off-screen"
    assert any(w.get("action", "").startswith("page:") for w in screen["widgets"])


def test_paging_keeps_button_indices_stable(ui_mod):
    many = [{"label": f"b{i}", "cap": f"cap.{i}"} for i in range(14)]
    _, m0 = ui_mod.build_pad_screen("n1", "Big", many, page=0)
    _, m1 = ui_mod.build_pad_screen("n1", "Big", many, page=1)
    assert m0["macro:0"]["cap"] == "cap.0"
    assert m1["macro:%d" % ui_mod._PAGE_SIZE]["cap"] == "cap.%d" % ui_mod._PAGE_SIZE
    assert set(m0) & set(m1) == set(), "pages must not reuse each other's actions"


def test_page_index_is_clamped(ui_mod):
    few = [{"label": "a", "cap": "x"}]
    for page in (-5, 99):
        screen, _ = ui_mod.build_pad_screen("n1", "T", few, page=page)
        assert [w for w in screen["widgets"] if w.get("action", "").startswith("macro:")]


def test_result_summary_never_hides_a_failure(ui_mod):
    lines = ui_mod.summarise_result("some.cap", {"error": "boom went the thing"})
    assert lines[0] == "FAILED" and "boom" in lines[1]
    # a success is summarised, not dumped
    ok = ui_mod.summarise_result("c", {"ok": True, "count": 3, "items": [1, 2, 3]})
    assert any("count: 3" in l for l in ok)
    assert all(len(l) < 130 for l in ok)
    assert ui_mod.summarise_result("c", {"ok": True}) == ["ok"]
    assert ui_mod.summarise_result("c", None) == ["ok"]


def test_confirm_buttons_are_carried_into_the_map(ui_mod):
    screen, mapping = ui_mod.build_pad_screen(
        "n1", "T", [{"label": "Wipe", "cap": "danger.wipe", "confirm": True},
                    {"label": "Safe", "cap": "safe.read"}])
    assert mapping["macro:0"]["confirm"] is True
    assert mapping["macro:1"]["confirm"] is False


def test_destructive_builtin_pads_ask_first(ui_mod):
    """A LAN scan from a knocked screen is rude; anything with real side effects
    should be flagged."""
    scan = [b for b in ui_mod.PANEL_PADS["netmap"]["buttons"] if "scan" in b["cap"]]
    assert scan and all(b.get("confirm") for b in scan)


def test_custom_pads_round_trip_and_register(ui_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_mod, "_PADS_CUSTOM", str(tmp_path / "pads.json"))
    pads = {"desk": {"id": "desk", "label": "Desk",
                     "buttons": [{"label": "Info", "cap": "mesh.sysinfo", "self": True}]}}
    ui_mod._save_custom_pads(pads)
    assert ui_mod._load_custom_pads()["desk"]["label"] == "Desk"
    ui_mod._register_pads()
    try:
        assert "pad:desk" in ui_mod.APPS
        screen, mapping = ui_mod.APPS["pad:desk"]["build"]("node-9", {})
        assert mapping["macro:0"]["args"]["node_id"] == "node-9"
    finally:
        monkeypatch.undo()
        ui_mod._register_pads()          # leave the registry as we found it


def test_missing_custom_pad_file_is_not_an_error(ui_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(ui_mod, "_PADS_CUSTOM", str(tmp_path / "nope.json"))
    assert ui_mod._load_custom_pads() == {}


# ── SD toolkit: card identification and safe archiving ─────────────────────

_SWITCH_CARD = [
    {"path": "/Nintendo/Contents/registered/00.nca", "size": 4096},
    {"path": "/atmosphere/package3", "size": 900000},
    {"path": "/bootloader/hekate_ipl.ini", "size": 400},
    {"path": "/games/Some Great Game [0100ABC].nsp", "size": 9_000_000_000},
    {"path": "/games/Another_Title (USA).xci", "size": 4_000_000_000},
]
_3DS_CARD = [
    {"path": "/Nintendo 3DS/00010/00020/data.bin", "size": 512},
    {"path": "/boot.firm", "size": 300000},
    {"path": "/cias/Cool Game (Rev 1).cia", "size": 700_000_000},
    {"path": "/3ds/homebrew.3dsx", "size": 120000},
]


def test_identifies_a_switch_card(ui_mod):
    r = ui_mod.identify_card(_SWITCH_CARD)
    assert r["console"] == "Nintendo Switch"
    assert r["game_count"] == 2
    assert r["by_platform"]["Switch"] == 2
    # biggest first — that is the one you care about when deciding what to pull
    assert r["games"][0]["size"] >= r["games"][1]["size"]


def test_identifies_a_3ds_card(ui_mod):
    r = ui_mod.identify_card(_3DS_CARD)
    assert r["console"] == "Nintendo 3DS"
    plats = {g["platform"] for g in r["games"]}
    assert "3DS" in plats and "3DS homebrew" in plats


def test_titles_are_cleaned_of_dump_decorations(ui_mod):
    r = ui_mod.identify_card(_SWITCH_CARD)
    titles = {g["title"] for g in r["games"]}
    assert "Some Great Game" in titles, titles
    assert "Another Title" in titles, titles


def test_unknown_card_is_reported_as_unknown_not_guessed(ui_mod):
    r = ui_mod.identify_card([{"path": "/DCIM/IMG_0001.JPG", "size": 100}])
    assert r["console"] == "unknown" and r["consoles"] == [] and r["game_count"] == 0


def test_empty_listing_is_safe(ui_mod):
    r = ui_mod.identify_card([])
    assert r["console"] == "unknown" and r["games"] == []


def test_store_path_cannot_escape_the_store(ui_mod):
    """The device chooses these names and it is not authenticated."""
    for nasty in ("../../etc/passwd", "/../../root/.ssh/id_rsa",
                  "..\\..\\windows\\system32\\x", "/a/../../../b"):
        p = os.path.abspath(ui_mod._sd_store_path("node-1", nasty))
        assert p.startswith(os.path.abspath(ui_mod._SD_STORE) + os.sep), p
    assert os.path.abspath(ui_mod._sd_store_path("../evil", "/x")).startswith(
        os.path.abspath(ui_mod._SD_STORE) + os.sep)


def test_store_path_keeps_a_usable_layout(ui_mod):
    p = ui_mod._sd_store_path("esp32-abc", "/games/Some Game.nsp")
    assert p.endswith(os.path.join("esp32-abc", "games", "Some Game.nsp"))


def test_firmware_walk_is_budgeted_and_iterative(ui_mod):
    """A recursive walk with nested File handles would exhaust the stack; the
    budget stops a 30k-file card producing an unsendable result."""
    src = _read(_INO)
    assert '"sd_walk"' in src and '"sd_upload"' in src
    assert "max_files" in src and "max_depth" in src
    assert "String stack[24]" in src, "walk must use an explicit stack, not recursion"
    assert "sendRequest(\"POST\", &f, sz)" in src, "files must stream, not buffer"


# ── Info dashboards on the panel ───────────────────────────────────────────

def test_dig_walks_dicts_lists_and_counts(ui_mod):
    d = ui_mod._dig
    assert d({"a": {"b": 3}}, "a.b") == 3
    assert d({"a": [{"b": "x"}]}, "a.0.b") == "x"
    # a collection reports its size, which is what a one-line readout wants
    assert d({"items": [1, 2, 3]}, "items") == 3
    assert d({"m": {"x": 1, "y": 2}}, "m") == 2
    # missing paths and wrong shapes must not raise on a live panel
    assert d({"a": 1}, "a.b.c") is None
    assert d({}, "nope") is None
    assert d({"a": [1]}, "a.9") is None
    assert d(None, "a") is None


def test_value_formatting_is_panel_sized(ui_mod):
    f = ui_mod._fmt
    assert f(3.100) == "3.1" and f(2.0) == "2"
    assert f(True) == "yes" and f(False) == "no"
    assert f(42) == "42"


_DECLARED_CAPS = None


def declared_caps():
    """Every capability NAME declared anywhere in the tree.

    Checked against the source rather than the live registry on purpose: the test
    process imports only a subset of modules, so the registry would report real
    capabilities as missing and invented ones as unjudgeable."""
    global _DECLARED_CAPS
    if _DECLARED_CAPS is None:
        pat = re.compile(r"""@capability\(\s*\n?\s*["']([a-zA-Z0-9_.]+)["']""")
        found = set()
        for root, _dirs, files in os.walk(os.path.join(_ROOT, "vera")):
            for f in files:
                if not f.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                        found |= set(pat.findall(fh.read()))
                except OSError:
                    pass
        _DECLARED_CAPS = found
    return _DECLARED_CAPS

def test_every_dashboard_row_names_a_real_capability(ui_mod):
    """A dashboard that points at a capability nobody registered renders a blank
    panel. Rows are data, so a typo is easy — catch it here."""
    missing = []
    for aid, spec in ui_mod.INFO_APPS.items():
        for row in spec.get("rows") or []:
            if row["cap"] not in declared_caps():
                missing.append(f"{aid}: {row['cap']}")
    assert not missing, "dashboards reference unknown capabilities: " + ", ".join(missing)


def test_every_pad_button_names_a_real_capability(ui_mod):
    missing = []
    pads = dict(ui_mod.PANEL_PADS)
    pads["scans"] = ui_mod.SCAN_PAD
    for pid, pad in pads.items():
        for b in pad.get("buttons") or []:
            if b["cap"] not in declared_caps():
                missing.append(f"{pid}: {b['cap']}")
    assert not missing, "pads reference unknown capabilities: " + ", ".join(missing)


def test_dashboard_reports_a_missing_capability_instead_of_blanking(ui_mod):
    import asyncio
    spec = {"_id": "info:test", "label": "T",
            "rows": [{"label": "gone", "cap": "definitely.not.a.cap", "pick": "x"}]}
    screen, _ = asyncio.run(ui_mod._app_dashboard_build(spec, "n1"))
    text = " ".join(w.get("text", "") for w in screen["widgets"])
    assert "no capability" in text and "definitely.not.a.cap" in text


def test_dashboard_shows_errors_rather_than_hiding_them(ui_mod, monkeypatch):
    import asyncio

    async def _boom(cap, **kw):
        return {"error": "upstream is down"}

    monkeypatch.setattr(ui_mod, "_call_cap", _boom)
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    spec = {"_id": "info:test", "label": "T",
            "rows": [{"label": "thing", "cap": "some.cap", "pick": "v"}]}
    screen, _ = asyncio.run(ui_mod._app_dashboard_build(spec, "n1"))
    assert any(w.get("text") == "err" for w in screen["widgets"])


def test_json_rows_parse_a_raw_body(ui_mod, monkeypatch):
    """This is what lets a keyless REST API (open-meteo) be a row without its own
    capability."""
    import asyncio

    async def _fake(cap, **kw):
        return {"ok": True, "body": '{"current": {"temperature_2m": 11.5}}'}

    monkeypatch.setattr(ui_mod, "_call_cap", _fake)
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    spec = {"_id": "info:w", "label": "W",
            "rows": [{"label": "temp", "cap": "http.get", "json": True,
                      "pick": "current.temperature_2m"}]}
    screen, _ = asyncio.run(ui_mod._app_dashboard_build(spec, "n1"))
    assert any(w.get("text") == "11.5" for w in screen["widgets"])


def test_bad_json_body_does_not_crash_the_panel(ui_mod, monkeypatch):
    import asyncio

    async def _fake(cap, **kw):
        return {"ok": True, "body": "<html>not json</html>"}

    monkeypatch.setattr(ui_mod, "_call_cap", _fake)
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    spec = {"_id": "info:w", "label": "W",
            "rows": [{"label": "temp", "cap": "http.get", "json": True, "pick": "a.b"}]}
    screen, _ = asyncio.run(ui_mod._app_dashboard_build(spec, "n1"))
    assert any(w.get("text") == "err" for w in screen["widgets"])


def test_unconfigured_weather_says_so(ui_mod, monkeypatch):
    import asyncio
    monkeypatch.setattr(ui_mod, "_weather_rows", lambda: [])
    screen, _ = asyncio.run(ui_mod.APPS["info:weather"]["build"]("n1", {}))
    text = " ".join(w.get("text", "") for w in screen["widgets"])
    assert "not configured" in text and "weather_lat" in text


def test_dashboards_all_offer_a_way_back(ui_mod):
    import asyncio
    for aid in ui_mod.INFO_APPS:
        screen, _ = asyncio.run(ui_mod.APPS[aid]["build"]("n1", {}))
        actions = {w.get("action") for w in screen["widgets"]}
        assert "app:launcher" in actions, f"{aid} strands the user"


# ── Monochrome glyphs only, and ASCII on the panel ─────────────────────────

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U0000FE0F⚠⚡⛔⚙]")


def test_mesh_system_carries_no_emoji():
    """The mesh UI is monochrome. Typographic marks (arrows, checks, hexagons)
    are fine; colour pictographs and emoji-presentation selectors are not."""
    offenders = {}
    root = os.path.join(_ROOT, "vera", "mesh")
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if not f.endswith((".py", ".html", ".ino", ".json", ".md", ".c", ".h")):
                continue
            p = os.path.join(dirpath, f)
            with open(p, encoding="utf-8", errors="ignore") as fh:
                hits = _EMOJI.findall(fh.read())
            if hits:
                offenders[os.path.relpath(p, _ROOT)] = sorted(
                    {"U+%04X" % ord(c) for c in hits})
    assert not offenders, f"emoji in the mesh system: {offenders}"


def test_panel_text_is_transliterated_not_mangled(ui_mod):
    """The 5x7 font is ASCII 0x20-0x7E and substitutes '?'. SD game titles and
    calendar entries are full of accents, so fold them rather than lose them."""
    t = ui_mod.to_panel_text
    assert t("Pokémon Legends") == "Pokemon Legends"
    assert t("Café – naïve") == "Cafe - naive"
    assert t("“quoted” and ‘single’") == '"quoted" and \'single\''
    assert t("a…b") == "a...b"
    assert t("20°C") == "20degC"
    # anything genuinely undrawable still degrades to a visible placeholder
    assert t("你好") == "??"
    assert all(0x20 <= ord(c) <= 0x7E for c in t("mixed ☃ \U0001F600 text"))


def test_every_pushed_screen_is_folded(ui_mod):
    """Done at the push choke point so no individual app has to remember."""
    screen = ui_mod._asciify_screen({
        "title": "Café",
        "widgets": [{"t": "label", "text": "Pokémon"},
                    {"t": "bar", "label": "naïve", "val": 5},
                    {"t": "rect", "w": 4}],
    })
    assert screen["title"] == "Cafe"
    assert screen["widgets"][0]["text"] == "Pokemon"
    assert screen["widgets"][1]["label"] == "naive"
    assert screen["widgets"][2]["w"] == 4, "non-text fields must pass through"


def test_asciify_tolerates_odd_screens(ui_mod):
    assert ui_mod._asciify_screen({}) == {}
    assert ui_mod._asciify_screen({"widgets": []}) == {"widgets": []}
    assert ui_mod._asciify_screen(None) is None


def test_result_screens_use_ascii_markers(ui_mod):
    """The tick/cross that used to head a result screen are outside the font."""
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_ui_capabilities.py"))
    assert '"FAIL " if failed else "OK "' in src


# ── OTA: the merged-vs-app image distinction ───────────────────────────────
# A merged image (bootloader + partition table + app at 0x10000) is what esptool
# writes at 0x0 over USB. An OTA writes into an APP slot, so handing it the
# merged image lands a bootloader where the app belongs: the slot fails to boot,
# rolls back, and the node looks like it simply ignored the update.

def test_builder_returns_a_separate_app_image():
    src = _read(os.path.join(_ROOT, "vera", "build", "builder_service.py"))
    assert '"app_b64"' in src and '"app_offset"' in src
    assert '".merged.bin", "merged.bin"' in src, \
        "the app image must be found by EXCLUDING the merged one"


def test_build_saves_and_records_the_ota_image():
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_capabilities.py"))
    assert '.ota.bin' in src, "the app image must be stored beside the merged one"
    assert '"ota_name": ota_name' in src, "the sidecar must record it for mesh.ota"


def test_mesh_ota_refuses_to_send_a_merged_image():
    """Refusing beats queueing a job that can only fail silently on the device."""
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_capabilities.py"))
    assert 'meta.get("merged")' in src
    assert "cannot be OTA'd" in src
    assert 'ota = meta.get("ota_name")' in src, "prefer the app image when present"


def test_firmware_does_not_claim_success_before_updating():
    """It used to sendResult('done','OTA starting') and then attempt the update,
    so a failure was indistinguishable from success."""
    src = _read(_INO)
    ota = src[src.index('type=="ota"'):]
    ota = ota[:ota.index("web_fetch")] if "web_fetch" in ota else ota
    assert '"OTA starting"' not in ota or 'sendResult(id,"done",rv,"OTA starting")' not in ota
    assert "onProgress" in ota, "progress must be reported while it runs"
    assert "ota_pct" in ota, "progress goes out as telemetry — success reboots the node"
    assert 'sendResult(id,"error",rv,"OTA failed' in ota, "a failure must be reported"


def test_panel_sends_the_artifact_not_a_raw_url():
    """A raw url bypasses the server-side swap to the app image."""
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_panel.html"))
    assert "artifact:(a.url||'').split('/').pop()" in src


# ── Fleet update ───────────────────────────────────────────────────────────

def _fleet(monkeypatch, ui_mod, nodes, arduino_bin=None, mpy_version="1.4.0-mpy"):
    async def _fake_cap(cap, **kw):
        if cap == "mesh.nodes":
            return {"nodes": nodes}
        return {"ok": True, "job_id": "j-" + kw.get("node_id", "")}
    monkeypatch.setattr(ui_mod, "_call_cap", _fake_cap)
    monkeypatch.setattr(ui_mod, "served_fw_version", lambda f="micropython": mpy_version)
    monkeypatch.setattr(ui_mod, "newest_arduino_bin", lambda chip="": arduino_bin)


def test_fleet_plan_matches_each_node_to_its_own_image(ui_mod, monkeypatch):
    import asyncio
    _fleet(monkeypatch, ui_mod, [
        {"node_id": "a", "runtime": "arduino", "fw": "1.7.0-ino", "chip": "ESP32-S3",
         "online": True, "channels": ["http"]},
        {"node_id": "m", "runtime": "micropython", "fw": "1.3.0-mpy",
         "online": True, "channels": ["http"]},
    ], arduino_bin={"name": "n.ota.bin", "fw_version": "1.8.0-ino", "board": "s3"})
    r = asyncio.run(ui_mod.plan_fleet_update())
    by = {p["node_id"]: p for p in r["plan"]}
    assert by["a"]["mode"] == "bin" and by["a"]["artifact"] == "n.ota.bin"
    assert by["m"]["mode"] == "file" and by["m"]["artifact"] == "main.py"


def test_fleet_plan_explains_every_skip(ui_mod, monkeypatch):
    import asyncio
    _fleet(monkeypatch, ui_mod, [
        {"node_id": "off", "runtime": "arduino", "fw": "1.0-ino", "online": False},
        {"node_id": "serial", "runtime": "arduino", "fw": "1.0-ino", "online": True,
         "channels": ["serial"]},
        {"node_id": "optout", "runtime": "arduino", "fw": "1.0-ino", "online": True,
         "channels": ["http"], "config": {"ota": {"auto": False}}},
        {"node_id": "mystery", "runtime": "", "fw": "1.1", "online": True,
         "channels": ["http"]},
        {"node_id": "current", "runtime": "micropython", "fw": "1.4.0-mpy",
         "online": True, "channels": ["http"]},
    ])
    r = asyncio.run(ui_mod.plan_fleet_update())
    reasons = {s["node_id"]: s["reason"] for s in r["skipped"]}
    assert r["plan"] == []
    assert "offline" in reasons["off"]
    assert "http" in reasons["serial"]
    assert "disabled" in reasons["optout"]
    assert "runtime" in reasons["mystery"]
    assert "current" in reasons["current"]


def test_fleet_update_is_dry_run_until_confirmed(ui_mod, monkeypatch):
    import asyncio
    _fleet(monkeypatch, ui_mod, [
        {"node_id": "m", "runtime": "micropython", "fw": "old-mpy",
         "online": True, "channels": ["http"]}])
    dry = asyncio.run(ui_mod.cap_mesh_ota_all())
    assert dry["dry_run"] and dry["count"] == 1 and "sent" not in dry
    live = asyncio.run(ui_mod.cap_mesh_ota_all(confirm=True))
    assert live["counts"]["sent"] == 1


def test_fleet_update_can_target_named_nodes(ui_mod, monkeypatch):
    import asyncio
    _fleet(monkeypatch, ui_mod, [
        {"node_id": "a", "runtime": "micropython", "fw": "old", "online": True,
         "channels": ["http"]},
        {"node_id": "b", "runtime": "micropython", "fw": "old", "online": True,
         "channels": ["http"]}])
    r = asyncio.run(ui_mod.plan_fleet_update(node_ids=["b"]))
    assert [p["node_id"] for p in r["plan"]] == ["b"]


# ── Touch shares the LCD bus ───────────────────────────────────────────────

def test_touch_restores_the_display_bus_not_just_pin_modes():
    """On this shield the touch panel sits on the LCD's own control lines
    (yp=CS, xm=DC). A read drives CS HIGH, which DESELECTS the panel — restoring
    only pin DIRECTION left it deselected, so every later draw was a no-op and
    the screen looked frozen until a reboot re-ran init()."""
    src = _read(_INO)
    fn = src[src.index("void touchRestore()"):]
    fn = fn[:fn.index("\n}")]
    assert "digitalWrite(p8.pCS, LOW)" in fn, "CS must be re-asserted after a touch read"
    assert "digitalWrite(p8.pDC, HIGH)" in fn
    assert "digitalWrite(p8.pWR, HIGH)" in fn
    assert "pinMode(p8.pD[b], OUTPUT)" in fn, "data lines must go back to outputs"


def test_touch_is_sampled_between_polls():
    """The loop blocks for seconds in the HTTP long-poll; sampling once per
    iteration misses a 200ms tap nearly every time."""
    src = _read(_INO)
    assert "void touchPump(" in src
    assert "touchPump(1200)" in src, "touch must get the floor between polls"


# ── List dashboards show content, not counts ───────────────────────────────

def test_list_dashboards_render_rows_not_totals(ui_mod, monkeypatch):
    import asyncio

    async def _fake(cap, **kw):
        return {"watchlist": [{"symbol": "BTC/USD", "last": 61234.5},
                              {"symbol": "ETH/USD", "last": 3312.0}]}

    monkeypatch.setattr(ui_mod, "_call_cap", _fake)
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    screen, _ = asyncio.run(ui_mod.APPS["info:watchlist"]["build"]("n1", {}))
    text = " ".join(w.get("text", "") for w in screen["widgets"])
    assert "BTC/USD" in text and "ETH/USD" in text
    assert "61234.5" in text, "the value column must be populated"
    assert "2" not in [w.get("text") for w in screen["widgets"]], "a count is not a dashboard"


def test_list_dashboard_truncates_with_a_marker(ui_mod, monkeypatch):
    import asyncio

    async def _fake(cap, **kw):
        return {"watchlist": [{"symbol": "S%d" % i, "last": i} for i in range(30)]}

    monkeypatch.setattr(ui_mod, "_call_cap", _fake)
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    screen, _ = asyncio.run(ui_mod.APPS["info:watchlist"]["build"]("n1", {}))
    text = " ".join(w.get("text", "") for w in screen["widgets"])
    assert "more" in text, "must say the list was cut, not silently drop rows"
    rows = [w for w in screen["widgets"] if w.get("size") == 2 and w.get("x") == 8]
    assert len(rows) <= ui_mod._LIST_ROWS


def test_pick_list_handles_dicts_and_missing_paths(ui_mod):
    pl = ui_mod._pick_list
    assert pl({"a": [1, 2]}, "a") == [1, 2]
    got = pl({"m": {"x": {"v": 1}}}, "m")
    assert got and got[0]["_key"] == "x"
    assert pl({}, "nope") == []
    assert pl({"a": 5}, "a") == []


def test_list_dashboards_all_name_real_capabilities(ui_mod):
    missing = [f"{aid}: {s['cap']}" for aid, s in ui_mod.LIST_APPS.items()
               if s["cap"] not in declared_caps()]
    assert not missing, missing


def test_sprite_app_reports_a_missing_sprite_clearly(ui_mod, monkeypatch):
    import asyncio
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)

    async def _fake(cap, **kw):
        return {"error": "no such sprite"}

    monkeypatch.setattr(ui_mod, "_call_cap", _fake)
    r = asyncio.run(ui_mod.cap_mesh_ui_sprite(node_id="n1", sprite="ghost"))
    assert "not found" in r["error"]


def test_sprite_listing_is_graceful_without_spritegen(ui_mod, monkeypatch):
    import asyncio
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: False)
    r = asyncio.run(ui_mod.cap_mesh_ui_sprites())
    assert r["sprites"] == [] and "not available" in r["note"]


# ── UI kit: geometry that cannot drift ─────────────────────────────────────

def test_kit_zones_derive_from_the_real_panel(ui_mod):
    m = ui_mod
    assert (m.UI_W, m.UI_H) == (480, 320)
    assert m.UI_BODY_Y > m.UI_RULE_Y > m.UI_TITLE_Y
    assert m.UI_BAR_Y + m.UI_BTN_H + m.UI_MARGIN == m.UI_H, "action bar must sit on the edge"
    assert m.UI_BODY_H > 0 and m.UI_ROWS >= 4, "body must hold a useful number of rows"
    assert 2 * m.UI_BTN_W + m.UI_GUTTER + 2 * m.UI_MARGIN == m.UI_W


def test_text_is_clipped_to_what_fits(ui_mod):
    m = ui_mod
    wide = "x" * 200
    lab = m.ui_label(m.UI_MARGIN, m.UI_BODY_Y, wide, m.UI_BODY)
    assert m.ui_text_w(lab["text"], m.UI_BODY) <= m.UI_W - 2 * m.UI_MARGIN


def test_colour_roles_accept_names_and_raw_values(ui_mod):
    m = ui_mod
    assert m.ui_colour("good") == m.UI_PALETTE["good"]
    assert m.ui_colour(0x1234) == 0x1234              # raw RGB565 passes through
    assert m.ui_colour("nonsense") == m.UI_ROLE["fg"] # falls back, never raises


def test_flow_never_runs_under_the_action_bar(ui_mod):
    m = ui_mod
    f = m.UiFlow()
    for i in range(60):
        f.kv("row %d" % i, str(i))
    widgets = f.done()
    assert f.overflow > 0, "the surplus rows must be counted, not drawn"
    for w in widgets:
        assert w["y"] + m.ui_text_h(w.get("size", 2)) <= m.UI_BAR_Y, "content under the buttons"
    assert any("more" in str(w.get("text", "")) for w in widgets), "must admit what it dropped"


def test_action_bar_caps_at_two_buttons(ui_mod):
    m = ui_mod
    bar = m.ui_action_bar([{"text": "a", "action": "x"}, {"text": "b", "action": "y"},
                           {"text": "c", "action": "z"}])
    assert len(bar) == 2
    assert all(b["y"] == m.UI_BAR_Y for b in bar)


# ── UI builder: declarative specs ──────────────────────────────────────────

def test_spec_validation_names_the_bad_block(ui_mod):
    v = ui_mod.validate_ui_spec({"blocks": [{"t": "kv", "items": []},
                                            {"t": "wat"}]})
    assert not v["ok"] and "blocks[1]" in v["error"] and "wat" in v["error"]
    assert not ui_mod.validate_ui_spec("nope")["ok"]
    assert not ui_mod.validate_ui_spec({"blocks": [{"t": "list"}]})["ok"]
    assert not ui_mod.validate_ui_spec({"blocks": [{"t": "image"}]})["ok"]
    assert not ui_mod.validate_ui_spec({"blocks": [{"t": "bars", "items": [{"label": "x"}]}]})["ok"]
    assert not ui_mod.validate_ui_spec({"actions": [1, 2, 3]})["ok"], "only 2 fit"
    assert ui_mod.validate_ui_spec({"title": "ok", "blocks": []})["ok"]


def test_spec_compiles_within_the_panel(ui_mod):
    m = ui_mod
    spec = {"title": "Kitchen", "blocks": [
        {"t": "kv", "items": [{"k": "temp", "v": "21C"}]},
        {"t": "list", "items": ["Milk", "Bread"]},
        {"t": "bars", "items": [{"label": "cpu", "val": 42}]},
        {"t": "grid", "items": [{"text": "Run", "action": "cap:mesh.sysinfo"}]},
    ], "actions": [{"text": "Refresh", "action": "self"}]}
    assert m.validate_ui_spec(spec)["ok"]
    screen = m.compile_ui_spec(spec, "kitchen")
    for w in screen["widgets"]:
        assert 0 <= w["x"] < m.UI_W and 0 <= w["y"] < m.UI_H, w
        assert w["x"] + w.get("w", 0) <= m.UI_W, w
        assert w["y"] + w.get("h", 0) <= m.UI_H, w
    text = " ".join(str(w.get("text", "")) for w in screen["widgets"])
    assert "Kitchen" in text and "temp" in text and "Milk" in text


def test_self_action_rebinds_to_the_saved_screen(ui_mod):
    screen = ui_mod.compile_ui_spec(
        {"blocks": [], "actions": [{"text": "Refresh", "action": "self"}]}, "abc")
    actions = [w.get("action") for w in screen["widgets"] if w.get("t") == "button"]
    assert "screen:abc" in actions


def test_grid_never_overlaps_the_action_bar(ui_mod):
    m = ui_mod
    spec = {"blocks": [{"t": "grid", "items": [{"text": "b%d" % i, "action": "x"}
                                               for i in range(30)]}]}
    screen = m.compile_ui_spec(spec)
    for w in screen["widgets"]:
        if w.get("t") == "button" and w["y"] != m.UI_BAR_Y:
            assert w["y"] + w["h"] <= m.UI_BAR_Y - m.UI_GUTTER


# ── Pads derived from the panel registry ───────────────────────────────────

def test_derived_pads_skip_what_makes_no_sense_on_a_panel(ui_mod, monkeypatch):
    monkeypatch.setattr(ui_mod, "_panel_registry", lambda: {
        "demo": {"label": "Demo", "ui_caps": [
            "mesh.sysinfo",          # keep
            "demo.panel",            # skip: a panel route
            "demo.items.delete",     # skip: destructive
            "demo.stream",           # skip: streaming
            "netscan.lan.scan",      # keep, but must ask first
        ]}})
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    pad = ui_mod.derive_panel_pad("demo")
    caps = [b["cap"] for b in pad["buttons"]]
    assert "mesh.sysinfo" in caps and "netscan.lan.scan" in caps
    assert not any(c in caps for c in ("demo.panel", "demo.items.delete", "demo.stream"))
    scan = next(b for b in pad["buttons"] if b["cap"] == "netscan.lan.scan")
    assert scan["confirm"], "a scan changes the world; ask first"
    assert next(b for b in pad["buttons"] if b["cap"] == "mesh.sysinfo")["self"], \
        "mesh caps must target the tapping node"


def test_curated_pads_keep_priority_but_hide_nothing(ui_mod, monkeypatch):
    monkeypatch.setattr(ui_mod, "_panel_registry", lambda: {
        "markets": {"label": "Markets", "ui_caps": ["markets.overview", "markets.bars"]}})
    monkeypatch.setattr(ui_mod, "_cap_exists", lambda n: True)
    pad = ui_mod.pad_for_panel("markets")
    caps = [b["cap"] for b in pad["buttons"]]
    assert caps[0] == ui_mod.PANEL_PADS["markets"]["buttons"][0]["cap"], "curated first"
    assert "markets.bars" in caps, "registry capabilities must still be offered"
    assert len(caps) == len(set(caps)), "no duplicates when both sources have one"


def test_labels_are_human_readable(ui_mod):
    assert ui_mod._pad_label("markets.watchlist.list") == "Watchlist list"
    assert ui_mod._pad_label("mesh.display.test") == "Display test"


def test_unknown_panel_yields_no_pad_rather_than_an_empty_one(ui_mod, monkeypatch):
    monkeypatch.setattr(ui_mod, "_panel_registry", lambda: {})
    assert ui_mod.pad_for_panel("nothing-here").get("buttons") in (None, [])


# ── Mesh topology: forwarders and honest animation ─────────────────────────
# The fleet reaches Vera through a relay; the old graph drew every node straight
# onto the hub, hiding the hop where things actually break.

def _rows(*specs):
    out = []
    for nid, ip, seen in specs:
        out.append({"node_id": nid, "name": nid, "ip": ip, "last_seen": seen,
                    "channels": '["http"]', "fw_version": "1.9.0-ino", "rssi": -55})
    return out


def test_forwarder_is_detected_from_the_peer_address(mesh, monkeypatch):
    """A node reports its own ip; Vera sees where the traffic came FROM. When
    they differ, something relayed it — that hop belongs on the map."""
    monkeypatch.setattr(mesh, "_server_host", lambda: "")
    monkeypatch.setattr(mesh, "_VIA", {"n1": {"ip": "192.168.1.197"}})
    topo = mesh.build_mesh_topology(_rows(("n1", "192.168.1.55", "2026-01-01T00:00:00+00:00")))
    ids = {n["id"] for n in topo["nodes"]}
    assert "fwd:192.168.1.197" in ids, "the relay must appear as its own node"
    edges = {(e["from"], e["to"]) for e in topo["edges"]}
    assert ("n1", "fwd:192.168.1.197") in edges
    assert ("fwd:192.168.1.197", "hub") in edges
    assert ("n1", "hub") not in edges, "must not pretend the node talks to Vera directly"


def test_direct_node_is_not_given_a_phantom_hop(mesh, monkeypatch):
    monkeypatch.setattr(mesh, "_server_host", lambda: "")
    monkeypatch.setattr(mesh, "_VIA", {"n1": {"ip": "192.168.1.55"}})
    topo = mesh.build_mesh_topology(_rows(("n1", "192.168.1.55", "2026-01-01T00:00:00+00:00")))
    assert ("n1", "hub") in {(e["from"], e["to"]) for e in topo["edges"]}
    assert not [n for n in topo["nodes"] if n["id"].startswith("fwd:")]


def test_configured_relay_shows_before_any_node_checks_in(mesh, monkeypatch):
    """The intended path is worth drawing even with an empty fleet."""
    monkeypatch.setattr(mesh, "_server_host", lambda: "192.168.1.197:8088")
    monkeypatch.setattr(mesh, "_VIA", {})
    topo = mesh.build_mesh_topology([])
    assert "fwd:192.168.1.197" in {n["id"] for n in topo["nodes"]}


def test_status_maps_to_the_topology_vocabulary(mesh, monkeypatch):
    monkeypatch.setattr(mesh, "_server_host", lambda: "")
    monkeypatch.setattr(mesh, "_VIA", {})
    monkeypatch.setattr(mesh, "_status_of", lambda s: s)
    topo = mesh.build_mesh_topology(_rows(("a", "1.1.1.1", "online"),
                                          ("b", "1.1.1.2", "stale"),
                                          ("c", "1.1.1.3", "offline")))
    st = {n["id"]: n["status"] for n in topo["nodes"]}
    assert (st["a"], st["b"], st["c"]) == ("ok", "warn", "err")
    assert all(n["status"] in ("ok", "warn", "err", "unknown") for n in topo["nodes"])


def test_snapshot_matches_the_element_contract(mesh, monkeypatch):
    """<vera-topology-map>.applySnapshot expects {id,label,kind,status} nodes and
    {from,to} edges; every edge must reference a node it can actually draw."""
    monkeypatch.setattr(mesh, "_server_host", lambda: "192.168.1.197")
    monkeypatch.setattr(mesh, "_VIA", {"n1": {"ip": "10.0.0.9"}})
    topo = mesh.build_mesh_topology(_rows(("n1", "192.168.1.55", "2026-01-01T00:00:00+00:00")))
    ids = {n["id"] for n in topo["nodes"]}
    for n in topo["nodes"]:
        assert {"id", "label", "kind", "status"} <= set(n), n
    for e in topo["edges"]:
        assert e["from"] in ids and e["to"] in ids, e


def test_peer_note_is_bounded(mesh):
    mesh._VIA.clear()
    for i in range(mesh._VIA_MAX + 120):
        mesh.note_peer("n%d" % i, "10.0.0.1")
    assert len(mesh._VIA) <= mesh._VIA_MAX
    mesh.note_peer("", "10.0.0.1")
    mesh.note_peer("x", "")
    assert "" not in mesh._VIA and "x" not in mesh._VIA
    mesh._VIA.clear()


def test_element_exposes_a_public_activity_hook():
    """The map's own animation is diff-gated on status, which cannot express
    traffic: a check-in never changes status but is exactly what should move."""
    src = _read(os.path.join(_ROOT, "vera", "topology_map_element.js"))
    assert "noteActivity(id, opts)" in src
    hook = src[src.index("noteActivity(id, opts)"):]
    hook = hook[:hook.index("applySnapshot(data)")]
    assert "this._nodes.has(id)" in hook, "must ignore ids it cannot draw"
    assert "_pulseEl" in hook and "_flyChip" in hook


def test_panel_primes_before_animating():
    """A first poll returns history; animating it would erupt on every page load."""
    src = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_panel.html"))
    assert "if (!act.primed" in src
    py = _read(os.path.join(_ROOT, "vera", "mesh", "mesh_capabilities.py"))
    assert '"primed": bool(since)' in py


# ── Touch pressure: the reason nothing ever registered ─────────────────────

def test_pressure_is_inverted_in_both_firmwares():
    """Across the plates z2-z1 is LARGE untouched (open circuit) and small under
    a press. Reporting the raw difference as pressure meant an untouched panel
    looked like maximum force and a real press like none — with a min/max band
    around it, NO touch could ever register."""
    ino = _read(_INO)
    assert "TOUCH_ADC_MAX - (z2 - z1)" in ino, "arduino pressure still inverted"
    assert "#define TOUCH_ADC_MAX 4095" in ino, "ESP32 analogRead is 12-bit"
    mpy = _read(_MPY)
    assert "4095 - (z2 - z1)" in mpy, "micropython pressure still inverted"


def test_pressure_band_admits_a_real_press():
    """A press now yields a HIGH value, so the band must sit under the ceiling."""
    ino = _read(_INO)
    m = re.search(r"int touchZMin = (\d+), touchZMax = (\d+);", ino)
    assert m, "pressure band not found"
    lo, hi = int(m.group(1)), int(m.group(2))
    assert 0 < lo < hi <= 4095
    assert hi >= 4000, "a firm press reads near full scale; do not clip it out"


# ── Visual language ────────────────────────────────────────────────────────

def test_palette_is_real_colour_not_eight_primaries(ui_mod):
    pal = ui_mod.UI_PALETTE
    assert len(set(pal.values())) >= 10, "a panel of 8 primaries reads as a debug console"
    assert pal["bg"] != 0x0000, "pure black flattens every surface above it"
    assert pal["surface"] != pal["bg"] and pal["surface2"] != pal["surface"]


def test_screens_have_a_header_and_footer_surface(ui_mod):
    m = ui_mod
    screen = m.ui_screen("Title", [], [{"text": "A", "action": "x"}])
    rects = [w for w in screen["widgets"] if w.get("t") == "rect"]
    assert any(r["y"] == 0 and r["w"] == m.UI_W for r in rects), "no title band"
    assert any(r["y"] >= m.UI_BAR_Y - 8 for r in rects), "no footer surface"
    assert any(w.get("t") == "label" and w.get("text") == "Title"
               for w in screen["widgets"])


def test_rows_band_and_right_align(ui_mod):
    m = ui_mod
    f = m.UiFlow()
    f.kv("alpha", "1").kv("beta", "22")
    ws = f.done()
    labels = [w for w in ws if w.get("t") == "label"]
    values = [w for w in labels if w["text"] in ("1", "22")]
    assert len(values) == 2
    right_edges = {w["x"] + m.ui_text_w(w["text"], w["size"]) for w in values}
    assert len(right_edges) == 1, "values must share a right edge to read as a column"
    assert any(w.get("t") == "rect" for w in ws), "no zebra banding"


def test_metric_tiles_stay_inside_the_panel(ui_mod):
    m = ui_mod
    f = m.UiFlow()
    f.metrics([{"label": "a", "value": "1"}, {"label": "b", "value": "2"},
               {"label": "c", "value": "3"}])
    for w in f.done():
        assert w["x"] >= 0 and w["x"] + w.get("w", 0) <= m.UI_W, w
        assert w["y"] + w.get("h", 0) <= m.UI_BAR_Y, w


def test_bar_shows_its_percentage(ui_mod):
    f = ui_mod.UiFlow()
    f.bar("cpu", 42)
    assert any(w.get("text") == "42%" for w in f.done())


# ── Widget -> spec mapping ─────────────────────────────────────────────────

def test_metric_widget_becomes_value_rows(ui_mod):
    spec = ui_mod.widget_to_spec(
        {"kind": "metric", "title": "Heap", "cap": "obs.health",
         "fields": [{"label": "free", "path": "heap.free"}]},
        {"heap": {"free": 1234}})
    assert spec["blocks"][0]["t"] == "kv"
    assert spec["blocks"][0]["items"][0] == {"k": "free", "v": "1234"}
    assert spec["actions"][0]["action"] == "self", "must refresh itself, not freeze"


def test_list_widget_becomes_rows(ui_mod):
    spec = ui_mod.widget_to_spec(
        {"kind": "table", "title": "Nodes", "path": "nodes",
         "left": ["node_id"], "right": ["fw"]},
        {"nodes": [{"node_id": "a", "fw": "1.9"}, {"node_id": "b", "fw": "1.8"}]})
    items = spec["blocks"][0]["items"]
    assert spec["blocks"][0]["t"] == "list"
    assert items[0] == {"text": "a", "value": "1.9"}


def test_gauge_widget_becomes_bars(ui_mod):
    spec = ui_mod.widget_to_spec(
        {"kind": "gauge", "path": "items", "left": ["label"], "right": ["value"]},
        {"items": [{"label": "cpu", "value": 42}]})
    assert spec["blocks"][0]["t"] == "bars"
    assert spec["blocks"][0]["items"][0] == {"label": "cpu", "val": 42.0}


def test_actions_widget_becomes_tappable_buttons(ui_mod):
    spec = ui_mod.widget_to_spec(
        {"kind": "actions", "items": [{"label": "Info", "cap": "mesh.sysinfo"}]})
    it = spec["blocks"][0]["items"][0]
    assert spec["blocks"][0]["t"] == "grid"
    assert it["action"] == "cap:mesh.sysinfo", "must stay interactive on the node"


def test_unmapped_widget_kind_is_refused_not_approximated(ui_mod):
    with pytest.raises(ValueError) as e:
        ui_mod.widget_to_spec({"kind": "sankey"})
    assert "sankey" in str(e.value) and "supported" in str(e.value)


def test_every_mapped_kind_produces_a_valid_spec(ui_mod):
    """Whatever the mapping emits must survive the builder's own validation."""
    samples = {
        "metric": {"fields": [{"label": "x", "path": "a"}]},
        "list": {"path": "rows"}, "table": {"path": "rows"},
        "gauge": {"path": "rows"}, "progress": {"path": "rows"},
        "chart": {"path": "rows"}, "stat": {}, "status": {},
        "actions": {"items": [{"label": "go", "cap": "mesh.sysinfo"}]},
        "buttons": {"items": [{"label": "go", "cap": "mesh.sysinfo"}]},
        "text": {"text": "hello"}, "image": {"url": "/x.v565"},
    }
    for kind, extra in samples.items():
        spec = ui_mod.widget_to_spec(dict({"kind": kind, "title": kind}, **extra),
                                     {"a": 1, "rows": [{"label": "l", "value": 5}]})
        v = ui_mod.validate_ui_spec(spec)
        assert v["ok"], f"{kind}: {v.get('error')}"
        ui_mod.compile_ui_spec(spec)          # must also lay out without raising
