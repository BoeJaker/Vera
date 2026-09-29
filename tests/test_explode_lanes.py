# -*- coding: utf-8 -*-
"""Multi-record lanes (EXPLODE.md §9, S7): several records drawn side by side are several diagrams until
something joins them. The same entity in two records is the REASON they were put side by side -- an entity's
evidence is exactly "the records that mention it" (explode.target) -- so it is drawn.
"""
import asyncio

import pytest

from vera.research import explode_capabilities as X


def card(cid, title, kind, path, start=0, layer="ner"):
    return {"id": cid, "layer": layer, "kind": kind, "title": title,
            "span": {"path": path, "start": start, "end": start + len(title)}, "fields": [], "badges": []}


def test_the_same_name_in_two_records_is_one_run_between_them():
    joins = X.link_lanes([card("a", "Contoso", "org", "r1"), card("b", "Contoso", "org", "r2")])
    assert len(joins) == 1
    e = joins[0]
    assert e["kind"] == "COREF" and e["resolution"] == "exact" and e["layer"] == "link.records"
    assert {e["from"], e["to"]} == {"a", "b"} and e["by"] == "across records"


def test_three_records_join_through_one_card_rather_than_every_pair():
    """Six runs between four records says nothing a reader cannot already see; one lane stands for the entity."""
    joins = X.link_lanes([card(x, "Contoso", "org", x) for x in ("r1", "r2", "r3", "r4")])
    assert len(joins) == 3 and {e["from"] for e in joins} == {"r1"}


def test_a_surname_or_an_acronym_joins_across_records_but_says_it_is_heuristic():
    joins = X.link_lanes([card("a", "Alice Carter", "person", "r1"), card("b", "Carter", "person", "r2"),
                          card("c", "Competition and Markets Authority", "org", "r1"),
                          card("d", "CMA", "org", "r2")])
    kinds = {(e["resolution"], e["label"]) for e in joins}
    assert ("heuristic", "shortened") in kinds and ("heuristic", "acronym") in kinds
    assert all(e["kind"] == "COREF" for e in joins)


def test_two_mentions_in_the_SAME_record_are_not_a_join():
    """Within one record that is the coref fold's business, and it merges them; a lane join is between lanes."""
    assert X.link_lanes([card("a", "Contoso", "org", "r1", 0), card("b", "Contoso", "org", "r1", 90)]) == []


def test_an_entity_only_one_record_mentions_joins_nothing():
    assert X.link_lanes([card("a", "Bristol", "place", "r1"), card("b", "Contoso", "org", "r2")]) == []


def test_a_name_that_could_be_two_people_joins_nothing_across_records_either():
    joins = X.link_lanes([card("a", "Alice Carter", "person", "r1"), card("c", "Bob Carter", "person", "r1"),
                          card("b", "Carter", "person", "r2")])
    assert not [e for e in joins if e["resolution"] == "heuristic"]


def test_only_entity_cards_join_and_only_ones_that_know_their_record():
    assert X.link_lanes([card("a", "Contoso", "org", "r1", layer="paragraphs"),
                         card("b", "Contoso", "org", "r2", layer="paragraphs")]) == []
    assert X.link_lanes([card("a", "Contoso", "org", ""), card("b", "Contoso", "org", "")]) == []


def test_the_layer_is_listed_off_for_one_record_and_says_when_it_runs():
    row = next(l for l in X.layer_list() if l["id"] == "link.records")
    assert row["default_on"] is False and row["kind"] == "relation"
    assert "lanes" in row["note"] and "exact" in row["note"]


def test_several_records_carry_the_join_and_its_receipt():
    recs = {"r1": "Contoso was acquired by Northwind Ltd. Alice Carter signed it in Bristol.",
            "r2": "Contoso, the smaller firm, said Carter had pushed for the deal."}
    orig = X._read_record
    X._read_record = lambda rid: ({"id": rid, "dataset_id": "d", "text": recs[rid], "source_id": ""} if rid in recs else None)
    try:
        doc = asyncio.get_event_loop().run_until_complete(
            X.explode_prose(record_ids=["r1", "r2"], layers=["ner"]))
    finally:
        X._read_record = orig
    row = next((l for l in doc["layers"] if l["id"] == "link.records"), None)
    assert row is not None and row["on"] is True
    assert row["count"] == len([e for e in doc["edges"] if e.get("layer") == "link.records"])
    assert "records" in row["note"]
    # a single record does not pretend to have lanes
    X._read_record = lambda rid: {"id": rid, "dataset_id": "d", "text": recs["r1"], "source_id": ""}
    try:
        one = asyncio.get_event_loop().run_until_complete(X.explode_prose(record_id="r1", layers=["ner"]))
    finally:
        X._read_record = orig
    assert not [e for e in one["edges"] if e.get("layer") == "link.records"]
