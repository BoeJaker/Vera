"""edge/ollama-vera.service -- the unit the whole inference fleet runs on.

Marked critical because every value here was learned from a real outage this
repo caused by not owning the file:

  * the unit existed only on the containers, so CT131/CT132 had none and sat
    dark with no way to notice from source;
  * HOME=/root looks tidier and breaks unprivileged nodes outright (/root is
    0:100000 700, so container root cannot traverse it and ollama exits with
    "mkdir /root/.ollama: permission denied");
  * the store is shared read-only across five nodes, so a pruning instance can
    delete another node's models.

Pure file parsing -- no app import, no I/O beyond reading the unit.
"""
import configparser
from pathlib import Path

import pytest

pytestmark = pytest.mark.critical

UNIT = Path(__file__).resolve().parents[1] / "edge" / "ollama-vera.service"


def _env() -> dict:
    """Environment="K=V" lines -> {K: V}. configparser keeps only the last value
    for a repeated key, so parse the raw text instead."""
    out = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            val = line.split("=", 1)[1].strip().strip('"')
            if "=" in val:
                k, v = val.split("=", 1)
                out[k] = v
    return out


def _service() -> dict:
    cp = configparser.ConfigParser(strict=False)
    cp.optionxform = str
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    return dict(cp["Service"])


def test_unit_exists_and_is_a_systemd_unit():
    assert UNIT.is_file(), f"{UNIT} is missing -- the fleet's unit must live in the repo"
    text = UNIT.read_text(encoding="utf-8")
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text


def test_serves_vera_on_11435_not_stock_11434():
    # Vera routes to :11435. Stock ollama.service owns 127.0.0.1:11434 and is a
    # different, unrelated process.
    assert _env()["OLLAMA_HOST"] == "0.0.0.0:11435"


def test_home_is_root_of_filesystem_so_unprivileged_containers_work():
    # NOT /root -- see the module docstring. This is the regression that took
    # the D/E nodes down.
    assert _env()["HOME"] == "/"


def test_models_path_is_under_home_so_ollama_resolves_it():
    env = _env()
    assert env["OLLAMA_MODELS"] == "/.ollama/models"
    assert env["OLLAMA_MODELS"].startswith(env["HOME"])


def test_pruning_is_disabled_for_the_shared_store():
    # The mount is read-only, but a prune attempt still logs errors and would
    # destroy models the moment anyone mounts the store writable.
    assert _env()["OLLAMA_NOPRUNE"] == "1"


def test_absolute_execstart_and_restart_policy():
    svc = _service()
    assert svc["ExecStart"].startswith("/"), "PATH is not guaranteed under systemd"
    assert svc["Restart"] == "always"


def test_enabled_for_multi_user():
    cp = configparser.ConfigParser(strict=False)
    cp.optionxform = str
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    assert cp["Install"]["WantedBy"] == "multi-user.target"


def test_documents_the_two_traps_that_cost_real_time():
    text = UNIT.read_text(encoding="utf-8")
    assert "unprivileged" in text.lower(), "the HOME=/ reason must stay written down"
    assert "403" in text or "root@pam" in text, \
        "pxstore.store.attach cannot attach a bind mount with an API token"
