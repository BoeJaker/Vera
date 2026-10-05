"""Double-escaped newlines in model-written Python - the pure fix.

The executor writes Python inside a JSON tool argument. To get the source text
`f.write(str(n) + '\\n')` into JSON it must escape the backslash once; models
regularly escape it TWICE, so the code that arrives reads `'\\\\n'` - a literal
backslash followed by n - and the file it writes is one long line of "\\n" text.

Measured 2026-10-01 over every dumped census + probe session: 30 of 126
exec.python.run calls (24%) wrote or printed such a literal, in 24 sessions. It is
the root cause of census analyse-data (200 numbers written as one line -> "mean 134,
std 0.00") and of the b-data-count probe (n.csv one line of "5\\n10\\n...", "0
above 50" when 23 were).

The fix runs on the code before it is executed: inside ordinary (non-raw) string
literals, an escaped `\\n` / `\\t` that is not itself part of a longer backslash run
becomes the newline / tab escape it was meant to be. Raw strings, comments and
code outside literals are never touched; code that does not tokenize is returned
unchanged. Pure.
"""

from __future__ import annotations

import io
import re
import tokenize
from typing import List, Tuple

#: Two backslashes then n/t, not preceded by a third backslash (`\\\\\\n` is a real
#: escaped backslash followed by a newline escape - leave it alone).
_DOUBLE = re.compile(r"(?<!\\)\\\\([nt])")
_PREFIX = re.compile(r"^([A-Za-z]*)")
_STRING_TYPES = {tokenize.STRING}
if hasattr(tokenize, "FSTRING_MIDDLE"):                  # Python 3.12+
    _STRING_TYPES.add(tokenize.FSTRING_MIDDLE)


def _is_raw(tok: tokenize.TokenInfo, raw_fstring: bool) -> bool:
    if tok.type == tokenize.STRING:
        return "r" in _PREFIX.match(tok.string).group(1).lower()
    return raw_fstring


def fix_double_escaped_newlines(code: str) -> Tuple[str, int]:
    """(code, number of literals changed)."""
    if not isinstance(code, str) or "\\\\" not in code:
        return code, 0
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return code, 0
    lines = code.splitlines(keepends=True)
    edits: List[Tuple[int, int, int, int, str]] = []
    raw_fstring = False
    for tok in toks:
        if hasattr(tokenize, "FSTRING_START") and tok.type == tokenize.FSTRING_START:
            raw_fstring = "r" in _PREFIX.match(tok.string).group(1).lower()
            continue
        if tok.type not in _STRING_TYPES or _is_raw(tok, raw_fstring):
            continue
        new = _DOUBLE.sub(lambda m: "\\" + m.group(1), tok.string)
        if new != tok.string:
            (sr, sc), (er, ec) = tok.start, tok.end
            edits.append((sr, sc, er, ec, new))
    if not edits:
        return code, 0
    # Apply from the end so earlier offsets stay valid.
    for sr, sc, er, ec, new in sorted(edits, reverse=True):
        head = lines[sr - 1][:sc]
        tail = lines[er - 1][ec:]
        lines[sr - 1:er] = [head + new + tail]
    return "".join(lines), len(edits)
