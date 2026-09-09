"""security_core.py -- pure, app-free security baselines for Foundry.

Foundry could already harden a host: a script ran, printed VERA_HARDEN_DONE,
and that was the end of it. Nobody recorded which hosts it touched, nothing
re-checked whether the settings survived the next person with sudo, and there
was no statement anywhere of what "hardened" was supposed to mean.

That is three different gaps, and they all come from the same thing -- the
baseline existed only as a shell script. Here a control is *data*: an id, why
it matters, how to set it, how to check it, and what a failure means. From one
definition you get the apply script, the verify script, and the human-readable
standard, and they cannot drift apart because they are generated from the same
rows.

── Verification is not the same as application ──────────────────────────────

Applying returns "the command ran". Verifying returns "the setting is still
what we set". Those differ the moment anyone edits sshd_config by hand, an
unattended upgrade replaces a config, or a container is rebuilt from an older
image. Drift is the normal case on a live estate, not an alarm condition, so
it is reported per control rather than as one pass/fail.

── File integrity ───────────────────────────────────────────────────────────

`fim_*` watches the paths where interference actually shows up: authorized_keys
gaining a line, a new setuid binary, a cron job nobody wrote, a systemd unit
appearing in a directory that had none. It records hashes, not contents, so a
manifest can be kept and compared without holding anyone's private keys.

Consumers import uppercase (Vera.vera.foundry.security_core); tests import
lowercase (vera.foundry.security_core) -- see `worktree-testable-cores-pattern`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

SEVERITY = ("critical", "high", "medium", "low")

# ── The controls ─────────────────────────────────────────────────────────────
#
# Each row is one checkable statement. `check` must print exactly PASS or FAIL
# (plus optional detail after a colon) so the results parse without guessing.
#
# `standard` cites the recognised control this maps to, so the panel can say
# what is being met rather than just "hardened". These are the CIS Linux
# Benchmark section numbers; they are a reference, not a claim of full CIS
# compliance -- a subset honestly labelled beats a badge nobody audited.

CONTROLS: List[Dict] = [
    {
        "id": "ssh-no-root-password",
        "title": "Root cannot log in over SSH with a password",
        "why": "A password-guessable root account reachable over the network is "
               "the single most attacked door on a Linux host.",
        "severity": "critical",
        "standard": "CIS 5.2.10",
        "apply": "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' "
                 "/etc/ssh/sshd_config",
        "check": "grep -qE '^PermitRootLogin\\s+(prohibit-password|no)' "
                 "/etc/ssh/sshd_config && echo PASS || echo FAIL",
        "remediate": "Set PermitRootLogin prohibit-password in /etc/ssh/sshd_config",
    },
    {
        "id": "ssh-no-password-auth",
        "title": "SSH accepts keys only, not passwords",
        "why": "Removes brute force as an attack path entirely rather than "
               "trying to rate-limit it.",
        "severity": "high",
        "standard": "CIS 5.2.11",
        "apply": "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' "
                 "/etc/ssh/sshd_config",
        "check": "grep -qE '^PasswordAuthentication\\s+no' /etc/ssh/sshd_config "
                 "&& echo PASS || echo FAIL",
        "remediate": "Set PasswordAuthentication no -- but confirm a working key "
                     "first, or you will lock yourself out",
        # Learned the hard way on the Pi: enabling SSH without a usable
        # credential is not access.
        "precondition": "at least one key in an authorized_keys file",
    },
    {
        "id": "firewall-default-deny",
        "title": "Host firewall defaults to deny inbound",
        "why": "A service that starts by accident should not be reachable by "
               "accident. Default-deny makes exposure an explicit act.",
        "severity": "high",
        "standard": "CIS 3.5",
        "apply": "if command -v ufw >/dev/null 2>&1; then "
                 "ufw default deny incoming >/dev/null 2>&1; "
                 "ufw allow 22/tcp >/dev/null 2>&1; "
                 "ufw --force enable >/dev/null 2>&1; fi",
        "check": "(ufw status verbose 2>/dev/null | grep -q 'deny (incoming)' "
                 "|| iptables -S INPUT 2>/dev/null | grep -q '^-P INPUT DROP') "
                 "&& echo PASS || echo FAIL",
        "remediate": "ufw default deny incoming; ufw allow 22/tcp; ufw enable",
    },
    {
        "id": "auto-security-updates",
        "title": "Security updates install unattended",
        "why": "Most compromises use a patched vulnerability. The gap that "
               "matters is between release and install, not release and disclosure.",
        "severity": "high",
        "standard": "CIS 1.9",
        "apply": "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                 "unattended-upgrades >/dev/null 2>&1 || true",
        "check": "(systemctl is-enabled unattended-upgrades 2>/dev/null | grep -q enabled "
                 "|| systemctl is-enabled dnf-automatic.timer 2>/dev/null | grep -q enabled) "
                 "&& echo PASS || echo FAIL",
        "remediate": "apt install unattended-upgrades (or enable dnf-automatic.timer)",
    },
    {
        "id": "no-empty-passwords",
        "title": "No account has an empty password",
        "why": "An empty password field lets any local or SSH login in with no "
               "credential at all.",
        "severity": "critical",
        "standard": "CIS 5.4.2",
        "apply": "awk -F: '($2 == \"\") {print $1}' /etc/shadow | "
                 "while read u; do passwd -l \"$u\" >/dev/null 2>&1; done",
        "check": "[ -z \"$(awk -F: '($2 == \"\") {print $1}' /etc/shadow 2>/dev/null)\" ] "
                 "&& echo PASS || echo FAIL",
        "remediate": "Lock or set a password on every account with an empty field",
    },
    {
        "id": "sysctl-network-hardening",
        "title": "Kernel network parameters hardened",
        "why": "Blocks source-routed packets, redirects and IP spoofing that "
               "the defaults still permit.",
        "severity": "medium",
        "standard": "CIS 3.2",
        "apply": "printf 'net.ipv4.conf.all.rp_filter=1\\n"
                 "net.ipv4.conf.all.accept_redirects=0\\n"
                 "net.ipv4.conf.all.accept_source_route=0\\n"
                 "kernel.kptr_restrict=1\\n"
                 "kernel.dmesg_restrict=1\\n' > /etc/sysctl.d/90-foundry-hardening.conf; "
                 "sysctl -p /etc/sysctl.d/90-foundry-hardening.conf >/dev/null 2>&1 || true",
        "check": "[ \"$(sysctl -n net.ipv4.conf.all.rp_filter 2>/dev/null)\" = 1 ] "
                 "&& [ \"$(sysctl -n net.ipv4.conf.all.accept_redirects 2>/dev/null)\" = 0 ] "
                 "&& echo PASS || echo FAIL",
        "remediate": "Write /etc/sysctl.d/90-foundry-hardening.conf and sysctl -p it",
    },
    {
        "id": "auditd-running",
        "title": "Audit daemon is recording",
        "why": "Without it there is no record of what happened before you "
               "noticed. Logs written after an incident are not evidence.",
        "severity": "medium",
        "standard": "CIS 4.1",
        "apply": "DEBIAN_FRONTEND=noninteractive apt-get install -y auditd "
                 ">/dev/null 2>&1 && systemctl enable --now auditd >/dev/null 2>&1 || true",
        "check": "systemctl is-active auditd 2>/dev/null | grep -q '^active' "
                 "&& echo PASS || echo FAIL",
        "remediate": "apt install auditd && systemctl enable --now auditd",
        "optional": True,   # heavy on a small container
    },
    {
        "id": "no-world-writable-suid",
        "title": "No world-writable setuid binaries",
        "why": "A setuid binary anyone can rewrite is a direct path to root.",
        "severity": "critical",
        "standard": "CIS 6.1",
        "apply": "find / -xdev -perm -4002 -type f 2>/dev/null | "
                 "while read f; do chmod o-w \"$f\"; done",
        "check": "[ -z \"$(find / -xdev -perm -4002 -type f 2>/dev/null | head -1)\" ] "
                 "&& echo PASS || echo FAIL",
        "remediate": "chmod o-w on each result of: find / -xdev -perm -4002 -type f",
    },
    {
        "id": "ssh-keys-present",
        "title": "Key-based access actually works",
        "why": "Disabling password auth without a working key does not harden a "
               "host, it strands it. This is checked before the lockout controls "
               "are allowed to matter.",
        "severity": "critical",
        "standard": "operational",
        "apply": "true",   # nothing to apply; this is a guard
        "check": "[ -s /root/.ssh/authorized_keys ] || "
                 "ls /home/*/.ssh/authorized_keys >/dev/null 2>&1 "
                 "&& echo PASS || echo FAIL",
        "remediate": "Install an authorized_keys entry BEFORE disabling passwords",
        "guard_for": ["ssh-no-password-auth", "ssh-no-root-password"],
    },
]

# ── Named baselines ──────────────────────────────────────────────────────────

PROFILES: Dict[str, Dict] = {
    "baseline": {
        "label": "Baseline",
        "description": "The controls every host on the estate should meet. "
                       "Safe on containers and small nodes.",
        "controls": ["ssh-keys-present", "ssh-no-root-password",
                     "ssh-no-password-auth", "firewall-default-deny",
                     "auto-security-updates", "no-empty-passwords",
                     "sysctl-network-hardening"],
    },
    "exposed": {
        "label": "Internet-exposed",
        "description": "For anything reachable from outside the LAN. Adds "
                       "auditing and setuid checks.",
        "controls": ["ssh-keys-present", "ssh-no-root-password",
                     "ssh-no-password-auth", "firewall-default-deny",
                     "auto-security-updates", "no-empty-passwords",
                     "sysctl-network-hardening", "auditd-running",
                     "no-world-writable-suid"],
    },
    "minimal": {
        "label": "Minimal",
        "description": "Only the controls that cannot lock anyone out. For "
                       "appliances and hosts you cannot easily get back into.",
        "controls": ["no-empty-passwords", "sysctl-network-hardening",
                     "auto-security-updates"],
    },
}

BY_ID = {c["id"]: c for c in CONTROLS}


def profile(name: str = "baseline") -> Dict:
    """The controls in a named baseline, resolved and ordered."""
    p = PROFILES.get(name)
    if not p:
        return {"error": "unknown profile %r; have %s"
                         % (name, ", ".join(sorted(PROFILES)))}
    controls = [BY_ID[c] for c in p["controls"] if c in BY_ID]
    return {"profile": name, "label": p["label"],
            "description": p["description"], "controls": controls,
            "counts": _severity_counts(controls)}


def _severity_counts(controls: List[Dict]) -> Dict[str, int]:
    out = {s: 0 for s in SEVERITY}
    for c in controls:
        out[c.get("severity", "low")] = out.get(c.get("severity", "low"), 0) + 1
    return out


def catalogue() -> Dict:
    """Every control and profile, for showing what "hardened" actually means."""
    return {
        "profiles": [{"name": k, **{kk: vv for kk, vv in v.items()
                                    if kk != "controls"},
                      "control_count": len(v["controls"])}
                     for k, v in PROFILES.items()],
        "controls": [{k: v for k, v in c.items() if k != "apply"}
                     for c in CONTROLS],
        "severities": list(SEVERITY),
        "note": "Standard references are CIS Linux Benchmark sections. This is "
                "a deliberately small subset, not a compliance claim.",
    }


# ── Script generation ────────────────────────────────────────────────────────

def render_verify(controls: List[Dict]) -> str:
    """A script that reports the state of each control without changing any.

    Output is one `id=RESULT` line per control, so a partial or interrupted run
    still parses -- a verify that has to complete to be readable tells you
    nothing about the host that hung.
    """
    lines = ["#!/bin/sh", "# Foundry verify -- read-only, changes nothing"]
    for c in controls:
        lines.append("printf '%s=' " + _sq(c["id"]))
        lines.append("( %s ) 2>/dev/null || echo FAIL" % c["check"])
    lines.append("echo VERIFY_COMPLETE")
    return "\n".join(lines) + "\n"


def render_apply(controls: List[Dict], dry_run: bool = False) -> str:
    """A script that brings a host up to the baseline.

    Guard controls run first and abort the rest on failure: locking out
    password auth on a host with no working key turns a hardening run into an
    outage.
    """
    guards = [c for c in controls if c.get("guard_for")]
    rest = [c for c in controls if not c.get("guard_for")]

    lines = ["#!/bin/sh", "# Foundry apply", "set -u"]
    for g in guards:
        lines += [
            "# guard: %s" % g["title"],
            "printf '%s=' " + _sq(g["id"]),
            "GUARD=$( ( %s ) 2>/dev/null || echo FAIL )" % g["check"],
            "echo \"$GUARD\"",
            'if [ "$GUARD" != "PASS" ]; then',
            "  echo 'ABORT: %s'" % g["remediate"].replace("'", ""),
            "  echo APPLY_ABORTED",
            "  exit 1",
            "fi",
        ]
    for c in rest:
        lines.append("# %s (%s, %s)" % (c["title"], c["severity"], c["standard"]))
        if dry_run:
            lines.append("echo 'WOULD APPLY: %s'" % c["id"])
        else:
            lines.append("( %s ) >/dev/null 2>&1 || true" % c["apply"])
            lines.append("printf '%s=' " + _sq(c["id"]))
            lines.append("( %s ) 2>/dev/null || echo FAIL" % c["check"])
    if not dry_run:
        lines.append("(systemctl reload sshd 2>/dev/null || "
                     "systemctl reload ssh 2>/dev/null || true)")
    lines.append("echo APPLY_COMPLETE")
    return "\n".join(lines) + "\n"


def _sq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def parse_results(output: str, controls: Optional[List[Dict]] = None) -> Dict:
    """Turn `id=PASS` lines into a per-control verdict.

    An id that produced no line is `unknown`, not `fail`. Those mean different
    things: one is a host that fell short, the other is a check that never ran,
    and treating the second as the first invents failures.
    """
    seen: Dict[str, str] = {}
    for line in (output or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        cid, _, val = line.partition("=")
        val = val.strip().split(":")[0].upper()
        if val in ("PASS", "FAIL"):
            seen[cid.strip()] = val

    ids = [c["id"] for c in (controls or CONTROLS)]
    results = []
    for cid in ids:
        c = BY_ID.get(cid, {})
        state = seen.get(cid, "unknown").lower()
        results.append({"id": cid, "title": c.get("title", cid),
                        "severity": c.get("severity", "low"),
                        "standard": c.get("standard", ""),
                        "state": state,
                        "remediate": c.get("remediate", "") if state != "pass" else ""})
    failed = [r for r in results if r["state"] == "fail"]
    unknown = [r for r in results if r["state"] == "unknown"]
    return {
        "results": results,
        "passed": sum(1 for r in results if r["state"] == "pass"),
        "failed": len(failed),
        "unknown": len(unknown),
        "complete": "VERIFY_COMPLETE" in (output or "") or "APPLY_COMPLETE" in (output or ""),
        "aborted": "APPLY_ABORTED" in (output or ""),
        "worst": _worst([r["severity"] for r in failed]),
    }


def _worst(sevs: List[str]) -> Optional[str]:
    for s in SEVERITY:
        if s in sevs:
            return s
    return None


def drift(previous: Dict, current: Dict) -> Dict:
    """What changed between two verifications of the same host.

    Regressions are the point: a control that passed when the host was
    provisioned and fails now is a different, more interesting fact than one
    that never passed.
    """
    prev = {r["id"]: r["state"] for r in (previous or {}).get("results", [])}
    cur = {r["id"]: r["state"] for r in (current or {}).get("results", [])}

    regressed, fixed, still_failing = [], [], []
    for cid, state in cur.items():
        was = prev.get(cid)
        if was == "pass" and state == "fail":
            regressed.append(cid)
        elif was == "fail" and state == "pass":
            fixed.append(cid)
        elif was == "fail" and state == "fail":
            still_failing.append(cid)
    return {
        "regressed": regressed, "fixed": fixed, "still_failing": still_failing,
        "drifted": bool(regressed),
        "summary": ("%d control(s) regressed since the last check" % len(regressed))
                   if regressed else "no regressions",
        "worst_regression": _worst([BY_ID[c]["severity"] for c in regressed
                                    if c in BY_ID]),
    }


# ── File integrity monitoring ────────────────────────────────────────────────
#
# Paths chosen because this is where interference is visible, not because they
# are large. Watching /usr wholesale produces noise on every package update;
# watching these produces a signal.

FIM_PATHS: Dict[str, List[str]] = {
    "access": [
        "/root/.ssh/authorized_keys", "/home/*/.ssh/authorized_keys",
        "/etc/ssh/sshd_config", "/etc/passwd", "/etc/shadow", "/etc/group",
        "/etc/sudoers", "/etc/sudoers.d",
    ],
    "persistence": [
        "/etc/systemd/system", "/etc/cron.d", "/etc/crontab",
        "/var/spool/cron/crontabs", "/etc/rc.local",
        "/etc/init.d", "/usr/local/sbin", "/usr/local/bin",
    ],
    "network": [
        "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
        "/etc/iptables", "/etc/ufw",
    ],
    "binaries": [
        "/bin", "/sbin", "/usr/bin", "/usr/sbin",
    ],
}

# What a change in each area probably means, so an alert says something useful.
FIM_MEANING = {
    "access": "a new key, user or sudo rule -- this is how access is granted "
              "and how it is quietly kept",
    "persistence": "something arranging to run again after a reboot; almost "
                   "every persistence technique lands here",
    "network": "name resolution or firewall redirection",
    "binaries": "a replaced system binary; noisy during package updates, "
                "serious outside them",
}

FIM_DEFAULT = ["access", "persistence", "network"]


def fim_scan_script(areas: Optional[List[str]] = None) -> str:
    """Emit `sha256  path` for every watched file.

    Hashes only. A manifest that contained the files themselves would be a copy
    of the host's private keys sitting in a database.
    """
    areas = areas or FIM_DEFAULT
    paths: List[str] = []
    for a in areas:
        paths.extend(FIM_PATHS.get(a, []))
    lines = ["#!/bin/sh", "# Foundry FIM scan -- read-only"]
    for p in paths:
        lines.append(
            "for f in %s; do [ -f \"$f\" ] && sha256sum \"$f\" 2>/dev/null; "
            "[ -d \"$f\" ] && find \"$f\" -maxdepth 2 -type f "
            "-exec sha256sum {} \\; 2>/dev/null; done" % p)
    lines.append("echo FIM_COMPLETE")
    return "\n".join(lines) + "\n"


def fim_parse(output: str) -> Dict[str, str]:
    """`sha256  path` lines into {path: hash}."""
    out: Dict[str, str] = {}
    for line in (output or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            out[parts[1].strip()] = parts[0]
    return out


def fim_area_of(path: str) -> str:
    """Which watched area a path belongs to -- drives what the alert says."""
    import fnmatch
    for area, patterns in FIM_PATHS.items():
        for p in patterns:
            if fnmatch.fnmatch(path, p) or path.startswith(p.rstrip("*") + "/") \
                    or path == p:
                return area
    return "other"


def fim_diff(baseline: Dict[str, str], current: Dict[str, str]) -> Dict:
    """What changed since the baseline, grouped by what it would mean.

    Removals are reported as loudly as additions: deleting an audit rule or a
    log is as much a signal as adding a key.
    """
    b, c = baseline or {}, current or {}
    added = sorted(set(c) - set(b))
    removed = sorted(set(b) - set(c))
    changed = sorted(p for p in (set(b) & set(c)) if b[p] != c[p])

    events = ([{"path": p, "change": "added", "area": fim_area_of(p)} for p in added]
              + [{"path": p, "change": "modified", "area": fim_area_of(p)} for p in changed]
              + [{"path": p, "change": "removed", "area": fim_area_of(p)} for p in removed])
    for e in events:
        e["meaning"] = FIM_MEANING.get(e["area"], "outside the watched areas")

    by_area: Dict[str, int] = {}
    for e in events:
        by_area[e["area"]] = by_area.get(e["area"], 0) + 1

    # An authorized_keys change is the one worth waking someone for.
    urgent = [e for e in events if e["area"] == "access"]
    return {
        "clean": not events,
        "added": added, "removed": removed, "changed": changed,
        "events": events, "by_area": by_area,
        "urgent": urgent,
        "summary": ("no changes since the baseline" if not events else
                    "%d change(s): %s" % (len(events),
                                          ", ".join("%s %d" % (k, v)
                                                    for k, v in sorted(by_area.items())))),
    }
