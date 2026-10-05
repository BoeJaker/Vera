"""The provisioning pages drive the vLLM node path end to end.

Static guards (pages and app modules cannot be imported app-free):
* Estate provisioning offers vllm_server and passes the vLLM build options;
* Estate → Machines switches a node with nodes.backend.switch (not the old
  Proxmox-only pxstore call), from any node with an Ollama instance;
* nodes.list says which node is switched and which Ollama node a vLLM server
  gates as;
* the Provision page sends the vLLM build options; the vLLM page sets gate_as.
"""
import ast
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = "vera/workers/nodes_capabilities.py"
SOFT = "vera/provisioning/software_capabilities.py"
WORKERS_UI = "vera/workers/workers_ollama_panel.html"
PROV_UI = "vera/provisioning/provision_panel.html"
VLLM_UI = "vera/vllm/vllm_panel.html"


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _func(rel, name):
    src = _src(rel)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {rel}")


def test_estate_catalogue_offers_vllm_server_and_runs_it_as_a_component():
    src = _src(NODES)
    cat = src[src.index("_COMPONENTS: Dict[str, Dict[str, Any]] = {"):]
    cat = cat[:cat.index("\n}\n")]
    assert '"vllm_server": {' in cat
    body = _func(NODES, "_prov_step")
    assert '"mesh_gateway", "vllm_server")' in body


def test_vllm_build_options_reach_the_installers():
    body = _func(NODES, "_prov_step")
    assert 'vllm_version=opt.get("vllm_version", "")' in body
    assert 'pip_spec=opt.get("pip_spec", "")' in body
    run = _func(SOFT, "cap_run")
    assert "mode=mode" in run and "vllm_version=vllm_version" in run and "pip_spec=pip_spec" in run


def test_nodes_list_reports_switch_state():
    body = _func(NODES, "_build_nodes")       # what nodes.list returns
    assert '"switched": iid in _sw_ids' in body
    assert '"gate_as": getattr(inst, "gate_as", "")' in body


def test_machines_switch_uses_nodes_backend_switch():
    ui = _src(WORKERS_UI)
    assert "api('/nodes/backend/switch','POST',body)" in ui
    assert "/pxstore/backend/switch" not in ui
    assert 'id="nd-sw"' in ui and "_mcFloat('nd-sw')" in ui
    assert "ndSwitchGo(true);" in ui          # opening the drawer previews first
    # any node with an Ollama instance, not only Proxmox guests
    assert "${(n.ollama||[]).length?`" in ui
    assert "ndSwitchClose, ndSwitchBackendChanged, ndSwitchGo," in ui


def test_estate_provision_drawer_sends_vllm_options():
    ui = _src(WORKERS_UI)
    for k in ("opt.vllm_mode=vm", "opt.vllm_version=vv", "opt.pip_spec=vw"):
        assert k in ui


def test_provision_page_sends_vllm_options():
    ui = _src(PROV_UI)
    assert "options.vllm_mode=" in ui and "options.pip_spec=" in ui     # Deploy tab
    assert "body.mode=" in ui and "body.vllm_version=" in ui           # Install tab
    assert "vllm_server" in ui


def test_vllm_page_sets_gate_as():
    ui = _src(VLLM_UI)
    assert "gate_as:  document.getElementById('add-gateas').value.trim()" in ui


def test_vllm_launch_page_launches_on_a_node_through_provisioning():
    ui = _src(VLLM_UI)
    assert 'name="lnch-where" value="node" checked' in ui           # a node is the default
    assert "'/nodes/provision', {node_id: n.node, components: ['vllm_server']" in ui
    assert "api(path, 'POST', body)" in ui and "'/nodes/backend/switch'" in ui
    assert "--limit-mm-per-prompt.image" in ui                       # text-only
    assert "data-host-only" in ui
    # the stale command shape is gone from the preview and the backend
    assert "vllm.entrypoints.openai.api_server" not in ui
    assert "--speculative-model" not in ui and "turbomind" not in ui.lower()
    start = _func("vera/vllm/vllm_capabilities.py", "vllm_server_start")
    assert "local_serve_argv" in start and "redact_argv" in start
    assert '" ".join(cmd)' not in start                                # never the raw key


def test_vllm_add_instance_points_at_a_node_port():
    ui = _src(VLLM_UI)
    assert 'id="add-url" placeholder="http://192.168.0.247:8000"' in ui
    assert "RTX 4090" not in ui


def test_vllm_playground_streams_through_the_proxy():
    ui = _src(VLLM_UI)
    send = ui[ui.index("async function pgSend()"):]
    send = send[:send.index("\n}\n")]
    assert "'/vllm/proxy/' + encodeURIComponent(inst)" in send
    assert "stream: true" in send and "getReader()" in send
    assert "chat_template_kwargs = {enable_thinking" in send
    assert "'/vllm/chat'" not in send        # the one-shot call that hit the request timeout
    assert 'id="pg-think"' in ui
    # the answer alone goes back into the conversation, not the thinking
    assert "S.pgMessages.push({role:'user',content:prompt},{role:'assistant',content:answer})" in send


def test_the_proxy_forwards_a_json_content_type():
    src = _src("vera/vllm/vllm_capabilities.py")
    proxy = src[src.index("async def _vllm_proxy"):]
    proxy = proxy[:proxy.index("inst.in_use += 1")]
    assert 'headers["Content-Type"] = request.headers.get("content-type") or "application/json"' in proxy


def test_vllm_launch_sets_a_reasoning_parser_for_qwen3():
    ui = _src(VLLM_UI)
    assert "a.push('--reasoning-parser', rp)" in ui
    assert "/qwen3/i.test(body.model" in ui
