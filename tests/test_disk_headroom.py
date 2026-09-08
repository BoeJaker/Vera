"""The disk filled with no warning, and took both databases down with it.

2026-09-08: `/mnt/dockerdata` hit **0 bytes free**. Postgres crashed and could
not complete crash recovery because it could not write; Neo4j refused to start
with `java.io.IOException: No space left on device`. Neither said "disk" — Vera
reported "cannot connect to postgres / neo4j", and `obs.health` reported both as
`true` the whole time because it proves a port is open, not that the database
answers.

490 exited `vera-sbx-*` session sandboxes had accumulated, the oldest 48 days
old. A loop run leaves one behind; a 12-goal census leaves twelve.

Every number in these fixtures is from that incident.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.docker import disk_headroom as DH                 # noqa: E402


# ── the moment it mattered ──────────────────────────────────────────────────
def test_the_full_disk_is_critical():
    """492G total, 0 free — the state that took the estate down."""
    assert DH.level(492, 0) == DH.CRITICAL


def test_the_state_after_the_reap_is_still_not_ok():
    """50G free of 492G is 90% used. Recovered, but not comfortable — and
    saying 'ok' here is how it silently refills."""
    assert DH.level(492, 50) == DH.WARN


def test_a_healthy_disk_is_ok():
    assert DH.level(492, 200) == DH.OK


# ── why two thresholds and not one ──────────────────────────────────────────
def test_a_percentage_alone_would_miss_a_big_disk_running_out():
    """10% free of 492G is 49G — comfortable by percentage, and a percentage-
    only rule would say WARN. But 4G free of 492G is 99% used AND tiny."""
    assert DH.level(492, 4) == DH.CRITICAL


def test_an_absolute_alone_would_miss_a_small_disk_running_out():
    """19G free sounds fine in isolation. On a 20G volume it is 5% used —
    nothing is wrong, and an absolute-only rule would cry CRITICAL."""
    assert DH.level(20, 19) == DH.OK


def test_a_small_disk_nearly_full_still_fires():
    assert DH.level(20, 0.5) == DH.CRITICAL


def test_the_absolute_floor_fires_where_the_PERCENTAGE_would_not():
    """100G with 8G free is 92% used — under the 95% critical line, yet 8G is
    not enough to survive a Postgres recovery plus an image pull. Without the
    absolute floor this reads WARN, and the disk fills anyway.

    Added after a mutation dropping the absolute floor survived: every earlier
    fixture happened to breach the percentage too, so nothing tested the floor
    on its own.
    """
    assert DH.level(100, 8) == DH.CRITICAL
    assert DH.level(100, 19) == DH.WARN          # warn floor, still under 85%


def test_the_percentage_fires_where_the_ABSOLUTE_would_not():
    """A 1TB array at 96% still has 40G free — comfortable by the floor, but a
    disk that full is filling fast. The mirror of the case above."""
    assert DH.level(1000, 40) == DH.CRITICAL
    assert DH.level(1000, 130) == DH.WARN        # 87% used, 130G free


# ── an unreadable disk is not an alarm ──────────────────────────────────────
def test_unreadable_readings_do_not_cry_wolf():
    """A monitor that alarms on its own failure to read gets muted, and then
    it is not a monitor."""
    for total, free in ((None, None), (0, 0), ("x", "y"), (492, None), (-1, 5)):
        assert DH.level(total, free) == DH.OK


def test_describe_says_so_rather_than_inventing_numbers():
    d = DH.describe("/mnt/dockerdata", None, None)
    assert d["level"] == DH.OK and d["readable"] is False


# ── the message has to be actionable ────────────────────────────────────────
def test_the_critical_message_names_the_real_consequence():
    """"disk low" did not help anyone at 03:00. What the databases actually do
    is the useful part."""
    n = DH.describe("/mnt/dockerdata", 492, 0)["note"]
    assert "Postgres" in n and "Neo4j" in n


def test_critical_reads_differently_from_warn():
    """A reader woken by this must be able to tell 'act now' from 'watch it'
    without comparing numbers. Added because a mutation that downgraded the
    critical wording still passed — the test only checked the tail of it."""
    crit = DH.describe("/mnt/dockerdata", 492, 0)["note"]
    warn = DH.describe("/mnt/dockerdata", 492, 50)["note"]
    assert "CRITICAL" in crit and "CRITICAL" not in warn


def test_the_message_carries_both_numbers():
    d = DH.describe("/mnt/dockerdata", 492, 50)
    assert "50G" in d["note"] and "492G" in d["note"] and "89.8%" in d["note"]
    assert d["pct_used"] == 89.8


# ── df parsing ──────────────────────────────────────────────────────────────
def test_a_real_df_row_parses():
    row = DH.parse_df_line("/dev/sdb1  492G  468G  0G  100% /mnt/dockerdata")
    assert row["mount"] == "/mnt/dockerdata"
    assert row["total_gb"] == 492 and row["free_gb"] == 0


def test_the_header_and_junk_are_skipped():
    assert DH.parse_df_line("Filesystem Size Used Avail Use% Mounted on") is None
    assert DH.parse_df_line("") is None
    assert DH.parse_df_line("garbage") is None


# ── the reaper: what it may and may not touch ───────────────────────────────
BOXES = [
    {"name": "vera-sbx-old", "running": False, "finished_hours_ago": 1163.0},
    {"name": "vera-sbx-day-old", "running": False, "finished_hours_ago": 27.0},
    {"name": "vera-sbx-recent", "running": False, "finished_hours_ago": 4.5},
    {"name": "vera-sbx-live", "running": True, "finished_hours_ago": None},
    {"name": "vera-dev-feat-someones-branch", "running": False,
     "finished_hours_ago": 900.0},
    {"name": "postgres", "running": True, "finished_hours_ago": None},
]


def test_only_old_exited_session_sandboxes_are_reapable():
    names = [r["name"] for r in DH.reapable(BOXES)]
    assert names == ["vera-sbx-old", "vera-sbx-day-old"]


def test_a_dev_sandbox_is_never_reaped():
    """`vera-dev-*` belongs to an agent and may sit idle for weeks mid-task.
    Reaping one destroys someone's work in progress."""
    assert not any(r["name"].startswith("vera-dev")
                   for r in DH.reapable(BOXES, retain_hours=1))


