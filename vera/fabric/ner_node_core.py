"""Entity NER for the fabric, served by the nodes.

The host's entity graph (fabric_web_acquisition) runs GLiNER or spaCy
IN-PROCESS and fell back to capitalisation heuristics when neither loads. Prod
has both installed (GLiNER is what it runs); a Vera without them - a sandbox,
a fresh host - used the heuristic, while every node serves OntoNotes-v5 NER
(nlp.ner) from the shared model store. So the nodes are the fallback BEFORE
the heuristic. OntoNotes labels ARE spaCy's labels (PERSON, ORG, GPE, FAC, DATE ...),
so the node's output maps onto the fabric's types with the same table spaCy
used.

Pure: label table and result mapping. Tested in tests/test_fabric_ner_on_nodes.py.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

#: OntoNotes-v5 / spaCy label -> fabric entity type
ONTONOTES_TYPE = {
    "PERSON": "person", "ORG": "organisation", "GPE": "location",
    "LOC": "location", "FAC": "location", "PRODUCT": "product",
    "EVENT": "event", "WORK_OF_ART": "work", "LAW": "concept",
    "LANGUAGE": "concept", "NORP": "concept", "DATE": "date",
    "TIME": "date", "MONEY": "money",
}

#: numeric labels that are not entities worth a graph node
DROP_LABELS = frozenset({"CARDINAL", "ORDINAL", "PERCENT", "QUANTITY"})

#: how much text one node call takes (the node chunks it; this bounds the wait)
MAX_CHARS = 100000


def node_entities(text: str, entities: Iterable[Dict[str, Any]],
                  slug=lambda s: s.lower(), type_of=None) -> List[Tuple[str, str, int, float]]:
    """(name, type, position, confidence) per usable node entity.

    The name is cut from the ORIGINAL text by the entity's offsets when it has
    them: the model's `word` is the tokenizer's rendering (a RoBERTa word
    carries a leading space, sub-words are glued), not what the page said.

    `type_of` maps a label as the model gave it (GLiNER's open labels -
    "organization", "job title") to the fabric's type; without it the label is
    read as OntoNotes."""
    out = []
    for e in entities or []:
        raw = str(e.get("entity") or e.get("entity_group") or "")
        label = raw.upper()
        label = label[2:] if label[:2] in ("B-", "I-") else label
        if not label or (type_of is None and label in DROP_LABELS):
            continue
        start, end = e.get("start"), e.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
            name, pos = text[start:end], start
        else:
            name = str(e.get("word") or "").strip()
            pos = text.find(name) if name else -1
            if pos < 0:
                continue
        name = name.strip()
        if not name:
            continue
        ety = type_of(raw) if type_of is not None else (ONTONOTES_TYPE.get(label) or slug(label))
        out.append((name, ety, pos, float(e.get("score", 0.8))))
    return out
