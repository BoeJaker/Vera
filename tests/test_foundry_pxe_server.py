"""Unit tests for the Foundry PXE-server config generators (foundry_core).

These codify the hand-proven netboot stack: a LAN-fenced dnsmasq config and an iPXE
boot menu (local-disk default + RAM ops/desktop/alpine + netboot.xyz + catalogue-
driven install entries). Imported via lowercase vera.* so it binds to the worktree."""
from vera.foundry.foundry_core import (pxe_dnsmasq_conf, pxe_ipxe_menu, pxe_ops_apkovl_files,
                                       pxe_desktop_apkovl_files)


# ── dnsmasq config (LAN-safety is the critical property) ──────────────────────────
def test_dnsmasq_is_fenced_to_one_interface():
    c = pxe_dnsmasq_conf("10.22.22.25", "vmbr2", "10.22.22.100", "10.22.22.150",
                         except_ifaces=["vmbr0"])
    assert "interface=vmbr2" in c
    assert "bind-interfaces" in c              # -> dnsmasq binds ONLY vmbr2 (never the LAN)
    assert "except-interface=lo" in c and "except-interface=vmbr0" in c
    # must NOT contain a wildcard/other-bridge LISTEN directive (a bare `interface=` line)
    lines = [l.strip() for l in c.splitlines()]
    assert "interface=vmbr0" not in lines      # except-interface=vmbr0 is fine; a listen line is not
    assert lines.count("bind-interfaces") == 1


def test_dnsmasq_dhcp_dns_tftp_and_ipxe_chain():
    c = pxe_dnsmasq_conf("10.22.22.25", "vmbr2", "10.22.22.100", "10.22.22.150")
    assert "dhcp-range=10.22.22.100,10.22.22.150,255.255.255.0,12h" in c
    assert "dhcp-option=option:router,10.22.22.25" in c
    assert "dhcp-option=option:dns-server,10.22.22.25" in c   # clients get a resolver
    assert "enable-tftp" in c and "tftp-root=/srv/foundry/tftp" in c
    # raw PXE -> iPXE, iPXE -> boot.ipxe
    assert "dhcp-boot=tag:!ipxe,tag:!efi,undionly.kpxe" in c
    assert "dhcp-boot=tag:!ipxe,tag:efi,ipxe.efi" in c
    assert "dhcp-boot=tag:ipxe,boot.ipxe" in c


# ── iPXE menu ─────────────────────────────────────────────────────────────────────
def test_menu_defaults_to_local_disk():
    m = pxe_ipxe_menu("10.22.22.25")
    assert "choose --default local --timeout 20000 target" in m   # protects installed OS
    assert ":local" in m and "sanboot --no-describe --drive 0x80" in m


def test_menu_has_ram_and_desktop_and_nbxyz():
    m = pxe_ipxe_menu("10.22.22.25")
    for tag in (":ops", ":desktop", ":alpine", ":nbxyz"):
        assert tag in m
    assert "node.apkovl.tar.gz" in m and "desktop.apkovl.tar.gz" in m
    assert "netboot.xyz.lkrn" in m and "netboot.xyz.efi" in m


def test_menu_alpine_boot_line_has_required_params():
    m = pxe_ipxe_menu("10.22.22.25")
    # the missing-params bug that caused the kernel panic must never recur
    assert "modloop=http://10.22.22.25/alpine/modloop-lts" in m
    assert "alpine_repo=" in m and "initrd=initramfs-lts" in m
    assert "modules=loop,squashfs,sd-mod,usb-storage" in m


def test_menu_install_entries_from_catalogue():
    imgs = [{"id": "debian-12-cloudimg", "os": "debian", "version": "12"},
            {"id": "ubuntu-24.04-cloudimg", "os": "ubuntu", "version": "24.04"}]
    m = pxe_ipxe_menu("10.22.22.25", install_images=imgs)
    assert "Install debian 12" in m and "Install ubuntu 24.04" in m
    assert ":img_debian-12-cloudimg" in m
    assert "kernel http://10.22.22.25/debian-12-cloudimg/linux" in m
    assert "OVERWRITES" in m               # install section is clearly labelled destructive


def test_menu_without_catalogue_has_no_install_section():
    m = pxe_ipxe_menu("10.22.22.25", install_images=[])
    assert "OVERWRITES" not in m           # no install entries -> no destructive section
    assert ":nbxyz" in m                   # ...but netboot.xyz is always available


# ── ops-node apkovl (diskless overlay) ────────────────────────────────────────────
def test_ops_apkovl_files_structure():
    f = pxe_ops_apkovl_files("10.22.22.25")
    for p in ("etc/apk/repositories", "etc/local.d/foundry.start",
              "usr/local/bin/foundry-tui", "etc/inittab", "root/.profile"):
        assert p in f and f[p]


def test_ops_apkovl_is_worker_only():
    f = pxe_ops_apkovl_files("10.22.22.25")
    start = f["etc/local.d/foundry.start"]
    assert "docker swarm join --token" in start        # joins as a worker
    assert "swarm init" not in start                   # NEVER self-promotes to manager
    assert "http://10.22.22.25/swarm/worker-token" in start


def test_ops_apkovl_autologin_and_tui():
    f = pxe_ops_apkovl_files("10.22.22.25")
    # busybox `login -f root` (in the Alpine base) — NOT agetty, which isn't present
    # at boot and panicked init.
    assert "/bin/login -f root" in f["etc/inittab"]
    assert "agetty" not in f["etc/inittab"]
    assert "foundry-tui" in f["root/.profile"]
    assert "whiptail" in f["usr/local/bin/foundry-tui"]


