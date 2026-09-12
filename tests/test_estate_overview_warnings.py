"""The Overview's warnings beyond the state store, containers and guests:
backups, disks and the core services. Fixtures follow the shapes
pxstore.backup.status, pxstore.disks, docker.disk.status, vfs.health and
identity.status returned on prod on 12 Sep 2026."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.estate import estate_health_core as core  # noqa: E402

pytestmark = pytest.mark.critical

NOW = 1789250000


def backup_status(**kw):
    status = {"jobs": [{"id": "vera-estate", "enabled": True}],
              "runs": [{"guest": "lxc-902", "at": NOW - 3600, "result": "ok",
                        "line": "INFO: Finished Backup of VM 902 (00:01:34)"}],
              "warnings": []}
    status.update(kw)
    return status


def by_message(section):
    return {f["message"]: f for f in section["findings"]}


def test_a_healthy_backup_system_has_no_findings():
    out = core.backups_section([{"node": "corp", "status": backup_status()}], now=NOW)
    assert out["findings"] == []
    assert out["facts"] == {"nodes": 1, "enabled_jobs": 1, "guests_ok": 1, "guests_failed": 0,
                            "latest_run_at": NOW - 3600}


def test_backup_warnings_failures_and_silence():
    status = backup_status(
        jobs=[],
        warnings=["no backup job is enabled, so nothing backs the estate up"],
        runs=[{"guest": "qemu-141", "at": NOW - 40 * 3600, "result": "error",
               "line": "ERROR: start failed: QEMU exited with code 1"}])
    found = by_message(core.backups_section([{"node": "corp", "status": status}], now=NOW))
    assert found["No backup job is enabled, so nothing backs the estate up."]["severity"] == core.WARN
    failed = found["The last backup of qemu-141 on corp failed."]
    assert failed["severity"] == core.ERROR and "QEMU exited" in failed["detail"]
    assert found["No guest on corp has been backed up in 40 hours."]["severity"] == core.WARN


def test_an_unreadable_backup_system_is_a_warning_not_a_blank():
    out = core.backups_section([{"node": "corp", "status": {"error": "ssh: connect timed out"}}], now=NOW)
    [f] = out["findings"]
    assert f["severity"] == core.WARN and "corp" in f["message"]


def disk(name, state, pool="", usb=False):
    return {"name": name, "size": 1200243695616, "model": "EG1200JEHMC", "usb": usb,
            "state": state, "pool": pool, "detail": pool}


def test_disk_states_and_the_docker_disk():
    disks = {"disks": [disk("sda", "in use", "tank_sda"), disk("sdj", "damaged", "mypool", usb=True),
                       disk("sdk", "free"), disk("sdl", "importable", "old")]}
    out = core.storage_section([{"node": "corp", "status": disks}],
                               {"level": "critical", "pct_used": 97.1, "mount": "/mnt/dockerdata",
                                "note": "/mnt/dockerdata: 21G free of 738G"})
    found = by_message(out)
    damaged = found["Disk sdj (1200 GB, EG1200JEHMC, USB) on corp holds a damaged pool mypool."]
    assert damaged["severity"] == core.WARN
    assert found["Disk sdk (1200 GB, EG1200JEHMC) on corp is unused."]["severity"] == core.INFO
    assert found["Disk sdl (1200 GB, EG1200JEHMC) on corp holds pool old, which is not imported."]["severity"] == core.INFO
    assert found["The Docker data disk on the Vera host is 97.1% full."]["severity"] == core.ERROR
    assert out["facts"] == {"disks": 4, "in_use": 1, "docker_disk_used_pct": 97.1}


@pytest.mark.parametrize("level,expected", [("ok", []), ("warn", [core.WARN]), ("critical", [core.ERROR])])
def test_docker_disk_levels(level, expected):
    out = core.storage_section([], {"level": level, "pct_used": 80})
    assert [f["severity"] for f in out["findings"]] == expected


def test_services_all_up_has_no_findings():
    vfs = {"ok": True, "services": {"smbd": True, "nfs-server": True, "wg-quick@wg0": True}, "estate_mounts": 86}
    out = core.services_section(vfs, {"configured": True, "reachable": True, "version": "4.13.1"})
    assert out["findings"] == []
    assert out["facts"] == {"file_fabric": "up", "estate_mounts": 86, "directory": "FreeIPA 4.13.1"}


def test_services_down_and_directory_problems():
    vfs = {"ok": False, "services": {"smbd": False, "nfs-server": True}, "estate_mounts": 0}
    found = by_message(core.services_section(vfs, {"configured": True, "reachable": False,
                                                   "error": "timed out"}))
    assert found["smbd is down on the file server (VFS-02)."]["severity"] == core.ERROR
    assert found["VFS-02 has no estate mounts, so the estate share shows nothing."]["severity"] == core.WARN
    assert found["The directory (FreeIPA) is not reachable."]["severity"] == core.ERROR


@pytest.mark.parametrize("identity,message", [
    ({"configured": False, "reachable": False}, "No directory (FreeIPA) is configured in Vera."),
    ({"error": "identity.status is not loaded"}, "Could not check the directory (FreeIPA)."),
])
def test_directory_not_configured_or_not_checkable_is_a_warning(identity, message):
    [f] = core.services_section(None, identity)["findings"]
    assert (f["severity"], f["message"]) == (core.WARN, message)


def test_the_summary_lists_all_six_sections_in_order():
    result = core.summarize({})
    assert list(result["sections"]) == ["state_store", "containers", "guests", "backups", "storage", "services"]
    assert result["level"] == "ok"
