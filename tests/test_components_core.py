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
    assert "systemctl enable vera-worker" in cmd
    # restart, not `enable --now`: a re-provision must leave the OLD commit
    assert "systemctl restart vera-worker" in cmd
    assert "nohup" in cmd
    # backend env carried through — incl. Chroma, which the old code dropped entirely
    env = _env_file(cmd)
    assert "REDIS_URL=" in env and "CHROMA_HOST=" in env and "POSTGRES_URL=" in env


def _env_file(cmd):
    import base64, re
    m = re.search(r"printf %s '?([A-Za-z0-9+/=]+)'? \| base64 -d > (\S+)/worker\.env", cmd)
    assert m, "credentials file is not written"
    return base64.b64decode(m.group(1)).decode()


def _unit(cmd):
    import base64, re
    m = re.search(r"printf %s '?([A-Za-z0-9+/=]+)'? \| base64 -d > /etc/systemd/system/vera-worker.service", cmd)
    return base64.b64decode(m.group(1)).decode()


def test_credentials_stay_out_of_the_world_readable_unit():
    cmd = native_worker_cmd(root="/opt/vera/worker", repo="", bundle=True,
                            redis_url="redis://vera-node:s3cret@10.0.0.1:6379",
                            backend_kv={"POSTGRES_URL": "postgresql://admin:admin@10.0.0.1:5433/p",
                                        "NEO4J_PASS": "neo"}, port=8990)
    unit = _unit(cmd)
    assert "s3cret" not in unit and "admin:admin" not in unit and "NEO4J_PASS" not in unit
    assert "EnvironmentFile=/opt/vera/worker/worker.env" in unit
    env = _env_file(cmd)
    assert 'REDIS_URL="redis://vera-node:s3cret@10.0.0.1:6379"' in env
    assert 'NEO4J_PASS="neo"' in env
    # written under umask 077, then pinned
    assert "umask 077" in cmd and "chmod 600 /opt/vera/worker/worker.env" in cmd
    # and never on the command line of the nohup fallback either
    assert "s3cret" not in cmd.split("nohup")[0].split("base64 -d > /opt/vera/worker/worker.env")[-1]


def test_native_worker_cmd_nohup_only_when_systemd_disabled():
    cmd = native_worker_cmd(root="/r", repo="u", redis_url="redis://x:6379", backend_kv={},
                            port=8990, use_systemd=False)
    assert "systemctl" not in cmd
    assert "cd /tmp;" in cmd and "nohup" in cmd
