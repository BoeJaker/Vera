"""A14/A15 re-verified on the mirror: the modes' strips, the attachment chips and the attachment editor sit on the composer's measure."""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "chat", "chat_panel.html")


def test_the_strips_chips_and_editor_share_the_composers_measure():
    assert 'body[data-view="minimal"].has-msgs:not(.has-panel) #cmpStrips,body[data-view="minimal"].has-msgs:not(.has-panel) #docChips,body[data-view="minimal"].has-msgs:not(.has-panel) #docEditor{width:calc(var(--measure) - 58px)' in HTML
    assert 'body[data-view="minimal"].has-msgs.has-cols #docEditor{width:calc(var(--measure) - 58px);max-width:calc(100% - 32px);margin:0 auto 6px}' in HTML
