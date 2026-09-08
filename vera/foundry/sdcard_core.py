"""sdcard_core.py -- pure, app-free builders for Foundry SD-CARD provisioning.

The third Foundry target. `foundry_core` can already render a Raspberry Pi
config.txt + cmdline.txt (`_render_rpi_config`, `_render_rpi_cmdline`) and a
wpa_supplicant.conf, but only along the PXE/netboot path -- there was no way to
take a physical SD card and make it a Vera node. This module fills that in:

    plan_adapt()  -- keep an existing rootfs, add boot files + a first-boot join
    plan_flash()  -- a freshly written OS image, same first-boot join
    apply_config_txt() -- idempotent edit of an existing config.txt

Everything here returns *data* -- {path: content} maps and lists of actions --
so the app layer decides how to write them (loop-mounted image, mounted card,
or a remote host over SSH) and this module stays unit-testable with no mounts.

ADAPT mode exists because real cards are rarely blank. The card that motivated
this held a decade of a user's own projects; wiping it was never acceptable.
Adapting also means inheriting whatever the card already did on boot, which is
why `hazard_services()` exists -- see its docstring.

Consumers import uppercase (Vera.vera.foundry.sdcard_core); tests import
lowercase (vera.foundry.sdcard_core) so pytest binds to the worktree copy --
see `worktree-testable-cores-pattern`. NO app imports.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# Marks the block we own inside a config.txt we did not write. Everything
# between the sentinels is ours to rewrite; everything else is left alone.
CFG_BEGIN = "# >>> foundry-tft >>>"
CFG_END = "# <<< foundry-tft <<<"

# Panels we know the overlay incantation for. The value is the fbtft driver
# name; the touch controller is always ADS7846-compatible (XPT2046 is a clone).
PANELS = {
    "ili9341": "3.2in 240x320 SPI (most generic red 3.2\" boards)",
    "ili9486": "3.5in 320x480 SPI (most generic 3.5\" boards, incl. Waveshare 3.5A)",
    "ili9488": "3.5in 320x480 SPI (some 3.5\" clones -- try if ili9486 is garbled)",
    "st7735r": "1.8in 128x160 SPI",
    "hx8357d": "3.5in Adafruit PiTFT",
}

# Services that make a re-purposed card dangerous to plug into a live LAN.
# A card pulled from an old project frequently still wants to be that project:
# hand out DHCP, raise an access point, or rejoin a storage cluster. On an
# estate that is a real outage, not a cosmetic problem -- so provisioning masks
# these by default and reports exactly what it masked.
HAZARD_SERVICES = {
    "dnsmasq": "runs a DHCP server -- competes with the LAN's real one",
    "hostapd": "raises a wifi access point",
    "isc-dhcp-server": "runs a DHCP server",
    "ceph-mon": "rejoins a Ceph cluster and may accept quorum changes",
    "ceph-osd": "rejoins a Ceph cluster and exposes its disk",
    "ceph-mgr": "rejoins a Ceph cluster",
    "glusterd": "rejoins a Gluster volume",
    "smbd": "serves SMB shares",
    "nmbd": "answers NetBIOS name queries -- can shadow real hosts",
    "samba-ad-dc": "acts as an Active Directory domain controller",
    "vsftpd": "serves FTP",
    "apache2": "serves HTTP on :80",
    "lighttpd": "serves HTTP on :80",
    "octoprint": "drives a 3D printer over serial",
    "motion": "records from cameras and fills the disk",
    "nordvpnd": "brings up a third-party VPN and can steal the default route",
    "openvpn": "brings up a VPN and can steal the default route",
    "wg-quick@wg0": "brings up WireGuard -- wg-quick installs a default route",
    "inetd": "legacy super-server, exposes whatever is in inetd.conf",
}


def hazard_services(enabled: List[str]) -> List[Dict]:
    """Given the unit names a card currently enables, return the subset that would
    disrupt a live network, each with the reason. Pure; the caller decides whether
    to mask them. Matching is on the unit's base name so `ceph-osd@1.service`,
    `ceph-osd.target` and `ceph-osd` all resolve to the same entry."""
    seen, out = set(), []
    for unit in (enabled or []):
        base = str(unit or "").strip()
        for suffix in (".service", ".target", ".socket"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        # ceph-osd@1 / user@1000 -> ceph-osd / user
        tmpl = base.split("@")[0] if "@" in base and not base.startswith("wg-quick") else base
        key = tmpl if tmpl in HAZARD_SERVICES else base
        if key in HAZARD_SERVICES and key not in seen:
            seen.add(key)
            out.append({"unit": unit, "name": key, "reason": HAZARD_SERVICES[key]})
    return out


def tft_config_block(display: str = "xpt2046", opts: Dict = None) -> str:
    """The config.txt lines that light up an SPI TFT + its XPT2046 touch controller.

    Deliberately does NOT disable HDMI. A wrong panel guess gives a blank or
    garbled TFT; if that also killed HDMI the node would be unrecoverable
    without pulling the card again. Keeping HDMI (and SSH, added separately)
    means a wrong guess is a five-minute fix instead of a re-flash."""
    opts = opts or {}
    if display in ("none", "hdmi", ""):
        return ""
    panel = str(opts.get("panel", "ili9341"))
    dc = int(opts.get("dc", 24))
    rst = int(opts.get("reset", 25))
    penirq = int(opts.get("penirq", 17))
    rotate = int(opts.get("rotate", 270))
    speed = int(opts.get("speed", 32000000))
    # Touch axis calibration differs per panel orientation; expose it rather
    # than baking in numbers that only suit one board.
    swapxy = 1 if int(opts.get("rotate", 270)) in (90, 270) else 0
    swapxy = int(opts.get("swapxy", swapxy))
    return "\n".join([
        CFG_BEGIN,
        "# Managed by Vera Foundry -- edit via foundry.sdcard.provision, not by hand.",
        f"# panel={panel} ({PANELS.get(panel, 'custom')})",
        "dtparam=spi=on",
        f"dtoverlay=fbtft,spi0-0,{panel},dc_pin={dc},reset_pin={rst},"
        f"rotate={rotate},speed={speed},fps=30,bgr=1",
        f"dtoverlay=ads7846,cs=1,penirq={penirq},penirq_pull=2,speed=1000000,"
        f"keep_vref_on=1,swapxy={swapxy},pmax=255,xohms=150,"
        "xmin=200,xmax=3900,ymin=200,ymax=3900",
        "# HDMI intentionally left enabled: it is the recovery path if the",
        "# panel driver above is wrong for this board.",
        CFG_END,
    ]) + "\n"


def apply_config_txt(existing: str, display: str = "xpt2046", opts: Dict = None) -> str:
    """Idempotently merge our TFT block into a config.txt we did not write.

    Replaces a previous foundry block if present, otherwise appends. Existing
    user settings are never reordered or dropped -- on an adapted card those
    lines are the difference between a Pi that boots and one that does not."""
    text = existing if existing is not None else ""
    block = tft_config_block(display, opts)
    if CFG_BEGIN in text and CFG_END in text:
        head = text.split(CFG_BEGIN)[0]
        tail = text.split(CFG_END, 1)[1].lstrip("\n")
        merged = head.rstrip("\n") + ("\n\n" + block if block else "\n")
        return (merged + tail).rstrip("\n") + "\n"
    if not block:
        return text
    sep = "" if text.endswith("\n") or not text else "\n"
    return text + sep + "\n" + block


def firstboot_unit(description: str = "Vera Foundry first-boot provisioning") -> str:
    """A oneshot unit that runs once and disables itself. `ConditionPathExists`
    on the marker means a half-finished run still retries on the next boot."""
    return "\n".join([
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "ConditionPathExists=!/var/lib/foundry/firstboot.done",
        "",
        "[Service]",
        "Type=oneshot",
        "RemainAfterExit=yes",
        "ExecStart=/usr/local/sbin/foundry-firstboot.sh",
        "StandardOutput=journal+console",
        "StandardError=journal+console",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]) + "\n"


def firstboot_script(vera_url: str = "", enroll_token: str = "", node_label: str = "",
                     role: str = "frame", mask: List[Dict] = None,
                     install_agent: bool = True) -> str:
    """First-boot script: neutralise inherited hazards, register with Vera, start
    the display agent. Written to be safe on a card whose OS is end-of-life --
    it never runs `apt upgrade` and treats every package install as optional, so
    a dead apt mirror degrades the node instead of bricking the boot."""
    mask = mask or []
    lines = [
        "#!/bin/sh",
        "# Vera Foundry -- first boot. Idempotent; safe to re-run.",
        "set -u",
        "mkdir -p /var/lib/foundry /var/log/foundry",
        "LOG=/var/log/foundry/firstboot.log",
        "exec >>\"$LOG\" 2>&1",
        "echo \"=== foundry first boot $(date -Is) ===\"",
        "",
    ]
    if mask:
        lines += [
            "# --- inherited services that would disrupt a live LAN ---",
            "# This card was adapted, not wiped, so it arrives still configured for",
            "# whatever it used to be. Mask (not just disable) so a dependency",
            "# cannot pull them back up.",
        ]
        for m in mask:
            unit = str(m.get("unit") or m.get("name") or "").strip()
            if not unit:
                continue
            why = str(m.get("reason", "")).replace("\n", " ")
            lines += [
                f"# {unit}: {why}",
                f"systemctl disable --now {unit} 2>/dev/null || true",
                f"systemctl mask {unit} 2>/dev/null || true",
            ]
        lines.append("")
    lines += [
        "# --- touchscreen calibration + framebuffer console on the TFT ---",
        "if [ -e /dev/fb1 ]; then",
        "  echo 'fb1 present (TFT is driving)' ",
        "else",
        "  echo 'WARNING: /dev/fb1 absent -- panel driver may be wrong for this board;'",
        "  echo 'HDMI and SSH are still up, re-run foundry.sdcard.provision with another panel.'",
        "fi",
        "",
    ]
    if vera_url:
        lines += [
            "# --- register this node with Vera ---",
            f"VERA='{vera_url}'",
            f"TOKEN='{enroll_token}'",
            f"LABEL='{node_label}'",
            f"ROLE='{role}'",
            "[ -n \"$LABEL\" ] || LABEL=$(hostname)",
            "IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')",
            "MAC=$(cat /sys/class/net/eth0/address 2>/dev/null "
            "|| cat /sys/class/net/wlan0/address 2>/dev/null)",
            "BODY=$(printf '{\"label\":\"%s\",\"role\":\"%s\",\"ip\":\"%s\",\"mac\":\"%s\","
            "\"token\":\"%s\",\"kind\":\"rpi\"}' \"$LABEL\" \"$ROLE\" \"$IP\" \"$MAC\" \"$TOKEN\")",
            "for try in 1 2 3 4 5; do",
            "  RESP=$(curl -sk -m 20 -H 'Content-Type: application/json' "
            "-d \"$BODY\" \"$VERA/foundry/node/checkin\" 2>&1)",
            "  case \"$RESP\" in *'\"ok\"'*) echo \"registered: $RESP\"; break ;; esac",
            "  echo \"check-in attempt $try failed: $RESP\"",
            "  sleep $((try * 10))",
            "done",
            "",
        ]
    if install_agent:
        lines += [
            "# --- display agent ---",
            "# Optional deps: the agent falls back to a blank screen rather than",
            "# failing the boot if these are unavailable (EOL mirrors are common).",
            "if ! python3 -c 'import PIL' 2>/dev/null; then",
            "  DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>/dev/null || true",
            "  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
            "python3-pil python3-rpi.gpio fonts-dejavu-core 2>/dev/null || true",
            "fi",
            "systemctl daemon-reload 2>/dev/null || true",
            "systemctl enable --now vera-frame.service 2>/dev/null || true",
            "systemctl enable --now vera-buttons.service 2>/dev/null || true",
            "",
        ]
    lines += [
        "touch /var/lib/foundry/firstboot.done",
        "echo \"=== foundry first boot complete $(date -Is) ===\"",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def frame_agent(vera_url: str = "", node_label: str = "", poll: int = 20,
                fb: str = "/dev/fb1", width: int = 320, height: int = 240) -> str:
    """The photoframe agent: ask Vera what to show, draw it to the SPI framebuffer.

    Writes raw RGB565 to the framebuffer directly rather than depending on X --
    these panels are usually headless and an X stack on a Pi Zero costs more
    than the whole job. Vera decides *what* to show (game art while a ROM runs,
    weather, crypto, a photo); this only decides how to paint it."""
    return f'''#!/usr/bin/env python3
"""Vera frame agent -- renders whatever Vera says onto an SPI TFT.

