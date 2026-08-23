"""Pure per-branch sandbox allocation (vera/evolve/sandbox_pool.py). No docker."""

from vera.evolve import sandbox_pool as sp


def test_slug_is_docker_safe():
    assert sp.slug_for_branch("feat/x") == "feat-x"
    assert sp.slug_for_branch("agentic-loop-improvements-2") == "agentic-loop-improvements-2"
    assert sp.slug_for_branch("Feat/UPPER_Case") == "feat-upper_case"
    assert sp.slug_for_branch("loop-lab/sandbox//weird**chars") == "loop-lab-sandbox-weird-chars"
    assert sp.slug_for_branch("") == "dev"
    assert sp.slug_for_branch("---") == "dev"
    assert len(sp.slug_for_branch("x" * 100)) <= 40


def test_container_name():
    assert sp.container_name("feat/x") == "vera-dev-feat-x"
    assert sp.container_name("agentic-loop-improvements-2") == "vera-dev-agentic-loop-improvements-2"


def test_alloc_port_high_first_skips_used_and_reserved():
    assert sp.alloc_port([]) == 8998                       # nothing used -> highest
    assert sp.alloc_port([8998]) == 8997                   # 8998 busy -> next down
    assert sp.alloc_port([8998, 8997, 8996]) == 8995
    # 8999 is reserved for prod even if "free"
    assert 8999 != sp.alloc_port([8998])
    # exhausted pool -> None
    assert sp.alloc_port(list(range(8980, 8999))) is None


def test_alloc_db_low_first_skips_used():
    assert sp.alloc_db([]) == 3                            # first dev db
    assert sp.alloc_db([3]) == 4
    assert sp.alloc_db([3, 4, 5]) == 6
    assert sp.alloc_db(list(range(3, 16))) is None         # exhausted


def test_two_branches_get_distinct_slots():
    # simulate: vera-dev already on 8998/db3; a second branch must not collide
    in_use_ports = [8999, 8998, 8996]   # prod, first dev, code sidecar
    in_use_dbs = [3]
    p = sp.alloc_port(in_use_ports)
    d = sp.alloc_db(in_use_dbs)
    assert p not in in_use_ports and p != 8999
    assert d not in in_use_dbs
    assert sp.container_name("agentic-loop-improvements-2") != "vera-dev"


def test_capacity_snapshot_reports_last_slot_without_reserving_it():
    got = sp.capacity_snapshot(
        used_ports=list(range(8981, 8999)),
        used_dbs=list(range(3, 15)),
    )
    assert got["available_slots"] == 1
    assert got["exhausted"] is False
    assert got["next_port"] == 8980
    assert got["next_redis_db"] == 15


def test_capacity_snapshot_fails_closed_when_either_resource_is_full():
    db_full = sp.capacity_snapshot([], list(range(3, 16)))
    assert db_full["available_slots"] == 0
    assert db_full["exhausted"] is True
    assert db_full["next_port"] == 8998
    assert db_full["next_redis_db"] is None

    port_full = sp.capacity_snapshot(list(range(8980, 8999)), [])
    assert port_full["available_slots"] == 0
    assert port_full["exhausted"] is True
