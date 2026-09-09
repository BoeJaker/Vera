"""Unit tests for Foundry security baselines.

The properties worth pinning are the ones that make this trustworthy on a live
estate: a verify never mutates, "didn't run" is never reported as "failed",
locking out password auth cannot happen on a host with no working key, and a
control that used to pass and now doesn't is distinguishable from one that
never passed.

Imported via the lowercase `vera.*` path so it resolves to THIS worktree.
"""
from vera.foundry.security_core import (
    BY_ID, CONTROLS, FIM_DEFAULT, FIM_PATHS, PROFILES, SEVERITY,
    catalogue, drift, fim_area_of, fim_diff, fim_parse, fim_scan_script,
    parse_results, profile, render_apply, render_verify,
)


# ── Control definitions are complete and coherent ───────────────────────────

def test_every_control_is_fully_specified():
    for c in CONTROLS:
        for field in ("id", "title", "why", "severity", "standard",
                      "apply", "check", "remediate"):
            assert c.get(field), f"{c.get('id')} missing {field}"
        assert c["severity"] in SEVERITY


def test_control_ids_are_unique():
    ids = [c["id"] for c in CONTROLS]
    assert len(ids) == len(set(ids))


def test_every_check_emits_pass_or_fail():
    """parse_results only understands PASS/FAIL; a check that prints anything
    else silently becomes 'unknown' forever."""
    for c in CONTROLS:
        assert "PASS" in c["check"] and "FAIL" in c["check"], c["id"]


def test_profiles_reference_real_controls():
    for name, p in PROFILES.items():
        for cid in p["controls"]:
            assert cid in BY_ID, f"profile {name} references unknown {cid}"


def test_unknown_profile_is_refused():
    assert "error" in profile("does-not-exist")


def test_baseline_profile_resolves_with_counts():
    p = profile("baseline")
    assert p["controls"]
    assert sum(p["counts"].values()) == len(p["controls"])


# ── The lockout guard ───────────────────────────────────────────────────────

def test_a_guard_exists_for_the_lockout_controls():
    """Disabling password auth without a working key strands the host -- this
    is exactly what happened to the Pi card earlier in this project."""
    guards = [c for c in CONTROLS if c.get("guard_for")]
    assert guards, "no guard control defined"
    guarded = {g for c in guards for g in c["guard_for"]}
    assert "ssh-no-password-auth" in guarded
    assert "ssh-no-root-password" in guarded


def test_apply_runs_guards_first_and_aborts_on_failure():
    script = render_apply(profile("baseline")["controls"])
    guard_pos = script.index("ssh-keys-present")
    lockout_pos = script.index("PasswordAuthentication no")
    assert guard_pos < lockout_pos, "guard must be evaluated before the lockout"
    assert "APPLY_ABORTED" in script
    assert "exit 1" in script


def test_minimal_profile_contains_no_lockout_controls():
    """For appliances you cannot easily get back into."""
    ids = PROFILES["minimal"]["controls"]
    assert "ssh-no-password-auth" not in ids
    assert "ssh-no-root-password" not in ids


# ── Verify must not mutate ──────────────────────────────────────────────────

def test_verify_script_only_reads():
    script = render_verify(CONTROLS)
    for mutating in ("sed -i", "chmod", "apt-get install", "ufw --force enable",
                     "systemctl enable", "passwd -l", "> /etc/"):
        assert mutating not in script, f"verify would mutate: {mutating}"


def test_verify_covers_every_control_given():
    controls = profile("exposed")["controls"]
    script = render_verify(controls)
    for c in controls:
        assert c["id"] in script


def test_dry_run_apply_changes_nothing():
    script = render_apply(profile("baseline")["controls"], dry_run=True)
    assert "WOULD APPLY" in script
    assert "sed -i" not in script


# ── Parsing: unknown is not failure ─────────────────────────────────────────

def test_pass_and_fail_are_read():
    controls = profile("minimal")["controls"]
    out = "\n".join("%s=PASS" % c["id"] for c in controls) + "\nVERIFY_COMPLETE"
    r = parse_results(out, controls)
    assert r["passed"] == len(controls) and r["failed"] == 0
    assert r["complete"] is True


def test_a_check_that_never_ran_is_unknown_not_failed():
    """A truncated run must not invent failures."""
    controls = profile("baseline")["controls"]
    out = "%s=PASS\n" % controls[0]["id"]          # then the host hung
    r = parse_results(out, controls)
    assert r["passed"] == 1
    assert r["failed"] == 0
    assert r["unknown"] == len(controls) - 1
    assert r["complete"] is False


def test_partial_output_still_parses():
    controls = profile("baseline")["controls"]
    r = parse_results("%s=FAIL\ngarbage line\n" % controls[1]["id"], controls)
    assert r["failed"] == 1


def test_worst_severity_is_reported():
    controls = profile("baseline")["controls"]
    crit = next(c for c in controls if c["severity"] == "critical")
    r = parse_results("%s=FAIL" % crit["id"], controls)
    assert r["worst"] == "critical"


