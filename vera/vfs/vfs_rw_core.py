"""Which guests the file server exposes WRITABLE - the plans, app-free.

VFS-02 projects every guest filesystem read-only under /srv/vfs/estate/<name>.
Guests named in /etc/vfs/estate-rw.list are additionally mounted writable
under /srv/vfs/estate-rw/<name>, a share only @vfs-admin can open. That list
is the one control: this module turns a wanted set of names into the exact
commands and the warnings that belong with them, and reads the share's
reach (which networks may open it) from smb.conf.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Dict, Iterable, List

RW_LIST = "/etc/vfs/estate-rw.list"
SMB_CONF = "/etc/samba/smb.conf"
SHARE = "estate-rw"
DOOR_CIDR = "10.55.55.0/24"           # the VFS WireGuard door - a device key per peer
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def parse_list(text: str) -> List[str]:
    """The names in estate-rw.list: one per line, # comments and blanks ignored."""
    out: List[str] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and ln not in out:
            out.append(ln)
    return out


def check_names(names: Iterable[Any], known: Iterable[str]) -> Dict[str, List[str]]:
    """Split wanted names into those the tree knows, and the rest by reason."""
    known_set = {str(k) for k in known}
    ok, unknown, bad = [], [], []
    for n in names or []:
        s = str(n or "").strip()
        if not s:
            continue
        if not _NAME_OK.match(s):
            bad.append(s)
        elif s not in known_set:
            unknown.append(s)
        elif s not in ok:
            ok.append(s)
    return {"ok": ok, "unknown": unknown, "bad": bad}


def set_plan(names: List[str], current: List[str], running: Iterable[str] = ()) -> Dict[str, Any]:
    """Commands + warnings that make exactly `names` writable."""
    running = set(running)
    adds = [n for n in names if n not in current]
    drops = [n for n in current if n not in names]
    body = "".join(n + "\n" for n in names)
    commands = [
        "install -d -m 0755 /etc/vfs",
        f"printf '%s' {shlex.quote(body)} > {RW_LIST}" if names else f": > {RW_LIST}",
        "systemctl start vfs-estate-sync.service",
    ]
    warnings = []
    if adds:
        warnings.append(f"{', '.join(adds)}: the root filesystem becomes writable over SMB for @vfs-admin "
                        "(estate-rw). Reads in [estate] stay read-only for everyone else.")
    live = [n for n in adds if n in running]
    if live:
        warnings.append(f"{', '.join(live)} {'is' if len(live) == 1 else 'are'} running: the guest writes the same files. "
                        "Editing config and text from outside is fine; touching databases, package state or anything "
                        "it holds open can corrupt it - stop the guest first for that.")
    if drops:
        warnings.append(f"{', '.join(drops)}: the writable view is unmounted on the next sync; open files on it will error.")
    warnings.append("The tree rebuild runs now and takes up to a minute; the 5-minute timer keeps it that way.")
    return {"commands": commands, "warnings": warnings, "adds": adds, "drops": drops, "names": list(names)}


def share_reach(smb_conf: str, share: str = SHARE) -> Dict[str, Any]:
    """The [share] block's `hosts allow`, or None when any network may open it."""
    m = re.search(r"^\[" + re.escape(share) + r"\]\s*$(.*?)(?=^\[|\Z)", smb_conf or "", re.M | re.S)
    if not m:
        return {"found": False, "hosts_allow": None, "valid_users": None}
    block = m.group(1)
    ha = re.search(r"^\s*hosts allow\s*=\s*(.+?)\s*$", block, re.M)
    vu = re.search(r"^\s*valid users\s*=\s*(.+?)\s*$", block, re.M)
    allow = ha.group(1).split() if ha else None
    return {"found": True, "hosts_allow": allow, "valid_users": vu.group(1) if vu else None,
            "door_only": bool(allow) and all(a in ("127.0.0.1", "localhost") or a.startswith(DOOR_CIDR.rsplit(".", 1)[0] + ".") for a in allow)}


def door_only_plan(enable: bool, share: str = SHARE) -> Dict[str, Any]:
    """Limit (or reopen) the writable share to the VFS WireGuard door.

    Edits only the [share] block, keeps a dated backup, refuses to reload
    unless testparm accepts the result, and reloads without dropping
    sessions (smbcontrol reload-config).
    """
    allow = f"{DOOR_CIDR} 127.0.0.1"
    py = (
        "import re,sys,io\n"
        f"p='{SMB_CONF}'; s=open(p).read()\n"
        f"m=re.search(r'^\\[{share}\\]\\s*$(.*?)(?=^\\[|\\Z)', s, re.M|re.S)\n"
        "assert m, 'no [%s] block' % " + repr(share) + "\n"
        "blk=m.group(1)\n"
        "blk=re.sub(r'^\\s*hosts allow\\s*=.*\\n', '', blk, flags=re.M)\n"
        "blk=re.sub(r'^\\s*hosts deny\\s*=.*\\n', '', blk, flags=re.M)\n"
        + (f"blk=blk.rstrip('\\n')+'\\n   hosts allow = {allow}\\n   hosts deny = ALL\\n\\n'\n" if enable else "")
        + "s=s[:m.start(1)]+blk+s[m.end(1):]\n"
        "open(p,'w').write(s)\n"
    )
    commands = [
        f"cp -a {SMB_CONF} {SMB_CONF}.bak-$(date +%Y%m%d-%H%M%S)",
        "python3 - <<'PY'\n" + py + "PY",
        f"testparm -s {SMB_CONF} >/dev/null",
        "smbcontrol all reload-config",
        f"testparm -s --section-name={share} 2>/dev/null | grep -E 'hosts (allow|deny)' || echo 'hosts allow: any'",
    ]
    if enable:
        warnings = [f"Writing to {share} then needs the VFS WireGuard door ({DOOR_CIDR}) - even at home. "
                    "Reads through [estate] stay reachable on the LAN.",
                    "A device on the door holds its own key; that key plus the @vfs-admin password are the two factors.",
                    "If testparm rejects the edit nothing is reloaded and the dated backup is beside smb.conf."]
    else:
        warnings = [f"{share} becomes writable from any network Samba listens on, for @vfs-admin.",
                    "If testparm rejects the edit nothing is reloaded and the dated backup is beside smb.conf."]
    return {"commands": commands, "warnings": warnings, "enable": enable}


def script(commands: List[str]) -> str:
    """One shell script that stops at the first failing step."""
    return "set -e\n" + "\n".join(commands) + "\n"
