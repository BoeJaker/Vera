import ast
import asyncio
from pathlib import Path

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


def test_stream_activity_keeps_metrics_without_message_or_response_copy():
    source = _function_source(
        ROOT / "vera" / "agents" / "agents.py", "agent_chat_stream_endpoint")

    assert source.count('"message_evidence": _text_evidence(message)') == 2
    assert '"message":      message' not in source
    assert '"preview":       "".join(_resp_head)' not in source
    assert source.count('"response_chars": _resp_chars') == 2


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
