"""turn_core: what a turn names - chunking, merging the NLP tier's answers with the patterns, the canvas item
(canvas-enrichment plan, slice 1)."""
import importlib.util
import pathlib

_P = pathlib.Path(__file__).resolve().parent.parent / "vera" / "canvas" / "turn_core.py"
_spec = importlib.util.spec_from_file_location("turn_core", _P)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

TEXT = ("Apple hired Tim Cook in Cupertino in 1998. Microsoft opened an office in London. "
        "Tim Cook later ran Apple, and BTC/USD rose to $42,000 on 3 January 2024. See https://example.com/a.")


def _ner(off, ents):
    return off, {"entities": ents}


def test_chunks_are_sentence_aligned_and_cover_the_text():
    long = " ".join("Sentence number %d is here." % i for i in range(200))
    parts = tc.chunks(long, size=300)
    assert len(parts) > 5 and all(len(c) <= 300 for _, c in parts)
    assert all(c.endswith(".") for _, c in parts[:-1])
    off, c = parts[3]
    assert long[off:off + 10] == c[:10]


def test_code_fences_are_not_prose():
    assert "def area" not in tc.prose("Here:\n```python\ndef area(w): pass\n```\nDone.")


def test_merge_counts_names_keeps_sentences_and_drops_weak_and_unsalient():
    tagged = [_ner(0, [{"entity": "ORG", "word": " Apple", "score": 0.99, "start": 0, "end": 5},
                       {"entity": "PERSON", "word": " Tim Cook", "score": 0.99, "start": 12, "end": 20},
                       {"entity": "GPE", "word": " Cupertino", "score": 0.99, "start": 24, "end": 33},
                       {"entity": "DATE", "word": " 1998", "score": 0.99, "start": 37, "end": 41},
                       {"entity": "CARDINAL", "word": " one", "score": 0.99, "start": 0, "end": 3},
                       {"entity": "ORG", "word": " Weakco", "score": 0.3, "start": 0, "end": 6}]),
              _ner(84, [{"entity": "PERSON", "word": "Tim Cook", "score": 0.97, "start": 0, "end": 8},
                        {"entity": "ORG", "word": "Apple", "score": 0.98, "start": 20, "end": 25}])]
    ents = tc.merge(TEXT, tagged)
    by = {(e["type"], e["text"]): e for e in ents}
    assert by[("PERSON", "Tim Cook")]["count"] == 2 and by[("ORG", "Apple")]["count"] == 2
    assert ("CARDINAL", "one") not in by and ("ORG", "Weakco") not in by
    assert any("Cupertino" in s for s in by[("GPE", "Cupertino")]["sentences"])
    assert ents[0]["count"] == 2                                   # most named first


def test_patterns_find_tickers_and_links_but_not_any_acronym():
    ents = tc.merge(TEXT + " The API and the CEO met.", [])
    names = {(e["type"], e["text"]) for e in ents}
    assert ("TICKER", "BTC/USD") in names and ("URL", "https://example.com/a") in names
    assert not any(e["text"] in ("API", "CEO") for e in ents)


def test_a_surname_after_the_full_name_is_the_same_person_and_the_is_dropped():
    text = "Tim Cook runs Apple. Later Cook spoke. The European Union replied to Apple's filing."
    ents = tc.merge(text, [_ner(0, [{"entity": "PERSON", "word": "Tim Cook", "score": 0.9, "start": 0, "end": 8},
                                    {"entity": "PERSON", "word": "Cook", "score": 0.9, "start": 26, "end": 30},
                                    {"entity": "ORG", "word": "The European Union", "score": 0.9, "start": 37, "end": 55},
                                    {"entity": "ORG", "word": "Apple's", "score": 0.9, "start": 67, "end": 74},
                                    {"entity": "ORG", "word": "Apple", "score": 0.9, "start": 14, "end": 19}])])
    by = {(e["type"], e["text"]): e for e in ents}
    assert by[("PERSON", "Tim Cook")]["count"] == 2 and ("PERSON", "Cook") not in by
    assert ("ORG", "European Union") in by and by[("ORG", "Apple")]["count"] == 2


