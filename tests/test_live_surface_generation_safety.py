import ast
import asyncio
from pathlib import Path
import re

import pytest


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


def _redacted_args(path: Path, function_name: str) -> set[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == function_name
    )
    decorator = next(
        item for item in node.decorator_list
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "capability"
    )
    value = next(
        item.value for item in decorator.keywords if item.arg == "redact_args"
    )
    return set(ast.literal_eval(value))


def _decorator_keyword(path: Path, function_name: str, keyword: str):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name == function_name
    )
    decorator = next(
        item for item in node.decorator_list
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "capability"
    )
    value = next(item.value for item in decorator.keywords if item.arg == keyword)
    return ast.literal_eval(value)


def test_chat_and_authoring_boundaries_redact_user_payloads():
    ide = ROOT / "vera" / "ide" / "ide_capabilities.py"
    agents = ROOT / "vera" / "agents" / "agents.py"
    workshop = ROOT / "vera" / "dag" / "dag_workshop_capabilities.py"

    assert {"prompt", "system", "history", "context_files"} <= _redacted_args(
        ide, "ide_agent_chat")
    assert {"prompt", "system"} <= _redacted_args(ide, "ide_generate")
    assert {"message", "history"} <= _redacted_args(agents, "agent_chat")
    assert _decorator_keyword(ide, "ide_agent_chat", "redact_result") is True
    assert _decorator_keyword(ide, "ide_generate", "redact_result") is True
    assert _decorator_keyword(agents, "agent_chat", "redact_result") is True
    assert {"task", "context_files", "requirements", "content"} <= _redacted_args(
        workshop, "cap_code_author")
    assert {"task", "context_files", "content", "text"} <= _redacted_args(
        workshop, "cap_prose_author")


def test_ide_chat_event_uses_payload_free_prompt_evidence():
    source = _function_source(
        ROOT / "vera" / "ide" / "ide_capabilities.py", "ide_agent_chat")

    assert '"prompt_evidence": _text_evidence(prompt)' in source
    assert "prompt_snippet" not in source


def test_prompt_evidence_does_not_retain_text():
    from vera.ide.ide_capabilities import _text_evidence

    secret = "private probe text"
    evidence = _text_evidence(secret)

    assert evidence["chars"] == len(secret)
    assert len(evidence["sha256"]) == 16
    assert secret not in repr(evidence)


def test_shared_router_request_log_never_retains_prompt_payloads():
    from vera import capability_orchestration as orchestration

    secret = "private authoring instruction that must not enter telemetry"
    clean = orchestration._sanitize_ollama_log_entry({
        "req_id": "request-1",
        "prompt_preview": secret[:24],
        "prompt_full": secret,
        "status": "done",
    })

    assert "prompt_full" not in clean
    assert clean["prompt_preview"].startswith("[prompt chars=")
    assert clean["prompt_evidence"]["chars"] == len(secret)
    assert len(clean["prompt_evidence"]["sha256"]) == 16
    assert secret not in repr(clean)
    assert secret[:24] not in repr(clean)

    safe_again = orchestration._sanitize_ollama_log_entry(clean)
    assert safe_again == clean
    assert safe_again["prompt_evidence"] == clean["prompt_evidence"]


def test_shared_router_generation_and_embedding_events_are_payload_free():
    source = (ROOT / "vera" / "capability_orchestration.py").read_text(
        encoding="utf-8")

    assert 'prompt_preview = _ollama_payload_preview(prompt, "prompt")' in source
    assert 'text_preview = _ollama_payload_preview(text, "embed")' in source
    assert '"prompt_full": (prompt or "")[:16000]' not in source
    assert '"prompt_full":  f"[embed]' not in source


def test_v1_agent_loop_does_not_reference_v2_only_phase_state():
    source = _function_source(
        ROOT / "vera" / "fabric" / "context.py", "cap_dag_agent_loop")

    for v2_only_name in (
            "_dw_cap_phase", "_emit_phase_v2", "explore_done", "validated"):
        assert v2_only_name not in source


