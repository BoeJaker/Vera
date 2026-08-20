"""Unit tests for the mesh-gateway rendering helpers (netsec_core).

These let a designated LAN mesh member bridge the mesh to a whole subnet (e.g. the
Vera stack's 192.168.0.0/24) so ops-node workers reach Vera over the mesh from any
network. Imported via lowercase vera.* so pytest binds to the worktree copy."""
from vera.networking.netsec_core import (wg_peer_allowed_ips, wg_gateway_postup,
                                         wg_gateway_postdown)


def test_peer_allowed_ips_plain_and_gateway():
    # a normal member: just its own /32
    assert wg_peer_allowed_ips("10.88.0.5", None) == "10.88.0.5/32"
    assert wg_peer_allowed_ips("10.88.0.5", []) == "10.88.0.5/32"
    # a GATEWAY member: /32 PLUS the subnets it advertises
    assert wg_peer_allowed_ips("10.88.0.1", ["192.168.0.0/24"]) == "10.88.0.1/32, 192.168.0.0/24"
    assert wg_peer_allowed_ips("10.88.0.1", ["192.168.0.0/24", "192.168.1.0/24"]) == \
        "10.88.0.1/32, 192.168.0.0/24, 192.168.1.0/24"
    # dedup + skip blanks
    assert wg_peer_allowed_ips("10.88.0.1", ["10.88.0.1/32", "", "192.168.0.0/24"]) == \
        "10.88.0.1/32, 192.168.0.0/24"


def test_gateway_postup_forwards_and_masquerades():
    up = wg_gateway_postup("10.88.0.0/16", "vera0")
    assert "net.ipv4.ip_forward=1" in up
    # masquerade mesh-sourced traffic leaving any NON-mesh iface, so LAN replies return
    assert "-s 10.88.0.0/16 ! -o vera0 -j MASQUERADE" in up
    assert "-C POSTROUTING" in up and "-A POSTROUTING" in up          # idempotent add
    dn = wg_gateway_postdown("10.88.0.0/16", "vera0")
    assert "-D POSTROUTING -s 10.88.0.0/16 ! -o vera0 -j MASQUERADE" in dn
