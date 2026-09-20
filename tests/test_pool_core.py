"""What a pool card can say once zpool's own words are parsed: layout, scrub,
throughput, compression, and plain findings. The fixture is the live node's
output on 19 Sep 2026 (seven single-disk pools, all scrubbed six days before);
the synthetic cases cover the layouts this estate does not have yet."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.proxmox import pool_core as pc  # noqa: E402

pytestmark = pytest.mark.critical
NOW = 1_789_830_000.0     # 19 Sep 2026


def sections():
    text = open(os.path.join(os.path.dirname(__file__), "fixtures_zpool_sample.txt"), encoding="utf-8").read()
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("###"):
            cur = line[3:]
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def test_the_live_node_seven_single_disks_all_scrubbed_last_sunday():
    st = pc.parse_zpool_status(sections()["ZSTATUS"], now=NOW)
    assert len(st) == 7
    for p in st.values():
        assert p["layout"] == "1 disk · no redundancy" and p["redundancy"] == "none" and p["disks"] == 1
        assert p["scan_state"] == "done" and p["scan_age_d"] == 6 and p["scan_errors"] == 0 and p["errors"] == 0
        assert p["state"] == "ONLINE" and p["errors_line"] == "No known data errors"
    assert st["tank_sdh"]["vdevs"][0]["disks"][0]["name"] == "wwn-0x58ce38e07c89442c"


def test_list_compress_and_iostat_parse_and_the_live_sample_wins():
    s = sections()
    lst = pc.parse_zpool_list(s["ZLIST"])
    assert lst["rpool"] == {"frag_pct": 48, "cap_pct": 63, "dedup": 1.0}
    assert pc.parse_compress(s["ZCOMP"])["tank_sda"] == 2.07
    io = pc.parse_iostat(s["ZIOSTAT"])
    assert io["tank_sde"] == {"read_ops": 0, "write_ops": 79, "read_bps": 0, "write_bps": 5080351}, \
        "the second block (one live second) must replace the since-boot average"


def test_merge_carries_everything_onto_the_inventory_row():
    s = sections()
    st = pc.parse_zpool_status(s["ZSTATUS"], now=NOW)
    rows = pc.merge([{"name": "tank_sdh", "size": 10, "alloc": 1, "free": 9, "health": "ONLINE"}],
                    st, pc.parse_zpool_list(s["ZLIST"]), pc.parse_compress(s["ZCOMP"]), pc.parse_iostat(s["ZIOSTAT"]))
    r = rows[0]
    assert r["layout"] == "1 disk · no redundancy" and r["compressratio"] == 1.1 and r["frag_pct"] == 8
    assert r["scan_age_d"] == 6 and r["write_ops"] == 0 and r["size"] == 10


MIRRORS = """  pool: tank
 state: DEGRADED
status: One or more devices could not be opened.
  scan: scrub in progress since Sat Sep 19 10:00:00 2026
\t120G scanned at 1G/s, 60G issued at 500M/s, 200G total
\t0B repaired, 30.00% done, 00:04:40 to go
config:

\tNAME            STATE     READ WRITE CKSUM
\ttank            DEGRADED     0     0     0
\t  mirror-0      ONLINE       0     0     0
\t    sda         ONLINE       0     0     0
\t    sdb         ONLINE       0     0     0
\t  mirror-1      DEGRADED     0     0     0
\t    sdc         ONLINE       0     0     2
\t    sdd         UNAVAIL      0     0     0
\tlogs
\t  nvme0n1p1     ONLINE       0     0     0
\tcache
\t  nvme0n1p2     ONLINE       0     0     0

errors: No known data errors

  pool: big
 state: ONLINE
  scan: none requested
config:

\tNAME        STATE     READ WRITE CKSUM
\tbig         ONLINE       0     0     0
\t  raidz1-0  ONLINE       0     0     0
\t    sde     ONLINE       0     0     0
\t    sdf     ONLINE       0     0     0
\t    sdg     ONLINE       0     0     0
\t    sdh     ONLINE       0     0     0

errors: No known data errors

  pool: fast
 state: ONLINE
  scan: scrub repaired 0B in 00:10:00 with 0 errors on Sun Aug  2 00:00:00 2026
config:

\tNAME        STATE     READ WRITE CKSUM
\tfast        ONLINE       0     0     0
\t  sdi       ONLINE       0     0     0
\t  sdj       ONLINE       0     0     0

errors: No known data errors
"""


def test_mirrors_raidz_stripes_logs_and_cache_are_described():
    st = pc.parse_zpool_status(MIRRORS, now=NOW)
    tank, big, fast = st["tank"], st["big"], st["fast"]
    assert tank["layout"] == "2 × mirror of 2 · cache, log" and tank["redundancy"] == "mirror" and tank["disks"] == 4
    assert tank["state"] == "DEGRADED" and tank["errors"] == 2
    assert tank["scan_state"] == "running" and tank["scan_kind"] == "scrub" and tank["scan_pct"] == 30.0
    assert big["layout"] == "raidz1 of 4" and big["redundancy"] == "raidz1" and big["scan_state"] == "never"
    assert fast["layout"] == "2 × 1 disk striped · no redundancy" and fast["redundancy"] == "none"
    assert fast["scan_age_d"] == 48


def test_findings_say_the_worst_first_in_plain_words():
    st = pc.parse_zpool_status(MIRRORS, now=NOW)
    rows = pc.merge([{"name": n, "health": st[n]["state"]} for n in ("tank", "big", "fast")], st,
                    {"fast": {"frag_pct": 61, "cap_pct": 91, "dedup": 1.0}}, {}, {})
    f = pc.pool_findings(rows)
    msgs = [(x["severity"], x["message"]) for x in f]
    assert msgs[0] == ("error", "Pool tank is degraded.")
    assert ("error", "Pool tank has counted 2 read, write or checksum errors.") in msgs
    assert ("warn", "Pool fast is 91% full.") in msgs
    assert ("warn", "Pool big has never been scrubbed.") in msgs
    assert ("warn", "Pool fast was last scrubbed 48 days ago.") in msgs
    assert ("info", "Pool fast has no redundancy: one failing disk loses the pool.") in msgs
    assert ("info", "Pool fast is 61% fragmented.") in msgs
    assert [x["severity"] for x in f] == sorted((x["severity"] for x in f), key={"error": 0, "warn": 1, "info": 2}.get)
    # a healthy mirrored pool scrubbed this week says nothing at all
    assert pc.pool_findings([{"name": "ok", "health": "ONLINE", "redundancy": "mirror", "disks": 2,
                              "scan_state": "done", "scan_age_d": 3, "errors": 0, "cap_pct": 40, "frag_pct": 5}]) == []


def test_the_inventory_asks_zpool_and_hands_the_words_to_pool_core():
    """Structural: the script collects the four sections and the parser routes
    them through pool_core, so the pool rows carry layout, scrub and throughput."""
    src = open(os.path.join(ROOT, "vera", "proxmox", "pxstore_capabilities.py"), encoding="utf-8").read()
    for marker in ("###ZPOOLX", "###ZCOMP", "###ZSTATUS", "###ZIOSTAT"):
        assert marker in src
    assert "zpool status 2>/dev/null" in src and "zpool iostat -Hp 1 2" in src
    assert "_pool.merge(" in src and '"pool_findings": _pool.pool_findings(pools)' in src
    panel = open(os.path.join(ROOT, "vera", "proxmox", "pxstore_panel.html"), encoding="utf-8").read()
    assert "p.layout" in panel and "scan_state" in panel and "compressratio" in panel and "pool_findings" in panel
