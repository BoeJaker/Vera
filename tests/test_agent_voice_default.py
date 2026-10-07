"""Agents left on the old built-in voice follow the voice set for all of Vera
(owner decision 2026-10-06); an agent's own deliberate choice still wins."""
import ast
import os
from typing import Optional

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = open(os.path.join(ROOT, "vera", "agents", "agents.py"), encoding="utf-8").read()


def _fn(name):
    tree = ast.parse(SRC)
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == name)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_OLD_DEFAULT_VOICE" for t in n.targets))]
    ns = {"Optional": Optional}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "x", "exec"), ns)
    return ns[name]


def test_spoken_voice():
    sv = _fn("spoken_voice")
    assert sv("af_heart") == "" and sv("") == "" and sv(None) == ""
    assert sv("bm_george") == "bm_george" and sv(" bf_emma ") == "bf_emma"


def test_agent_speech_sends_a_voice_only_when_the_agent_has_its_own():
    assert '_v = spoken_voice(agent.voice)' in SRC
    worker = SRC[SRC.index("async def _tts_worker(voice, speed, engine):"):SRC.index("# Start the serial worker")]
    assert '"voice": voice' not in worker and 'if voice:' in worker
    chat = SRC[SRC.index("tts_body: dict = {"):SRC.index("tts_body: dict = {") + 400]
    assert '"voice": agent.voice' not in chat and "spoken_voice(agent.voice)" in chat


def test_editors_default_new_agents_to_veras_voice():
    panel = open(os.path.join(ROOT, "vera", "agents", "agent_panel.html"), encoding="utf-8").read()
    assert '<option value="">Vera\'s voice' in panel and "seSel('f-voice',a.voice||'')" in panel
    char = open(os.path.join(ROOT, "vera", "character", "character_element.js"), encoding="utf-8").read()
    assert "this._voice = '';" in char and "voice: this._voice, speed" not in char
