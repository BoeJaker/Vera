"""Unit tests for Foundry PXE netboot rendering — the unified x86 + Raspberry Pi
(incl. XPT2046 3.2" SPI touchscreen) boot-artifact generator.

These exercise the pure render helpers directly (no redis / no network), so they
would fail if the netboot config generation regressed.

Imported from the app-free core via the lowercase `vera.*` path so it resolves to
THIS worktree (Vera.vera.* would resolve to the main checkout) and pulls in no app
dependencies — see foundry_core.py header + dev-lifecycle §8.3."""
from vera.foundry.foundry_core import (
    _render_boot, _render_features_script, _pxe_slug, pick_node,
)

CFG = {"gateway": "10.42.0.1", "http_base": "http://10.42.0.1/foundry"}


def _prof(**kw):
    base = {"id": "p1", "name": "workstation", "image_id": "debian-12-cloudimg",
            "features": ["enrol", "hardening"], "arch": "amd64", "boot_type": "uefi",
            "display": "hdmi", "ip": "", "disk": 20}
    base.update(kw)
    return base


def test_x86_uefi_renders_ipxe_and_autoinstall():
    r = _render_boot(_prof(), CFG, {"source_url": "debian:12"})
    assert r["boot_type"] == "uefi" and r["arch"] == "amd64"
    a = r["artifacts"]
    assert "boot.ipxe" in a and "autoinstall/user-data" in a
    assert a["boot.ipxe"].startswith("#!ipxe")
    # the feature bundle must actually run on first boot
    assert "features.sh" in a["autoinstall/user-data"]
    assert "runcmd" in a["autoinstall/user-data"]


def test_features_bundle_carried_into_firstboot():
    r = _render_boot(_prof(features=["enrol", "hardening", "file-client"]), CFG, {})
    fb = r["artifacts"]["features.sh"]
    assert "hardening" in fb.lower()
    assert "cifs-utils" in fb            # file-client mounts SMB/NFS


def test_rpi_netboot_xpt2046_touchscreen_overlay():
    r = _render_boot(_prof(name="kiosk", arch="arm64", boot_type="rpi-netboot",
                           display="xpt2046", ip="10.42.0.60"), CFG, {})
    assert r["boot_type"] == "rpi-netboot" and r["display"] == "xpt2046"
    cfgtxt = r["artifacts"]["config.txt"]
    assert "dtparam=spi=on" in cfgtxt
    assert "ads7846" in cfgtxt           # XPT2046 == ADS7846-compatible resistive touch
    assert "ili9341" in cfgtxt           # 3.2" TFT panel
    cmd = r["artifacts"]["cmdline.txt"]
    assert "nfsroot" in cmd              # diskless netboot root over NFS
    assert "10.42.0.60" in cmd           # static IP (main LAN has no DHCP)
    assert "fbcon=map:1" in cmd          # console on the SPI framebuffer


def test_rpi_hdmi_has_no_touch_overlay():
    r = _render_boot(_prof(arch="arm64", boot_type="rpi-netboot", display="hdmi"), CFG, {})
    cfgtxt = r["artifacts"]["config.txt"]
    assert "ads7846" not in cfgtxt
    assert "hdmi_force_hotplug=1" in cfgtxt


def test_rpi_flash_plan_references_image():
    r = _render_boot(_prof(arch="arm64", boot_type="rpi-flash", display="xpt2046"),
                     CFG, {"source_url": "raspios-arm64.img.xz"})
    assert "flash.plan" in r["artifacts"]
    assert "raspios-arm64.img.xz" in r["artifacts"]["flash.plan"]
    # a flashed card still gets the touchscreen overlay
    assert "ads7846" in r["artifacts"]["config.txt"]


def test_features_script_minimal_when_no_bundles():
    fb = _render_features_script([])
    assert fb.startswith("#!/bin/sh")
    assert "cifs-utils" not in fb


def test_autoinstall_embeds_features_base64():
    import base64
    r = _render_boot(_prof(features=["hardening", "file-client"]), CFG, {})
    ud = r["artifacts"]["autoinstall/user-data"]
    assert "encoding: b64" in ud          # script embedded base64, not raw YAML block
    # the embedded blob must decode back to the real feature script
    line = [x for x in ud.splitlines() if x.strip().startswith("content:")][0]
    blob = line.split("content:", 1)[1].strip()
    decoded = base64.b64decode(blob).decode()
    assert decoded.startswith("#!/bin/sh")
    assert "cifs-utils" in decoded


def test_xpt2046_display_opts_override_pins_and_panel():
    r = _render_boot(_prof(arch="arm64", boot_type="rpi-netboot", display="xpt2046",
                           display_opts={"panel": "ili9486", "penirq": 25, "rotate": 90}),
                     CFG, {})
    c = r["artifacts"]["config.txt"]
    assert "ili9486" in c and "ili9341" not in c   # overridden panel
    assert "penirq=25" in c
    assert "rotate=90" in c


def test_slug():
    assert _pxe_slug("Work Station!") == "work-station"
    assert _pxe_slug("") == "node"


# ── node auto-resolve (real-VM E2E: empty node → clone HTTP 501) ──────────────────
def test_pick_node_prefers_online():
    j = '[{"node":"a","status":"offline"},{"node":"corp","status":"online"}]'
    assert pick_node(j) == "corp"


def test_pick_node_single():
    assert pick_node('[{"node":"corp","status":"online","cpu":0.1}]') == "corp"


def test_pick_node_falls_back_to_first_named_when_none_online():
    assert pick_node('[{"node":"a","status":"unknown"}]') == "a"


def test_pick_node_empty_or_bad_input():
    assert pick_node("") == ""
    assert pick_node("not json") == ""
    assert pick_node("[]") == ""
    assert pick_node('{"node":"x"}') == ""   # not a list


# ── cluster-join scripts baked into the first-boot feature bundle ─────────────────
def test_render_boot_bakes_cluster_scripts_into_features_and_autoinstall():
    import base64
    prof = _prof(features=["enrol", "docker-swarm"])
    cs = ["# --- join Docker Swarm ---\ndocker swarm join --token X mgr:2377"]
    r = _render_boot(prof, CFG, {}, cs)
    fb = r["artifacts"]["features.sh"]
    assert "docker swarm join --token X mgr:2377" in fb
    ud = r["artifacts"]["autoinstall/user-data"]
    blob = [l for l in ud.splitlines() if l.strip().startswith("content:")][0].split("content:", 1)[1].strip()
    assert "docker swarm join" in base64.b64decode(blob).decode()   # embedded in cloud-init too


def test_features_script_legacy_docker_install_when_no_cluster():
    fb = _render_features_script(["docker-swarm"])
    assert "get.docker.com" in fb          # install-only fallback (host ready to join manually)
    assert "swarm join" not in fb


def test_features_script_prefers_cluster_scripts_over_legacy_fallback():
    fb = _render_features_script(["docker-swarm"], ["# join\ndocker swarm join --token T a:2377"])
    assert "docker swarm join --token T a:2377" in fb
    assert "get.docker.com" not in fb      # a real join was provided → no bare-install fallback
