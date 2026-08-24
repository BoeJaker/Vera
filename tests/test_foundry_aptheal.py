"""Live-test finding: the 90s enrol timeout can cancel a mid-`apt` install, and the
next feature's apt then failed (lock briefly held / dpkg interrupted). The OS adapter's
pkg_install now makes apt WAIT for the dpkg lock (DPkg::Lock::Timeout, honored by modern
apt, ignored by old) and heals interrupted state (dpkg --configure -a) before installing."""
from vera.foundry.features_core import OS_ADAPTER, feature_script


def test_adapter_has_apt_lock_wait_and_heal():
    assert 'DPkg::Lock::Timeout' in OS_ADAPTER
    assert 'dpkg --configure -a' in OS_ADAPTER
    assert '99foundry-lock' in OS_ADAPTER


def test_feature_scripts_inherit_apt_robustness():
    # every feature sources the adapter, so all apt installs get the lock-wait
    for feat in ("hardening", "file-client", "vera-worker", "mesh"):
        sc = feature_script(feat, {})
        if sc:
            assert 'DPkg::Lock::Timeout' in sc, feat
