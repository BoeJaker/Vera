"""Unit tests for Foundry VM import/export and pre-wipe salvage.

The interesting properties are not "does it emit a command" but the ones that
stop this destroying something: a wipe cannot be authorised by an unverified
backup, a harvest of a developer's home directory is not 90% node_modules, and
an image whose extension lies is imported as what it actually is.

Imported via the lowercase `vera.*` path so it resolves to THIS worktree.
"""
import fnmatch

from vera.foundry.salvage_core import (
    DEFAULT_PROFILES, EXCLUDES, PROFILE_ORDER, PROFILE_SETS,
    detect_os, estimate, harvest_plan, image_plan, verify_plan,
    wipe_authorisation,
)
from vera.foundry.vmport_core import (
    detect_format, export_plan, guest_defaults, import_plan,
)


# ── The wipe gate: the property that matters most ───────────────────────────

def test_wipe_refused_with_no_backup():
    r = wipe_authorisation([])
    assert r["allowed"] is False
    assert "nothing has been backed up" in r["reason"]


def test_wipe_refused_when_backup_missing():
    r = wipe_authorisation([{"artifact": "disk.img.gz", "exists": False}])
    assert r["allowed"] is False
    assert "missing" in r["reason"]


def test_wipe_refused_when_backup_unverified():
    """A finished job is not a verified backup -- this is the exact mistake
    that motivated the gate."""
    r = wipe_authorisation([{"artifact": "disk.img.gz", "exists": True,
                             "verified": False}])
    assert r["allowed"] is False
    assert "not verified" in r["reason"]
    assert "hint" in r


def test_wipe_allowed_only_when_every_backup_verified():
    both_ok = [{"artifact": "disk.img.gz", "exists": True, "verified": True},
               {"artifact": "harvest.tgz", "exists": True, "verified": True}]
    assert wipe_authorisation(both_ok)["allowed"] is True

    one_bad = both_ok[:1] + [{"artifact": "harvest.tgz", "exists": True,
                              "verified": False}]
    assert wipe_authorisation(one_bad)["allowed"] is False


def test_force_is_allowed_but_never_silent():
    r = wipe_authorisation([{"artifact": "d.img.gz", "exists": True,
                             "verified": False}], force=True)
    assert r["allowed"] is True
    assert r["forced"] is True
    assert "warning" in r and r["warning"]


def test_verification_is_a_read_not_a_stat():
    """Checking a file exists proves nothing about its contents."""
    steps = verify_plan("/backups/disk.img.gz")["steps"]
    cmds = " ".join(" ".join(s.get("cmd", [])) + s.get("shell", "") for s in steps)
    assert "gzip -t" in cmds          # reads and CRC-checks the whole stream
    assert "ls" not in cmds and "stat" not in cmds


def test_tar_verification_walks_the_archive():
    steps = verify_plan("/backups/harvest.tar.gz")["steps"]
    assert any("tar" in " ".join(s.get("cmd", [])) for s in steps)


# ── Harvest selection ───────────────────────────────────────────────────────

def test_default_profiles_are_all_real():
    for p in DEFAULT_PROFILES:
        assert p in PROFILE_ORDER
        for os_kind in PROFILE_SETS:
            assert p in PROFILE_SETS[os_kind], f"{p} missing for {os_kind}"


def test_vscode_settings_are_captured_on_every_platform():
    """The user asked for editor settings specifically."""
    for os_kind, needle in (("linux", ".config/Code/User/settings.json"),
                            ("windows", "AppData/Roaming/Code/User/settings.json"),
                            ("macos", "Application Support/Code/User/settings.json")):
        pats = harvest_plan(os_kind, ["editor"])["includes"]
        assert any(needle in p for p in pats), f"{os_kind} misses {needle}"


def test_extension_list_is_captured_not_the_extensions_themselves():
    """Extensions are large and re-downloadable; the list of them is not."""
    pats = harvest_plan("linux", ["editor"])["includes"]
    assert any(p.endswith("extensions/*/package.json") for p in pats)
    assert not any(p.endswith(".vscode/extensions/**") for p in pats)


def test_ssh_keys_are_in_the_default_set():
    plan = harvest_plan("linux")
    assert any(".ssh" in p for p in plan["includes"])


