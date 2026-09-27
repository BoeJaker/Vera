"""Redis ACL users with passwords in OpenBao (redis_auth_core) and the host's
boot path (config._apply_local_redis_credentials).

Lowercase imports with the worktree on sys.path, so the worktree copy is what
is tested."""
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from vera.security import redis_auth_core as core  # noqa: E402


def test_urls_gain_credentials_without_losing_the_rest():
    u = core.with_credentials("redis://192.168.0.138:6379/3", "vera-node", "p-w_x")
    assert u == "redis://vera-node:p-w_x@192.168.0.138:6379/3"
    assert core.has_credentials(u)
    assert core.credentials_of(u) == ("vera-node", "p-w_x")
    # an explicit URL is never overwritten
    assert core.with_credentials(u, "vera", "other") == u
    # no password -> unchanged
    assert core.with_credentials("redis://h:6379", "vera", "") == "redis://h:6379"


def test_a_hosts_credential_is_never_passed_on():
    assert core.without_credentials("redis://vera:s3cret@10.0.0.1:6379/0") == "redis://10.0.0.1:6379/0"
    assert core.without_credentials("redis://10.0.0.1:6379") == "redis://10.0.0.1:6379"


def test_logs_never_carry_the_password():
    assert core.redact_url("redis://vera:s3cret@h:6379/0") == "redis://vera:***@h:6379/0"
    assert "s3cret" not in core.redact_url("redis://:s3cret@h:6379")
    assert core.redact_url("redis://h:6379") == "redis://h:6379"


def test_passwords_are_long_and_url_safe():
    pw = core.new_password()
    assert len(pw) >= 40 and all(c.isalnum() or c in "-_" for c in pw)
    assert core.new_password() != pw


def test_setuser_adds_so_a_rotation_can_overlap():
    args = core.setuser_args("vera", add=["NEW"])
    assert args[:2] == ["vera", "on"] and ">NEW" in args and "resetpass" not in args
    # retire: reset then only the current one
    args = core.setuser_args("vera", add=["NEW"], reset=True)
    assert args.index("resetpass") < args.index(">NEW")
    # the read-only user really is read-only
    ins = core.setuser_args("inspector", add=["x"])
    assert "-@all" in ins and "+@read" in ins and "+@all" not in ins
    try:
        core.setuser_args("mallory", add=["x"])
        assert False, "unknown user accepted"
    except ValueError:
        pass


def test_the_acl_file_seed_changes_nothing_on_its_own():
    assert core.ACL_FILE_SEED.strip() == "user default on nopass ~* &* +@all"


def test_default_is_locked_only_when_nobody_unexpected_is_on_it():
    clients = [{"addr": "172.18.0.32:40000", "user": "default"},      # vikunja
               {"addr": "172.18.0.1:5000", "user": "vera"},
               {"addr": "192.168.0.246:6000", "user": "vera-node"}]
    assert core.default_lock_plan(clients, ["172.18.0.32"])["ok"]
    clients.append({"addr": "172.18.0.1:5001", "user": "default", "name": "stray"})
    plan = core.default_lock_plan(clients, ["172.18.0.32"])
    assert not plan["ok"] and plan["unexpected"][0]["addr"] == "172.18.0.1:5001"


def test_host_boots_from_its_local_sealed_copy(tmp_path):
    """The real boot path: a Fernet-sealed file opened by config at import."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    token = "fernet:" + Fernet(key.encode()).encrypt(b"vera:s3cret-pw").decode()
    f = tmp_path / "redis.auth"
    f.write_text(token)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ROOT, "HOME": str(tmp_path),
           "VERA_SECRET_KEY": key, "VERA_REDIS_AUTH_FILE": str(f),
           "REDIS_URL": "redis://localhost:6379"}
    code = ("import os; from vera import config; "
            "print(os.environ['REDIS_URL']); print(repr(config.cfg))")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[0] == "redis://vera:s3cret-pw@localhost:6379"
    assert "s3cret-pw" not in lines[1] and "vera:***@" in lines[1]   # repr is redacted


def test_no_copy_means_no_change(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ROOT, "HOME": str(tmp_path),
           "VERA_REDIS_AUTH_FILE": str(tmp_path / "absent"), "REDIS_URL": "redis://h:6379"}
    out = subprocess.run([sys.executable, "-c",
                          "import os; from vera import config; print(os.environ['REDIS_URL'])"],
                         env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "redis://h:6379"
