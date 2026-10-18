"""What each node NLP model is FOR - the `serves=` bindings of the nlp.* caps.

Pure (no Vera runtime imports), so tests and the model.callable view can read it
without loading the capability module. The model ids are NOT repeated here: they
stay in nlp_dispatch_core.DEFAULT_MODELS (which the node may override per task);
the view resolves them. nlp_dispatch_core itself is copied onto the nodes and
imported there without the vera package, so it must not import this.

See vera/model_binding_core.py and
shared-planning/four-minds/PROPOSAL-model-bindings-2026-10-10.md.
"""
from __future__ import annotations

try:
    from Vera.vera.model_binding_core import binding as _bind
except ImportError:  # standalone/test import path
    from vera.model_binding_core import binding as _bind


# What each node model is FOR (model.callable; appended to the caps'
# descriptions so an LLM picking a tool sees it). The model ids stay in
# nlp_dispatch_core.DEFAULT_MODELS - the view reads them there.
_NLP = dict(family="nlp", placement="nlp")
_SERVES_RERANK = _bind(
    "rerank", "fastembed", placement="nlp", io_in="query+texts", io_out="scores",
    uses="Score how well each passage answers a query (cross-encoder) to reorder search results, "
         "memory hits or candidate tools before the top few are used.",
    limits="generating text; long passages beyond ~512 tokens are cut.")
_SERVES_CLASSIFY = [
    _bind("classify", selector={"task": "classify"}, io_out="label", labels=["POSITIVE", "NEGATIVE"],
          uses="Binary sentiment of English text (SST-2).",
          limits="neutral or mixed text, other languages, informal chat (use task='sentiment3').", **_NLP),
    _bind("sentiment3", selector={"task": "sentiment3"}, io_out="label",
          labels=["negative", "neutral", "positive"],
          uses="Three-way sentiment (negative/neutral/positive) of short informal text such as "
               "messages, reviews and posts.",
          limits="long documents; non-English text.", **_NLP),
]
_SERVES_NER = [
    _bind("ner", selector={"task": "ner"}, io_out="spans",
          uses="Find named entities in English text with OntoNotes types, including DATE, TIME, MONEY, "
               "PERCENT and quantities - for timelines and fact extraction.",
          limits="other languages (use ner_multi); custom entity types (use gliner).", **_NLP),
    _bind("ner_multi", selector={"task": "ner_multi"}, io_out="spans",
          uses="Find people, organisations and places in text in any of ten languages.",
          limits="dates, money or other types (English: use ner).", **_NLP),
    _bind("ner", "gliner", selector={"task": "gliner"}, placement="nlp", io_in="text+labels",
          io_out="spans",
          uses="Zero-shot entity extraction for ANY entity types named at call time (labels), e.g. "
               "product, software, law, medical condition; the fabric's 37 types by default.",
          limits="when the fixed OntoNotes types suffice (ner is faster)."),
    _bind("ner", "spacy", selector={"task": "spacy"}, placement="nlp", io_out="spans",
          uses="Statistical English NER from the full spaCy pipeline (sentences, POS, dependency parse) "
               "- fast, and what the fabric's spaCy path used.",
          limits="other languages; uncommon entity types (use gliner)."),
]
_SERVES_ZEROSHOT = _bind(
    "zeroshot", io_in="text+labels", io_out="labels",
    uses="Classify text against labels you supply at call time (NLI) - intent, topic or a yes/no "
         "criterion - with no training and no LLM call.",
    limits="many (>20) labels at once; long texts (truncated).", **_NLP)
_SERVES_QA = _bind(
    "qa", io_in="question+context", io_out="answer",
    uses="Extract the exact answer span to a question from a given passage, with a confidence score.",
    limits="answers not stated in the passage (it cannot infer or invent); multi-hop questions.", **_NLP)
_SERVES_LANGID = _bind(
    "langid", io_out="label",
    uses="Identify which of 20 languages a text is in, to route it (e.g. ner vs ner_multi).",
    limits="very short strings (a word or two); languages outside the 20.", **_NLP)
_SERVES_EMBED = _bind(
    "embed", io_in="texts", io_out="vectors",
    uses="384-d sentence embeddings (MiniLM) for similarity, clustering, kNN and decision cards.",
    limits="the memory store's vectors (those are nomic 768-d via the embedding service).", **_NLP)

_SERVES_NLI = _bind(
    "nli", io_in="pair", io_out="scores", labels=["entailment", "neutral", "contradiction"],
    uses="Check whether a passage supports, contradicts or says nothing about a statement (NLI) - "
         "e.g. a claim in an answer against the evidence it cites - with no LLM call.",
    limits="long passages (cut at ~320 tokens); world knowledge the passage does not state; it "
           "over-flags paraphrase, so treat 'not entailed' as 'check this', not as wrong.", **_NLP)

#: cap name -> its serves= (one binding or a list)
NLP_SERVES = {
    "nlp.rerank": _SERVES_RERANK,
    "nlp.classify": _SERVES_CLASSIFY,
    "nlp.ner": _SERVES_NER,
    "nlp.zeroshot": _SERVES_ZEROSHOT,
    "nlp.qa": _SERVES_QA,
    "nlp.nli": _SERVES_NLI,
    "nlp.langid": _SERVES_LANGID,
    "nlp.embed": _SERVES_EMBED,
}