def test_a_ticker_typed_in_lower_case_is_a_ticker_but_a_word_stays_a_word():
    names = {(e["type"], e["text"]) for e in tc.merge("how is btc/usd doing, and eth?", [])}
    assert ("TICKER", "BTC/USD") in names and ("TICKER", "ETH") in names
    names = {(e["type"], e["text"]) for e in tc.merge("connect the dot to the link near the meta etc", [])}
    assert not any(t == "TICKER" for t, _ in names)
    assert ("TICKER", "DOT/USD") in {(e["type"], e["text"]) for e in tc.merge("what about dot/usd", [])}


def test_a_name_cut_inside_a_word_is_widened_to_the_word_but_only_where_the_offset_is_true():
    text = "Ethereum was proposed by Vitalik Buterin in 2013."
    ents = tc.merge(text, [_ner(0, [{"entity": "PERSON", "word": "italik Buterin", "score": 0.9, "start": 26, "end": 40}])])
    assert [e["text"] for e in ents if e["type"] == "PERSON"] == ["Vitalik Buterin"]
    # an offset that does not point at the word leaves the tagger's word as it came
    ents = tc.merge(text, [_ner(0, [{"entity": "PERSON", "word": "Buterin", "score": 0.9, "start": 3, "end": 10}])])
    assert [e["text"] for e in ents if e["type"] == "PERSON"] == ["Buterin"]


def test_salient_and_the_canvas_item():
    ents = tc.merge(TEXT, [_ner(0, [{"entity": "ORG", "word": "Apple", "score": 0.9, "start": 0, "end": 5},
                                    {"entity": "PERSON", "word": "Tim Cook", "score": 0.9, "start": 12, "end": 20},
                                    {"entity": "DATE", "word": "1998", "score": 0.9, "start": 37, "end": 41}])])
    assert {e["type"] for e in tc.salient(ents)} >= {"ORG", "PERSON"} and all(e["type"] != "DATE" for e in tc.salient(ents))
    it = tc.item(ents, mid="m4", subject="Apple")
    assert it["title"] == "Mentioned · Apple" and it["mid"] == "m4"
    groups = [g["group"] for g in it["groups"]]
    assert groups.index("people") < groups.index("organisations") < groups.index("dates")
    assert it["count"] == sum(len(g["entities"]) for g in it["groups"])


def test_a_coin_said_by_name_is_its_ticker():
    names = {(e["type"], e["text"]) for e in tc.merge("How is bitcoin doing, and Ethereum?", [])}
    assert ("TICKER", "BTC") in names and ("TICKER", "ETH") in names


def test_to_look_is_deterministic_most_named_first_and_skips_dates_money_and_links():
    ents = [
        {"text": "Microsoft", "type": "ORG", "count": 3, "score": 0.9},
        {"text": "3 January 2024", "type": "DATE", "count": 5, "score": 0.99},
        {"text": "$42,000", "type": "MONEY", "count": 4, "score": 0.99},
        {"text": "Apple", "type": "ORG", "count": 3, "score": 0.95},
        {"text": "London", "type": "GPE", "count": 1, "score": 0.99},
        {"text": "apple", "type": "PRODUCT", "count": 1, "score": 0.5},
        {"text": "AI", "type": "ORG", "count": 9, "score": 0.9},
    ]
    picks = [e["text"] for e in tc.to_look(ents, 3)]
    # most named, then confidence, then the name; a two-letter name and the "when / how much" kinds are not fetched
    assert picks == ["Apple", "Microsoft", "London"]
    # the same entities in any order give the same names
    assert [e["text"] for e in tc.to_look(list(reversed(ents)), 3)] == picks
    # each name once, whatever type it was also tagged as
    assert [e["text"].lower() for e in tc.to_look(ents, 10)].count("apple") == 1
    assert tc.to_look(ents, 0) == []
