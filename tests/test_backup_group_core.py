"""One backup group's rules: which guests a job covers, each guest's latest copy
and state, every schedule with its owner, the Vera host's file backup, and the
warnings, including a failed attempt that a later copy has already fixed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.estate import backup_group_core as core  # noqa: E402
from vera.estate import estate_health_core as health  # noqa: E402

pytestmark = pytest.mark.critical

NOW = 1_789_300_000
H = 3600
ESTATE_JOB = {"id": "vera-estate", "enabled": True, "all": True, "exclude": "104,126,160",
              "vmid": "", "storage": "pbs-estate", "mode": "snapshot", "schedule": "02:30",
              "next_run": NOW + 10 * H}
OLD_JOB = {"id": "backup-d6b4", "enabled": False, "all": False, "vmid": "105,106", "exclude": "",
           "storage": "bpool", "mode": "snapshot", "schedule": "04:00", "next_run": NOW + 12 * H}
PBS = {"name": "pbs-estate", "type": "pbs", "active": True, "total": 800, "used": 120, "avail": 680}
BPOOL = {"name": "bpool", "type": "dir", "active": True, "total": 480, "used": 95, "avail": 385}


def guest(vmid, name, **kw):
    return dict({"vmid": vmid, "name": name, "type": "lxc", "node": "corp", "status": "running",
                 "template": 0, "cluster_id": "home"}, **kw)


def content(vmid, ctime, state=None, size=100):
    row = {"volid": f"pbs-estate:backup/ct/{vmid}/{ctime}", "vmid": vmid, "ctime": ctime, "size": size,
           "notes": f"g{vmid}"}
    if state:
        row["verification"] = {"state": state, "upid": "x"}
    return row


def test_systemctl_show_reads_unix_stamps_and_keeps_other_values():
    timer = core.parse_show("LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
                            "LastTriggerUSec=@1789260048\nNextElapseUSecRealtime=@1789345891\n")
    svc = core.parse_show("Result=success\nExecMainStatus=0\nActiveState=inactive\n"
                          "ExecMainStartTimestamp=@1789260048\nExecMainExitTimestamp=Sun 2026-09-13 03:21:26 BST\n")
    assert timer["LastTriggerUSec"] == 1789260048 and timer["UnitFileState"] == "enabled"
    assert svc["ExecMainExitTimestamp"] == "Sun 2026-09-13 03:21:26 BST"
    assert core.parse_show("ExecMainStartTimestamp=n/a")["ExecMainStartTimestamp"] is None
    host = core.host_backup(timer, svc)
    assert host["enabled"] and host["scheduled"] and not host["running"]
    assert host["last_start"] == 1789260048 and host["next"] == 1789345891
    assert core.host_backup({"LoadState": "not-found"}, {})["error"]


def test_latest_backups_keeps_the_newest_copy_and_its_verification():
    b = core.latest_backups([content(901, NOW - 30 * H, "ok"), content(901, NOW - 6 * H, size=7),
                             content(902, NOW - 2 * H, "failed"), {"vmid": "x"}])
    assert b[901]["count"] == 2 and b[901]["latest_at"] == NOW - 6 * H and b[901]["size"] == 7
    assert b[901]["verified"] is None and b[902]["verified"] == "failed"
    assert b[901]["storage"] == "pbs-estate"


def test_coverage_follows_enabled_jobs_all_with_excludes_and_vmid_lists():
    jobs = [ESTATE_JOB, OLD_JOB, dict(OLD_JOB, id="list", enabled=True, vmid="105, 106")]
    assert core.coverage(901, jobs) == {"covered_by": ["vera-estate"], "excluded_by": []}
    assert core.coverage(160, jobs) == {"covered_by": [], "excluded_by": ["vera-estate"]}
    assert core.coverage(105, jobs)["covered_by"] == ["vera-estate", "list"]


def test_guest_states():
    guests = [guest(901, "ipa"), guest(902, "dc"), guest(903, "new"), guest(160, "VFS-02"),
              guest(136, "Void", type="qemu", status="stopped"), guest(137, "Arch", type="qemu")]
    backups = core.latest_backups([content(901, NOW - 6 * H), content(902, NOW - 40 * H),
                                   content(136, NOW - 2 * H), content(137, NOW - 20 * H)])
    runs = [{"vmid": 136, "at": NOW - 8 * H, "result": "error", "line": "start failed"},
            {"vmid": 137, "at": NOW - 5 * H, "result": "error", "line": "QEMU exited"}]
    rows = {r["vmid"]: r for r in core.guest_rows(guests, [ESTATE_JOB], backups, runs, NOW)}
    assert rows[901]["state"] == "ok" and rows[901]["age_h"] == 6.0
    assert rows[902]["state"] == "stale"
    assert rows[903]["state"] == "never" and rows[903]["latest_at"] is None
    assert rows[160]["state"] == "excluded"
    assert rows[136]["state"] == "ok"            # the failure was fixed by a later copy
    assert rows[137]["state"] == "failed" and rows[137]["last_run"]["line"] == "QEMU exited"
    no_job = core.guest_rows([guest(5, "x")], [OLD_JOB], {}, [], NOW)
    assert no_job[0]["state"] == "not covered"


def test_schedules_list_every_owner_once():
    host = {"enabled": True, "scheduled": True, "next": NOW + 4 * H, "last_start": NOW - 20 * H}
    vera = {"enabled": False, "interval_hours": 24, "last_run": 0, "proxmox": {"storage": ""},
            "docker": {"enabled": True}}
    timers = {"corp": {"sanoid.timer": {"next": NOW + 60, "last": NOW - 60},
                       "vera-replicate.timer": {"next": None, "last": None}}}
    s = core.schedules({"corp": [ESTATE_JOB, OLD_JOB], "pve02": [ESTATE_JOB]}, timers, vera, host)
    assert [x["id"] for x in s] == ["vera-estate", "backup-d6b4", "corp:sanoid.timer",
                                    "corp:vera-replicate.timer", "nodes.backup", core.HOST_TIMER]
    assert s[0]["what"] == "every guest except 104,126,160" and s[1]["what"] == "guests 105,106"
    assert s[3]["enabled"] is False and s[4]["what"].endswith("plus Docker volumes")
    assert s[5]["owner"] == "vera-host" and s[5]["next_run"] == NOW + 4 * H
    assert [x["owner"] for x in core.schedules({}, {}, None, {"error": "x"})] == []


def messages(findings, severity=None):
    return [f["message"] for f in findings if severity is None or f["severity"] == severity]


def test_warnings_in_plain_language():
    rows = core.guest_rows(
        [guest(901, "ipa"), guest(902, "dc"), guest(903, "new"), guest(160, "VFS-02"), guest(137, "Arch")],
        [ESTATE_JOB], core.latest_backups([content(901, NOW - H), content(902, NOW - 50 * H),
                                           content(137, NOW - 30 * H)]),
        [{"vmid": 137, "at": NOW - 2 * H, "result": "error", "line": "QEMU exited with code 1"}], NOW)
    vera_on = {"enabled": True, "interval_hours": 24, "proxmox": {"storage": "pbs-estate"}}
    scheds = core.schedules({"corp": [ESTATE_JOB, OLD_JOB]}, {}, vera_on, None)
    f = core.warnings(rows, scheds, None, True, NOW)
    errors = messages(f, health.ERROR)
    assert errors == ["The last backup of Arch (137) failed."]
    warns = messages(f, health.WARN)
    assert "Vera's own backup schedule and a Proxmox job both back up every guest." in warns
    assert "1 guest(s) in a backup job have no backup yet: new (903)." in warns
    assert "1 guest(s) have not been backed up in 36 hours: dc (902)." in warns
    infos = messages(f, health.INFO)
    assert "1 disabled Proxmox backup job(s) are still defined." in infos
    assert "1 guest(s) are left out of the backup job on purpose: VFS-02 (160)." in infos
    assert "No backup has been verified yet." in infos


def test_the_host_backup_warnings():
    def host(**kw):
        return dict({"enabled": True, "scheduled": True, "running": False, "result": "success",
                     "exit_status": "0", "last_start": NOW - 10 * H, "last_end": NOW - 9 * H}, **kw)
    assert core.warnings([], [], host(), False, NOW) == []
    assert messages(core.warnings([], [], host(enabled=False), False, NOW))[0].startswith(
        "The Vera host's file backup timer is off")
    assert messages(core.warnings([], [], host(result="exit-code", exit_status="2"), False, NOW),
                    health.ERROR) == ["The Vera host's last file backup failed."]
    assert messages(core.warnings([], [], host(last_end=NOW - 40 * H), False, NOW)) == [
        "The Vera host's file backup last finished 40 hours ago."]
    assert messages(core.warnings([], [], host(last_start=None, last_end=None), False, NOW)) == [
        "The Vera host's file backup has never run."]
    assert messages(core.warnings([], [], {"error": "systemctl missing"}, False, NOW), health.INFO) == [
        "The Vera host's own file backup can only be read on the Vera host."]


def test_run_target_uses_the_covering_job_else_pbs():
    row = {"node": "corp", "covered_by": ["vera-estate"]}
    storages = [dict(PBS, node="corp"), dict(BPOOL, node="corp")]
    assert core.run_target(row, [ESTATE_JOB], storages) == {"storage": "pbs-estate", "mode": "snapshot",
                                                             "job": "vera-estate"}
    excluded = {"node": "corp", "covered_by": []}
    assert core.run_target(excluded, [ESTATE_JOB], storages, mode="stop")["storage"] == "pbs-estate"
    assert core.run_target(excluded, [], storages, storage="bpool")["storage"] == "bpool"
    assert "not an active backup storage" in core.run_target(excluded, [], storages, storage="nope")["error"]
    assert "mode must be" in core.run_target(row, [ESTATE_JOB], storages, mode="fast")["error"]
    assert "no active backup storage" in core.run_target(excluded, [], [])["error"]


def test_summarize_joins_nodes_and_sorts_errors_first():
    status = {"jobs": [ESTATE_JOB], "storages": [PBS], "timers": {}, "snapshots": {"cpool": 12},
              "replication": [{"at": "t", "msg": "ok"}], "warnings": ["backup storage 'bpool' is 90% full"],
              "runs": [{"vmid": 137, "at": NOW - H, "result": "error", "line": "QEMU exited"}]}
    out = core.summarize([{"node": "corp", "status": status}, {"node": "pve02", "status": {"error": "ssh refused"}}],
                         [content(137, NOW - 30 * H, "ok"), content(901, NOW - H, "ok")],
                         [guest(137, "Arch"), guest(901, "ipa")], None, None, now=NOW)
    assert out["counts"]["failed"] == 1 and out["counts"]["ok"] == 1
    assert out["findings"][0]["severity"] == health.ERROR
    assert "Could not read the backup system on pve02." in messages(out["findings"], health.WARN)
    assert "Backup storage 'bpool' is 90% full." in messages(out["findings"], health.WARN)
    assert out["storages"][0]["node"] == "corp" and out["snapshots"] == {"corp": {"cpool": 12}}
    assert "No backup has been verified yet." not in messages(out["findings"])
