"""A sandbox must have real Redis, and must be unable to reach prod's.

Sandboxes were configured with REDIS_URL pointing at prod's Redis SERVER on an
isolated DB number. That never worked — redis-server binds 127.0.0.1 only while
a container resolves host.docker.internal to the docker bridge — so every
sandbox has run Redis-less, `evolve.tasks` silently served 14 in-code defaults
instead of 33 real ones, and `evolve.sandbox.snapshot` had nothing to seed.

The isolation itself is deliberate and must survive the fix, so the sandbox
gets its OWN server rather than access to prod's. The central test here is
`pollution_risk`: isolation by DB NUMBER is not isolation at all, because a
process that can reach the server can SELECT any DB on it.

The second thing under test is the GPU gate. `_ensure_coord_redis` derives its
endpoint from REDIS_URL's authority, so a private data Redis would silently
hand each sandbox its own capacity-1 gate wearing the shared gate's name. The
gate is already a no-op in sandboxes today; it must stay one, explicitly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.evolve import sandbox_redis as SR                 # noqa: E402

PROD = "redis://localhost:6379/0"
PROD_VIA_DOCKER = "redis://host.docker.internal:6379/10"
BOX = "vera-dev-feat-something"


# ── the pollution guarantee ─────────────────────────────────────────────────
def test_a_sandbox_cannot_reach_prods_redis():
    assert SR.pollution_risk(SR.data_url(BOX), PROD) == ""


def test_a_db_number_is_NOT_isolation():
    """The old design: same server, different DB. A process that can reach the
    server can SELECT any DB on it, so this has to read as a risk."""
    risk = SR.pollution_risk(PROD_VIA_DOCKER, "redis://host.docker.internal:6379/0")
    assert risk and "does not contain it" in risk


def test_the_same_server_is_the_same_server_whatever_db_is_named():
    assert SR.reaches_same_server("redis://h:6379/0", "redis://h:6379/15") is True
    assert SR.reaches_same_server("redis://h:6379/0", "redis://other:6379/0") is False


def test_credentials_do_not_disguise_the_host():
    assert SR.reaches_same_server("redis://user:pw@h:6379/0", "redis://h:6379/3") is True


def test_a_different_port_on_one_host_is_a_different_server():
    assert SR.reaches_same_server("redis://h:6379/0", "redis://h:6380/0") is False


def test_an_unreadable_url_is_treated_as_unsafe():
    """Fail closed: if we cannot tell where it points, do not claim it is
    contained."""
    for bad in ("", None, "not-a-url", "://"):
        assert SR.pollution_risk(bad, PROD)


# ── the sidecar ─────────────────────────────────────────────────────────────
def test_each_sandbox_gets_its_own_named_redis():
    assert SR.sidecar_name(BOX) == BOX + "-redis"
    assert SR.sidecar_name("a") != SR.sidecar_name("b")


def test_the_data_url_points_at_that_sidecar():
    assert SR.data_url(BOX) == "redis://vera-dev-feat-something-redis:6379/0"


def test_the_sidecar_is_not_published_to_the_host():
    """No ports: reachable only from the compose network. A published port
    would put a wide-open Redis on the host, which is the outcome the
    isolation exists to avoid."""
    y = SR.sidecar_service_yaml(BOX, "vera-net")
    assert "ports:" not in y


def test_the_sidecar_keeps_nothing_across_a_rebuild():
    """Scratch state. A surviving dump would resurrect a state nobody chose."""
    y = SR.sidecar_service_yaml(BOX, "vera-net")
    assert '"--save", ""' in y and '"--appendonly", "no"' in y


def test_the_sidecar_joins_the_sandbox_network():
    assert "vera-net" in SR.sidecar_service_yaml(BOX, "vera-net")


def test_the_sidecar_yaml_is_indented_for_a_services_block():
    y = SR.sidecar_service_yaml(BOX, "n")
    assert y.startswith("  ") and "\n    image:" in y


# ── the GPU gate must not silently become per-sandbox ───────────────────────
def test_a_sandbox_disables_coordination_explicitly():
    assert SR.coord_setting() == SR.COORD_OFF


def test_the_sentinel_turns_coordination_off():
    assert SR.coord_url_from(SR.COORD_OFF, "redis://derived:6379/0") is None
    assert SR.coord_url_from("OFF", "redis://derived:6379/0") is None


def test_an_unset_value_leaves_prods_behaviour_alone():
    """Prod does not set this. Its derived endpoint must be untouched."""
    d = "redis://localhost:6379/0"
    assert SR.coord_url_from(None, d) == d
    assert SR.coord_url_from("", d) == d
    assert SR.coord_url_from("   ", d) == d


def test_an_explicit_endpoint_wins():
    """The seam a genuinely shared coordination Redis would use."""
    assert SR.coord_url_from("redis://coord:6379/0", "redis://derived:6379/0") \
        == "redis://coord:6379/0"


def test_off_is_distinguishable_from_unconfigured():
    """Compose cannot express "unset", so "" must not mean "off" — that would
    make every unconfigured process silently drop out of the gate."""
    assert SR.coord_url_from("", "d") == "d"
    assert SR.coord_url_from(SR.COORD_OFF, "d") is None
