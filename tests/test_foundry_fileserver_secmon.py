"""Foundry gap #3: file-server (Samba/NFS host) + security-monitoring (auditd + optional
log shipping) were declared 'planned' but had no builder. Now implemented in features_core."""
from vera.foundry.features_core import feature_script, FEATURES


def test_file_server_feature():
    sc = feature_script("file-server", {})
    assert sc.startswith("#!/bin/sh")
    assert "pkg_install samba" in sc and "nfs" in sc
    assert "/etc/samba/smb.conf" in sc
    assert "/etc/exports" in sc
    assert "[foundry] file-server configured" in sc
    # default export present
    assert "/srv/foundry" in sc


def test_file_server_custom_exports():
    sc = feature_script("file-server", {"exports": [{"path": "/data/media", "name": "media"}]})
    assert "/data/media" in sc and "[media]" in sc


def test_security_monitoring_feature():
    sc = feature_script("security-monitoring", {})
    assert "pkg_install auditd" in sc
    assert "/etc/audit/rules.d/foundry.rules" in sc
    assert "sshd_config -p wa" in sc
    assert "[foundry] security-monitoring configured" in sc
    # no collector -> no rsyslog shipping
    assert "rsyslog.d/99-foundry-ship.conf" not in sc


def test_security_monitoring_with_collector():
    sc = feature_script("security-monitoring", {"log_collector": "10.0.0.9"})
    assert "99-foundry-ship.conf" in sc and "@@10.0.0.9:514" in sc


def test_features_registered():
    assert "file-server" in FEATURES and "security-monitoring" in FEATURES