Vera drives the content through /foundry/node/frame, which returns:
    {{"mode": "image", "url": ...}}      e.g. box art for the ROM being played
    {{"mode": "text",  "lines": [...]}}  e.g. weather, crypto, build status
    {{"mode": "blank"}}
Unreachable Vera is a normal state (the node may boot before the server), so
the agent holds the last frame and keeps retrying rather than exiting.
"""
import os, struct, sys, time, urllib.request, json, io

VERA  = {vera_url!r}
LABEL = {node_label!r} or os.uname()[1]
POLL  = {poll}
FB    = {fb!r}
W, H  = {width}, {height}

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None


def to_rgb565(img):
    """Pack an RGB image into the RGB565 layout these panels expect."""
    img = img.convert("RGB").resize((W, H))
    out = bytearray()
    for r, g, b in img.getdata():
        out += struct.pack("<H", ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))
    return bytes(out)


def blit(img):
    if not os.path.exists(FB):
        return False
    with open(FB, "wb") as f:
        f.write(to_rgb565(img))
    return True


def text_frame(lines):
    img = Image.new("RGB", (W, H), (8, 10, 14))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        small = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        font = small = ImageFont.load_default()
    y = 8
    for i, ln in enumerate(lines[:8]):
        d.text((8, y), str(ln)[:34], font=(font if i == 0 else small),
               fill=(240, 240, 240) if i == 0 else (150, 160, 175))
        y += 26 if i == 0 else 19
    return img


def fetch():
    if not VERA:
        return {{"mode": "text", "lines": ["Vera frame", LABEL, "no server configured"]}}
    url = "%s/foundry/node/frame?label=%s" % (VERA.rstrip("/"), LABEL)
    req = urllib.request.Request(url, headers={{"Accept": "application/json"}})
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    if Image is None:
        sys.stderr.write("PIL unavailable -- frame agent idle\\n")
        while True:
            time.sleep(300)
    last_url = None
    while True:
        try:
            spec = fetch()
            mode = spec.get("mode", "blank")
            if mode == "image" and spec.get("url"):
                if spec["url"] != last_url:
                    import ssl
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(spec["url"], timeout=20,
                                                context=ctx) as r:
                        blit(Image.open(io.BytesIO(r.read())))
                    last_url = spec["url"]
            elif mode == "text":
                blit(text_frame(spec.get("lines") or [LABEL]))
                last_url = None
            elif mode == "blank":
                blit(Image.new("RGB", (W, H), (0, 0, 0)))
                last_url = None
        except Exception as e:
            # Hold the last good frame; a flapping network should not flash
            # an error at someone sitting in the room.
            sys.stderr.write("frame: %s\\n" % e)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
'''


def buttons_agent(vera_url: str = "", node_label: str = "",
                  pins: List[int] = None, actions: List[str] = None) -> str:
    """The 3 hat buttons as a Vera macro pad. Each pin POSTs a named action;
    what the action *does* is Vera's business, so the same firmware serves a
    photoframe, a build-status light, or a stream deck."""
    pins = pins or [17, 22, 27]
    actions = actions or ["frame.next", "frame.mode", "frame.info"]
    return f'''#!/usr/bin/env python3
"""Vera macro-pad agent -- GPIO buttons -> named Vera actions."""
import json, ssl, sys, time, urllib.request

VERA    = {vera_url!r}
LABEL   = {node_label!r}
PINS    = {pins!r}
ACTIONS = {actions!r}

try:
    import RPi.GPIO as GPIO
except Exception:
    sys.stderr.write("RPi.GPIO unavailable -- buttons idle\\n")
    while True:
        time.sleep(300)

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def press(action):
    if not VERA:
        return
    body = json.dumps({{"label": LABEL, "action": action}}).encode()
    req = urllib.request.Request(
        VERA.rstrip("/") + "/foundry/node/action", data=body,
        headers={{"Content-Type": "application/json"}})
    try:
        urllib.request.urlopen(req, timeout=10, context=_ctx).read()
    except Exception as e:
        sys.stderr.write("action %s failed: %s\\n" % (action, e))


def main():
    GPIO.setmode(GPIO.BCM)
    for p in PINS:
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    # Debounce in software: these hats rarely have hardware debounce and a
    # single press otherwise fires a burst of actions.
    last = {{p: 0.0 for p in PINS}}
    while True:
        now = time.time()
        for p, action in zip(PINS, ACTIONS):
            if GPIO.input(p) == 0 and now - last[p] > 0.35:
                last[p] = now
                press(action)
        time.sleep(0.02)


if __name__ == "__main__":
    main()
'''


def agent_unit(name: str, description: str, exec_path: str) -> str:
    return "\n".join([
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "",
        "[Service]",
        f"ExecStart={exec_path}",
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]) + "\n"


def plan_adapt(existing_config_txt: str = "", enabled_units: List[str] = None,
               display: str = "xpt2046", display_opts: Dict = None,
               vera_url: str = "", enroll_token: str = "", node_label: str = "",
               role: str = "frame", wifi: List[Tuple[str, str]] = None,
               ssh: bool = True, userconf: str = "",
               mask_hazards: bool = True, buttons: List[int] = None,
               button_actions: List[str] = None) -> Dict:
    """Plan an ADAPT of a card that already has an OS on it.

    Returns {"boot": {...}, "root": {...}, "enable": [...], "mask": [...],
             "notes": [...]} -- files keyed by path relative to each partition.
    Writes nothing. The caller mounts and applies, so this stays testable and
    so a plan can be reviewed before it touches someone's only copy of a disk."""
    opts = dict(display_opts or {})
    w, h = int(opts.get("width", 320)), int(opts.get("height", 240))
    hazards = hazard_services(enabled_units or []) if mask_hazards else []

    boot: Dict[str, str] = {
        "config.txt": apply_config_txt(existing_config_txt, display, opts),
    }
    if ssh:
        # The escape hatch. Everything else in this plan is a guess about
        # hardware we cannot see; SSH is how a wrong guess gets fixed.
        boot["ssh"] = ""
    if userconf:
        boot["userconf.txt"] = userconf.rstrip("\n") + "\n"
    if wifi:
        boot["wpa_supplicant.conf"] = _wpa(wifi)

    root: Dict[str, str] = {
        "usr/local/sbin/foundry-firstboot.sh": firstboot_script(
            vera_url, enroll_token, node_label, role, hazards),
        "etc/systemd/system/foundry-firstboot.service": firstboot_unit(),
        "usr/local/sbin/vera-frame.py": frame_agent(
            vera_url, node_label, width=w, height=h),
        "etc/systemd/system/vera-frame.service": agent_unit(
            "vera-frame", "Vera photoframe / status display",
            "/usr/bin/python3 /usr/local/sbin/vera-frame.py"),
        "usr/local/sbin/vera-buttons.py": buttons_agent(
            vera_url, node_label, buttons, button_actions),
        "etc/systemd/system/vera-buttons.service": agent_unit(
            "vera-buttons", "Vera macro-pad buttons",
            "/usr/bin/python3 /usr/local/sbin/vera-buttons.py"),
    }

    notes = [
        "config.txt is merged, not replaced -- existing settings are preserved.",
        "HDMI is left enabled and SSH is turned on so a wrong panel guess is recoverable.",
    ]
    if hazards:
        notes.append(
            "%d inherited service(s) will be masked on first boot: %s"
            % (len(hazards), ", ".join(h["name"] for h in hazards)))
    if display != "none" and opts.get("panel", "ili9341") not in PANELS:
        notes.append("panel %r is not in the known list -- overlay written as given."
                     % opts.get("panel"))
    return {"mode": "adapt", "boot": boot, "root": root,
            "enable": ["foundry-firstboot.service"],
            "mask": hazards, "notes": notes}


