"""Unit tests for the WireGuard mesh install script builder (netsec_core).

The real-VM E2E (2026-08-10) showed the mesh install racing a fresh cloud image's
cloud-init apt and losing the lock. These guard that the builder now waits for
cloud-init + the apt/dpkg lock BEFORE installing. Imported via lowercase `vera.*`
so it binds to the worktree (see dev-lifecycle §8.3 / worktree-testable-cores-pattern)."""
from vera.networking.netsec_core import wireguard_install_script


def test_waits_for_cloudinit_and_apt_lock_before_install():
    s = wireguard_install_script()
    # the fix: wait out cloud-init + the apt/dpkg lock ...
    assert "cloud-init status --wait" in s
    assert "fuser" in s and "/var/lib/dpkg/lock-frontend" in s
    # ... and that wait must come BEFORE the apt-get install, or it's useless
    assert s.index("cloud-init status --wait") < s.index("apt-get -o DPkg::Lock::Timeout")


def test_apt_carries_lock_timeout():
    s = wireguard_install_script()
    # belt-and-braces: a late lock waits up to 300s rather than failing outright
    assert s.count("DPkg::Lock::Timeout=300") >= 2   # both update and install


def test_all_package_managers_still_covered():
    s = wireguard_install_script()
    for mgr in ("apt-get", "dnf", "yum", "zypper", "pacman", "apk", "opkg"):
        assert mgr in s, f"missing package manager: {mgr}"


def test_preserves_markers_and_log_tail():
    s = wireguard_install_script()
    for marker in ("VERA_WG_PRESENT", "VERA_WG_INSTALLED", "VERA_WG_FAIL",
                   "VERA_WG_NOPKG", "---VERA_WG_LOG_TAIL---"):
        assert marker in s
    assert "/tmp/vera_wg_install.log" in s


def test_sudo_elevation_when_not_root():
    s = wireguard_install_script()
    assert 'S="sudo -n"' in s and '[ "$(id -u)" != "0" ]' in s
    # the lock-wait also elevates (fuser on root-owned locks needs it)
    assert "$S fuser" in s


def test_short_circuits_when_already_installed():
    s = wireguard_install_script()
    # if wg is already present, exit before any package work
    assert s.index("VERA_WG_PRESENT") < s.index("cloud-init status --wait")
