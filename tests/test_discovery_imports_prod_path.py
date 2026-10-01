"""The discovery read model imports the way the running app imports it.

2026-09-30: a release took 17 operator.* capabilities off prod. operator_web_capabilities imports the discovery read
model, whose chain (discovery_benchmark -> discovery_contract, context_provider, fabric.dataset_provider) was written
with plain `from vera.X import` - the spelling the TESTS have, because they put the repository root on sys.path. The
running app has only `Vera.vera` (the checkout's parent on the path), so the chain raised ModuleNotFoundError: 'vera'
and the whole operator module failed to load. Every test passed, because every test had the other spelling.

So this runs the import in a fresh interpreter laid out like the app: a directory holding only `Vera` (a link to this
checkout) on the path, and no plain `vera` anywhere.
"""
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHAIN = ["discovery_operator_readmodel", "discovery_benchmark", "discovery_contract", "context_provider",
         "fabric.dataset_provider"]


def _app_layout_import(modules):
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(ROOT, os.path.join(d, "Vera"), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot make a directory link here")
        code = (
            "import importlib, sys\n"
            "try:\n    import vera\n    print('PLAIN-VERA-IMPORTABLE')\nexcept ImportError:\n    pass\n"
            "for m in %r:\n    importlib.import_module('Vera.vera.' + m)\n"
            "print('OK')\n" % (modules,)
        )
        env = dict(os.environ, PYTHONPATH=d)
        return subprocess.run([sys.executable, "-c", code], cwd=d, env=env, capture_output=True, text=True, timeout=120)


def test_the_discovery_chain_imports_as_the_app_imports_it():
    r = _app_layout_import(CHAIN)
    assert "PLAIN-VERA-IMPORTABLE" not in r.stdout, "the layout must not have a plain 'vera' - it would hide the bug"
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-1500:]


def test_every_import_in_the_chain_is_dual_spelled():
    for m in CHAIN[:3]:
        src = open(os.path.join(ROOT, "vera", *m.split(".")) + ".py", encoding="utf-8").read()
        plain = [l for l in src.splitlines() if l.startswith("from vera.") or l.startswith("import vera.")]
        assert not plain, "%s imports plain vera at module level: %s" % (m, plain)


def test_the_ontology_evaluation_imports_as_the_app_imports_it():
    # eval.ontology.decision answered "No module named 'vera'" on prod: the same plain-only spelling, a lazy import
    r = _app_layout_import(["ontologies.capability_ontology_evaluation"])
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-1500:]


def test_no_module_the_app_loads_has_a_plain_only_import_in_its_chain():
    """The standing guard: every module the app loads at startup, and every module those import at module level, may
    import a plain `vera.X` only where the same file also spells it `Vera.vera.X` (the fallback the app takes)."""
    import re
    imp = re.compile(r"^(?:    )?from ((?:Vera\.)?vera(?:\.\w+)+) import|^(?:    )?import ((?:Vera\.)?vera(?:\.\w+)+)", re.M)
    rel = re.compile(r"^(?:    )?from (\.+)([\w.]*) import ([^\n]+)", re.M)

    def path_of(m):
        for p in (os.path.join(ROOT, *m.split(".")) + ".py", os.path.join(ROOT, *m.split("."), "__init__.py")):
            if os.path.exists(p):
                return p
        return None
    co = open(os.path.join(ROOT, "vera", "capability_orchestration.py"), encoding="utf-8").read()
    rels = sorted(set(re.findall(r"[\"']((?:[\w ]+/)*\w+\.py)[\"']", co)))
    todo = ["vera." + r[:-3].replace("/", ".") for r in rels if os.path.exists(os.path.join(ROOT, "vera", r))]
    assert len(todo) > 100, "the app's module list was not found"
    seen, bad = set(), {}
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        # importing a.b.c RUNS a/__init__ and a/b/__init__ first: every parent package is walked too (the model
        # package's __init__ is what carried the plain import into worldview_jepa - 2026-10-01)
        parts = m.split(".")
        todo.extend(".".join(parts[:i]) for i in range(2, len(parts)))
        seen.add(m)
        p = path_of(m)
        if not p:
            continue
        src = open(p, encoding="utf-8", errors="replace").read()
        for a, b in imp.findall(src):
            name = a or b
            plain = name.replace("Vera.vera", "vera") if name.startswith("Vera.") else name
            if name.startswith("vera.") and ("Vera." + name) not in src:
                bad.setdefault(m, []).append(name)
            todo.append(plain)
        # RELATIVE imports are followed too: a package's __init__ pulling in '.ml_workshop_training_adapter' carried a
        # plain import into worldview_jepa and the model inventory, and the walk never went there (2026-10-01)
        pkg = m if p.endswith("__init__.py") else m.rsplit(".", 1)[0]
        for dots, rest, names in rel.findall(src):
            base = pkg.split(".")
            if len(dots) > 1:
                base = base[:len(base) - (len(dots) - 1)]
            target = ".".join(base + ([rest] if rest else []))
            todo.append(target)
            if not rest:
                for nm in re.split(r"[,\s()]+", names):
                    if nm and nm != "as" and re.match(r"^\w+$", nm):
                        todo.append(target + "." + nm)
    assert not bad, "plain-only 'vera.' imports the app cannot resolve: %s" % bad


def test_the_operator_survives_a_read_model_that_cannot_load():
    src = open(os.path.join(ROOT, "vera", "operator", "operator_web_capabilities.py"), encoding="utf-8").read()
    assert "DISCOVERY_OPERATOR_LEDGER = None" in src
    assert src.count('if DISCOVERY_OPERATOR_LEDGER is None:') == 2
