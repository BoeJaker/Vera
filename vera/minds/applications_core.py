"""The decision points beyond the loop, as applications every candidate model is tested on (four-minds FM-2, the
candidate matrix - owner, 17 Oct: "introduce and test them in parallel").

Each entry has the shape of judge_core.QUESTIONS (a system prompt, the label each answer token means, binary or
choice) plus the label cards the embedding judges use, and user_message() builds the text from the inputs the
training store keeps for that decision. judge_core merges these in, so every judge - each small LLM, kNN, cards,
zero-shot, NLI, the cross-encoder, a trained head - scores them with no further wiring.

An application joins only when its inputs are stored: intel.entity_resolve, intel.material_change and the memory
points are listed in PENDING with why, and the matrix report shows them as untested.

Pure: no I/O, no Vera imports.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MAX_FIELD = 1200

QUESTIONS: Dict[str, Dict[str, Any]] = {
    "intel.signal_relevant": {
        "system": ("You decide whether one web item matters to a topic someone follows. You get the topic and the "
                   "item. Answer with one word: yes if it is relevant to the topic, no if it is not."),
        "labels": {"yes": "relevant", "no": "not_relevant"}, "kind": "binary"},
    "intel.page_genre": {
        "system": ("Classify what kind of page a web item is. Answer with one letter: A = news (a report of "
                   "something that happened), B = research (a paper, study or analysis), C = marketing (selling a "
                   "product or service), D = listing (a list of products, jobs or items), E = repo (a code "
                   "repository or package), F = index (a page that only links to other pages), G = unsure."),
        "labels": {"a": "news", "b": "research", "c": "marketing", "d": "listing", "e": "repo", "f": "index",
                   "g": "unsure"}, "kind": "choice"},
    "intel.enrich_worth": {
        "system": ("Decide whether a web item deserves a full read by a language model (summary, claims, entities) "
                   "or only a quick automatic pass. Answer with one word: yes if it deserves the full read, no if "
                   "the quick pass is enough."),
        "labels": {"yes": "model", "no": "nlp_only"}, "kind": "binary"},
    "intel.material_change": {
        "system": ("A topic someone follows has a new summary. You get the previous headline, the new one, what "
                   "changed and how many competitor items appeared. Answer with one word: yes if the picture changed "
                   "enough that the reader should be alerted, no if it did not."),
        "labels": {"yes": "alert", "no": "no_alert"}, "kind": "binary"},
    "discovery.page_on_topic": {
        "system": ("A crawler is collecting pages about one topic. Decide whether this page is about that topic. "
                   "Answer with one word: yes if it is on topic, no if it is off topic."),
        "labels": {"yes": "on_topic", "no": "off_topic"}, "kind": "binary"},
}

CARDS: Dict[str, Dict[str, str]] = {
    "intel.signal_relevant": {
        "relevant": "This item is about the followed topic and worth the reader's attention.",
        "not_relevant": "This item is not about the followed topic, or only mentions it in passing."},
    "intel.page_genre": {
        "news": "A news report of something that happened.",
        "research": "A research paper, study or in-depth analysis.",
        "marketing": "A page selling or promoting a product or service.",
        "listing": "A list of products, jobs, events or items.",
        "repo": "A code repository, package or software project page.",
        "index": "A page that mostly links to other pages.",
        "unsure": "A page whose kind cannot be told."},
    "intel.enrich_worth": {
        "model": "A substantial item worth a careful full read, a summary and its claims extracted.",
        "nlp_only": "A thin or routine item where a quick automatic pass is enough."},
    "intel.material_change": {
        "alert": "The topic's picture changed materially: the reader should be told now.",
        "no_alert": "Nothing material changed: the new summary restates or refines the old one."},
    "discovery.page_on_topic": {
        "on_topic": "This page is about the crawl's topic.",
        "off_topic": "This page is not about the crawl's topic."},
}

#: decision points on the ledger that cannot be tested yet, and why
PENDING: Dict[str, str] = {
    "intel.entity_resolve": "its stored inputs hold the candidates' Wikidata ids only, no names - nothing to judge",
    "intel.worldview_drift": "no decisions recorded with inputs",
    "memory.seek": "the training store's memory surface is off (owner's call)",
    "memory.chat_recall": "the training store's memory surface is off (owner's call)",
    "loop.caps_shortlist": "a ranking, not a label - scored by caps.shortlist.replay and the FM-3.5 set",
}


def _s(v: Any, n: int = MAX_FIELD) -> str:
    return " ".join(str(v or "").split())[:n]


def user_message(question_id: str, state: Dict[str, Any]) -> Optional[str]:
    """The text put to a model for one stored decision, or None when the inputs lack what it needs."""
    if question_id not in QUESTIONS or not isinstance(state, dict):
        return None
    if question_id == "intel.material_change":
        prev, now = _s(state.get("prev"), 400), _s(state.get("now"), 400)
        if not now:
            return None
        return (f"Topic: {_s(state.get('topic'), 200)}\nPrevious headline: {prev or '(none)'}\nNew headline: {now}\n"
                f"Changed: {_s(state.get('changed'), 600) or '(nothing listed)'}\n"
                f"Competitor items: {_s(state.get('comps'), 20) or '0'}\n\nAnswer with one word.")
    if question_id.startswith("intel."):
        title, snippet = _s(state.get("title"), 300), _s(state.get("snippet"))
        if not title and not snippet:
            return None
        head = f"Topic: {_s(state.get('topic_name') or state.get('topic'), 200)}\n" \
            if question_id == "intel.signal_relevant" else ""
        return (f"{head}Title: {title}\nSource: {_s(state.get('source'), 120)}\nURL: {_s(state.get('url'), 300)}\n"
                f"Excerpt: {snippet}\n\n" + ("Answer with one letter." if QUESTIONS[question_id]["kind"] == "choice"
                                             else "Answer with one word."))
    if question_id == "discovery.page_on_topic":
        if not _s(state.get("title")) and not _s(state.get("excerpt")):
            return None
        return (f"Topic: {_s(state.get('topic'), 300)}\nTitle: {_s(state.get('title'), 300)}\n"
                f"URL: {_s(state.get('url'), 300)}\nExcerpt: {_s(state.get('excerpt'), 700)}\n\nAnswer with one word.")
    return None