def test_agent_loop_accepts_only_empty_call_spelling_of_an_allowed_tool():
    from vera.fabric.context import _canonical_tool_name, _terminal_action_summary

    allowed = ["system.timestamp"]
    assert _canonical_tool_name("system.timestamp", allowed) == "system.timestamp"
    assert _canonical_tool_name("system.timestamp()", allowed) == "system.timestamp"
    assert _canonical_tool_name(" system.timestamp ( ) ", allowed) == "system.timestamp"
    assert _canonical_tool_name("system.timestamp(1)", allowed) == "system.timestamp(1)"
    assert _canonical_tool_name("other.tool()", allowed) == "other.tool()"

    assert _terminal_action_summary(
        {"action": "final", "summary": "finished"}) == (True, "finished")
    assert _terminal_action_summary(
        {"summary": "finished"}, had_success=True) == (True, "finished")
    assert _terminal_action_summary(
        {"summary": "not yet"}, had_success=False) == (False, "")
    assert _terminal_action_summary(
        {"action": "invented", "summary": "not yet"}, had_success=True) == (False, "")


def test_stream_activity_keeps_metrics_without_message_or_response_copy():
    source = _function_source(
        ROOT / "vera" / "agents" / "agents.py", "agent_chat_stream_endpoint")

    assert source.count('"message_evidence": _text_evidence(message)') == 2
    assert '"message":      message' not in source
    assert '"preview":       "".join(_resp_head)' not in source
    assert source.count('"response_chars": _resp_chars') == 2


def test_extra_chat_generations_are_off_by_default_and_visible_in_ui():
    source = (ROOT / "vera" / "agents" / "agents.py").read_text(encoding="utf-8")
    panel = (ROOT / "vera" / "agents" / "agent_panel.html").read_text(
        encoding="utf-8")

    assert re.search(r"^\s*quick_opener:\s*bool\s*=\s*False", source, re.M)
    assert re.search(r'^\s*two_tier:\s*str\s*=\s*"off"', source, re.M)
    assert '_qo_enabled and len(message or "") >= _qo_threshold' in source
    assert '_tt_plan.get("split")' in source
    assert 'id="f-quick_opener"' in panel
    assert 'id="f-two_tier"' in panel
    assert "quick_opener:false" in panel
    assert "two_tier:'off'" in panel


def test_ide_generate_uses_shared_router_and_preserves_effective_route(monkeypatch):
    from vera.ide import ide_capabilities as ide

    observed = {}

    async def fake_generate(prompt, **kwargs):
        observed.update({"prompt": prompt, **kwargs})
        kwargs["meta_out"].update({"model": "effective-model", "instance": "gpu-250"})
        return "generated"

    async def fake_record(**kwargs):
        observed["recorded"] = kwargs

    monkeypatch.setattr(ide, "ollama_generate", fake_generate)
    monkeypatch.setattr(ide, "_record", fake_record)

    async def run():
        result = await ide.ide_generate(
            agent="writer",
            prompt="write one line",
            system="stay concise",
            model="requested-model",
            instance_id="gpu-250",
            temperature=0.25,
            session_id="safety-test",
        )
        await asyncio.sleep(0)
        return result

    result = asyncio.run(run())

    assert result == {
        "text": "generated",
        "agent": ide.IDE_AGENT_WRITER,
        "model": "effective-model",
        "instance": "gpu-250",
    }
    assert observed["prompt"] == "write one line"
    assert observed["instance_id"] == "gpu-250"
    assert observed["profile"] == "ide"
    assert observed["role"] == "writer"
    assert observed["request_stage"] == "generation"
    assert observed["options"]["temperature"] == 0.25


def test_ide_generate_has_no_private_direct_provider_path():
    source = _function_source(
        ROOT / "vera" / "ide" / "ide_capabilities.py", "ide_generate")

    assert "ollama_generate(" in source
    assert "httpx.AsyncClient" not in source
    assert "prompt_preview" not in source
    assert "prompt_full" not in source
    assert "emit_event(" not in source


def test_ide_stream_uses_shared_router_and_payload_free_activity():
    source = _function_source(
        ROOT / "vera" / "ide" / "ide_capabilities.py", "ide_stream_endpoint")

    assert "ollama_generate(" in source
    assert "stream_cb=_on_token" in source
    assert "generation.cancel()" in source
    assert "httpx.AsyncClient" not in source
    assert '"prompt_evidence": _text_evidence(prompt)' in source
    assert '"system_evidence": _text_evidence(system)' in source
    assert '"context_file_count":' in source
    assert '"prompt":        prompt' not in source
    assert '"preview":        full_text' not in source
