"""Unit tests for the ops-node network bake (WiFi + Twingate from sealed secrets).

Imported via lowercase vera.* so pytest binds to the worktree copy (see
worktree-testable-cores-pattern)."""
import base64
from vera.foundry.foundry_core import (parse_ops_secrets, wpa_supplicant_conf,
                                       ops_network_overlay, pxe_ops_apkovl_files,
                                       pxe_desktop_apkovl_files)


def test_parse_ops_secrets():
    d = parse_ops_secrets("# c\n\nWIFI_SSID=Home\nWIFI_PASSWORD=p@ss=word\nBLANK=\n")
    assert d["WIFI_SSID"] == "Home"
    assert d["WIFI_PASSWORD"] == "p@ss=word"   # value keeps '=' after first split
    assert d["BLANK"] == ""
    assert "c" not in d


def test_wpa_supplicant_conf():
    c = wpa_supplicant_conf([("Home", "secret"), ("", "skipme"), ("Open", "")])
    assert 'ssid="Home"' in c and 'psk="secret"' in c
    assert 'ssid="Open"' in c and "key_mgmt=NONE" in c   # open network (blank psk)
    assert "skipme" not in c                              # blank ssid dropped
    assert c.count("network={") == 2


def test_ops_network_overlay_wifi_and_twingate():
    key = base64.b64encode(b'{"network":"x.twingate.com","service_account_id":"a"}').decode()
    secrets = {"WIFI_SSID": "Home", "WIFI_PASSWORD": "pw",
               "TWINGATE_NETWORK": "x.twingate.com", "TWINGATE_SERVICE_KEY_B64": key}
    files, tail = ops_network_overlay(secrets)
    assert "etc/wpa_supplicant/wpa_supplicant.conf" in files
    assert 'ssid="Home"' in files["etc/wpa_supplicant/wpa_supplicant.conf"]
    assert "wpa_supplicant -B" in tail and "udhcpc" in tail            # WiFi auto-brought-up
    assert files["etc/foundry/twingate/service_key.json"].startswith("{")  # key decoded
    assert "usr/local/bin/foundry-twingate" in files                  # connect helper baked
    assert "twingate" not in tail.lower()                             # on-demand, not at boot


def test_ops_network_overlay_empty():
    assert ops_network_overlay({}) == ({}, "")
    assert ops_network_overlay(None) == ({}, "")


def test_pxe_apkovl_bakes_secrets_into_boot():
    secrets = {"WIFI_SSID": "Home", "WIFI_PASSWORD": "pw"}
    base = pxe_ops_apkovl_files("10.22.22.25")
    withwifi = pxe_ops_apkovl_files("10.22.22.25", secrets=secrets)
    assert "etc/wpa_supplicant/wpa_supplicant.conf" not in base           # baseline: none
    assert "etc/wpa_supplicant/wpa_supplicant.conf" in withwifi
    assert "wpa_supplicant -B" in withwifi["etc/local.d/foundry.start"]   # baked into boot
    dsk = pxe_desktop_apkovl_files("10.22.22.25", secrets=secrets)
    assert "wpa_supplicant -B" in dsk["etc/local.d/desktop.start"]        # desktop too
