import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[1] / "vera" / "ide" / "vera_mcp_bridge.py"
_SPEC = importlib.util.spec_from_file_location("vera_mcp_bridge_under_test", _PATH)
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


def test_bridge_forwards_codex_identity_and_session():
    client = bridge.Vera("https://vera.test", "", caller_kind="codex",
                         session_id="codex-session-1")
    client._name_map = {"evolve_pipeline_list": "evolve.pipeline.list"}
    seen = {}

    def fake_post(path, payload):
        seen.update({"path": path, "payload": payload})
        return {"content": {"ok": True}}

    client._post = fake_post
    assert client.call_tool("evolve_pipeline_list", {"limit": 1}) == {"ok": True}
    assert seen["path"] == "/mcp/call"
    assert seen["payload"]["caller_kind"] == "codex"
    assert seen["payload"]["session_id"] == "codex-session-1"


def test_bridge_keeps_legacy_mcp_default():
    assert bridge.Vera("https://vera.test", "").caller_kind == "mcp"
