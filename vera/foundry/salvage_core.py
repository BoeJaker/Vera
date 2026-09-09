"""salvage_core.py -- pure, app-free planning for getting data off a machine
before it is wiped.

Plug a disk into the provisioning host, take what matters off it, and only then
reimage. The order is the whole point: a wipe is irreversible and a backup is
only real once something has read it back.

── Two backups, not one ─────────────────────────────────────────────────────

A full image and a file-level harvest answer different questions.

  image     the safety net. Captures the boot sector, every partition, and
            everything the harvest's filters missed. You will almost never
            open it, and you would be sunk without it.
  harvest   what you actually wanted. Documents, code, dotfiles, editor
            settings, SSH keys, browser profiles. Small enough to browse,
            search, and restore onto a different machine.

Doing only the harvest means discovering a month later that the thing you
needed was outside the patterns. Doing only the image means a 500 GB file
nobody ever mounts. So both, with the harvest verified first because it is
the one that gets used.

── Why the wipe is gated in code ────────────────────────────────────────────

`wipe_authorisation()` refuses unless a backup exists AND has been verified by
reading it back. This is not defensive programming for its own sake: earlier in
this project a card was declared "backed up" on the strength of a job having
been started, and the claim was wrong until someone actually ran gzip -t over
the result. A started backup is not a backup.

Consumers import uppercase (Vera.vera.foundry.salvage_core); tests import
lowercase (vera.foundry.salvage_core) -- see `worktree-testable-cores-pattern`.
"""
from __future__ import annotations

from typing import Dict, List, Optional

# ── What counts as "the user's stuff", per platform ──────────────────────────
#
# Paths are relative to the mounted filesystem root. Globs are shell-style;
# `**` spans directories.

LINUX_PROFILES: Dict[str, List[str]] = {
    "documents": [
        "home/*/Documents/**", "home/*/Desktop/**", "home/*/Pictures/**",
        "home/*/Videos/**", "home/*/Music/**", "home/*/Downloads/**",
        "root/Documents/**", "srv/**",
    ],
    "code": [
        "home/*/*.py", "home/*/*.sh",
        "home/*/[Pp]rojects/**", "home/*/[Cc]ode/**", "home/*/[Dd]ev/**",
        "home/*/git/**", "home/*/repos/**", "home/*/src/**",
        "opt/*/**",
    ],
    "editor": [
        # VS Code: settings, keybindings, snippets and the extension list.
        "home/*/.config/Code/User/settings.json",
        "home/*/.config/Code/User/keybindings.json",
        "home/*/.config/Code/User/snippets/**",
        "home/*/.config/Code/User/globalStorage/state.vscdb",
        "home/*/.vscode/extensions/*/package.json",
        "home/*/.vscode-server/**/settings.json",
        # Other editors people actually configure.
        "home/*/.config/JetBrains/**", "home/*/.vimrc", "home/*/.config/nvim/**",
        "home/*/.emacs.d/init.el", "home/*/.config/sublime-text/**",
    ],
    "dotfiles": [
        "home/*/.bashrc", "home/*/.zshrc", "home/*/.profile",
        "home/*/.bash_aliases", "home/*/.gitconfig", "home/*/.tmux.conf",
        "home/*/.config/gh/**", "home/*/.config/git/**",
        "etc/fstab", "etc/hosts", "etc/hostname",
    ],
    "credentials": [
        "home/*/.ssh/**", "root/.ssh/**",
        "home/*/.gnupg/**",
        "home/*/.aws/**", "home/*/.kube/config", "home/*/.docker/config.json",
        "etc/wpa_supplicant/**", "etc/wireguard/**", "etc/ssh/ssh_host_*",
    ],
    "browser": [
        "home/*/.mozilla/firefox/*/places.sqlite",
        "home/*/.mozilla/firefox/*/logins.json",
        "home/*/.mozilla/firefox/*/key4.db",
        "home/*/.config/google-chrome/*/Bookmarks",
        "home/*/.config/chromium/*/Bookmarks",
    ],
    "databases": [
        "var/lib/postgresql/**", "var/lib/mysql/**", "var/lib/influxdb/**",
    ],
    "services": [
        "etc/systemd/system/*.service", "etc/systemd/system/*.timer",
        "etc/cron.d/**", "var/spool/cron/**", "etc/nginx/**", "etc/apache2/sites-available/**",
        "opt/*/docker-compose.yml", "home/*/docker-compose.yml",
    ],
}

