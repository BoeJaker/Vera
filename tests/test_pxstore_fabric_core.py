"""The Storage panel's backend moved onto the Vera File Fabric (VFS-02).

Marked critical: each behaviour below guards something that can destroy or
strand data that five inference nodes serve from.
  * the store has ONE writer, loopback-only on VFS-02; the unit must refuse to
    start when the store is not mounted, or it pulls onto the container rootfs;
  * pull jobs are shell-built from user input (the model name);
  * network sharing reuses a read-only export and must never widen it to rw;
  * the legacy hypervisor share is retired only when nobody is connected.
"""
import json
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vera.proxmox import pxstore_fabric_core as core  # noqa: E402

pytestmark = pytest.mark.critical

EDGE_UNIT = Path(__file__).resolve().parents[1] / "edge" / "ollama-store-writer.service"


# ── paths ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("host, fabric", [
    ("/tank_sdh/vera-store", "/srv/pools/tank_sdh/vera-store"),
    ("/tank_sde/vfs/backup", "/srv/pools/tank_sde/vfs/backup"),
    ("/rpool/data/vera-data", "/srv/pools/rpool/vera-data"),
    ("/mnt/BigDat/media", "/srv/pools/BigDat/media"),
    ("/tank_sdh", "/srv/pools/tank_sdh"),
    ("/tank_sdh/", "/srv/pools/tank_sdh"),
])
def test_fabric_path_maps_every_bound_pool(host, fabric):
    assert core.fabric_path(host) == fabric


@pytest.mark.parametrize("bad", ["/rpool/ROOT/pve-1", "/tank_sdhx/a", "relative",
                                 "/tank_sdh/../etc", "/tank_sdh/a b"])
def test_fabric_path_refuses_paths_the_server_cannot_see(bad):
    with pytest.raises(ValueError):
        core.fabric_path(bad)


