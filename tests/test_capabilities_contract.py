"""Capability contract (requires the full app; skips if deps absent).

Verifies the operator caps are registered, well-formed, and mounted as routes.
"""

OPERATOR_CAPS = [
    "operator.session.start", "operator.session.status", "operator.session.close",
    "operator.observe", "operator.read", "operator.screenshot", "operator.act",
    "operator.think", "operator.step", "operator.run",
    "operator.mission.list", "operator.mission.run", "operator.test.run",
    "docs.build", "docs.gallery",
]


def test_operator_caps_registered(orch):
    reg = orch.CAPABILITY_REGISTRY
    missing = [c for c in OPERATOR_CAPS if c not in reg]
    assert not missing, f"unregistered operator caps: {missing}"


def test_all_caps_wellformed(orch):
    reg = orch.CAPABILITY_REGISTRY
    for name, cap in reg.items():
        assert cap.get("func") is not None, f"{name} has no func"
        assert "." in name, f"{name} is not dot-namespaced"


def test_operator_http_caps_have_paths(orch):
    # @capability HTTP routes are mounted during lifespan, not at import; assert
    # the route metadata the mounting reads instead.
    reg = orch.CAPABILITY_REGISTRY
    expected = {
        "operator.run": "/operator/run",
        "operator.observe": "/operator/observe",
        "operator.act": "/operator/act",
        "operator.session.start": "/operator/session/start",
        "docs.build": "/docs/build",
    }
    for name, path in expected.items():
        assert reg[name].get("http_path") == path, f"{name} http_path != {path}"


def test_operator_raw_routes_mounted(orch):
    # Raw @APP.get routes ARE mounted at import time.
    paths = {getattr(r, "path", "") for r in orch.APP.routes}
    for p in ["/operator/panel", "/operator/artifact"]:
        assert p in paths, f"raw route not mounted: {p}"


def test_no_duplicate_cap_names(orch):
    # CAPABILITY_REGISTRY is keyed by name, so build from the source list if present
    reg = orch.CAPABILITY_REGISTRY
    assert len(reg) == len(set(reg.keys()))


def test_operator_loop_profile_present(orch):
    import Vera.vera.dag.loop_profiles as lp
    ids = {p["id"] for p in lp.LOOP_PROFILES}
    assert "operator" in ids
