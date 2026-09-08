"""Unit tests for Foundry SD-CARD provisioning — the third target, alongside
PXE netboot and Proxmox CT/VM.

The behaviour that matters here is not "does it emit a config file" but "is it
safe to point at a card that already holds something irreplaceable". These
tests pin the three properties that make that true: the merge preserves what it
did not write, the recovery paths (HDMI + SSH) survive, and services inherited
from the card's previous life are caught before they can disrupt a live LAN.

Imported via the lowercase `vera.*` path so it resolves to THIS worktree
(Vera.vera.* would resolve to the main checkout) — see sdcard_core.py header."""
from vera.foundry.sdcard_core import (
    CFG_BEGIN, CFG_END, PANELS, apply_config_txt, buttons_agent, firstboot_script,
    frame_agent, hazard_services, plan_adapt, plan_flash, tft_config_block,
)

# A real Raspberry Pi config.txt, of the kind found on a card that has been in
# service for years — settings we must not disturb.
EXISTING = """hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=39
dtparam=i2c_arm=on
dtparam=spi=on
dtparam=audio=on
[pi4]
dtoverlay=vc4-fkms-v3d
max_framebuffers=2
[all]
enable_uart=1
display_rotate=2
"""


# --- config.txt merging -----------------------------------------------------

def test_merge_preserves_every_existing_line():
    out = apply_config_txt(EXISTING, "xpt2046")
    for line in EXISTING.strip().splitlines():
        assert line in out, f"merge dropped {line!r}"


def test_merge_is_idempotent():
    once = apply_config_txt(EXISTING, "xpt2046")
    twice = apply_config_txt(once, "xpt2046")
    assert once == twice
    assert once.count(CFG_BEGIN) == 1 and once.count(CFG_END) == 1


def test_remerge_replaces_only_our_block_when_panel_changes():
    first = apply_config_txt(EXISTING, "xpt2046", {"panel": "ili9341"})
    second = apply_config_txt(first, "xpt2046", {"panel": "ili9486"})
    assert "ili9486" in second and "ili9341" not in second
    assert second.count(CFG_BEGIN) == 1
    # the user's own settings are still there after a re-provision
    assert "hdmi_mode=39" in second and "display_rotate=2" in second


def test_merge_into_empty_config_still_valid():
    out = apply_config_txt("", "xpt2046")
    assert out.startswith(CFG_BEGIN) or CFG_BEGIN in out
    assert "dtoverlay=ads7846" in out


def test_display_none_is_a_no_op():
    assert apply_config_txt(EXISTING, "none") == EXISTING
    assert tft_config_block("none") == ""


# --- the recovery paths -----------------------------------------------------

def test_hdmi_is_never_disabled():
    """A wrong panel guess must not cost the only other way onto the box."""
    block = tft_config_block("xpt2046")
    assert "hdmi_force_hotplug=0" not in block
    assert "hdmi_blanking" not in block
    merged = apply_config_txt(EXISTING, "xpt2046")
    assert "hdmi_force_hotplug=1" in merged  # the card's own setting survives


def test_ssh_flag_written_by_default():
    plan = plan_adapt(EXISTING, [])
    assert "ssh" in plan["boot"]


def test_ssh_can_be_declined():
    plan = plan_adapt(EXISTING, [], ssh=False)
    assert "ssh" not in plan["boot"]


# --- touch controller -------------------------------------------------------

def test_touch_overlay_present_and_configurable():
    block = tft_config_block("xpt2046", {"penirq": 26, "rotate": 90})
    assert "dtoverlay=ads7846" in block
    assert "penirq=26" in block
    assert "rotate=90" in block


def test_swapxy_follows_rotation_but_can_be_overridden():
    assert "swapxy=1" in tft_config_block("xpt2046", {"rotate": 90})
    assert "swapxy=0" in tft_config_block("xpt2046", {"rotate": 180})
    assert "swapxy=0" in tft_config_block("xpt2046", {"rotate": 90, "swapxy": 0})


def test_known_panels_render_their_driver_name():
    for panel in PANELS:
        assert panel in tft_config_block("xpt2046", {"panel": panel})


# --- inherited-service hazards ---------------------------------------------

# Exactly what the motivating card had enabled.
REAL_UNITS = ["apache2", "ceph-mgr.target", "ceph-mon.target", "ceph-osd.target",
              "containerd", "cron", "dhcpcd", "dnsmasq", "fail2ban", "glusterd",
              "hostapd", "inetd", "motion.service", "nmbd", "nordvpnd.service",
              "octoprint.service", "smbd", "ssh", "vsftpd", "wg-quick@wg0"]


def test_real_card_hazards_are_caught():
    names = {h["name"] for h in hazard_services(REAL_UNITS)}
    # the ones that would actually break a LAN
    assert {"dnsmasq", "hostapd"} <= names
    # the ones that would rejoin someone else's cluster
    assert {"ceph-mon", "ceph-osd", "glusterd"} <= names
    # the ones that would hijack the default route
    assert {"nordvpnd", "wg-quick@wg0"} <= names


def test_benign_services_are_left_alone():
    names = {h["name"] for h in hazard_services(REAL_UNITS)}
    for safe in ("ssh", "cron", "dhcpcd", "fail2ban", "containerd"):
        assert safe not in names, f"{safe} should not be masked"