# ── the writer unit ───────────────────────────────────────────────────────────
def _env(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("Environment="):
            k, v = line.split("=", 1)[1].strip('"').split("=", 1)
            out[k] = v
    return out


def test_edge_unit_is_exactly_what_the_capability_installs():
    assert EDGE_UNIT.is_file(), "the writer's unit must live in the repo"
    assert EDGE_UNIT.read_text(encoding="utf-8") == core.writer_unit()


def test_writer_is_loopback_only_on_its_own_port():
    env = _env(core.writer_unit())
    assert env["OLLAMA_HOST"] == "127.0.0.1:11436"


def test_writer_never_prunes_and_writes_the_shared_models_dir():
    env = _env(core.writer_unit())
    assert env["OLLAMA_NOPRUNE"] == "1"
    assert env["OLLAMA_MODELS"] == "/srv/pools/tank_sdh/vera-store/models/ollama"


def test_writer_refuses_to_start_without_the_store_mounted():
    text = core.writer_unit()
    assert "ExecStartPre=/usr/bin/mountpoint -q /srv/pools/tank_sdh/vera-store" in text
    assert text.index("ExecStartPre=") < text.index("ExecStart=/usr/local/bin/ollama")


# ── model names and pull jobs ─────────────────────────────────────────────────
@pytest.mark.parametrize("ok", ["llama3.1:8b", "nomic-embed-text", "nomic-embed-text:latest",
                                "jaahas/qwen3.5-uncensored:9b",
                                "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M"])
def test_valid_model_accepts_real_references(ok):
    assert core.valid_model(ok)


@pytest.mark.parametrize("bad", ["", "a;rm -rf /", "$(id)", "x y", "-rf", "../x", "a:b:c",
                                 "name'quote", "a/b/c/d/e/f", "x" * 300, None])
def test_valid_model_rejects_anything_a_shell_could_reinterpret(bad):
    assert not core.valid_model(bad)


def test_pull_slug_is_unit_safe_and_distinct_for_similar_names():
    a, b = core.pull_slug("llama3.1:8b"), core.pull_slug("llama3-1:8b")
    assert a != b
    assert all(c.isalnum() or c == "-" for c in a)


def test_pull_start_script_delivers_the_json_body_intact():
    model = "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M"
    script = core.pull_start_script(model)
    run_line = next(l for l in script.splitlines() if l.startswith("systemd-run"))
    argv = shlex.split(run_line)
    inner_argv = shlex.split(argv[argv.index("-c") + 1])
    assert json.loads(inner_argv[inner_argv.index("-d") + 1]) == {"model": model}
    assert "http://127.0.0.1:11436/api/pull" in inner_argv


def test_pull_start_script_checks_the_writer_and_an_existing_job_first():
    lines = core.pull_start_script("llama3.1:8b").splitlines()
    run = next(i for i, l in enumerate(lines) if l.startswith("systemd-run"))
    assert any("ALREADY_RUNNING" in l for l in lines[:run])
    assert any("WRITER_DOWN" in l for l in lines[:run])


def test_pull_start_script_refuses_an_invalid_model():
    with pytest.raises(ValueError):
        core.pull_start_script("x; reboot")


# ── pull progress ─────────────────────────────────────────────────────────────
def _log(*events):
    return [json.dumps(e) for e in events]


def test_progress_sums_layers_at_their_furthest_point():
    lines = _log({"status": "pulling manifest"},
                 {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 10},
                 {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 60},
                 {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 40})
    p = core.parse_pull_log(lines, unit_active=True)
    assert p["state"] == "running"
    assert (p["completed"], p["total"], p["percent"]) == (100, 400, 25.0)


def test_success_then_exit_zero_is_done():
    p = core.parse_pull_log(_log({"status": "writing manifest"}, {"status": "success"},
                                 {"exit": 0}), unit_active=False)
    assert p["state"] == "done" and not p["error"]


def test_an_error_event_is_a_failure_even_with_exit_zero():
    p = core.parse_pull_log(_log({"error": "pull model manifest: file does not exist"},
                                 {"exit": 0}), unit_active=False)
    assert p["state"] == "failed" and "does not exist" in p["error"]


def test_curl_noise_and_nonzero_exit_is_a_failure_that_says_why():
    p = core.parse_pull_log(["curl: (7) Failed to connect to 127.0.0.1 port 11436",
                             json.dumps({"exit": 7})], unit_active=False)
    assert p["state"] == "failed" and "Failed to connect" in p["error"]


def test_a_stream_that_stopped_without_success_is_not_reported_done():
    p = core.parse_pull_log(_log({"status": "pulling a", "digest": "d", "total": 9,
                                  "completed": 3}), unit_active=False)
    assert p["state"] == "failed"


def test_no_log_is_unknown_not_failed():
    assert core.parse_pull_log([], unit_active=False)["state"] == "unknown"


def test_read_only_error_is_recognised():
    assert core.is_read_only_error("open /.ollama/models/blobs/x: read-only file system")
    assert not core.is_read_only_error("connection refused")


# ── exports ───────────────────────────────────────────────────────────────────
EXPORTS = """
/srv/pools/tank_sde/vfs/home    192.168.0.0/24(rw,sync,no_subtree_check,root_squash)
/srv/pools/tank_sdh/vera-store  192.168.0.0/24(ro,sync,no_subtree_check,root_squash,crossmnt) 10.55.55.0/24(ro,sync,crossmnt)
# /old/export 1.2.3.4(rw)
"""


def test_parse_exports_reads_every_client_and_skips_comments():
    ex = core.parse_exports(EXPORTS)
    assert set(ex) == {"/srv/pools/tank_sde/vfs/home", "/srv/pools/tank_sdh/vera-store"}
    assert [c for c, _ in ex["/srv/pools/tank_sdh/vera-store"]] == ["192.168.0.0/24",
                                                                    "10.55.55.0/24"]


@pytest.mark.parametrize("client, covered", [("192.168.0.138", True), ("192.168.0.0/25", True),
                                             ("10.55.55.9", True), ("192.168.1.5", False),
                                             ("192.168.0.0/16", False), ("garbage", False)])
def test_export_for_answers_whether_a_client_is_already_admitted(client, covered):
    hit = core.export_for(core.parse_exports(EXPORTS), "/srv/pools/tank_sdh/vera-store", client)
    assert (hit is not None) == covered
    if hit:
        assert hit["read_only"]


def test_add_export_client_defaults_read_only_and_keeps_a_backup():
    s = core.add_export_client_script("/srv/pools/tank_sdh/vera-store", "192.168.1.0/24")
    assert "192.168.1.0/24(ro," in s and "(rw" not in s
    assert "/etc/exports.vera-bak" in s and s.rstrip().endswith("echo EXPORT_OK")


@pytest.mark.parametrize("client", ["192.168.1.0/24; rm -rf /", "*", "", "host name"])
def test_add_export_client_refuses_anything_but_an_ip_or_cidr(client):
    with pytest.raises(ValueError):
        core.add_export_client_script("/srv/x", client)


def test_fstab_line_cannot_hang_a_boot():
    line = core.fstab_line("192.168.0.160", "/srv/pools/tank_sdh/vera-store", "/vera-store")
    assert line == ("192.168.0.160:/srv/pools/tank_sdh/vera-store /vera-store nfs "
                    "ro,hard,vers=4.2,_netdev,nofail 0 0")


@pytest.mark.parametrize("args", [("1.2.3.4;id", "/a", "/b"), ("h", "a", "/b"),
                                  ("h", "/a", "/b c")])
def test_fstab_line_rejects_injection(args):
    with pytest.raises(ValueError):
        core.fstab_line(*args)


# ── consumers ─────────────────────────────────────────────────────────────────
def test_parse_store_consumers_from_the_live_config_shape():
    grep = "\n".join([
        "/etc/pve/lxc/129.conf:mp0: /tank_sdh/vera-store/models/ollama,mp=/root/.ollama/models,ro=1",
        "/etc/pve/lxc/126.conf:mp1: /tank_sdh/vera-store/models/ollama,mp=/.ollama/models,ro=1",
        "/etc/pve/lxc/140.conf:mp0: /tank_sdh/vera-store-old/x,mp=/x",
        "/etc/pve/lxc/150.conf:description: mentions /tank_sdh/vera-store",
    ])
    rows = core.parse_store_consumers(grep, "/tank_sdh/vera-store")
    assert [(r["vmid"], r["mp_key"], r["ct_path"], r["ro"]) for r in rows] == [
        (126, "mp1", "/.ollama/models", True), (129, "mp0", "/root/.ollama/models", True)]


# ── backup target ─────────────────────────────────────────────────────────────
def test_backup_target_guards_against_writing_to_the_root_disk():
    s = core.backup_target_script("vfs-backup", "/tank_sde/vfs/backup")
    assert "--is_mountpoint /tank_sde/vfs/backup" in s
    assert "--content backup" in s and "keep-last=3" in s
    assert s.index("ALREADY_EXISTS") < s.index("pvesm add")
    assert s.index("NOT_MOUNTED") < s.index("pvesm add")


@pytest.mark.parametrize("sid", ["VFS", "a", "x;id", "-rf", ""])
def test_backup_target_rejects_bad_storage_ids(sid):
    with pytest.raises(ValueError):
        core.backup_target_script(sid, "/tank_sde/vfs/backup")


# ── legacy share ──────────────────────────────────────────────────────────────
def test_parse_legacy_probe():
    p = core.parse_legacy_probe("active=active\nenabled=enabled\nsessions=0\nlistening=2\n")
    assert p == {"active": True, "enabled": True, "sessions": 0, "listening": True}


def test_retire_refuses_while_anyone_is_connected_unless_forced():
    guarded, forced = core.retire_legacy_script(), core.retire_legacy_script(force=True)
    assert "IN_USE" in guarded and "IN_USE" not in forced
    assert guarded.index("IN_USE") < guarded.index("systemctl disable")


def test_retire_keeps_config_so_restore_is_one_step():
    s = core.retire_legacy_script()
    assert "rm " not in s and "smb.conf" not in s
    assert "enable --now smbd" in core.restore_legacy_script()


def test_consolidate_never_overwrites_a_blob_or_copies_a_partial_pull():
    assert "--ignore-existing" in core.CONSOLIDATE_RSYNC_FLAGS
    assert "*-partial*" in core.CONSOLIDATE_RSYNC_FLAGS