def test_ops_apkovl_has_proxmox_vms_console():
    f = pxe_ops_apkovl_files("10.22.22.25")
    tui = f["usr/local/bin/foundry-tui"]
    assert "pct enter" in tui and "qm terminal" in tui   # console into CTs / VMs
    assert f.get("etc/foundry/pve")                        # default Proxmox host configured


def test_ops_apkovl_docker_survives_broken_openrc_diskless_boot():
    """On a RAM/diskless boot the OpenRC boot runlevel never completes (fsck/sysfs
    report 'would not start'), which kills the udev-trigger coldplug AND makes a plain
    `service docker start` fail its dependency check, so dockerd never comes up. The
    generated boot script must side-step all of that (root-caused + verified live on a
    diskless ThinkPad ops node)."""
    start = pxe_ops_apkovl_files("10.22.22.25")["etc/local.d/foundry.start"]
    # modloop mounted + Docker's storage/net kernel modules loaded before dockerd
    assert "rc-service modloop start" in start
    assert "modprobe -a overlay" in start
    # udevd started DIRECTLY (the udev-trigger service can't run in the broken runlevel)
    assert "/sbin/udevd --daemon" in start
    assert "udevadm trigger --action=add" in start
    # cgroups + docker started with --nodeps to bypass the phantom dependency failure
    assert "rc-service --nodeps cgroups start" in start
    assert "rc-service --nodeps docker start" in start
    # the exact buggy pattern that never brought Docker up must not come back
    assert "&& service docker start" not in start
    assert "service docker start\n" not in start


def test_ops_apkovl_tui_has_proxmox_host_picker():
    """The menu must let you pick ANY published Proxmox host (not a single hard-coded
    one) and SSH into it with the estate key — the 'easily connect to any Proxmox host'
    requirement."""
    f = pxe_ops_apkovl_files("10.22.22.25")
    tui = f["usr/local/bin/foundry-tui"]
    assert "choosepve" in tui                       # host-picker function exists
    assert "/ops/pve_hosts" in tui                  # reads the published host list
    assert 'host "Pick Proxmox host' in tui         # menu entry to switch host
    assert "-i /root/.ssh/id_estate" in tui         # uses the estate key to reach Proxmox
    # target defaults to the reachable server IP, not the old hard-coded LAN address
    assert "192.168.0.200" not in tui
    assert f["etc/foundry/pve"].strip() == "10.22.22.25"


def test_ops_apkovl_start_fetches_estate_key_and_menu():
    """The boot script must pull the estate SSH key (to reach Proxmox) and refresh the
    menu from the server so TUI updates propagate without rebuilding the image."""
    start = pxe_ops_apkovl_files("10.22.22.25")["etc/local.d/foundry.start"]
    assert "http://10.22.22.25/ops/id_estate" in start
    assert "/root/.ssh/id_estate" in start
    assert "http://10.22.22.25/ops/foundry-tui" in start


def test_ops_apkovl_sdwriter_present_and_safe():
    """The SD-card writer must exist, be wired into the menu, and — critically — only
    offer removable/USB/MMC disks so a laptop's internal system disk can't be wiped."""
    f = pxe_ops_apkovl_files("10.22.22.25")
    assert "usr/local/bin/foundry-sdwrite" in f
    sd = f["usr/local/bin/foundry-sdwrite"]
    # SAFETY: only removable / usb / mmc targets are ever listed as a write destination
    assert 'case "$rmv:$trn" in 1:*|*:usb|*:mmc)' in sd
    assert 'dd of="$DEV"' in sd                          # writes with dd to the chosen disk
    assert "/ops/pi_images" in sd                        # Raspberry Pi image catalogue
    assert "xz -dc" in sd                                # handles .img.xz (Raspberry Pi OS)
    # wired into the ops menu
    assert "sdcard) clear; /usr/local/bin/foundry-sdwrite" in f["usr/local/bin/foundry-tui"]


def test_desktop_apkovl_structure_and_input_fix():
    """The desktop overlay must carry the diskless input fix (direct udevd + manual
    coldplug, since the broken OpenRC boot runlevel skips udev-trigger) plus the full
    XFCE desktop and the ops menu, so a fresh desktop node comes up with working
    keyboard/touchpad and can reach Proxmox."""
    f = pxe_desktop_apkovl_files("10.22.22.25")
    for pth in ("etc/local.d/desktop.start", "etc/inittab", "root/.profile", "root/.xinitrc",
                "etc/X11/xorg.conf.d/10-foundry-fallback.conf",
                "etc/X11/xorg.conf.d/40-libinput-touchpad.conf",
                "usr/share/applications/foundry-ops.desktop"):
        assert pth in f and f[pth]
    start = f["etc/local.d/desktop.start"]
    # the diskless input fix
    assert "/sbin/udevd --daemon" in start                       # udevd started directly
    assert "udevadm trigger --type=devices --action=add" in start
    assert "rc-service modloop start" in start
    assert "rc-service --nodeps docker start" in start           # --nodeps bypass
    # ops menu + SD writer + estate key fetched from the server
    assert "http://10.22.22.25/ops/foundry-tui" in start
    assert "http://10.22.22.25/ops/foundry-sdwrite" in start
    assert "http://10.22.22.25/ops/id_estate" in start
    # X input config lets libinput auto-add (no restrictive AllowEmptyInput=false)
    xflags = f["etc/X11/xorg.conf.d/10-foundry-fallback.conf"]
    assert 'AutoAddDevices" "on"' in xflags and "AllowEmptyInput" not in xflags
    # X waits for input devices before starting (avoids the udev race)
    assert "/dev/input/event*" in f["root/.profile"] and "startx" in f["root/.profile"]
    # menu launcher opens the ops TUI in a terminal
    assert "foundry-tui" in f["usr/share/applications/foundry-ops.desktop"]
