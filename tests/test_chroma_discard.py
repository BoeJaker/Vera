"""A failed Chroma connect closes what it opened (RF-82).

A mirror whose chromadb client could not talk to the server retried every 5 minutes and
each attempt left a System in chromadb's registry with an open connection: 518 connections
in a day, which starved prod's memory search.
"""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.fabric import chroma_bound_core as cb  # noqa: E402

pytestmark = pytest.mark.critical


class _Sess:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _System:
    def __init__(self):
        self.sess = _Sess()
        self._instances = {"server": types.SimpleNamespace(_session=self.sess), "other": object()}
        self.stopped = False

    def stop(self):
        self.stopped = True


@pytest.fixture
def registry(monkeypatch):
    reg = {}
    monkeypatch.setattr(cb, "_registry", lambda: reg)
    return reg


def test_a_failed_connect_closes_and_unregisters_what_it_created(registry):
    kept = _System()
    registry["prod:8008"] = kept
    made = _System()
    with pytest.raises(KeyError):
        with cb.discard_on_failure():
            registry["mirror:8008"] = made
            raise KeyError("_type")
    assert made.sess.closed and made.stopped and "mirror:8008" not in registry
    assert registry == {"prod:8008": kept} and not kept.sess.closed


def test_a_replaced_system_under_the_same_key_is_closed_too(registry):
    registry["chroma:8008"] = old = _System()
    new = _System()
    with pytest.raises(RuntimeError):
        with cb.discard_on_failure():
            registry["chroma:8008"] = new
            raise RuntimeError("could not connect")
    assert new.sess.closed and "chroma:8008" not in registry and not old.sess.closed


def test_success_touches_nothing(registry):
    made = _System()
    with cb.discard_on_failure():
        registry["chroma:8008"] = made
    assert registry["chroma:8008"] is made and not made.sess.closed


def test_without_a_registry_it_just_reraises(monkeypatch):
    monkeypatch.setattr(cb, "_registry", lambda: None)
    with pytest.raises(ValueError):
        with cb.discard_on_failure():
            raise ValueError("x")


def test_close_system_never_raises():
    bad = types.SimpleNamespace(_instances={"s": types.SimpleNamespace(_session=None)})
    assert cb.close_system(bad) == 0
    assert cb.close_system(object()) == 0
