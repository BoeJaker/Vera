"""Live-test-driven provisioning robustness: Proxmox forbids API tokens from creating
privileged CTs or setting the `features` flag (nesting/keyctl) -- those are root@pam-only.
Foundry renders a root `pct create` (pct_create_cmd) and runs it over SSH instead."""
from vera.foundry.foundry_core import pct_create_cmd


def test_pct_create_cmd_privileged_nesting():
    c = pct_create_cmd(200, "bpool:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst", "n1",
                       "local-zfs", 2, 2048, 10,
                       net0="name=eth0,bridge=vmbr0,ip=192.168.0.150/24,gw=192.168.0.1",
                       unprivileged=False, features="nesting=1,keyctl=1")
    assert c.startswith("pct create 200 ")
    assert "--unprivileged 0" in c
    assert "--features" in c and "nesting=1,keyctl=1" in c
    assert "--rootfs" in c and "local-zfs:10" in c
    assert "--net0" in c and "192.168.0.150/24" in c
    assert "--hostname" in c and "n1" in c
    assert c.rstrip().endswith("--start 0")


def test_pct_create_cmd_unprivileged_no_features():
    c = pct_create_cmd(201, "tmpl", "n2", "cpool", 1, 1024, 8)
    assert "--unprivileged 1" in c
    assert "--features" not in c
    assert "--net0" not in c


def test_pct_create_cmd_hostname_default():
    c = pct_create_cmd(202, "tmpl", "", "cpool", 1, 1024, 8)
    assert "ct-202" in c