WINDOWS_PROFILES: Dict[str, List[str]] = {
    "documents": [
        "Users/*/Documents/**", "Users/*/Desktop/**", "Users/*/Pictures/**",
        "Users/*/Videos/**", "Users/*/Music/**", "Users/*/Downloads/**",
    ],
    "code": [
        "Users/*/source/**", "Users/*/[Pp]rojects/**", "Users/*/[Cc]ode/**",
        "Users/*/git/**", "Users/*/repos/**",
    ],
    "editor": [
        "Users/*/AppData/Roaming/Code/User/settings.json",
        "Users/*/AppData/Roaming/Code/User/keybindings.json",
        "Users/*/AppData/Roaming/Code/User/snippets/**",
        "Users/*/.vscode/extensions/*/package.json",
        "Users/*/AppData/Roaming/JetBrains/**",
        "Users/*/AppData/Roaming/Sublime Text*/Packages/User/**",
        "Users/*/_vimrc", "Users/*/.gitconfig",
    ],
    "dotfiles": [
        "Users/*/.gitconfig", "Users/*/.bashrc",
        "Users/*/Documents/WindowsPowerShell/**",
        "Users/*/AppData/Roaming/Microsoft/Windows/PowerShell/**",
    ],
    "credentials": [
        "Users/*/.ssh/**", "Users/*/.aws/**", "Users/*/.kube/config",
        "Users/*/AppData/Roaming/gnupg/**",
    ],
    "browser": [
        "Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/places.sqlite",
        "Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/logins.json",
        "Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*/key4.db",
        "Users/*/AppData/Local/Google/Chrome/User Data/*/Bookmarks",
    ],
    "databases": [],
    "services": ["Users/*/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/**"],
}

MACOS_PROFILES: Dict[str, List[str]] = {
    "documents": ["Users/*/Documents/**", "Users/*/Desktop/**",
                  "Users/*/Pictures/**", "Users/*/Downloads/**"],
    "code": ["Users/*/[Pp]rojects/**", "Users/*/[Cc]ode/**", "Users/*/src/**"],
    "editor": [
        "Users/*/Library/Application Support/Code/User/settings.json",
        "Users/*/Library/Application Support/Code/User/keybindings.json",
        "Users/*/Library/Application Support/Code/User/snippets/**",
        "Users/*/.vscode/extensions/*/package.json",
    ],
    "dotfiles": ["Users/*/.zshrc", "Users/*/.bashrc", "Users/*/.gitconfig"],
    "credentials": ["Users/*/.ssh/**", "Users/*/.aws/**", "Users/*/.gnupg/**"],
    "browser": ["Users/*/Library/Application Support/Firefox/Profiles/*/places.sqlite"],
    "databases": [],
    "services": ["Users/*/Library/LaunchAgents/**"],
}

PROFILE_SETS = {"linux": LINUX_PROFILES, "windows": WINDOWS_PROFILES,
                "macos": MACOS_PROFILES}

# Ordered so the default selection reads sensibly in a UI.
PROFILE_ORDER = ["documents", "code", "editor", "dotfiles", "credentials",
                 "browser", "services", "databases"]

# The default when someone just wants "my stuff". Deliberately excludes
# `databases` (large, and usually needs a dump rather than a file copy) and
# includes `credentials`, because losing your SSH keys in a wipe is exactly the
# kind of thing this is for.
DEFAULT_PROFILES = ["documents", "code", "editor", "dotfiles", "credentials",
                    "browser", "services"]

