"""The specialist-model catalog: what the builder may put in the shared store.

Curated entries per family, plus Hugging Face picks the user searched for
(flagged unvetted). Pure - no Vera import, no network - so the job a catalog
entry becomes is testable.

Honesty rule for the labels: `in_use` means a serving node loads this model
today (the nlp_server registry, the gpu_inference defaults). Every other
curated entry is a known-good *candidate*, not a claim that it has been
exported and loaded here; the store inventory says what has actually been built.

Consumers import uppercase (Vera.vera.catalog.specialist_catalog); tests import
lowercase so pytest binds to the worktree copy.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: family -> what serves it and where it lives in the store
FAMILIES: Dict[str, Dict[str, str]] = {
    "nlp":     {"label": "NLP (nlp_server)", "serves": "nlp_server on every node",
                "store": "nlp"},
    "whisper": {"label": "Speech-to-text (Whisper)", "serves": "gpu_inference WHISPER_MODEL",
                "store": "whisper"},
    "tts":     {"label": "Text-to-speech", "serves": "gpu_inference (Kokoro)", "store": "tts"},
    "sd":      {"label": "Diffusion", "serves": "gpu_inference SD_MODEL_ID", "store": "sd"},
    "gliner":  {"label": "GLiNER (zero-shot NER)", "serves": "the host's fabric entity graph",
                "store": "gliner"},
}

#: nlp_server tasks and the Hugging Face pipeline tag a search for each uses
NLP_TASK_PIPELINE = {
    "ner": "token-classification", "ner_multi": "token-classification",
    "classify": "text-classification", "sentiment3": "text-classification",
    "langid": "text-classification", "zeroshot": "zero-shot-classification",
    "qa": "question-answering", "embed": "sentence-similarity",
}

#: what a Hugging Face search filters on per non-NLP family
FAMILY_SEARCH = {"sd": {"pipeline_tag": "text-to-image", "library": "diffusers"},
                 "gliner": {"library": "gliner"}}

_SD_PATTERNS = ["model_index.json", "*/*.json", "*/*.txt", "*/*.model", "*/*.safetensors"]
_SD_IGNORE = ["*non_ema*", "*.ckpt", "*.bin", "*.fp16.*"]

_KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v0.2.1"

CURATED: List[Dict[str, Any]] = [
    # ── NLP: the registry (in use) and alternatives per task ────────────────
    {"family": "nlp", "task": "ner", "model": "djagatiya/ner-roberta-base-ontonotesv5-englishv4",
     "in_use": True, "note": "OntoNotes-v5 (18 types, has DATE)"},
    {"family": "nlp", "task": "ner", "model": "dslim/bert-base-NER",
     "note": "CoNLL-2003 (PER/ORG/LOC/MISC only, no DATE) - smaller"},
    {"family": "nlp", "task": "ner", "model": "Jean-Baptiste/roberta-large-ner-english",
     "note": "CoNLL-2003, roberta-large - heavier, more accurate"},
    {"family": "nlp", "task": "ner_multi", "model": "Davlan/xlm-roberta-base-ner-hrl",
     "in_use": True, "note": "10 high-resource languages"},
    {"family": "nlp", "task": "classify", "model": "distilbert-base-uncased-finetuned-sst-2-english",
     "in_use": True, "note": "binary sentiment (SST-2)"},
    {"family": "nlp", "task": "sentiment3", "model": "cardiffnlp/twitter-roberta-base-sentiment-latest",
     "in_use": True, "note": "negative / neutral / positive"},
    {"family": "nlp", "task": "zeroshot", "model": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",  # pragma: allowlist secret
     "in_use": True, "note": "NLI zero-shot, base"},
    {"family": "nlp", "task": "zeroshot", "model": "MoritzLaurer/deberta-v3-large-zeroshot-v2.0",
     "note": "large - better labels, ~3x slower on CPU"},
    {"family": "nlp", "task": "zeroshot", "model": "facebook/bart-large-mnli",
     "note": "the classic zero-shot model"},
    {"family": "nlp", "task": "qa", "model": "deepset/roberta-base-squad2",
     "in_use": True, "note": "extractive QA, SQuAD 2.0"},
    {"family": "nlp", "task": "qa", "model": "deepset/tinyroberta-squad2",
     "note": "distilled - faster, a little less accurate"},
    {"family": "nlp", "task": "langid", "model": "papluca/xlm-roberta-base-language-detection",
     "in_use": True, "note": "20 languages"},
    {"family": "nlp", "task": "embed", "model": "sentence-transformers/all-MiniLM-L6-v2",  # pragma: allowlist secret
     "in_use": True, "note": "384-d, fast"},
    {"family": "nlp", "task": "embed", "model": "BAAI/bge-small-en-v1.5",
     "note": "384-d, stronger retrieval"},
    {"family": "nlp", "task": "embed", "model": "sentence-transformers/all-mpnet-base-v2",
     "note": "768-d, slower, higher quality"},
    {"family": "nlp", "task": "rerank", "model": "Xenova/ms-marco-MiniLM-L-6-v2",
     "in_use": True, "note": "cross-encoder via fastembed"},
    {"family": "nlp", "task": "gliner", "model": "urchade/gliner_medium-v2.1",
     "in_use": True, "note": "zero-shot NER on the nodes - a torch checkpoint, not an ONNX export"},
    {"family": "nlp", "task": "spacy", "model": "en_core_web_sm",
     "in_use": True, "note": "spaCy English pipeline - a pinned wheel the component installs"},
    # ── Whisper (openai-whisper checkpoint names) ───────────────────────────
    {"family": "whisper", "model": "tiny", "note": "fastest, lowest accuracy"},
    {"family": "whisper", "model": "base", "in_use": True, "note": "gpu_inference default"},
    {"family": "whisper", "model": "small", "note": "good balance on a V100"},
    {"family": "whisper", "model": "medium", "note": "higher accuracy, ~5 GB VRAM"},
    {"family": "whisper", "model": "turbo", "note": "large-v3 distilled decoder - fast + accurate"},
    {"family": "whisper", "model": "large-v3", "note": "most accurate, ~10 GB VRAM"},
    # ── TTS ──────────────────────────────────────────────────────────────────
    {"family": "tts", "model": "kokoro-v1.0", "in_use": True,
     "urls": [f"{_KOKORO}/kokoro-v1.0.onnx", f"{_KOKORO}/voices-v1.0.bin"],
     "note": "Kokoro ONNX + 54 voices (what gpu_inference downloads at start)"},
    # ── Diffusion (SD-1.x pipelines: what gpu_inference loads) ──────────────
    {"family": "sd", "model": "runwayml/stable-diffusion-v1-5", "in_use": True,
     "note": "gpu_inference default"},
    {"family": "sd", "model": "Lykon/dreamshaper-8", "note": "SD-1.5 fine-tune, general purpose"},
    {"family": "sd", "model": "SG161222/Realistic_Vision_V5.1_noVAE",
     "note": "SD-1.5 fine-tune, photographic"},
    # ── GLiNER ───────────────────────────────────────────────────────────────
    {"family": "gliner", "model": "urchade/gliner_medium-v2.1", "in_use": True,
     "note": "the host's FABRIC_GLINER_MODEL default"},
    {"family": "gliner", "model": "urchade/gliner_small-v2.1", "note": "smaller, faster"},
    {"family": "gliner", "model": "urchade/gliner_multi-v2.1", "note": "multilingual"},
]

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def entry_id(e: Dict[str, Any]) -> str:
    return ":".join(x for x in (e["family"], e.get("task", ""), e["model"]) if x)


def curated(family: str = "") -> List[Dict[str, Any]]:
    return [dict(e, id=entry_id(e), vetted=True, in_use=bool(e.get("in_use")))
            for e in CURATED if not family or e["family"] == family]


def find(entry: str) -> Optional[Dict[str, Any]]:
    for e in curated():
        if e["id"] == entry:
            return e
    return None


def job_for(e: Dict[str, Any]) -> Dict[str, Any]:
    """The builder job that puts a catalog entry (curated or an HF pick) in the store."""
    fam = e["family"]
    if fam not in FAMILIES:
        raise ValueError(f"unknown family {fam!r}")
    if fam == "nlp":
        return {"kind": "nlp_export", "model": e["model"], "task": e["task"]}
    if fam == "whisper":
        return {"kind": "whisper", "model": e["model"]}
    if fam == "tts":
        if not e.get("urls"):
            raise ValueError("a TTS entry needs curated urls")
        return {"kind": "url_fetch", "family": "tts", "model": e["model"], "urls": list(e["urls"])}
    if fam == "sd":
        return {"kind": "hf_snapshot", "family": "sd", "model": e["model"],
                "allow_patterns": list(_SD_PATTERNS), "ignore_patterns": list(_SD_IGNORE)}
    return {"kind": "hf_snapshot", "family": fam, "model": e["model"]}


def hf_pick(family: str, model: str, task: str = "") -> Dict[str, Any]:
    """An unvetted Hugging Face repo the user picked, as a catalog entry. Only
    families whose job is a Hugging Face download or export accept one: Whisper
    names and TTS files come from the curated list alone (the latter are
    URLs, and the builder takes no free-form URL)."""
    if family not in ("nlp", "sd", "gliner"):
        raise ValueError("Hugging Face picks are accepted for nlp, sd and gliner only")
    if not _REPO.match(model or ""):
        raise ValueError("model must be a Hugging Face repo id: owner/name")
    if family == "nlp" and task not in NLP_TASK_PIPELINE:
        raise ValueError(f"nlp needs a task: one of {sorted(NLP_TASK_PIPELINE)}")
    e = {"family": family, "model": model, "vetted": False, "in_use": False,
         "note": "unvetted Hugging Face pick - the build reports whether it exports"}
    if family == "nlp":
        e["task"] = task
    e["id"] = entry_id(e)
    return e


def search_params(family: str, task: str = "", query: str = "", limit: int = 20) -> Dict[str, Any]:
    """Query parameters for https://huggingface.co/api/models for one family."""
    p: Dict[str, Any] = {"search": query or "", "sort": "downloads", "direction": -1,
                         "limit": max(1, min(50, int(limit or 20)))}
    if family == "nlp":
        if task not in NLP_TASK_PIPELINE:
            raise ValueError(f"nlp search needs a task: one of {sorted(NLP_TASK_PIPELINE)}")
        p["pipeline_tag"] = NLP_TASK_PIPELINE[task]
    elif family in FAMILY_SEARCH:
        p.update(FAMILY_SEARCH[family])
    else:
        raise ValueError("search is available for nlp, sd and gliner")
    return p


def mark_built(entries: List[Dict[str, Any]], store: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Annotate entries with whether the builder's store inventory holds them."""
    fams = (store or {}).get("families") or {}
    for e in entries:
        rows = fams.get(FAMILIES[e["family"]]["store"]) or []
        names = {r.get("name") for r in rows}
        if e["family"] == "whisper":
            e["built"] = False           # whisper files are loose .pt files; see below
        elif e["family"] == "nlp" and e.get("task") == "rerank":
            e["built"] = "_fastembed" in names
        else:
            e["built"] = e["model"].replace("/", "__") in names
    whisper_files = set((store or {}).get("whisper_files") or [])
    for e in entries:
        if e["family"] == "whisper":
            e["built"] = f"{e['model']}.pt" in whisper_files
    return entries
