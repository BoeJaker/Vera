"""tg.send_audio uploads a file from the box to Telegram, so the path it will
accept is pinned: an audio file inside Vera's state directory, nothing else."""
import ast
import os
import sys
import types
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _guard(state_root: Path):
    src = open(os.path.join(ROOT, "vera", "telegram", "telegram_capabilities.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_audio_path_ok")
    fake = types.ModuleType("Vera.vera.state_paths")
    fake.state_root = lambda: state_root
    saved = sys.modules.get("Vera.vera.state_paths")
    sys.modules["Vera.vera.state_paths"] = fake
    from typing import Optional
    ns = {"Path": Path, "Optional": Optional}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "x", "exec"), ns)
    return ns["_audio_path_ok"], saved


def test_only_audio_inside_the_state_directory(tmp_path):
    state = tmp_path / "vera-state"
    (state / "intel" / "audio").mkdir(parents=True)
    ok_file = state / "intel" / "audio" / "abc.mp3"
    ok_file.write_bytes(b"x")
    (state / "secrets.json").write_text("{}")
    outside = tmp_path / "elsewhere.mp3"
    outside.write_bytes(b"x")
    guard, saved = _guard(state)
    try:
        assert guard(str(ok_file)) == str(ok_file.resolve())
        assert guard(str(state / "secrets.json")) is None                       # not audio
        assert guard(str(outside)) is None                                      # outside the state dir
        assert guard(str(state / "intel" / "audio" / ".." / ".." / ".." / "elsewhere.mp3")) is None
        assert guard(str(state / "intel" / "audio" / "missing.mp3")) is None
    finally:
        if saved is not None:
            sys.modules["Vera.vera.state_paths"] = saved
        else:
            sys.modules.pop("Vera.vera.state_paths", None)