def test_unknown_profile_is_refused_with_the_list():
    r = harvest_plan("linux", ["documents", "nonsense"])
    assert "error" in r and "nonsense" in r["error"]
    assert r["available"] == PROFILE_ORDER


def test_unknown_os_is_refused():
    assert "error" in harvest_plan("plan9")


def test_databases_excluded_from_defaults():
    """A live database directory copied file-by-file is usually corrupt; it
    needs a dump, so it is opt-in."""
    assert "databases" not in DEFAULT_PROFILES


# ── Exclusions: what makes a harvest usable rather than enormous ────────────

def _excluded(path, excludes):
    return any(fnmatch.fnmatch(path, e) for e in excludes)


def test_the_usual_junk_is_excluded():
    ex = harvest_plan("linux")["excludes"]
    for junk in ("home/joe/projects/app/node_modules/left-pad/index.js",
                 "home/joe/.cache/pip/wheels/x.whl",
                 "home/joe/x/__pycache__/y.pyc",
                 "home/joe/.venv/lib/python3/site.py",
                 "Users/joe/AppData/Local/Google/Chrome/User Data/Default/Cache/f_00001"):
        assert _excluded(junk, ex), f"should be excluded: {junk}"


def test_virtual_disks_and_swap_are_excluded():
    """A 25 GB .vdi inside a home directory would dwarf the real content --
    which is exactly the file this project just spent an hour moving."""
    ex = harvest_plan("linux")["excludes"]
    for big in ("Users/Lenovo/Virtualbox/Kali/disk.vdi",
                "pagefile.sys", "hiberfil.sys", "swapfile",
                "home/joe/vm/win.qcow2"):
        assert _excluded(big, ex), f"should be excluded: {big}"


def test_real_work_is_not_excluded():
    ex = harvest_plan("linux")["excludes"]
    for keep in ("home/joe/projects/app/src/main.py",
                 "home/joe/.ssh/id_ed25519",
                 "home/joe/.config/Code/User/settings.json",
                 "home/joe/Documents/thesis.odt"):
        assert not _excluded(keep, ex), f"should be kept: {keep}"


def test_git_working_tree_kept_but_pack_files_dropped():
    ex = harvest_plan("linux")["excludes"]
    assert not _excluded("home/joe/repo/src/x.py", ex)
    assert _excluded("home/joe/repo/.git/objects/pack/pack-abc.pack", ex)


def test_extra_excludes_are_additive_not_replacing():
    plan = harvest_plan("linux", extra_excludes=["**/secret/**"])
    assert "**/secret/**" in plan["excludes"]
    for e in EXCLUDES:
        assert e in plan["excludes"]


# ── OS detection ────────────────────────────────────────────────────────────

def test_detects_linux_windows_macos():
    assert detect_os(["etc/os-release", "etc/fstab", "etc/passwd"])["os"] == "linux"
    assert detect_os(["Windows/System32/config/SYSTEM",  # pragma: allowlist secret
                      "Windows/explorer.exe"])["os"] == "windows"
    assert detect_os(["System/Library/CoreServices/SystemVersion.plist"])["os"] == "macos"


def test_a_data_partition_is_reported_as_unknown_not_guessed():
    r = detect_os(["photos/2019/img.jpg", "music/album/track.flac"])
    assert r["os"] == "unknown"
    assert r["confidence"] == 0


# ── Imaging ─────────────────────────────────────────────────────────────────

def test_image_plan_saves_the_partition_table():
    """Without it, loop-mounting the image later means guessing byte offsets."""
    p = image_plan("/dev/sdk", "/backups/node1")
    assert any(s["stage"] == "table" for s in p["steps"])
    assert any("sfdisk" in " ".join(s.get("cmd", [])) for s in p["steps"])


def test_image_plan_carries_its_own_verification():
    p = image_plan("/dev/sdk", "/backups/node1")
    assert p["verify_with"]
    assert any("gzip" in " ".join(s.get("cmd", [])) for s in p["verify_with"])


def test_image_plan_requires_both_arguments():
    assert "error" in image_plan("", "/backups")
    assert "error" in image_plan("/dev/sdk", "")


# ── VM import ───────────────────────────────────────────────────────────────

