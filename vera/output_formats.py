"""
output_formats.py — Vera shared output-format registry
======================================================

A single source of truth for "how should the LLM shape its answer". Historically
the dream `synthesize` stage carried two overlapping style systems:

  * REVIEW_STYLES   — structure/content palette (docs / critique / improvement /
                      integration / architecture), each a {system, instruction}.
  * output_style    — length/voice directives (quick / short / long / audio)
                      appended to the system prompt.

Both lived inside dream_capabilities and were unreachable from chat or any other
caller. This module lifts them out so ANY Ollama call can opt into a format by
name, and adds a few deliverable profiles (markdown / report / json / email /
slides / plain). `apply_format()` is the "prompt modification section" — given a
base system prompt and a profile name, it returns the modified system prompt.

The export side (render_capabilities.py, pandoc-backed) reads each profile's
`target_file_format` hint to pick a sensible default file type.

Nothing here imports the orchestrator, so it is safe to import from anywhere
(capabilities.py, dream_capabilities.py, chat, render) without circular deps.
"""

from __future__ import annotations

from typing import Any, Dict, List

# ─────────────────────────────────────────────────────────────────────────────
# Anti-hallucination preamble (shared with dream synthesize)
# ─────────────────────────────────────────────────────────────────────────────

ANTI_HALLU = (
    "STRICT GROUNDING RULES — these override everything else:\n"
    "1. You may ONLY discuss content that appears in the data provided below. "
    "Do not invent topics, themes, papers, projects, or activities not explicitly present in the data.\n"
    "2. If the provided data is empty or trivial, write a single short note saying so — do not fabricate content to fill space.\n"
    "3. Do NOT introduce generic subjects ('AI advancements', 'recent papers', etc.) "
    "unless those exact subjects appear verbatim in the data.\n"
    "4. Quote or directly reference the source entries when making observations.\n\n"
)

# Backwards-compatible alias — dream_capabilities used the name `_ANTI_HALLU`.
_ANTI_HALLU = ANTI_HALLU

# ─────────────────────────────────────────────────────────────────────────────
# REVIEW_STYLES — structure/content palette. Shape preserved verbatim
# ({label, system, instruction}) so dream's synthesize/review code can import
# and use it exactly as before.
# ─────────────────────────────────────────────────────────────────────────────

