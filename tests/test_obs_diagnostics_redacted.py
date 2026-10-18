"""obs.diagnostics must never return the Redis password (18 Oct 2026: it returned REDIS_URL with the `vera`
user's password over an API that has no auth)."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "vera" / "capability_orchestration.py"


def test_diagnostics_does_not_return_the_raw_url():
    s = SRC.read_text(encoding="utf-8")
    assert '"redis_url":   REDIS_URL,' not in s
    assert '"redis_url":   _redact_redis_url(REDIS_URL)' in s


def test_redact_helper_hides_the_password():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_redact_redis_url")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    out = ns["_redact_redis_url"]("redis://vera:s3cr3t-value@localhost:6379/0")
    assert "s3cr3t-value" not in out and "vera" in out and "localhost:6379" in out
    assert ns["_redact_redis_url"]("redis://localhost:6379") == "redis://localhost:6379"
