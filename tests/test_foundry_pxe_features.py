"""PXE/physical target: the first-boot script now applies the SAME OS-agnostic
feature bundles (features_core) as CT/VM provisioning, rendered in the app layer
and threaded through _render_boot -> _render_autoinstall -> _render_features_script."""
from vera.foundry.foundry_core import _render_features_script, _render_boot


def test_pxe_features_embeds_prerendered_scripts():
    s = _render_features_script(
        ["hardening", "mesh"], None,
        feature_scripts=["echo HARDEN_BODY", "echo MESH_BODY"])
    assert s.startswith("#!/bin/sh")
    assert "HARDEN_BODY" in s and "MESH_BODY" in s
    # strict `set -e` dropped: one soft failure must not abort the remaining bundles
    assert '"set -e"' not in s and "\nset -e\n" not in s
    # the old Debian-only ad-hoc hardening embed is gone
    assert "_HARDEN" not in s


def test_pxe_features_none_is_safe():
    s = _render_features_script([], None, feature_scripts=None)
    assert s.startswith("#!/bin/sh")
    assert "foundry-features-done" in s


def test_pxe_boot_threads_feature_scripts():
    prof = {"name": "n1", "arch": "amd64", "boot_type": "uefi", "features": ["mesh"]}
    arts = _render_boot(prof, {"gateway": "10.42.0.1"}, {"source_url": "http://x"},
                        None, feature_scripts=["echo MESH_BODY"])
    fsh = arts["artifacts"]["features.sh"]
    assert "MESH_BODY" in fsh
    # and base64-embedded into the cloud-init NoCloud user-data
    import re, base64
    ud = arts["artifacts"]["autoinstall/user-data"]
    m = re.search(r"content: (\S+)", ud)
    assert m and b"MESH_BODY" in base64.b64decode(m.group(1))
