"""ZFS operations as plans: the commands, the warnings, and the refusals that
keep an operator out of trouble."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.proxmox import zfs_ops_core as z  # noqa: E402

pytestmark = pytest.mark.critical
G = 1 << 30


def test_sizes_parse_and_print():
    assert z.parse_size("200G") == 200 * G and z.parse_size("1.5T") == int(1.5 * (1 << 40)) and z.parse_size("+10G") == 10 * G
    assert z.parse_size("lots") is None and z.parse_size(4096) == 4096
    assert z.fmt_size(200 * G) == "200G" and z.fmt_size(int(1.5 * G)) == "1.5G"


def test_a_subvol_shrinks_by_refquota_and_keeps_the_proxmox_line_in_step():
    ds = {"name": "tank_sdh/subvol-126-disk-0", "type": "filesystem", "used": 67 * G, "refquota": 500 * G}
    p = z.resize_plan(ds, "200G", guest={"vmid": 126, "type": "lxc"}, disk_key="rootfs")
    assert p["ok"] and p["direction"] == "shrink" and p["before"] == "500G" and p["after"] == "200G"
    assert p["commands"][0] == "zfs set refquota=200G tank_sdh/subvol-126-disk-0"
    assert any("/etc/pve/lxc/126.conf" in c and "size=200G" in c for c in p["commands"])
    assert any("pre-resize" in c for c in p["commands"]), "the config is backed up first"
    assert any("refuses to shrink" in w for w in p["warnings"])


def test_a_subvol_never_shrinks_below_what_it_holds():
    ds = {"name": "tank_sdh/subvol-130-disk-0", "type": "filesystem", "used": 41 * G, "refquota": 200 * G}
    p = z.resize_plan(ds, "40G")
    assert not p["ok"] and "below what it already holds" in p["error"]
    p = z.resize_plan(ds, "45G")
    assert p["ok"] and any("headroom" in w for w in p["warnings"])


def test_a_zvol_grows_but_never_shrinks():
    ds = {"name": "tank_sdf/vm-104-disk-0", "type": "volume", "used": 300 * G, "volsize": 500 * G}
    assert not z.resize_plan(ds, "400G")["ok"]
    p = z.resize_plan(ds, "600G", guest={"vmid": 104, "type": "qemu"}, disk_key="scsi0")
    assert p["ok"] and p["commands"][0] == "zfs set volsize=600G tank_sdf/vm-104-disk-0"
    assert any("/etc/pve/qemu-server/104.conf" in c for c in p["commands"])
    assert any("growpart" in w for w in p["warnings"])


def test_scrub_trim_and_snapshot_are_simple_and_named():
    assert z.scrub_plan("tank_sdh")["commands"] == ["zpool scrub tank_sdh"]
    assert z.scrub_plan("tank_sdh", "stop")["commands"] == ["zpool scrub -s tank_sdh"]
    assert not z.scrub_plan("tank_sdh/child")["ok"] and not z.scrub_plan("bad name")["ok"]
    assert z.trim_plan("tank_sdh")["commands"] == ["zpool trim tank_sdh"]
    s = z.snapshot_plan("tank_sdh/vera-store", "before-cleanup")
    assert s["commands"] == ["zfs snapshot tank_sdh/vera-store@before-cleanup"]
    assert "$(date" in z.snapshot_plan("tank_sdh/vera-store")["commands"][0]
    assert z.snapshot_plan("tank_sdh/vera-store", "", recursive=True)["commands"][0].startswith("zfs snapshot -r ")
    assert not z.snapshot_plan("tank_sdh/vera-store", "bad name")["ok"]


def test_a_rollback_says_what_it_throws_away():
    snaps = ["ds@a", "ds@b", "ds@c"]
    p = z.rollback_plan("ds", "c", snaps)
    assert p["commands"] == ["zfs rollback ds@c"] and p["destroys"] == []
    p = z.rollback_plan("ds", "a", snaps)
    assert p["commands"] == ["zfs rollback -r ds@a"] and p["destroys"] == ["b", "c"]
    assert any("2 newer snapshot(s) are destroyed" in w for w in p["warnings"])
    assert not z.rollback_plan("ds", "zz", snaps)["ok"]


def test_tuning_allows_a_fixed_set_with_sane_values():
    p = z.tune_plan("tank_sdh/vera-store", {"recordsize": "1M", "compression": "zstd", "atime": "off"})
    assert p["ok"] and p["commands"] == ["zfs set recordsize=1m tank_sdh/vera-store",
                                         "zfs set compression=zstd tank_sdh/vera-store",
                                         "zfs set atime=off tank_sdh/vera-store"]
    assert not z.tune_plan("x", {"mountpoint": "/etc"})["ok"], "mountpoint is not tunable from here"
    assert not z.tune_plan("x", {"recordsize": "3M"})["ok"]
    assert any("power cut" in w for w in z.tune_plan("x", {"sync": "disabled"})["warnings"])
    assert not z.tune_plan("x", {})["ok"]


SINGLE = [{"type": "disk", "role": "data", "name": "wwn-1", "disks": [{"name": "wwn-1"}]}]


def test_attaching_a_disk_to_a_single_disk_makes_a_mirror():
    p = z.attach_plan("tank_sdh", SINGLE, "wwn-1", "wwn-2")
    assert p["ok"] and p["commands"] == ["zpool attach tank_sdh wwn-1 wwn-2"]
    assert p["before"] == "1 disk · no redundancy" and p["after"] == "mirror of 2"
    assert any("resilvers" in w for w in p["warnings"]) and any("zpool detach" in w for w in p["warnings"])
    assert not z.attach_plan("tank_sdh", SINGLE, "wwn-9", "wwn-2")["ok"]
    raidz = [{"type": "raidz1", "role": "data", "name": "raidz1-0", "disks": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}]
    assert not z.attach_plan("big", raidz, "a", "d")["ok"]


def test_adding_a_vdev_shows_the_layout_after_and_says_what_it_costs():
    p = z.add_plan("tank_sdh", SINGLE, ["wwn-3", "wwn-4"])
    assert p["ok"] and p["commands"] == ["zpool add tank_sdh mirror wwn-3 wwn-4"]
    assert p["after"] == "1 disk, mirror of 2" and any("unusual" in w for w in p["warnings"])
    p = z.add_plan("tank_sdh", SINGLE, ["wwn-3"])
    assert p["commands"] == ["zpool add tank_sdh wwn-3"] and p["after"] == "2 × 1 disk striped · no redundancy"
    assert any("no redundancy" in w for w in p["warnings"])
    assert not z.add_plan("tank_sdh", SINGLE, ["a", "b"], "raidz1")["ok"], "raidz1 needs three"


def test_every_control_is_a_dry_run_unless_confirmed_and_layout_changes_want_the_pool_typed():
    root = os.path.join(os.path.dirname(__file__), "..")
    caps = open(os.path.join(root, "vera", "proxmox", "zfs_ops_capabilities.py"), encoding="utf-8").read()
    for name in ("resize", "scrub", "trim", "snapshot", "snapshots", "rollback", "tune", "attach", "add"):
        assert f'"pxstore.zfs.{name}"' in caps, name
    assert "if not confirm:\n        return {\"ok\": True, \"dry_run\": True, \"plan\": plan}" in caps
    assert caps.count("confirm_pool != pool") == 2, "attach and add both want the pool's name typed back"
    assert "mkdir -p /root/ct-conf-backups" in caps, "a resize backs the guest config up before editing it"
    orch = open(os.path.join(root, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    assert orch.count("proxmox/zfs_ops_capabilities.py") == 1
    panel = open(os.path.join(root, "vera", "proxmox", "pxstore_panel.html"), encoding="utf-8").read()
    fn = panel[panel.index("P.zfsOp = async"):panel.index("P.zfsResize = ")]
    # the plan-then-confirm flow lives in vera-estate.js now; Storage delegates to it
    assert "veraEstate.plan(capName, base, title, opts)" in fn, "Storage must use the shared plan flow"
    assert "field:'confirm_pool'" in fn, "layout changes pass the typed-name requirement through"
    shared = open(os.path.join(root, "vera", "estate", "vera-estate.js"), encoding="utf-8").read()
    assert "if (!confirm(text))" in shared and "{confirm: true}" in shared, "the plan is shown before anything runs"
    for btn in ("P.zfsOp('pxstore.zfs.scrub'", "P.zfsOp('pxstore.zfs.trim'", "P.zfsMirror(", "P.zfsAddVdev(", "P.zfsResize("):
        assert btn in panel, btn
    assert "p.redundancy==='none'&&p.disks===1" in panel, "Mirror it is offered only to a single-disk pool"