def test_a_running_container_is_never_reaped():
    assert not any(r["name"] == "vera-sbx-live" for r in DH.reapable(BOXES, 0.1))


def test_a_RESTARTED_container_is_not_reaped_on_its_old_finish_time():
    """The dangerous case, and the one the fixtures missed.

    `docker inspect` keeps `FinishedAt` from a container's PREVIOUS stop, so a
    long-lived sandbox that was restarted an hour ago still reports a finish
    time from weeks back. Reading age alone would delete a container that is
    running right now, mid-run.

    Added after a mutation removing the running check survived: every running
    fixture had an unknown finish time, so the unknown-age guard was silently
    doing this one's job.
    """
    restarted = [{"name": "vera-sbx-restarted", "running": True,
                  "finished_hours_ago": 900.0}]
    assert DH.reapable(restarted, retain_hours=24) == []
    assert DH.reap_summary(restarted)["running_kept"] == 1


def test_unrelated_containers_are_never_reaped():
    assert not any(r["name"] == "postgres" for r in DH.reapable(BOXES, 0.1))


def test_the_24h_floor_holds():
    """The operator's constraint during the incident: do not stop anything used
    in the last 24 hours. 27h goes, 4.5h stays."""
    names = [r["name"] for r in DH.reapable(BOXES, retain_hours=24)]
    assert "vera-sbx-day-old" in names and "vera-sbx-recent" not in names


def test_an_unknown_finish_time_is_KEPT():
    """A reaper that guesses is worse than a full disk. An exited container
    whose timestamp will not parse is exactly the one to leave alone."""
    odd = [{"name": "vera-sbx-mystery", "running": False,
            "finished_hours_ago": None},
           {"name": "vera-sbx-mystery2", "running": False,
            "finished_hours_ago": "ages"}]
    assert DH.reapable(odd, retain_hours=0) == []


def test_the_retention_is_adjustable_but_still_excludes_the_living():
    assert len(DH.reapable(BOXES, retain_hours=0)) == 3   # all three exited sbx
    assert len(DH.reapable(BOXES, retain_hours=2000)) == 0


# ── the dry run has to be honest ────────────────────────────────────────────
def test_the_summary_accounts_for_every_session_container():
    s = DH.reap_summary(BOXES)
    assert s["session_total"] == 4
    assert s["running_kept"] + s["recent_kept"] + s["reapable"] == s["session_total"]


def test_the_summary_says_what_it_is_keeping_and_why():
    s = DH.reap_summary(BOXES)
    assert s["reapable"] == 2 and s["running_kept"] == 1 and s["recent_kept"] == 1
    assert "keeping" in s["note"]


def test_junk_input_does_not_crash_either_path():
    assert DH.reapable([None, "x", 3]) == []
    assert DH.reap_summary(None)["session_total"] == 0
