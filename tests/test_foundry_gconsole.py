"""Graphical VM console in the ops menu: a new 'gconsole' option opens the Proxmox
noVNC console for a chosen VM in Firefox (already baked into the desktop image), working
for any VM without reconfiguring its display. Guarded to the desktop node (needs X)."""
from vera.foundry.foundry_core import pxe_ops_apkovl_files, pxe_desktop_apkovl_files


def test_ops_tui_has_graphical_console():
    tui = pxe_ops_apkovl_files("10.0.0.1")["usr/local/bin/foundry-tui"]
    assert "gconsole" in tui
    assert "novnc=1" in tui                       # Proxmox noVNC console
    assert ":8006" in tui                          # Proxmox web endpoint
    assert "console=kvm" in tui                    # VM (kvm) console
    assert "firefox" in tui                        # launched in the baked browser
    assert 'if [ -z "$DISPLAY" ]' in tui           # guarded to the desktop node


def test_desktop_reuses_same_tui_with_gconsole():
    # the desktop image reuses the ops-node foundry-tui, so it gets gconsole too
    tui = pxe_desktop_apkovl_files("10.0.0.1")["usr/local/bin/foundry-tui"]
    assert "gconsole" in tui and "novnc=1" in tui