def test_unit_suffixes_and_templates_normalise():
    assert hazard_services(["dnsmasq.service"])[0]["name"] == "dnsmasq"
    assert hazard_services(["ceph-osd@1.service"])[0]["name"] == "ceph-osd"
    assert hazard_services(["ceph-mon.target"])[0]["name"] == "ceph-mon"
    # wg-quick@wg0 is a template but the interface matters, so it stays whole
    assert hazard_services(["wg-quick@wg0.service"])[0]["name"] == "wg-quick@wg0"


def test_no_duplicate_masks():
    h = hazard_services(["dnsmasq", "dnsmasq.service", "dnsmasq"])
    assert len(h) == 1


def test_every_hazard_carries_a_reason():
    for h in hazard_services(REAL_UNITS):
        assert h["reason"].strip(), f"{h['name']} masked without explanation"


def test_hazard_masking_can_be_declined():
    plan = plan_adapt(EXISTING, REAL_UNITS, mask_hazards=False)
    assert plan["mask"] == []
    assert "systemctl mask" not in plan["root"]["usr/local/sbin/foundry-firstboot.sh"]


def test_masked_units_reach_the_firstboot_script():
    plan = plan_adapt(EXISTING, REAL_UNITS)
    script = plan["root"]["usr/local/sbin/foundry-firstboot.sh"]
    for unit in ("dnsmasq", "hostapd"):
        assert f"systemctl mask {unit}" in script
    # disable before mask, or masking a running unit leaves it running
    assert script.index("systemctl disable --now dnsmasq") < \
           script.index("systemctl mask dnsmasq")


# --- the plan ---------------------------------------------------------------

def test_adapt_plan_shape():
    plan = plan_adapt(EXISTING, REAL_UNITS, vera_url="https://vera:8999",
                      node_label="frame-01", enroll_token="tok")
    assert plan["mode"] == "adapt"
    assert "config.txt" in plan["boot"]
    for path in ("usr/local/sbin/foundry-firstboot.sh",
                 "etc/systemd/system/foundry-firstboot.service",
                 "usr/local/sbin/vera-frame.py",
                 "usr/local/sbin/vera-buttons.py"):
        assert path in plan["root"], f"missing {path}"
    assert "foundry-firstboot.service" in plan["enable"]
    assert plan["notes"]


def test_adapt_plan_writes_nothing_absolute():
    """Paths are partition-relative; an absolute path here would let a caller
    write to the HOST filesystem instead of the mounted card."""
    plan = plan_adapt(EXISTING, REAL_UNITS)
    for path in list(plan["boot"]) + list(plan["root"]):
        assert not path.startswith("/"), f"{path} is absolute"
        assert ".." not in path, f"{path} escapes the partition"


def test_wifi_and_userconf_optional():
    bare = plan_adapt(EXISTING, [])
    assert "wpa_supplicant.conf" not in bare["boot"]
    assert "userconf.txt" not in bare["boot"]
    full = plan_adapt(EXISTING, [], wifi=[("HomeNet", "secret"), ("Open", "")],
                      userconf="pi:$6$hash")
    wpa = full["boot"]["wpa_supplicant.conf"]
    assert 'ssid="HomeNet"' in wpa and 'psk="secret"' in wpa
    assert "key_mgmt=NONE" in wpa  # the open network
    assert full["boot"]["userconf.txt"].startswith("pi:")


def test_flash_plan_overwrites_and_skips_hazard_masking():
    plan = plan_flash("raspios-lite.img.xz", vera_url="https://vera:8999")
    assert plan["mode"] == "flash" and plan["image"] == "raspios-lite.img.xz"
    assert plan["mask"] == []
    assert "OVERWRITTEN" in plan["notes"][0]


# --- generated agents are valid python -------------------------------------

def test_generated_agents_compile():
    import ast
    ast.parse(frame_agent("https://vera:8999", "frame-01"))
    ast.parse(buttons_agent("https://vera:8999", "frame-01"))


def test_frame_agent_survives_an_unreachable_server():
    """The node will usually boot before Vera answers; that must not be fatal."""
    src = frame_agent("https://vera:8999", "frame-01")
    assert "except Exception" in src
    assert "time.sleep" in src


def test_buttons_debounced():
    src = buttons_agent("https://vera:8999", "n", [17, 22, 27],
                        ["frame.next", "frame.mode", "frame.info"])
    assert "0.35" in src  # debounce window
    assert "frame.next" in src and "PUD_UP" in src


def test_firstboot_never_upgrades_an_eol_distro():
    """apt upgrade on an end-of-life release is how a working node dies."""
    s = firstboot_script("https://vera:8999", "tok", "n")
    assert "apt-get upgrade" not in s and "dist-upgrade" not in s


def test_firstboot_is_rerunnable():
    s = firstboot_script("https://vera:8999", "tok", "n")
    assert "firstboot.done" in s
    unit = plan_adapt(EXISTING, [])["root"][
        "etc/systemd/system/foundry-firstboot.service"]
    assert "ConditionPathExists=!/var/lib/foundry/firstboot.done" in unit


def test_firstboot_warns_when_panel_guess_looks_wrong():
    s = firstboot_script("https://vera:8999", "tok", "n")
    assert "/dev/fb1" in s and "WARNING" in s