def test_abort_is_surfaced():
    r = parse_results("ssh-keys-present=FAIL\nAPPLY_ABORTED\n")
    assert r["aborted"] is True


def test_remediation_only_offered_for_failures():
    controls = profile("minimal")["controls"]
    out = "\n".join("%s=PASS" % c["id"] for c in controls)
    for row in parse_results(out, controls)["results"]:
        assert row["remediate"] == ""


# ── Drift ───────────────────────────────────────────────────────────────────

def _res(**states):
    return {"results": [{"id": k, "state": v} for k, v in states.items()]}


def test_regression_is_distinguished_from_never_passing():
    prev = _res(a="pass", b="fail")
    cur = _res(a="fail", b="fail")
    d = drift(prev, cur)
    assert d["regressed"] == ["a"]
    assert d["still_failing"] == ["b"]
    assert d["drifted"] is True


def test_fixes_are_reported_too():
    d = drift(_res(a="fail"), _res(a="pass"))
    assert d["fixed"] == ["a"] and d["drifted"] is False


def test_no_change_is_not_drift():
    d = drift(_res(a="pass"), _res(a="pass"))
    assert d["drifted"] is False and "no regressions" in d["summary"]


def test_regression_severity_uses_the_real_control():
    prev = _res(**{"ssh-no-root-password": "pass"})
    cur = _res(**{"ssh-no-root-password": "fail"})
    assert drift(prev, cur)["worst_regression"] == "critical"


# ── File integrity ──────────────────────────────────────────────────────────

def test_fim_watches_where_access_is_granted():
    access = FIM_PATHS["access"]
    assert any("authorized_keys" in p for p in access)
    assert any("sudoers" in p for p in access)
    assert any("shadow" in p for p in access)


def test_fim_watches_where_persistence_lands():
    p = FIM_PATHS["persistence"]
    assert any("systemd" in x for x in p)
    assert any("cron" in x for x in p)


def test_fim_scan_is_read_only_and_hashes_only():
    s = fim_scan_script()
    assert "sha256sum" in s
    for mutating in ("rm ", "chmod", "> /etc", "sed -i"):
        assert mutating not in s
    # The manifest must not contain file contents -- it would be a copy of
    # every private key on the host.
    assert "cat " not in s


def test_fim_parse_reads_sha256sum_output():
    out = ("a" * 64 + "  /etc/passwd\n"
           + "b" * 64 + "  /root/.ssh/authorized_keys\n"
           "not a hash line\nFIM_COMPLETE")
    m = fim_parse(out)
    assert m["/etc/passwd"] == "a" * 64
    assert len(m) == 2


def test_fim_detects_added_modified_removed():
    base = {"/etc/passwd": "a" * 64, "/etc/hosts": "b" * 64}
    cur = {"/etc/passwd": "c" * 64,                      # modified
           "/root/.ssh/authorized_keys": "d" * 64}       # added; hosts removed
    d = fim_diff(base, cur)
    assert d["clean"] is False
    assert d["changed"] == ["/etc/passwd"]
    assert d["added"] == ["/root/.ssh/authorized_keys"]
    assert d["removed"] == ["/etc/hosts"]


def test_a_new_authorized_key_is_urgent():
    d = fim_diff({}, {"/root/.ssh/authorized_keys": "a" * 64})
    assert d["urgent"], "a new authorized_keys entry must be flagged"
    assert d["urgent"][0]["area"] == "access"
    assert d["urgent"][0]["meaning"]


def test_removals_are_reported_not_ignored():
    """Deleting an audit rule is as much a signal as adding a key."""
    d = fim_diff({"/etc/cron.d/backup": "a" * 64}, {})
    assert d["removed"] == ["/etc/cron.d/backup"]
    assert any(e["change"] == "removed" for e in d["events"])


def test_identical_manifests_are_clean():
    m = {"/etc/passwd": "a" * 64}
    d = fim_diff(m, dict(m))
    assert d["clean"] is True and not d["events"]


def test_area_classification():
    assert fim_area_of("/root/.ssh/authorized_keys") == "access"
    assert fim_area_of("/etc/systemd/system/evil.service") == "persistence"
    assert fim_area_of("/etc/resolv.conf") == "network"
    assert fim_area_of("/tmp/whatever") == "other"


def test_binaries_not_watched_by_default():
    """Watching /usr/bin produces a diff on every package update, which trains
    people to ignore the alerts."""
    assert "binaries" not in FIM_DEFAULT


# ── The catalogue is the human-readable standard ────────────────────────────

def test_catalogue_explains_without_leaking_the_apply_commands():
    cat = catalogue()
    assert cat["profiles"] and cat["controls"]
    for c in cat["controls"]:
        assert c["why"] and c["standard"]
        assert "apply" not in c


def test_catalogue_does_not_overclaim_compliance():
    assert "not a compliance claim" in catalogue()["note"]
