"""Unit tests for provision.worker native command-building (components_core).

These lock in the three fixes that were needed to get a native Vera worker to actually
register on a fresh node (proven live). Imported via lowercase vera.* so pytest binds
to the worktree copy, not the main checkout."""
from vera.provisioning.components_core import rewrite_host, native_worker_cmd


def test_rewrite_host_repoints_local_backends():
    # a remote worker can't reach the orchestrator's own localhost stores
    assert rewrite_host("redis://localhost:6379", "192.168.0.138") == "redis://192.168.0.138:6379"
    assert rewrite_host("postgresql://admin:admin@127.0.0.1:5433/postgres", "10.0.0.5") == \
        "postgresql://admin:admin@10.0.0.5:5433/postgres"
    assert rewrite_host("bolt://host.docker.internal:7687", "1.2.3.4") == "bolt://1.2.3.4:7687"
    # a non-local host is left alone
    assert rewrite_host("redis://192.168.0.138:6379", "10.0.0.5") == "redis://192.168.0.138:6379"
    # empty inputs are safe
    assert rewrite_host("", "1.2.3.4") == "" and rewrite_host("x", "") == "x"


def test_native_worker_cmd_fixes_layout_cwd_and_durability():
    cmd = native_worker_cmd(root="/root/.vera/worker", repo="https://github.com/BoeJaker/Vera.git",
                            redis_url="redis://192.168.0.138:6379",
                            backend_kv={"POSTGRES_URL": "postgresql://a:b@192.168.0.138:5433/postgres",
                                        "CHROMA_HOST": "192.168.0.138", "CHROMA_PORT": "8008"},
                            port=8990)
    # LAYOUT: the repo's vera/ package is exposed as Vera/vera (not the repo root itself)
    assert "ln -sfn /root/.vera/worker/src/vera /root/.vera/worker/app/Vera/vera" in cmd
    assert "git clone --depth 1 https://github.com/BoeJaker/Vera.git /root/.vera/worker/src" in cmd
    # CWD: launched from /tmp (never the package dir) so vera/operator can't shadow stdlib
    assert "cd /tmp;" in cmd or "WorkingDirectory=/tmp" in cmd
    assert "cd /root/.vera/worker/src/vera" not in cmd          # the old shadow-causing cwd
    # unbuffered so a boot failure is actually visible in the log
    assert "python -u -m Vera.vera.capability_orchestration" in cmd
    # DURABLE: systemd unit installed, with a nohup fallback for non-systemd hosts
    assert "/etc/systemd/system/vera-worker.service" in cmd
    assert "systemctl enable --now vera-worker" in cmd
    assert "nohup" in cmd
    # backend env carried through — incl. Chroma, which the old code dropped entirely
    assert "REDIS_URL" in cmd and "CHROMA_HOST" in cmd and "POSTGRES_URL" in cmd


def test_native_worker_cmd_nohup_only_when_systemd_disabled():
    cmd = native_worker_cmd(root="/r", repo="u", redis_url="redis://x:6379", backend_kv={},
                            port=8990, use_systemd=False)
    assert "systemctl" not in cmd
    assert "cd /tmp;" in cmd and "nohup" in cmd
