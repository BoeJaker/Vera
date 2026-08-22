"""Unit tests for the ops-node mode toggles (foundry-mode): Vera worker / mesh / swarm.
Imported via lowercase vera.* so pytest binds to the worktree copy."""
from vera.foundry.foundry_core import (foundry_mode_script, pxe_ops_apkovl_files,
                                       pxe_desktop_apkovl_files)


def test_mode_script_structure():
    s = foundry_mode_script("10.22.22.25")
    assert s.startswith("#!/bin/sh")
    assert "SRV=10.22.22.25" in s and "__SRV__" not in s          # placeholder substituted
    for fn in ("apply_vera()", "apply_mesh()", "apply_swarm()"):
        assert fn in s
    assert "vera|mesh|swarm)" in s
    assert "status)" in s and "boot)" in s and "menu)" in s
    assert "/etc/foundry/modes" in s                              # persisted desired-state
    assert "vera-worker" in s and "/ops/vera-worker-env" in s and "FOUNDRY_VERA_IMAGE" in s
    assert "wg-quick up vera0" in s and "wg-quick down vera0" in s
    assert "docker swarm join" in s and "docker swarm leave" in s


def test_ops_overlay_wires_modes():
    f = pxe_ops_apkovl_files("10.22.22.25")
    assert "usr/local/bin/foundry-mode" in f
    assert "etc/local.d/zz-foundry-modes.start" in f
    assert "foundry-mode boot" in f["etc/local.d/zz-foundry-modes.start"]
    assert 'modes "Node modes' in f["usr/local/bin/foundry-tui"]
    assert "modes) clear; /usr/local/bin/foundry-mode menu" in f["usr/local/bin/foundry-tui"]
    assert "/etc/foundry/modes/swarm" in f["etc/local.d/foundry.start"]   # swarm-off persists


def test_desktop_overlay_wires_modes():
    d = pxe_desktop_apkovl_files("10.22.22.25")
    assert "usr/local/bin/foundry-mode" in d
    assert "etc/local.d/zz-foundry-modes.start" in d
    assert "/etc/foundry/modes/swarm" in d["etc/local.d/desktop.start"]
