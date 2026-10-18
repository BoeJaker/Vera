"""S11 / D13-4: the OWL capabilities reach the ontologies.

skills_owl did `from Vera.vera.skills import ONTOLOGIES` - the PACKAGE, which has no ONTOLOGIES - so all seven OWL
capabilities failed at call time. And importing the skills module by its full name ran a second copy with an empty
ONTOLOGIES. Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import importlib
import os
import re
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.skills.skills as sk  # noqa: E402  (fails loudly, never skips)
import Vera.vera.skills.skills_owl as owl  # noqa: E402
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY  # noqa: E402


def test_one_skills_module_under_every_name():
    assert sys.modules["skills"] is sk
    assert importlib.import_module("Vera.vera.skills.skills") is sk
    assert importlib.import_module("vera.skills.skills") is sk


def test_the_import_the_owl_caps_use_reaches_the_live_ontologies():
    from Vera.vera.skills.skills import ONTOLOGIES, ontologies_create, ontologies_update  # what the caps import
    assert ONTOLOGIES is sk.ONTOLOGIES and callable(ontologies_create) and callable(ontologies_update)


def test_owl_context_runs_through_the_import():
    fn = CAPABILITY_REGISTRY["ontologies.owl_context"]["raw"]
    out = asyncio.run(fn(ontology_ids="s11-no-such-ontology"))
    if not owl._HAS_RDFLIB:                                   # the cap stops before the import without rdflib
        assert "rdflib not installed" in out["error"]
        return
    assert out == {"turtle": "", "ontology_count": 0, "length": 0, "triples": 0}


def test_nothing_imports_skills_names_from_the_package():
    bad = re.compile(r"from\s+Vera\.vera\.skills\s+import\s+\(?\s*(ONTOLOGIES|SKILLS|ontologies_|skills_)")
    hits = []
    for base, _d, files in os.walk(os.path.join(ROOT, "vera")):
        for n in files:
            if n.endswith(".py"):
                p = os.path.join(base, n)
                src = open(p, encoding="utf-8", errors="ignore").read()
                hits += [os.path.relpath(p, ROOT) for m in bad.finditer(src)]
    assert not hits, f"the package exports nothing; import from Vera.vera.skills.skills: {hits}"
