"""Exercise the generation boundary without importing runtime services."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.critical
SOURCE = Path(__file__).resolve().parents[1] / "vera/capabilities/capabilities.py"


def boundary():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"_normalize_generation_think", "llm_generate"}]
    for node in functions:
        node.decorator_list = []
        if node.name == "llm_generate":
            node.body = [stmt for stmt in node.body if not isinstance(stmt, ast.ImportFrom)]
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), (None, None),
    ("False", False), (" false ", False), ("TRUE", True), ("high", "high")])
def test_think_preserves_boolean_semantics(value, expected):
    assert boundary()["_normalize_generation_think"](value) == expected


@pytest.mark.parametrize("value", [0, 1, [], {}, "invalid"])
def test_invalid_think_is_rejected(value):
    with pytest.raises(ValueError):
        boundary()["_normalize_generation_think"](value)


@pytest.mark.asyncio
async def test_empty_backend_result_is_explicit_and_never_saved():
    scope = boundary()
    observed = {}

    async def generate(prompt, **kwargs):
        observed.update(kwargs)
        return ""

    async def context(*args, **kwargs):
        return 32

    async def save(*args):
        pytest.fail("failed generation must not save an empty artifact")

    scope.update(os=SimpleNamespace(getenv=lambda *args: "32"),
                 _ollama_caller_info=lambda: {"caller_func": "test"},
                 _output_budget=None, effective_num_ctx=context,
                 ollama_generate=generate, _llm_save_output=save,
                 OLLAMA_MODEL="fixture")
    result = await scope["llm_generate"]("fixture", backend="ollama",
                                          think="False", save_as="result.txt")
    assert observed["think"] is False
    assert result["error_code"] == "empty_generation"
    assert result["error"]
    assert "path" not in result


@pytest.mark.asyncio
async def test_an_empty_generation_names_the_reason_the_transport_gave():
    """Operator census run3 (2026-09-24): a thinker asked for `fast-8b`, Ollama
    404'd, and llm.generate said only "no usable text" - so the thinker's
    retry-without-the-model, which looks for the 404, never ran."""
    from vera.dag.operator_model_arg_core import is_model_not_found_error
    scope = boundary()
    why = "Exception: ollama returned 404: {\"error\":\"model 'fast-8b' not found\"}"

    async def generate(prompt, **kwargs):
        kwargs["meta_out"]["error"] = why
        return ""

    async def context(*args, **kwargs):
        return 32

    scope.update(os=SimpleNamespace(getenv=lambda *args: "32"),
                 _ollama_caller_info=lambda: {"caller_func": "test"},
                 _output_budget=None, effective_num_ctx=context,
                 ollama_generate=generate, _llm_save_output=lambda *a: _none(),
                 OLLAMA_MODEL="fixture")
    result = await scope["llm_generate"]("fixture", backend="ollama", model="fast-8b")
    assert result["error_code"] == "empty_generation"
    assert result["error"].startswith("Generation returned no usable text")
    assert "fast-8b" in result["error"] and "404" in result["error"]
    assert is_model_not_found_error(result["error"])      # what the thinker keys on


@pytest.mark.asyncio
async def test_an_empty_generation_with_no_reason_reads_as_before():
    scope = boundary()

    async def generate(prompt, **kwargs):
        return ""

    async def context(*args, **kwargs):
        return 32

    scope.update(os=SimpleNamespace(getenv=lambda *args: "32"),
                 _ollama_caller_info=lambda: {"caller_func": "test"},
                 _output_budget=None, effective_num_ctx=context,
                 ollama_generate=generate, _llm_save_output=lambda *a: _none(),
                 OLLAMA_MODEL="fixture")
    result = await scope["llm_generate"]("fixture", backend="ollama")
    assert result["error"] == "Generation returned no usable text; inspect the provider request log."


async def _none():
    return {}
