"""Storage panel: physical disks and the backup system.

Marked critical because both guard a failure that went unnoticed for over a
year: a disk the UI never listed, and backups that silently were not running.
Fixtures are trimmed from the real node (corp) on 2026-09-12.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vera.proxmox import pxstore_backup_core as core  # noqa: E402

pytestmark = pytest.mark.critical

LSBLK = json.dumps({"blockdevices": [
    {"name": "sda", "size": 1200243695616, "type": "disk", "rota": True, "model": "EG1200JEHMC",
     "tran": "sas", "fstype": None, "label": None, "mountpoint": None,
     "children": [{"name": "sda1", "type": "part", "fstype": "zfs_member", "label": "tank_sda"},
                  {"name": "sda9", "type": "part", "fstype": None, "label": None}]},
    {"name": "sdc", "size": 1200243695616, "type": "disk", "rota": True, "model": "EG1200JEHMC",
     "tran": "sas", "fstype": None, "label": None, "mountpoint": None, "children": []},
    {"name": "sdd", "size": 1200243695616, "type": "disk", "rota": True, "model": "EG1200JEMDA",
     "tran": "sas", "children": [{"name": "sdd2", "fstype": "vfat"},
                                 {"name": "sdd3", "fstype": "zfs_member", "label": "rpool"}]},
    {"name": "sdh", "size": 2000398934016, "type": "disk", "rota": True,
     "model": "ST2000LM006 HN-M201RAD", "tran": "usb",
     "children": [{"name": "sdh3", "fstype": "LVM2_member",
                   "children": [{"name": "pve-root", "type": "lvm", "fstype": "ext4"}]}]},
    {"name": "sdi", "size": 12001339219968, "type": "disk", "rota": True, "model": "ST12000NM0127",
     "tran": "usb", "fstype": "ext4", "label": "BigDat", "mountpoint": "/mnt/BigDat", "children": []},
    {"name": "sdj", "size": 125069950976, "type": "disk", "rota": True, "model": "Internal SD-CARD",
     "tran": "usb", "fstype": "zfs_member", "label": "spool",
     "children": [{"name": "sdj1", "fstype": "zfs_member", "label": "mypool"}]},
    {"name": "sdk", "size": 500107862016, "type": "disk", "rota": "0", "model": "SSD", "tran": "sata",
     "children": [{"name": "sdk1", "fstype": "xfs"}]},
    {"name": "sr0", "size": 1073741312, "type": "rom", "tran": "sata"},
    {"name": "zd0", "size": 4194304, "type": "disk", "rota": False, "fstype": "iso9660"},
]})
IMPORTED = ["backup", "cpool", "rpool", "tank_sda", "tank_sde", "tank_sdf", "tank_sdh"]
IMPORTABLE_TEXT = "   pool: mypool\n  state: UNAVAIL\n"


def _by_name():
    disks = core.classify_disks(LSBLK, IMPORTED, core.parse_importable(IMPORTABLE_TEXT),
                                core.parse_pvs("  /dev/sdh3  pve\n"))
    return {d["name"]: d for d in disks}


def test_a_disk_with_nothing_on_it_is_listed_as_free():
    d = _by_name()["sdc"]
    assert (d["role"], d["state"]) == ("empty", "free")


def test_zvols_roms_and_partitions_are_not_disks():
    names = set(_by_name())
    assert "zd0" not in names and "sr0" not in names and "sda1" not in names


def test_pool_membership_from_the_partition_label():
    d = _by_name()
    assert (d["sda"]["role"], d["sda"]["pool"], d["sda"]["state"]) == ("zfs pool", "tank_sda", "in use")
    assert d["sdd"]["pool"] == "rpool"


def test_a_damaged_unimported_pool_is_flagged_not_called_free():
    d = _by_name()["sdj"]
    assert d["pool"] == "mypool"          # the partition label, not the stale 'spool'
    assert d["state"] == "damaged" and "UNAVAIL" in d["detail"]


def test_a_zfs_label_is_never_called_free_even_when_the_import_listing_is_empty():
    # Seen live: `zpool import` came back empty once on a busy node, and the SD
    # card carrying a pool label was reported "free".
    disks = {d["name"]: d for d in core.classify_disks(LSBLK, IMPORTED, {}, {})}
    assert disks["sdj"]["state"] == "labelled" and "mypool" in disks["sdj"]["detail"]
    assert "sdj" not in [d["name"] for d in disks.values() if d["state"] == "free"]


def test_usb_attached_disks_are_marked():
    d = _by_name()
    assert d["sdh"]["usb"] and d["sdi"]["usb"] and not d["sda"]["usb"]


def test_lvm_and_mounted_filesystems():
    d = _by_name()
    assert (d["sdh"]["role"], d["sdh"]["detail"]) == ("lvm", "volume group pve")
    assert (d["sdi"]["role"], d["sdi"]["detail"]) == ("filesystem", "ext4 at /mnt/BigDat")
    assert (d["sdk"]["state"], d["sdk"]["rotational"]) == ("not mounted", False)


def test_unparseable_lsblk_yields_nothing_rather_than_crashing():
    assert core.classify_disks("not json", [], {}, {}) == []


# ── backup system ─────────────────────────────────────────────────────────────
TRANSCRIPT = """###JOBS
[{"enabled":0,"id":"backup-d6b4df56-8699","next-run":1789268400,"schedule":"04:00","storage":"bpool","vmid":"105,106"},
 {"all":1,"exclude":"104,148","id":"vera-estate","next-run":1789266600,"schedule":"02:30","storage":"pbs-estate","comment":"Nightly"}]
