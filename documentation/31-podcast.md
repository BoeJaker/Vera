# 31 — Podcast Generation

`vera/podcast/podcast_capabilities.py` turns fabric data feeds, capability
results, URLs or free notes into a produced multi-voice podcast episode. There
is **no separate panel** — the chat toolbar's **Pod** button opens a composer
above the chat input, and finished episodes render as playable cards in the
conversation. The agent loop can drive the same caps directly.

## Pipeline

```
sources ─ gather ─→ script (llm.generate, JSON {title, segments[]})
        └ fabric.query / any cap / url / text
script  ─ voices ─→ one GPU-TTS call per segment (/tts on GPU_INFER_URL,
                    per-speaker voice + speed — kokoro or coqui)
audio   ─ stitch ─→ WAV concat + gap_ms silences (+ ffmpeg → mp3 if available)
episode ─ persist ─→ vera/podcast/generated/<id>.{mp3|wav,json}
                    + fabric dataset `podcasts.episodes`
                    + served at GET /podcast/audio/<id>
```

## Capabilities

| cap | verb | notes |
|---|---|---|
| `podcast.generate` | POST /podcast/generate | full pipeline; `wait=false` (default) returns `{job_id}` to poll, `wait=true` blocks (agent-friendly) |
| `podcast.script` | POST /podcast/script | script only, for preview/editing before synthesis |
| `podcast.status` | GET /podcast/status | job progress `{stage: gather→script→voice→stitch→done, pct}` |
| `podcast.list` / `podcast.get` / `podcast.delete` | GET/GET/POST | episode management |
| `podcast.settings.get/set` | GET/POST | persisted defaults (speakers/voices, style, minutes, gap_ms, format, show_name) |

`sources` is a JSON list, each item one of:

```json
{"type":"text",    "text":"raw notes"}
{"type":"dataset", "dataset_id":"mesh.esp32-1.temp", "query":"optional", "top_k":12}
{"type":"fabric",  "query":"fabric-wide search"}
{"type":"cap",     "name":"web.search", "args":{"query":"..."}}
{"type":"url",     "url":"https://..."}
```

`speakers` (up to 6): `[{name, voice, speed, persona}]` — voices come from
`tts.voices` (Kokoro catalogue). Styles: conversational, interview, news,
deep-dive, debate, story.

Progress is emitted as `podcast.progress` events and mirrored in the in-memory
job table (`podcast.status`). Episodes are indexed in the fabric so other
systems (dream, research) can discover them.

## Chat composer (chat_panel.html)

- **Pod** toolbar button toggles `#podcastBar` above the input bar.
- Topic / style / minutes; speaker rows (name, voice dropdown, speed, persona);
  sources: *this chat* checkbox (recent messages become a text source), fabric
  dataset picker, url/note/web-search adder.
- *Script preview* writes the script into an editable textarea (`Name: line`
  format) — Generate then uses the edited text verbatim.
- Generation polls `podcast.status` and renders a progress bar; the finished
  episode becomes a chat card with an `<audio>` player, transcript toggle and
  download link. *Episodes* re-inserts past episodes.
- *Defaults* persists the current speaker/style config server-side
  (`podcast.settings.set`), so agent-initiated generations use the same voices.

## Screenshots

## Generation stages

Podcast generation is a staged media job: resolve topic and supplied context,
author a speaker-structured script, synthesize segments, assemble audio, persist
the artifact, and expose it through list/get. Context may come from files or
capability output; a separate topic is optional when that material already
defines the subject.

Keep script generation and speech synthesis separately inspectable. A good
script can fail at voice lookup or audio assembly, while successful TTS can
faithfully render a poorly attributed script. Record voice/settings and segment
ordering with the job so regeneration is deterministic enough to diagnose.

Large inputs should be summarized or sectioned before scripting. Never embed
credentials or private source material unless the resulting persistent audio is
authorized to contain it. `podcast.status` is the operational surface for
long-running work; list/get/delete operate on completed persisted episodes.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
