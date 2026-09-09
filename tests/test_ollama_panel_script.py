"""The Ollama panel's inline script must parse.

It is 300k+ characters of hand-edited JavaScript served as one blob, and a
syntax error anywhere in it takes out the WHOLE panel - Workers, Ollama nodes,
routing, and the background queue with them. Nothing was checking it: the
census panel has this guard (test_census_panel_modals) and this one did not,
which is a gap rather than a decision.

Skips when node is unavailable rather than passing quietly, so a green run in
an environment without node cannot be mistaken for a checked one.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

_PANEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "vera", "workers", "workers_ollama_panel.html")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


def _blocks():
    src = open(_PANEL, encoding="utf-8").read()
    return re.findall(r"<script[^>]*>(.*?)</script>", src, re.S | re.I)


def test_the_panel_has_inline_script_to_check():
    """Guards the extraction itself - if the markup changes shape this file
    would otherwise pass by finding nothing to parse."""
    blocks = _blocks()
    assert blocks, "no <script> blocks found - the extraction is broken"
    assert sum(len(b) for b in blocks) > 50_000


@pytest.mark.parametrize("idx", range(10))
def test_each_inline_script_block_parses(idx):
    blocks = _blocks()
    if idx >= len(blocks):
        pytest.skip("only %d block(s)" % len(blocks))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(blocks[idx])
        path = f.name
    try:
        r = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "block %d does not parse:\n%s" % (idx, r.stderr)
    finally:
        os.unlink(path)


def test_the_background_queue_renderer_is_still_wired_up():
    """The widget is only useful if the panel still calls it and exports it -
    a renderer nobody invokes is the same as no renderer."""
    src = open(_PANEL, encoding="utf-8").read()
    assert "async function renderBgQueue()" in src
    assert src.count("renderBgQueue") >= 3        # defined, called, exported
    assert 'data-wid="ol-bgqueue"' in src
