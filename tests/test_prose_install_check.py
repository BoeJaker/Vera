"""prose.author's install-command grounding is for documents about the run's OWN project
(1 Aug 2026: a README told the reader to `npm install` a backend that was never built) -
not for a report about third-party software (4 Oct 2026: a vector-database report failed
for saying `pip install`)."""

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vera.dag import prose_grounding_core as P  # noqa: E402

SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")

ONLY_HTML = ["index.html", "style.css"]


@pytest.mark.parametrize("path, task, want", [
    ("README.md", "", True),
    ("readme.txt", "", True),
    ("docs/INSTALL.md", "", True),
    ("getting-started.md", "", True),
    ("notes.md", "write a README for the timer app", True),
    ("guide.md", "write setup instructions for the app", True),
    ("guide.md", "explain how to run the server", True),
    ("report.md", "compare three open-source vector databases", False),
    ("redis_valkey_report.md", "report on the Redis licence change and the Valkey fork", False),
    ("why_tests.md", "why unit tests matter", False),
])
def test_is_project_doc(path, task, want):
    assert P.is_project_doc(path, task) is want


def test_the_original_readme_failure_is_still_caught():
    txt = "## Setup\n```\nnpm install\nnpm run dev\n```\nThen `pip install -r requirements.txt`."
    refs = P.ungrounded_install_refs(txt, ONLY_HTML)
    assert any(r.startswith("npm install") for r in refs)
    assert any("npm run dev" in r for r in refs)
    assert any("requirements.txt" in r for r in refs)


@pytest.mark.parametrize("cmd", ["pip install qdrant-client", "`pip install pymilvus>=2.4`",
                                 "pip install -U weaviate-client", "npm install express",
                                 "npm i -g", "yarn add react", "go get github.com/x/y",
                                 "cargo add serde", "composer require monolog/monolog",
                                 "pip install chromadb[server]"])
def test_named_packages_need_no_manifest(cmd):
    # (`npm i` is not one of the matched verbs at all)
    assert P.ungrounded_install_refs(cmd, ONLY_HTML) == []


@pytest.mark.parametrize("cmd", ["pip install .", "pip install -e .", "pip install",
                                 "poetry install", "cargo build --release", "go build ./...",
                                 "mvn package", "npm ci", "bundle install"])
def test_project_installs_and_builds_still_need_their_manifest(cmd):
    assert P.ungrounded_install_refs(cmd, ONLY_HTML) != [], cmd


def test_a_present_manifest_grounds_the_command():
    assert P.ungrounded_install_refs("npm install && npm run build", ["package.json"]) == []
    assert P.ungrounded_install_refs("pip install .", ["pyproject.toml"]) == []
    assert P.ungrounded_install_refs("pip install -r requirements.txt", ["requirements.txt"]) == []


def test_unknown_listing_flags_nothing():
    assert P.ungrounded_install_refs("npm install", None) == []


# ── the monolith uses it, and only for project documents ─────────────────────

def test_the_helper_skips_a_report_and_checks_a_readme():
    from vera.dag import dag_workshop_capabilities as W
    report = "Qdrant ships a Python client: `pip install` it, or `pip install qdrant-client`."
    assert W._v5_prose_ungrounded_refs(report, ONLY_HTML, path="report.md",
                                       task="compare vector databases") == []
    readme = "Run `npm install` then `npm start`."
    assert W._v5_prose_ungrounded_refs(readme, ONLY_HTML, path="README.md", task="") != []
    # a caller that passes neither keeps the old, strict behaviour
    assert W._v5_prose_ungrounded_refs("pip install qdrant-client", ONLY_HTML) != []


def test_prose_author_passes_path_and_task_at_both_checks():
    body = SRC[SRC.index("# ── One repair pass if it still names a manifest"):]
    body = body[:body.index("saved = await code_store_save(")]
    assert body.count('_v5_prose_ungrounded_refs(content, real_files') == 2
    assert body.count('path=path or ""') == 2 and body.count('task=task or ""') == 2
