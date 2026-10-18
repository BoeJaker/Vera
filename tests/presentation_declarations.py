"""The capabilities' declared presentations, read out of their own decorators (no import, no registry): what /mcp/tools
carries for them. Used by the widget fixture tests (the page gets these from /mcp/tools) and by the check that
tests/fixtures/presentation_declared.json is what the code declares.

    python tests/presentation_declarations.py --write    regenerates the fixture
"""
import ast
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "presentation_declared.json")


def declared_in_source(root: str = ROOT) -> dict:
    out = {}
    for base, _, files in os.walk(os.path.join(root, "vera")):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(base, f)
            try:
                src = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if '"presentation"' not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "capability":
                    continue
                # the name is the first argument, or `name=` (vllm, openclaw)
                if node.args:
                    first = node.args[0]
                else:
                    first = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                for kw in node.keywords:
                    if kw.arg == "contract" and isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and k.value == "presentation":
                                out[first.value] = ast.literal_eval(v)
    return dict(sorted(out.items()))


def load_fixture() -> dict:
    return json.load(open(FIXTURE, encoding="utf-8"))


if __name__ == "__main__" and "--write" in sys.argv:
    d = declared_in_source()
    with open(FIXTURE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(d, indent=1, sort_keys=True) + "\n")
    print(len(d), "declarations ->", FIXTURE)
