"""capture.py — frame capture + GIF / time-lapse assembly for the operator.

Two capabilities live on top of this:

  • **run → GIF**   — ``operator.run`` already saves one PNG per step; those
    frames assemble straight into an animated GIF of the whole observe→act loop.
  • **time-lapse**  — a background sampler screenshots a session's page every N
    ms while some long task runs (a dream cycle, a backtest, a loop), then
    assembles the frames into a GIF for the docs.

GIF assembly uses Pillow (a ``requirements-operator.txt`` extra). Everything
degrades gracefully: the module imports without Pillow, and assembly returns a
clear ``{"error": …, "hint": …}`` instead of raising. ``assemble_gif`` is pure
(unit-testable); the sampler needs a live page but is injected for testability.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("vera.operator.capture")

_PIL_OK = False
_PIL_ERR = ""
try:
    from PIL import Image  # type: ignore
    _PIL_OK = True
except Exception as e:  # pragma: no cover - depends on host env
    Image = None  # type: ignore
    _PIL_ERR = str(e)

PIL_HINT = ("Pillow is required to assemble GIFs. Install the operator extra: "
            "pip install -r requirements-operator.txt")


def pil_available() -> bool:
    return _PIL_OK


def assemble_gif(frames: List[str], out_path: str, *, duration_ms: int = 800,
                 loop: int = 0, max_width: int = 900,
                 max_frames: int = 200) -> Dict[str, Any]:
    """Assemble PNG frame paths into an animated GIF at ``out_path``.

    Downsizes to ``max_width`` and caps frame count (evenly sampled) to keep the
    file GitHub-friendly. Returns {ok, path, frames, bytes, width} or {error}.
    Never raises.
    """
    if not _PIL_OK:
        return {"error": PIL_HINT, "hint": PIL_HINT}
    paths = [f for f in (frames or []) if f and os.path.exists(f)]
    if not paths:
        return {"error": "no frames to assemble"}
    # Evenly subsample if there are more frames than the cap.
    if len(paths) > max_frames > 0:
        step = len(paths) / float(max_frames)
        paths = [paths[int(i * step)] for i in range(max_frames)]
    imgs = []
    width = 0
    for f in paths:
        try:
            im = Image.open(f).convert("RGB")
            if max_width and im.width > max_width:
                h = max(1, int(im.height * max_width / im.width))
                im = im.resize((max_width, h))
            width = im.width
            imgs.append(im)
        except Exception as e:
            log.debug("capture: skipping unreadable frame %s: %s", f, e)
    if not imgs:
        return {"error": "no readable frames"}
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        # Palette conversion keeps the GIF small; disposal=2 clears each frame.
        pal = [im.convert("P", palette=Image.ADAPTIVE, colors=256) for im in imgs]
        pal[0].save(out_path, save_all=True, append_images=pal[1:],
                    duration=max(20, int(duration_ms)), loop=int(loop),
                    optimize=True, disposal=2)
    except Exception as e:
        return {"error": f"gif assembly failed: {e}"}
    return {"ok": True, "path": out_path, "frames": len(imgs),
            "bytes": os.path.getsize(out_path), "width": width}


# ─────────────────────────────────────────────────────────────────────────────
#  Background time-lapse sampler
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Capture:
    capture_id: str
    session_id: str
    frames_dir: str
    interval_ms: int = 1000
    max_frames: int = 180
    frames: List[str] = field(default_factory=list)
    running: bool = True
    started: float = field(default_factory=time.time)
    task: Any = None

    def summary(self) -> Dict[str, Any]:
        return {"capture_id": self.capture_id, "session_id": self.session_id,
                "frames": len(self.frames), "running": self.running,
                "interval_ms": self.interval_ms, "max_frames": self.max_frames,
                "elapsed_s": round(time.time() - self.started, 1)}


_CAPTURES: Dict[str, Capture] = {}


async def _sampler(cap: Capture, get_page: Callable[[], Any]) -> None:
    """Screenshot the current page every interval until stopped / capped."""
    n = 0
    while cap.running and n < cap.max_frames:
        page = None
        try:
            page = get_page()
        except Exception:
            page = None
        if page is None:
            break
        fp = os.path.join(cap.frames_dir, f"f-{n:04d}.png")
        try:
            await page.screenshot(path=fp, type="png")
            cap.frames.append(fp)
            n += 1
        except Exception as e:
            log.debug("capture sampler frame failed: %s", e)
        try:
            await asyncio.sleep(max(0.05, cap.interval_ms / 1000.0))
        except asyncio.CancelledError:
            break
    cap.running = False


def start_capture(session_id: str, frames_dir: str, get_page: Callable[[], Any],
                  *, interval_ms: int = 1000, max_frames: int = 180,
                  capture_id: str = "") -> Capture:
    cid = capture_id or f"cap-{uuid.uuid4().hex[:8]}"
    os.makedirs(frames_dir, exist_ok=True)
    cap = Capture(cid, session_id, frames_dir, int(interval_ms), int(max_frames))
    cap.task = asyncio.get_event_loop().create_task(_sampler(cap, get_page))
    _CAPTURES[cid] = cap
    return cap


def get_capture(capture_id: str) -> Optional[Capture]:
    return _CAPTURES.get(capture_id)


def list_captures() -> List[Dict[str, Any]]:
    return [c.summary() for c in _CAPTURES.values()]


async def stop_capture(capture_id: str) -> Optional[Capture]:
    cap = _CAPTURES.get(capture_id)
    if not cap:
        return None
    cap.running = False
    if cap.task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(cap.task), timeout=3)
        except Exception:
            try:
                cap.task.cancel()
            except Exception:
                pass
    return cap
