"""The writable estate: which guests VFS-02 exposes read-write is one list on
the server; Vera reads it, plans a change with the warnings that belong to
it, and can pull the writable share behind the VFS WireGuard door."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.vfs import vfs_rw_core as rw  # noqa: E402

pytestmark = pytest.mark.critical


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_list_is_names_one_per_line_comments_ignored():
    assert rw.parse_list("# writable guests\nn8n\n\nNWM-02\nn8n\n") == ["n8n", "NWM-02"]
    assert rw.parse_list("") == []


def test_only_guests_the_tree_knows_can_be_made_writable():
    c = rw.check_names(["n8n", "nope", "bad name!", "", "n8n"], ["n8n", "NWM-02"])
    assert c == {"ok": ["n8n"], "unknown": ["nope"], "bad": ["bad name!"]}


def test_the_plan_writes_the_list_rebuilds_and_warns_about_running_guests():
    p = rw.set_plan(["n8n", "NWM-02"], current=["Gitea"], running=["NWM-02"])
    assert p["adds"] == ["n8n", "NWM-02"] and p["drops"] == ["Gitea"]
    assert p["commands"][1].startswith("printf '%s' 'n8n\nNWM-02\n' > /etc/vfs/estate-rw.list")
    assert p["commands"][-1] == "systemctl start vfs-estate-sync.service"
    text = " ".join(p["warnings"])
    assert "NWM-02 is running" in text and "stop the guest first" in text
    assert "Gitea: the writable view is unmounted" in text
    assert "@vfs-admin" in text
    empty = rw.set_plan([], current=["n8n"])
    assert empty["commands"][1] == ": > /etc/vfs/estate-rw.list" and empty["drops"] == ["n8n"]
    assert rw.script(p["commands"]).startswith("set -e\n")


def test_the_share_reach_is_read_from_its_block_only():
    conf = "[estate]\n   path = /srv/vfs/estate\n   read only = yes\n   valid users = @vfs-admin\n\n[estate-rw]\n   path = /srv/vfs/estate-rw\n   read only = no\n   valid users = @vfs-admin\n\n[models]\n   hosts allow = 1.2.3.4\n"
    r = rw.share_reach(conf)
    assert r == {"found": True, "hosts_allow": None, "valid_users": "@vfs-admin", "door_only": False}
    conf2 = conf.replace("   read only = no\n", "   read only = no\n   hosts allow = 10.66.66.0/24 127.0.0.1\n   hosts deny = ALL\n")
    r2 = rw.share_reach(conf2)
    assert r2["hosts_allow"] == ["10.66.66.0/24", "127.0.0.1"] and r2["door_only"] is True
    assert rw.share_reach(conf2, door_cidr="10.9.9.0/24")["door_only"] is False, "judged against the door the mesh reports"
    assert rw.share_reach("[x]\n")["found"] is False
    # the reader hands share_reach the sed slice that already begins at the header - a
    # second header in front made the block empty and the share read as open
    caps = read("vera", "vfs", "vfs_capabilities.py")
    assert "_rw.share_reach(smb, _rw.SHARE)" in caps and '"[" + _rw.SHARE + "]' not in caps


def test_door_only_is_backed_up_testparm_gated_and_reloads_without_dropping_sessions():
    p = rw.door_only_plan(True)
    c = p["commands"]
    assert c[0].startswith("cp -a /etc/samba/smb.conf /etc/samba/smb.conf.bak-")
    assert "python3 - <<'PY'" in c[1] and "hosts allow = 10.66.66.0/24 127.0.0.1" in c[1] and "hosts deny = ALL" in c[1]
    assert "hosts allow = 10.77.0.0/16 127.0.0.1" in rw.door_only_plan(True, door_cidr="10.77.0.0/16")["commands"][1]
    assert c[2] == "testparm -s /etc/samba/smb.conf >/dev/null", "nothing reloads unless Samba accepts the file"
    assert c[3] == "smbcontrol all reload-config"
    assert c.index("testparm -s /etc/samba/smb.conf >/dev/null") < c.index("smbcontrol all reload-config")
    assert any("even at home" in w for w in p["warnings"])
    off = rw.door_only_plan(False)
    assert "hosts allow = 10.66.66.0/24" not in off["commands"][1] and "hosts allow" in off["commands"][1]
    caps = read("vera", "vfs", "vfs_capabilities.py")
    assert "await _door_cidr()" in caps and '"netsec.mesh.members"' in caps, "the cidr is the door's, never a constant guessed here"
    # set -e: a rejected testparm stops before the reload
    assert rw.script(c).startswith("set -e\n")


def test_the_capabilities_and_the_storage_controls_exist():
    caps = read("vera", "vfs", "vfs_capabilities.py")
    for name in ('"vfs.estate.rw"', '"vfs.estate.rw.set"', '"vfs.estate.rw.door_only"'):
        assert name in caps, name
    body = caps[caps.index("async def cap_estate_rw_set("):caps.index("async def cap_estate_rw_door_only(")]
    assert "if not confirm:" in body and '"dry_run": True' in body, "dry run by default"
    assert "not in the estate tree" in body, "unknown names are refused, not guessed"
    assert 'estate.machines' in body, "the plan knows which guests are running"
    panel = read("vera", "proxmox", "pxstore_panel.html")
    assert "veraEstate.plan('/vfs/estate/rw/set',{names}" in panel
    assert "veraEstate.plan('/vfs/estate/rw/door_only',{enable}" in panel
    assert 'class="vfs-rw-cb"' in panel and "soft(api('/vfs/estate/rw','POST',{}))" in panel
    assert "subvol-(\d+)-" in panel, "rows link to the guest through the one list"