###STORAGE
Name              Type     Status           Total            Used       Available        %
bpool              dir     active       483098112        95872896       387225216   19.85%
pbs-estate         pbs     active       817889280       714000000       103889280   87.30%
###POOLS
backup	1195708186624	1486000000	1194222186624	0
rpool	1198295875584	760000000000	438295875584	63
tank_sdh	3826815565824	3300000000000	526815565824	86
###DATASETS
backup	1486000000	1150000000000	0
backup/pbs	1400000000	837000000000	837518622720
###SNAPS
      8 backup
     49 cpool
     66 rpool
###TIMERS
[{"next":1789227900000000,"left":1789227900000000,"last":1789227000242730,"passed":1789227000242730,"unit":"sanoid.timer","activates":"sanoid.service"},
 {"next":1789265700000000,"left":1789265700000000,"last":0,"passed":0,"unit":"vera-replicate.timer","activates":"vera-replicate.service"}]
###GUARD
-- No entries --
###REPL
2026-09-12T16:37:47+0100 corp vera-replicate[2484786]: replicated tank_sde/vfs/home
###VZDUMP
lxc-106|1789227000|2026-09-12 16:30:02 INFO: Finished Backup of VM 106 (00:00:40)
lxc-118|1789227300|2026-09-12 16:35:00 INFO: Starting Backup of VM 118 (lxc)
qemu-101|1752290086|2025-07-12 04:28:44 ERROR: Backup of VM 101 failed - no space
###RUNNING
2711 vzdump --all 1 --exclude 104,148 --storage pbs-estate
"""


def _s():
    return core.sections(TRANSCRIPT)


def test_sections_keep_multiline_json_intact():
    assert len(core.parse_jobs(_s()["JOBS"])) == 2


def test_jobs_default_enabled_when_pve_omits_the_field():
    jobs = {j["id"]: j for j in core.parse_jobs(_s()["JOBS"])}
    assert jobs["vera-estate"]["enabled"] and jobs["vera-estate"]["all"]
    assert not jobs["backup-d6b4df56-8699"]["enabled"]


def test_storage_sizes_are_converted_from_kib():
    st = {s["name"]: s for s in core.parse_pvesm_status(_s()["STORAGE"])}
    assert st["pbs-estate"]["total"] == 817889280 * 1024 and st["pbs-estate"]["type"] == "pbs"


def test_snapshots_timers_journal_and_runs():
    assert core.parse_snap_counts(_s()["SNAPS"]) == {"backup": 8, "cpool": 49, "rpool": 66}
    t = core.parse_timers(_s()["TIMERS"])
    assert t["sanoid.timer"]["last"] == 1789227000 and t["vera-replicate.timer"]["last"] is None
    assert core.parse_journal(_s()["GUARD"]) == []
    assert core.parse_journal(_s()["REPL"])[0]["msg"] == "replicated tank_sde/vfs/home"
    runs = {r["guest"]: r for r in core.parse_vzdump(_s()["VZDUMP"])}
    assert (runs["lxc-106"]["result"], runs["lxc-118"]["result"], runs["qemu-101"]["result"]) == \
        ("ok", "running", "error")
    assert runs["qemu-101"]["vmid"] == 101


def test_warnings_name_the_real_problems():
    s = _s()
    w = core.backup_warnings(core.parse_jobs(s["JOBS"]), core.parse_pvesm_status(s["STORAGE"]),
                             core.parse_pools(s["POOLS"]), core.parse_timers(s["TIMERS"]))
    text = " | ".join(w)
    assert "'pbs-estate' is 87% full" in text
    assert "pool tank_sdh is 86% full" in text
    assert "vera-snap-guard.timer is not installed" in text
    assert "no backup job is enabled" not in text        # vera-estate is enabled
    assert "root disk" not in text                        # the bpool job is disabled


def test_no_enabled_job_is_the_first_warning():
    w = core.backup_warnings([{"id": "old", "enabled": False, "storage": "bpool"}], [], [], {})
    assert w[0].startswith("no backup job is enabled")


def test_an_enabled_job_on_the_root_disk_folder_is_called_out():
    w = core.backup_warnings([{"id": "j", "enabled": True, "storage": "bpool"}],
                             [{"name": "bpool", "type": "dir", "active": True, "total": 0, "used": 0}],
                             [], {u: {"next": 1, "last": 1} for u in core.TIMER_UNITS})
    assert w == ["job j writes to 'bpool', a folder on the hypervisor's root disk"]


def test_scripts_are_read_only():
    for script in (core.DISKS_SCRIPT, core.BACKUP_SCRIPT):
        for verb in ("zpool create", "zpool destroy", "zfs destroy", "pvesh set", "pvesh create",
                     "pvesm add", "zpool import -", "rm "):
            assert verb not in script, verb
    assert "zpool import 2>&1" in core.DISKS_SCRIPT     # listing form only