def plan_flash(image: str, **kw) -> Dict:
    """Plan a FLASH: write `image` to the card, then apply the same provisioning.
    A freshly written image has no inherited services, so no hazard masking."""
    kw.pop("existing_config_txt", None)
    kw.pop("enabled_units", None)
    kw["mask_hazards"] = False
    plan = plan_adapt(existing_config_txt="", enabled_units=[], **kw)
    plan["mode"] = "flash"
    plan["image"] = image
    plan["notes"].insert(0, "Card will be OVERWRITTEN with %s." % image)
    return plan


def _wpa(networks) -> str:
    """Local copy of foundry_core.wpa_supplicant_conf's output shape, kept here so
    this module has no intra-package import (the uppercase/lowercase split in
    `worktree-testable-cores-pattern` makes cross-core imports resolve to the
    wrong tree under test)."""
    lines = ["ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
             "update_config=1", "country=GB", ""]
    for entry in (networks or []):
        ssid, psk = (entry if isinstance(entry, (list, tuple)) else (entry, ""))
        ssid = (str(ssid) if ssid is not None else "").strip()
        if not ssid:
            continue
        psk = (str(psk) if psk is not None else "").strip()
        lines += ["network={", '\tssid="%s"' % ssid]
        lines.append('\tpsk="%s"' % psk if psk else "\tkey_mgmt=NONE")
        lines += ["}", ""]
    return "\n".join(lines)