REVIEW_STYLES: Dict[str, Dict[str, str]] = {
    "docs": {
        "label": "Documentation",
        "system": "You are a senior engineer writing authoritative developer "
                  "documentation. Be thorough, structured, and concrete.",
        "instruction":
            "Write comprehensive developer documentation for this module in "
            "Markdown. Cover: (1) purpose and responsibilities; (2) the key "
            "classes, functions and capabilities it defines, with signatures and "
            "what each does; (3) the data/control flow through the module; "
            "(4) external dependencies and the events/streams it emits or "
            "consumes; (5) configuration and important constants; (6) concrete "
            "usage examples. Use headings and tables where helpful. Be long and "
            "detailed — aim for complete coverage, not a summary.",
    },
    "critique": {
        "label": "Critique",
        "system": "You are a meticulous staff engineer doing a rigorous code "
                  "critique. Be specific and cite concrete locations.",
        "instruction":
            "Provide a detailed critique of this module in Markdown. Identify "
            "design issues, code smells, correctness risks, race conditions, "
            "error-handling gaps, unclear abstractions, and edge cases. For each "
            "finding give: a severity (critical/high/medium/low), the specific "
            "function or line context, why it matters, and a suggested direction. "
            "Group findings by severity. Be exhaustive and specific.",
    },
    "improvement": {
        "label": "Improvement notes",
        "system": "You are a pragmatic architect proposing concrete, prioritised "
                  "improvements.",
        "instruction":
            "Write detailed improvement notes for this module in Markdown. "
            "Propose concrete refactors and enhancements, prioritised (quick wins "
            "first, then larger refactors). For each: the rationale, the expected "
            "benefit, the risk/effort, and a short sketch of the change (pseudocode "
            "or signatures). Cover performance, readability, testability, and "
            "robustness. Be specific to THIS code, not generic advice.",
    },
    "integration": {
        "label": "Integration ideas",
        "system": "You are a systems architect mapping how a module fits into the "
                  "wider platform and how to integrate it better.",
        "instruction":
            "Analyse how this module integrates with the rest of the Vera system "
            "in Markdown. Cover: what it depends on and what depends on it; the "
            "capabilities it exposes and how other modules would call them; the "
            "events/streams it participates in; coupling and seams. Then propose "
            "concrete integration ideas — new connections to other subsystems "
            "(dream, ide, memory, fabric, dag, project), new capabilities to "
            "expose, or ways to make it more composable. Be specific and "
            "ambitious but grounded in the actual code.",
    },
    "architecture": {
        "label": "Architecture",
        "system": "You are a principal engineer explaining and assessing module "
                  "architecture.",
        "instruction":
            "Produce a detailed architectural analysis of this module in Markdown: "
            "its role in the overall system, the patterns it uses, its internal "
            "structure and layering, state management, concurrency model, and "
            "failure modes. Include an assessment of how well the architecture "
            "serves its purpose and where it strains. Be thorough.",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# FORMAT_PROFILES — the unified palette exposed to every caller. Each entry:
#   label              human label for pickers
#   kind               length | structure | deliverable
#   system_suffix      text appended to the base system prompt by apply_format()
#   target_file_format default file type for render/export (md|txt|docx|pdf|...)
# ─────────────────────────────────────────────────────────────────────────────

def _review_suffix(style: str) -> str:
    sdef = REVIEW_STYLES[style]
    return f"{sdef['system']} {sdef['instruction']}"


FORMAT_PROFILES: Dict[str, Dict[str, Any]] = {
    # ── length / voice (lifted from the dream `output_style` block) ───────────
    "quick": {
        "label": "Quick", "kind": "length", "target_file_format": "md",
        "system_suffix": "OUTPUT STYLE: QUICK — respond in 2-4 sentences maximum. "
                         "No markdown headers. Just the key insight.",
    },
    "short": {
        "label": "Short", "kind": "length", "target_file_format": "md",
        "system_suffix": "OUTPUT STYLE: SHORT — respond in 1-2 short paragraphs "
                         "(50-150 words). One optional header. Punchy and direct.",
    },
    "standard": {
        "label": "Standard", "kind": "length", "target_file_format": "md",
        "system_suffix": "OUTPUT STYLE: STANDARD — a clear, well-structured answer "
                         "in clean markdown. Use ## sections only where they help.",
    },
    "long": {
        "label": "Long", "kind": "length", "target_file_format": "md",
        "system_suffix": "OUTPUT STYLE: LONG — detailed analysis with multiple "
                         "sections. 800-1500 words. Use ## headers for each section.",
    },
    "exhaustive": {
        "label": "Exhaustive", "kind": "length", "target_file_format": "md",
        "system_suffix": "OUTPUT STYLE: EXHAUSTIVE — an in-depth, research-grade "
                         "treatment. Use ## sections for background, each theme, "
                         "evidence, open questions and next steps. 1200-2500 words "
                         "IF the material supports it — shorter and honest if not.",
    },
    "audio": {
        "label": "Audio-ready", "kind": "length", "target_file_format": "txt",
        "system_suffix": "OUTPUT STYLE: AUDIO-READY — write as if this will be read "
                         "aloud. No markdown formatting, no bullet points, no headers. "
                         "Use natural speech patterns, short sentences, and clear "
                         "transitions. 150-300 words. Start directly with content, no preamble.",
    },
    # ── structure / content (derived from REVIEW_STYLES) ──────────────────────
    "docs": {
        "label": "Documentation", "kind": "structure", "target_file_format": "docx",
        "system_suffix": _review_suffix("docs"),
    },
    "critique": {
        "label": "Critique", "kind": "structure", "target_file_format": "md",
        "system_suffix": _review_suffix("critique"),
    },
    "improvement": {
        "label": "Improvement notes", "kind": "structure", "target_file_format": "md",
        "system_suffix": _review_suffix("improvement"),
    },
    "integration": {
        "label": "Integration ideas", "kind": "structure", "target_file_format": "md",
        "system_suffix": _review_suffix("integration"),
    },
    "architecture": {
        "label": "Architecture", "kind": "structure", "target_file_format": "md",
        "system_suffix": _review_suffix("architecture"),
    },
    # ── deliverable formats (new) ─────────────────────────────────────────────
    "markdown": {
        "label": "Markdown", "kind": "deliverable", "target_file_format": "md",
        "system_suffix": "OUTPUT FORMAT: clean GitHub-Flavored Markdown. Start with "
                         "an H1 title. Use ## sections, tables, fenced code blocks and "
                         "bullet lists where they genuinely help. No preamble outside the document.",
    },
    "report": {
        "label": "Report", "kind": "deliverable", "target_file_format": "docx",
        "system_suffix": "OUTPUT FORMAT: a formal report in Markdown. H1 title, a one-"
                         "paragraph executive summary, then ## sections (Background, "
                         "Findings, Analysis, Recommendations). Professional tone, "
                         "suitable for export to a document.",
    },
    "json": {
        "label": "JSON", "kind": "deliverable", "target_file_format": "json",
        "system_suffix": "OUTPUT FORMAT: respond with STRICT, valid JSON only. No "
                         "markdown, no code fences, no commentary before or after. "
                         "If a schema was described, follow it exactly.",
    },
    "email": {
        "label": "Email", "kind": "deliverable", "target_file_format": "txt",
        "system_suffix": "OUTPUT FORMAT: a ready-to-send email. Subject line first, "
                         "then a greeting, concise body paragraphs, and a sign-off. "
                         "Plain professional prose — no markdown headers or bullet symbols.",
    },
    "slides": {
        "label": "Slides", "kind": "deliverable", "target_file_format": "pptx",
        "system_suffix": "OUTPUT FORMAT: a slide deck in Markdown. Use a level-1 "
                         "heading per slide and short bullet points beneath each. "
                         "Separate slides with a horizontal rule (---). Keep each "
                         "slide scannable — no long paragraphs.",
    },
    "plain": {
        "label": "Plain text", "kind": "deliverable", "target_file_format": "txt",
        "system_suffix": "OUTPUT FORMAT: plain text only. No markdown, no headers, "
                         "no bullet symbols, no code fences. Just readable prose.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC PROFILES — runtime contributions (the skills system feeds these in).
#
# The in-code FORMAT_PROFILES above is the immutable baseline; it is never
# mutated. The skills module (vera/skills/skills.py) registers every
# `output_format`-type skill here via register_profile(), which is how the
# Skills library becomes the single authoring surface for output styles:
# whatever a user (or a built-in format skill) defines shows up in chat's
# Output-format picker (llm.formats → list_profiles) and is honoured by
# apply_format() — and therefore by llm.generate, the agent runner and render —
# without any of those callers needing to know about skills. A dynamic profile
# overrides a baseline profile of the same id, so editing the built-in "docs"
# format skill re-shapes that format everywhere.
# ─────────────────────────────────────────────────────────────────────────────

_DYNAMIC_PROFILES: Dict[str, Dict[str, Any]] = {}


def _merged_profiles() -> Dict[str, Dict[str, Any]]:
    """Baseline profiles overlaid with runtime (skill-contributed) ones."""
    return {**FORMAT_PROFILES, **_DYNAMIC_PROFILES}


def register_profile(profile_id: str, *, label: str, system_suffix: str,
                     kind: str = "deliverable", target_file_format: str = "md",
                     source: str = "skill") -> None:
    """Add or replace a runtime output-format profile.

    Used by the skills system to surface `output_format` skills as first-class
    formats. Re-registering the same id overwrites; a dynamic profile shadows a
    baseline profile of the same id in every public view.
    """
    pid = (profile_id or "").strip()
    if not pid:
        return
    _DYNAMIC_PROFILES[pid] = {
        "label":              (label or pid).strip() or pid,
        "kind":               (kind or "deliverable").strip() or "deliverable",
        "system_suffix":      system_suffix or "",
        "target_file_format": (target_file_format or "md").strip() or "md",
        "source":             source or "skill",
    }


def unregister_profile(profile_id: str) -> None:
    """Remove a previously registered runtime profile (no-op if absent)."""
    _DYNAMIC_PROFILES.pop((profile_id or "").strip(), None)


def get_profile(profile_id: str) -> Dict[str, Any] | None:
    """Look up one profile (dynamic first, then baseline). Returns None if unknown."""
    return _merged_profiles().get((profile_id or "").strip())


def apply_format(system: str, profile: str) -> str:
    """Return `system` with the named format profile's directive appended.

    Unknown / empty `profile` returns `system` unchanged, so callers can pass an
    optional output_format through unconditionally.
    """
    prof = get_profile(profile)
    if not prof:
        return system or ""
    suffix = prof.get("system_suffix", "").strip()
    if not suffix:
        return system or ""
    base = (system or "").rstrip()
    return f"{base}\n\n{suffix}" if base else suffix


def list_profiles() -> List[Dict[str, Any]]:
    """Public, picker-friendly view of the registry (no behaviour-bearing text).

    Includes both the in-code baseline and any skill-contributed profiles, with
    a `source` flag ('builtin' | 'skill') so pickers can group/badge them.
    """
    return [
        {"id": pid, "label": p["label"], "kind": p["kind"],
         "target_file_format": p["target_file_format"],
         "source": p.get("source", "builtin")}
        for pid, p in _merged_profiles().items()
    ]