"""Trust > Identity shows the zone's names (add / remove one value through the
confirm flow) and can give a user a second factor sealed to keydrop."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical


def read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_names_card_reads_the_zone_and_edits_through_the_confirm_flow():
    p = read("vera", "provisioning", "identity_panel.html")
    assert "api('/identity/dns/records')" in p
    assert "veraEstate.confirmRun('/identity/dns/record',{name,ip:addr}" in p
    assert "veraEstate.confirmRun('/identity/dns/delete',{name,ip}" in p
    assert "veraEstate.chip(veraEstate.refFor({addr:ip}),ip)" in p, "each address links to its machine"
    assert 'data-select="machines"' in p, "the address field offers the one list"
    for src in ("/ui/vera-estate.js", "/ui/vera-select.js"):
        assert f'<script src="{src}"></script>' in p, src


def test_a_user_can_be_given_a_second_factor_sealed_to_keydrop():
    p = read("vera", "provisioning", "identity_panel.html")
    assert "veraEstate.confirmRun('/identity/user/mfa',{login,no_expiry:true}" in p
    assert "never shown here" in p
