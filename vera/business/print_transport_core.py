"""print_transport_core.py -- pure, app-free transport helpers for the thermal
printer subsystem, so the raw-device detection + chunk pacing are unit-testable
without booting the capability app.

Consumers import uppercase (Vera.vera.business.print_transport_core); tests import
lowercase (vera.business.print_transport_core) so pytest binds to the worktree copy.
"""
from __future__ import annotations

import glob
import os
from typing import Iterator, List, Optional, Tuple


def is_raw_lp(port: str) -> bool:
    """A usblp character device (or its udev symlink). pyserial cannot open these;
    they take a plain chunked file write rather than a tty serial session."""
    p = port or ""
    return p == "/dev/vera-printer" or p.startswith("/dev/usb/lp") or "/vera-printer" in p


def paced_chunks(data: bytes, chunk: int) -> Iterator[Tuple[bytes, bool]]:
    """Yield (slice, more_follows) in ``chunk``-sized pieces so a large raster does
    not overrun a cheap printer's bulk-write buffer (the -108 USB reset)."""
    chunk = max(1, int(chunk))
    n = len(data)
    for i in range(0, n, chunk):
        yield data[i:i + chunk], (i + chunk < n)


def find_server_device(candidates: Optional[List[str]] = None) -> str:
    """Best-effort auto-detect of a server-attached thermal printer device."""
    pats = candidates if candidates is not None else (
        ["/dev/vera-printer"]
        + sorted(glob.glob("/dev/usb/lp*"))
        + sorted(glob.glob("/dev/ttyUSB*"))
        + sorted(glob.glob("/dev/ttyACM*")))
    for d in pats:
        if os.path.exists(d):
            return d
    return ""
