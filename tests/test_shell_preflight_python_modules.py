"""A shell command's python MODULES must be preflighted, not just its binaries.

Census run 3dce62f7 (2026-08-26): a step that had just authored a package and
its tests spent NINE exec.bash.run calls discovering the environment -
"/usr/local/bin/python3: No module named pytest", then a working pytest
elsewhere, then "file or directory not found". Every warning in that whole
census was exec.bash.run.

`python3 -m pytest` defeated both existing safety nets at once:
  * preflight_shell scanned BINARIES; python3 was present, so nothing was
    missing as far as it could tell - the module was invisible;
  * the reactive "<bin>: not found" auto-install never fired, because the shell
    never said anything was not found.

Imports the app module (plugin: needs the orchestrator first), so it runs
in-container and skips on a host venv.
"""
import pytest

try:
    import Vera.vera.capability_orchestration          # noqa: F401
    from Vera.vera.remote import session_sandbox_capabilities as S
except Exception:                                      # pragma: no cover
    S = None

pytestmark = pytest.mark.skipif(S is None, reason="app module not importable here")


def test_this_module_actually_imported_the_app():
    """A skipped suite proves nothing - assert we really have the module."""
    assert S is not None and hasattr(S, "scan_shell_python_modules")


@pytest.mark.parametrize("cmd,expected", [
    ("python3 -m pytest test_stats.py", "pytest"),
    ("python -m pytest -q", "pytest"),
    ("python3.11 -m pytest", "pytest"),
    ("cd /workspace && python3 -m pytest tests/ -v", "pytest"),
])
def test_dash_m_module_is_detected(cmd, expected):
    assert expected in S.scan_shell_python_modules(cmd)


def test_bare_console_script_is_detected():
    """The model reaches for a bare `pytest ...` as often as `python -m pytest`."""
    assert "pytest" in S.scan_shell_python_modules("pytest -q test_stats.py")


def test_scanner_does_not_guess_from_arbitrary_words():
    """A wrong guess prompts the human to install something irrelevant."""
    for cmd in ("ls -la /workspace", "echo pytest is great", "grep -r numpy ."):
        got = S.scan_shell_python_modules(cmd)
        assert got == [] or all(g in ("pytest", "numpy") and g in cmd.split() for g in got), \
            "over-eager match on %r -> %r" % (cmd, got)


def test_plain_python_invocation_names_no_module():
    assert S.scan_shell_python_modules("python3 script.py") == []
    assert S.scan_shell_python_modules("python3 -c 'print(1)'") == []


def test_pytest_is_in_the_catalog_as_a_pip_package():
    """apt-installing pytest would fail or fetch something else entirely."""
    ent = [e for e in S._PKG_CATALOG if e.get("name") == "pytest"]
    assert ent, "pytest missing from the package catalog"
    assert ent[0]["kind"] == "pip"
    assert "pytest" in (ent[0].get("bins") or []), "the bare console script must map too"


def test_a_pip_console_script_is_not_requested_as_apt(monkeypatch):
    """The kind must come from the catalog entry, not be hardcoded to apt."""
    import asyncio
    captured = {}

    async def fake_probe_bins(sid, bins):
        return {b: False for b in bins}

    async def fake_probe_mods(sid, mods):
        return {m: False for m in mods}

    async def fake_gate(sid, reqs=None, context="", event_sid=""):
        captured["reqs"] = reqs
        return None

    monkeypatch.setattr(S, "_probe_bins", fake_probe_bins)
    monkeypatch.setattr(S, "_probe_python_modules", fake_probe_mods)
    monkeypatch.setattr(S, "_package_gate", fake_gate)

    asyncio.run(S.preflight_shell("sid", "python3 -m pytest test_stats.py"))
    reqs = captured.get("reqs") or []
    assert reqs, "a missing pytest must produce a requirement"
    kinds = {r["package"]: r["kind"] for r in reqs}
    assert kinds.get("pytest") == "pip", "pytest must be requested via pip, got %r" % kinds


def test_bare_pytest_is_requested_via_pip_not_apt(monkeypatch):
    """Isolates the KIND path.

    A bare `pytest ...` is picked up by scan_shell_bins (it is in command
    position), so it flows through the BINARY branch - which used to hardcode
    kind="apt". apt has no `pytest`, so that request would fail or fetch
    something unrelated. This is the test that bites if the kind is hardcoded
    again; the `-m pytest` form goes down the module branch instead and would
    not catch it.
    """
    import asyncio
    captured = {}

    async def fake_probe_bins(sid, bins):
        return {b: False for b in bins}

    async def fake_probe_mods(sid, mods):
        return {m: False for m in mods}

    async def fake_gate(sid, reqs=None, context="", event_sid=""):
        captured["reqs"] = reqs
        return None

    monkeypatch.setattr(S, "_probe_bins", fake_probe_bins)
    monkeypatch.setattr(S, "_probe_python_modules", fake_probe_mods)
    monkeypatch.setattr(S, "_package_gate", fake_gate)

    # command position, so scan_shell_bins sees it
    assert "pytest" in S.scan_shell_bins("pytest -q test_stats.py"), \
        "precondition: the catalog entry must make scan_shell_bins see pytest"

    asyncio.run(S.preflight_shell("sid", "pytest -q test_stats.py"))
    reqs = captured.get("reqs") or []
    assert reqs, "a missing pytest must produce a requirement"
    kinds = {r["package"]: r["kind"] for r in reqs}
    assert kinds.get("pytest") == "pip", \
        "pytest must be requested via pip, not apt - got %r" % kinds