def test_format_detected_from_magic_over_extension():
    """A qcow2 named .img imported as raw boots to nothing."""
    r = detect_format("disk.img", b"QFI\xfb\x00\x00\x00\x03")
    assert r["format"] == "qcow2"
    assert r["by_magic"] is True
    assert "mismatch" not in r          # .img is not a claimed qcow2 rival


def test_extension_lie_is_flagged():
    r = detect_format("disk.vdi", b"KDMV\x01\x00\x00\x00")
    assert r["format"] == "vmdk"
    assert "mismatch" in r


def test_iso_is_refused_as_a_disk_import():
    r = import_plan(147, "/tmp/kali.iso", "local-zfs")
    assert "error" in r and "ISO" in r["error"]


def test_ova_is_refused_with_an_explanation():
    r = import_plan(147, "/tmp/appliance.ova", "local-zfs")
    assert "error" in r and "OVF" in r["error"] or "unpack" in r["error"]


def test_import_attaches_the_disk_and_sets_boot_order():
    """importdisk leaves the disk as unusedN; without these the VM boots to
    a BIOS prompt, which is how this went wrong by hand."""
    p = import_plan(147, "/dump/kali.vdi", "local-zfs", name="kali")
    stages = [s["stage"] for s in p["steps"]]
    assert "import" in stages
    assert stages.index("attach") > stages.index("import")
    assert "boot-order" in stages


def test_windows_guests_get_sata_not_virtio():
    """A Windows image from another hypervisor has no virtio driver, so a
    virtio disk is invisible to it."""
    p = import_plan(148, "/dump/win.vhdx", "local-zfs", os_hint="Windows 10")
    assert p["guest"]["disk_bus"] == "sata"
    assert any("--sata0" in " ".join(s["cmd"]) for s in p["steps"])


def test_linux_guests_get_virtio():
    p = import_plan(149, "/dump/deb.qcow2", "local-zfs", os_hint="Debian 12")
    assert p["guest"]["disk_bus"] == "scsi"


def test_win11_gets_uefi_and_a_tpm():
    p = import_plan(150, "/dump/w11.vhdx", "local-zfs", os_hint="Windows 11")
    assert p["guest"]["bios"] == "ovmf"
    assert any(s["stage"] == "tpm" for s in p["steps"])


def test_sparse_image_size_is_called_out():
    """The Kali import was 25 GB of data in a 500 GB virtual disk."""
    p = import_plan(147, "/dump/kali.vdi", "local-zfs",
                    virtual_bytes=500_000_000_000, actual_bytes=25_630_000_000)
    assert any("sparse" in n for n in p["notes"])
    assert p["estimated_disk_bytes"] == 500_000_000_000


def test_import_requires_its_arguments():
    assert "error" in import_plan(0, "/x.vdi", "local-zfs")
    assert "error" in import_plan(147, "", "local-zfs")
    assert "error" in import_plan(147, "/x.vdi", "")


# ── VM export ───────────────────────────────────────────────────────────────

def test_export_insists_the_vm_is_stopped():
    p = export_plan(147, "local-zfs:vm-147-disk-0", "/dump")
    assert p["steps"][0]["stage"] == "stop-check"
    assert any("stopped" in n for n in p["notes"])


def test_export_verifies_what_it_wrote():
    p = export_plan(147, "local-zfs:vm-147-disk-0", "/dump")
    assert any(s["stage"] == "verify" for s in p["steps"])


def test_raw_export_warns_about_losing_sparseness():
    p = export_plan(147, "local-zfs:vm-147-disk-0", "/dump", fmt="raw")
    assert any("sparse" in n for n in p["notes"])


def test_unsupported_export_format_refused():
    assert "error" in export_plan(147, "d", "/dump", fmt="tar")


# ── Estimation ──────────────────────────────────────────────────────────────

def test_estimate_groups_and_ranks():
    e = estimate([
        {"path": "a.py", "bytes": 100, "profile": "code"},
        {"path": "b.odt", "bytes": 5000, "profile": "documents"},
        {"path": "c.png", "bytes": 900, "profile": "documents"},
    ])
    assert e["files"] == 3 and e["bytes"] == 6000
    assert e["by_profile"]["documents"]["files"] == 2
    assert e["largest"][0]["path"] == "b.odt"


def test_estimate_handles_nothing():
    e = estimate([])
    assert e["files"] == 0 and e["bytes"] == 0