# ── What never goes in a harvest ─────────────────────────────────────────────
#
# These are big, regenerable, or actively harmful to copy. Without them a
# "user files" backup of a developer's home directory is mostly node_modules.
EXCLUDES: List[str] = [
    "**/node_modules/**", "**/.npm/**", "**/.yarn/cache/**",
    "**/__pycache__/**", "**/*.pyc", "**/.venv/**", "**/venv/**",
    "**/.cache/**", "**/Cache/**", "**/CachedData/**", "**/GPUCache/**",
    "**/.gradle/**", "**/.m2/repository/**", "**/target/debug/**",
    "**/.cargo/registry/**", "**/go/pkg/mod/**",
    "**/*.iso", "**/*.vdi", "**/*.vmdk", "**/*.qcow2", "**/*.vhdx",
    # Root-level too, not just "**/": these live at the filesystem root and
    # fnmatch's "**/" requires at least one directory component, so the glob
    # alone silently fails to match the very files it names.
    "**/*.swap", "**/swapfile", "**/pagefile.sys", "**/hiberfil.sys",
    "swapfile", "pagefile.sys", "hiberfil.sys", "swap.img",
    "**/.Trash/**", "**/$RECYCLE.BIN/**", "**/System Volume Information/**",
    "**/.local/share/Trash/**",
    "**/Steam/steamapps/**", "**/.wine/drive_c/windows/**",
    "**/.git/objects/pack/**",   # the working tree is kept; huge packs are not
]

# Cheap heuristics for spotting the OS on a mounted filesystem.
OS_MARKERS = {
    "linux": ["etc/os-release", "etc/fstab", "etc/passwd"],
    "windows": ["Windows/System32/config/SYSTEM",  # pragma: allowlist secret
                "Windows/explorer.exe",
                "pagefile.sys"],
    "macos": ["System/Library/CoreServices/SystemVersion.plist",
              "private/etc/hostconfig"],
}


def detect_os(present_paths: List[str]) -> Dict:
    """Guess the OS from paths found on a mounted filesystem.

    Takes the list of paths rather than touching a disk, so it is testable and
    so the caller decides how deep to look.
    """
    have = {p.strip("/").replace("\\", "/") for p in (present_paths or [])}
    scores = {}
    for os_name, markers in OS_MARKERS.items():
        scores[os_name] = sum(1 for m in markers if m in have)
    best = max(scores, key=lambda k: scores[k]) if scores else "linux"
    if not scores.get(best):
        return {"os": "unknown", "confidence": 0, "scores": scores,
                "note": "no OS markers found -- this may be a data partition "
                        "rather than a system one"}
    return {"os": best, "confidence": scores[best] / len(OS_MARKERS[best]),
            "scores": scores}


def harvest_plan(os_kind: str, profiles: Optional[List[str]] = None,
                 extra_includes: Optional[List[str]] = None,
                 extra_excludes: Optional[List[str]] = None) -> Dict:
    """Which paths to take off a machine, and which to skip."""
    os_kind = (os_kind or "linux").lower()
    table = PROFILE_SETS.get(os_kind)
    if table is None:
        return {"error": "unknown os %r; expected one of %s"
                         % (os_kind, ", ".join(sorted(PROFILE_SETS)))}

    chosen = [p for p in (profiles or DEFAULT_PROFILES)]
    unknown = [p for p in chosen if p not in table]
    if unknown:
        return {"error": "unknown profile(s): %s" % ", ".join(unknown),
                "available": PROFILE_ORDER}

    includes: List[str] = []
    for p in chosen:
        for pat in table[p]:
            if pat not in includes:
                includes.append(pat)
    for pat in (extra_includes or []):
        if pat not in includes:
            includes.append(pat)

    excludes = list(EXCLUDES) + [e for e in (extra_excludes or [])
                                 if e not in EXCLUDES]
    return {"os": os_kind, "profiles": chosen, "includes": includes,
            "excludes": excludes,
            "note": "patterns are relative to the mounted filesystem root"}


