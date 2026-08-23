"""Unit tests for the OS-agnostic Foundry feature builders (features_core).
Imported via lowercase vera.* so pytest binds to the worktree copy."""
from vera.foundry.features_core import (OS_ADAPTER, FEATURES, feature_script)


def test_os_adapter_covers_distros():
    for mgr in ("apt-get", "apk", "pacman", "dnf"):
        assert mgr in OS_ADAPTER
    assert "pkg_install()" in OS_ADAPTER and "svc_enable()" in OS_ADAPTER
    assert "systemctl enable" in OS_ADAPTER and "rc-update add" in OS_ADAPTER   # systemd + OpenRC


def test_every_feature_wraps_adapter():
    for f in FEATURES:
        s = feature_script(f, {"vera_url": "https://v:8999", "mesh_token": "T",
                               "vera_worker_env": "REDIS_URL=redis://v:6379",
                               "shares": [{"remote": "//nas/share", "mountpoint": "/mnt/share"}]})
        assert s.startswith("#!/bin/sh") and "pkg_install()" in s   # adapter sourced


def test_mesh_feature():
    s = feature_script("mesh", {"vera_url": "https://v:8999", "mesh_token": "TOK"})
    assert "wg genkey" in s and "/netsec/mesh/enroll" in s
    assert "VERA='https://v:8999'" in s and "TOKEN='TOK'" in s
    assert "wg-quick up vera0" in s


def test_worker_feature():
    s = feature_script("distributed-compute", {"vera_image": "reg:5000/vera:latest",
                                               "vera_worker_env": "REDIS_URL=redis://v:6379\nX=1"})
    assert "docker run -d --name vera-worker" in s and "reg:5000/vera:latest" in s
    assert "insecure-registries" in s and "REDIS_URL=redis://v:6379" in s


def test_hardening_feature():
    s = feature_script("hardening", {})
    assert "PermitRootLogin prohibit-password" in s and "PasswordAuthentication no" in s
    assert "unattended-upgrades" in s and "ufw" in s and "firewall-cmd" in s and "51820/udp" in s


def test_file_client_feature():
    s = feature_script("file-client", {"shares": [{"remote": "//nas/pub", "mountpoint": "/mnt/pub", "type": "cifs"}]})
    assert "cifs-utils" in s and "/etc/fstab" in s and "//nas/pub /mnt/pub cifs" in s


def test_unknown_feature_empty():
    assert feature_script("nope", {}) == ""
    assert feature_script("", {}) == ""
