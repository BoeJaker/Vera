"""A dev restart (os.execv keeps the environment) must not bring Vera back on a rotated-out Redis password
(18 Oct 2026: after redis.auth.ensure rotate=true + sys.dev.restart, prod still used the OLD password because
config.py had written it into os.environ["REDIS_URL"] on the first boot)."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "vera" / "capability_orchestration.py"


def _fn():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_redis_url_for_relaunch")
    ns = {"Optional": __import__("typing").Optional}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["_redis_url_for_relaunch"]


def test_credentials_dropped_when_the_sealed_copy_exists():
    f = _fn()
    assert f("redis://vera:old-secret@localhost:6379/0", True) == "redis://localhost:6379/0"


def test_left_alone_without_a_copy_or_without_credentials():
    f = _fn()
    assert f("redis://vera:old-secret@localhost:6379/0", False) is None
    assert f("redis://localhost:6379", True) is None
    assert f("", True) is None


def test_the_reexec_applies_it_before_execv():
    s = SRC.read_text(encoding="utf-8")
    i = s.index("_redis_url_for_relaunch(os.environ.get(\"REDIS_URL\"")
    j = s.index("os.execv(sys.executable, _relaunch_argv())")
    assert i < j