def image_plan(device: str, dest: str, *, compress: bool = True,
               partitions_only: bool = False) -> Dict:
    """The full-image half: a byte-for-byte copy of the device.

    Compressed by default because an empty 500 GB disk compresses to almost
    nothing, and the read time is dominated by the device, not the CPU.
    """
    if not device or not dest:
        return {"error": "device and dest are required"}
    target = dest.rstrip("/") + "/disk.img" + (".gz" if compress else "")
    read = "dd if=%s bs=4M status=progress" % device
    steps = [
        {"stage": "table",
         "cmd": ["sfdisk", "-d", device],
         "why": "the partition table, so the image can be loop-mounted later "
                "without guessing offsets",
         "capture_to": dest.rstrip("/") + "/partition-table.sfdisk"},
        {"stage": "image",
         "shell": "%s | %s > %s" % (read, "gzip -1" if compress else "cat", target),
         "why": "gzip -1 because the bottleneck is the reader, not the CPU",
         "slow": True},
    ]
    if partitions_only:
        steps[1]["why"] += " (per-partition, so a damaged table does not stop it)"
    return {"ok": True, "device": device, "target": target,
            "compressed": compress, "steps": steps,
            "verify_with": verify_plan(target)["steps"]}


def verify_plan(artifact: str) -> Dict:
    """How to prove an artifact is readable.

    A backup that has not been read back is a hypothesis. For a gzip stream
    `gzip -t` decompresses the whole thing and checks the CRC, which is the
    difference between "the job exited 0" and "the data is there".
    """
    if artifact.endswith(".gz"):
        steps = [
            {"stage": "crc", "cmd": ["gzip", "-t", artifact],
             "why": "decompresses the entire stream and checks the CRC"},
            {"stage": "size", "shell":
                "gzip -dc %s | wc -c" % artifact,
             "why": "the uncompressed length should equal the source device"},
        ]
    elif artifact.endswith((".tar", ".tgz", ".tar.gz")):
        steps = [
            {"stage": "list", "cmd": ["tar", "-tzf", artifact],
             "why": "walks the whole archive; a truncated tar fails here"},
        ]
    else:
        steps = [
            {"stage": "hash", "cmd": ["sha256sum", artifact],
             "why": "compare against a hash of the source"},
        ]
    return {"artifact": artifact, "steps": steps,
            "rule": "an unverified backup does not authorise a wipe"}


def wipe_authorisation(backups: Optional[List[Dict]] = None,
                       force: bool = False) -> Dict:
    """Decide whether wiping the source is allowed yet.

    Every backup must exist AND be verified. `force` is honoured but recorded,
    because there are legitimate reasons to wipe an unbacked-up disk and none
    of them should be silent.
    """
    backups = backups or []
    if not backups:
        return {"allowed": False,
                "reason": "nothing has been backed up from this device yet"}

    missing = [b.get("artifact", "?") for b in backups if not b.get("exists")]
    unverified = [b.get("artifact", "?") for b in backups
                  if b.get("exists") and not b.get("verified")]

    if missing:
        return {"allowed": False,
                "reason": "backup artifact(s) missing: %s" % ", ".join(missing)}
    if unverified:
        if force:
            return {"allowed": True, "forced": True,
                    "reason": "wipe forced despite %d unverified backup(s): %s"
                              % (len(unverified), ", ".join(unverified)),
                    "warning": "these have not been read back; if they are "
                               "truncated the data is gone once the wipe runs"}
        return {"allowed": False,
                "reason": "backup(s) not verified: %s" % ", ".join(unverified),
                "hint": "run the verify step first -- a finished job is not "
                        "proof the bytes are readable"}
    return {"allowed": True, "forced": False,
            "reason": "%d backup(s) present and verified" % len(backups)}


def estimate(files: Optional[List[Dict]] = None) -> Dict:
    """Summarise a harvest before running it, so nobody is surprised by a
    300 GB "documents" folder."""
    files = files or []
    total = sum(int(f.get("bytes", 0)) for f in files)
    by_profile: Dict[str, Dict] = {}
    for f in files:
        p = f.get("profile", "other")
        g = by_profile.setdefault(p, {"files": 0, "bytes": 0})
        g["files"] += 1
        g["bytes"] += int(f.get("bytes", 0))
    biggest = sorted(files, key=lambda f: -int(f.get("bytes", 0)))[:10]
    return {"files": len(files), "bytes": total,
            "human": _human(total),
            "by_profile": {k: {**v, "human": _human(v["bytes"])}
                           for k, v in sorted(by_profile.items())},
            "largest": [{"path": f.get("path"), "human": _human(f.get("bytes", 0))}
                        for f in biggest]}


def _human(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
    return "%.1f TB" % n
