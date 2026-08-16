"""Unit tests for the Foundry PXE-server config generators (foundry_core).

These codify the hand-proven netboot stack: a LAN-fenced dnsmasq config and an iPXE
boot menu (local-disk default + RAM ops/desktop/alpine + netboot.xyz + catalogue-
driven install entries). Imported via lowercase vera.* so it binds to the worktree."""
from vera.foundry.foundry_core import pxe_dnsmasq_conf, pxe_ipxe_menu, pxe_ops_apkovl_files


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
