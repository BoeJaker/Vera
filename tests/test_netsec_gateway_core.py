"""Unit tests for the mesh-gateway rendering helpers (netsec_core).

These let a designated LAN mesh member bridge the mesh to a whole subnet (e.g. the
Vera stack's 192.168.0.0/24) so ops-node workers reach Vera over the mesh from any
network. Imported via lowercase vera.* so pytest binds to the worktree copy."""
from vera.networking.netsec_core import (wg_peer_allowed_ips, wg_gateway_postup,
                                         wg_gateway_postdown, wg_routes_for_member)


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


def test_routes_excluded_for_member_inside_subnet():
    routes = ["192.168.0.0/24"]
    # OFF-LAN members (a different subnet) SHOULD receive the advertised route
    assert wg_routes_for_member(routes, "10.4.5.6") == ["192.168.0.0/24"]
    assert wg_routes_for_member(routes, "192.168.1.50") == ["192.168.0.0/24"]
    # ON-LAN members (inside the advertised subnet) must NOT receive it (no self-tunnel)
    assert wg_routes_for_member(routes, "192.168.0.96") == []
    assert wg_routes_for_member(routes, "192.168.0.90") == []
    # multi-route: drop ONLY the subnet the member sits inside
    assert wg_routes_for_member(["192.168.0.0/24", "192.168.1.0/24"], "192.168.0.96") == \
        ["192.168.1.0/24"]
    # non-IP host (hostname) -> fail-open, keep routes
    assert wg_routes_for_member(routes, "e2e-mgr.vera.int") == ["192.168.0.0/24"]
    # blanks skipped; unparseable route kept (fail-open); no routes -> empty
    assert wg_routes_for_member(["", "bogus", "192.168.0.0/24"], "10.0.0.1") == \
        ["bogus", "192.168.0.0/24"]
    assert wg_routes_for_member(None, "192.168.0.96") == []


def test_end_to_end_allowedips_excludes_onlan_member():
    # gateway 10.88.0.1 advertises the LAN; an ON-LAN member's rendered AllowedIPs is
    # ONLY the /32 (it keeps using its own link route), an OFF-LAN member also gets the /24.
    routes = ["192.168.0.0/24"]
    onlan = wg_peer_allowed_ips("10.88.0.1", wg_routes_for_member(routes, "192.168.0.96"))
    offlan = wg_peer_allowed_ips("10.88.0.1", wg_routes_for_member(routes, "10.9.9.9"))
    assert onlan == "10.88.0.1/32"
    assert offlan == "10.88.0.1/32, 192.168.0.0/24"


def test_wg_client_config():
    from vera.networking.netsec_core import wg_client_config
    peers = [{"pubkey": "PK1", "ip": "10.88.0.1", "endpoint": "1.2.3.4:51820", "routes": ["192.168.0.0/24"]},
             {"pubkey": "PK2", "ip": "10.88.0.2"}]
    conf = wg_client_config("10.88.0.9", 51820, peers, client_host="10.5.5.5")
    assert "PrivateKey = __PRIVKEY__" in conf and "Address = 10.88.0.9/32" in conf
    assert "PublicKey = PK1" in conf and "Endpoint = 1.2.3.4:51820" in conf
    assert "10.88.0.1/32, 192.168.0.0/24" in conf   # off-LAN client gets the gateway route
    assert "PublicKey = PK2" in conf
    conf2 = wg_client_config("10.88.0.9", 51820, peers, client_host="192.168.0.50")
    assert "192.168.0.0/24" not in conf2            # on-LAN client: no self-tunnel route
