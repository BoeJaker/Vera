"""
The Sandbox quick menu as the Canvas board draws it (sbx-tree · sbx-term · sbx-act): the container's status, its FILE
TREE as the files widget form read from fs.list inside the container, its TERMINAL as <vera-terminal> over the
descriptor sandbox.session.terminal returns (the same instance across renders), the actions. Text-level.
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_sandbox_menu_has_its_tree_and_terminal_widgets():
    assert "const r=await _capCall('sandbox.session.terminal',{session_id:sid, shell:'bash'});" in HTML
    assert "await _capCall('fs.list',{kind:'docker', docker_host_id:_sbxQ.term.host, container:_sbxQ.term.container, path:'/workspace'})" in HTML, "the tree is read inside the container"
    assert 'data-w="file tree · files" data-tpl="lhm:sbx-tree"' in HTML and "window.VeraWidget.draw('files', files, 'm', {bare:true})" in HTML, "the files widget form"
    assert 'data-w="terminal · terminal" data-tpl="lhm:sbx-term"' in HTML and "_sbxQ.el=document.createElement('vera-terminal'); _sbxQ.el.setAttribute('ws', term.ws);" in HTML, "the estate's terminal element"
    assert "if(slot&&_sbxQ.el.parentNode!==slot){ slot.innerHTML=''; slot.appendChild(_sbxQ.el); }" in HTML, "the same instance across renders"
    assert 'data-w="actions · controls" data-tpl="lhm:actions"' in HTML and 'data-act="vscode"' in HTML and 'data-act="code"' in HTML, "the actions stay"
