# 31 — Podcast Generation

`vera/podcast/podcast_capabilities.py` turns fabric data feeds, capability
results, workspace files, URLs or free notes into a produced **multi-voice
podcast episode**: it gathers source material, has the LLM write a speaker-tagged
script, voices each segment on the TTS media node, stitches the audio, and
stores the episode in a local library, the data fabric and (when configured)
the object store.

There is **no separate panel**: the chat toolbar's **Pod** button opens a
composer above the chat input, and finished episodes render as playable cards
in the conversation. The agentic loop, Dream (`podcast` delivery channel) and
`output.redeliver` drive the same capabilities directly.

**Status:** stable. Requires a reachable TTS media service (Kokoro by default;
Coqui optional). MP3 output needs `ffmpeg` on the PATH; otherwise episodes are
WAV.

## Contents

- [1. Pipeline](#1-pipeline)
- [2. Source map](#2-source-map)
- [3. Capabilities](#3-capabilities)
- [4. Inputs in detail](#4-inputs-in-detail)
  - [4.1 Sources](#41-sources)
  - [4.2 Speakers, styles and length](#42-speakers-styles-and-length)
  - [4.3 Settings and defaults](#43-settings-and-defaults)
- [5. Generation stages](#5-generation-stages)
- [6. Outputs and storage](#6-outputs-and-storage)
- [7. Chat composer](#7-chat-composer)
- [8. Other entry points](#8-other-entry-points)
- [9. Events](#9-events)
- [10. Worked examples](#10-worked-examples)
- [11. Failure modes and troubleshooting](#11-failure-modes-and-troubleshooting)
- [See also](#see-also)

---

## 1. Pipeline

```
sources ─ gather ─→ text blocks [{label, text}]   (≤ ~9,000 chars total)
        └ text / file / dataset / fabric / cap / url
blocks  ─ script ─→ llm.generate → JSON {title, description, segments:[{speaker,text}]}
                    (one strict retry if the JSON does not parse)
script  ─ voice  ─→ one POST <tts media node>/tts per segment
                    (per-speaker voice + speed; engine kokoro or coqui)
audio   ─ stitch ─→ WAV concat, sample-rate normalised, gap_ms silence between turns
                    (+ ffmpeg → 96 kbps MP3 when available)
episode ─ persist ─→ vera/podcast/generated/<id>.{mp3|wav,json}
                    + session workspace: <slug>.<fmt> and <slug>.script.md
                    + object store podcasts/<id>.<fmt> (if configured)
                    + fabric dataset podcasts.episodes
                    + served at GET /podcast/audio/<id>
```

---

## 2. Source map

| Path | Responsibility |
|---|---|
| `vera/podcast/podcast_capabilities.py` | Source gathering, script writing, TTS calls, WAV stitching, MP3 conversion, persistence, job table, all `podcast.*` caps, `/podcast/audio/{id}`. |
| `vera/podcast/generated/` | Episode library (`<id>.wav`/`.mp3`, `<id>.json`) and `settings.json`. |
| `vera/chat/chat_panel.html` | The Pod composer and episode cards. |
| `vera/delivery.py` | The `podcast` delivery channel (`podcast.generate`, format `audio`). |

---

## 3. Capabilities

| Cap | Route | Notes |
|---|---|---|
| `podcast.generate` | `POST /podcast/generate` | Full pipeline. `wait=false` (default) returns `{job_id}` to poll; `wait=true` blocks and returns the episode (agent-friendly). Streams `podcast.progress`. |
| `podcast.script` | `POST /podcast/script` | Script only (gather + write, no audio), for preview or editing before synthesis. |
| `podcast.status` | `GET /podcast/status` | Job progress `{stage, pct, message, …}` for a `job_id`. |
| `podcast.list` | `GET /podcast/list` | Episodes, newest first (`limit`, default 50). |
| `podcast.get` | `GET /podcast/get` | One episode including its script (`episode_id`). |
| `podcast.delete` | `POST /podcast/delete` | Delete an episode's audio and metadata. |
| `podcast.settings.get` / `podcast.settings.set` | `GET /podcast/settings`, `POST /podcast/settings/set` | Persisted defaults used when `podcast.generate` arguments are omitted. |

`podcast.generate` arguments: `topic`, `sources`, `speakers`, `style`, `minutes`, `gap_ms`, `engine`, `format` (`mp3_if_possible` or `wav`), `script` (a pre-written `{segments:[{speaker,text}]}` that skips the LLM), `show_name`, `intro`, `instructions` (extra guidance for the script writer), `session_id`, `wait`. Either `topic`, `sources` or `script.segments` is required. With sources and no topic, the writer determines the subject from the material itself.

---

## 4. Inputs in detail

### 4.1 Sources

`sources` may be a JSON list of typed specs, or a bare string or newline/comma list that is auto-classified (`http(s)://…` or `www.…` → URL; something that looks like a path → file; otherwise text). This lets a loop pass a file or a previous step's output directly.

```json
{"type":"text",    "text":"raw notes", "label":"optional"}
{"type":"file",    "path":"./research.md"}
{"type":"dataset", "dataset_id":"mesh.esp32-1.temp", "query":"optional", "top_k":12}
{"type":"fabric",  "query":"fabric-wide search", "top_k":12}
{"type":"cap",     "name":"web.search", "args":{"query":"..."}}
{"type":"url",     "url":"https://..."}
```

| Type | Resolution |
|---|---|
| `text` | Used as-is. |
| `file` | Read from the session's artifact workspace (up to 200 KB). |
| `dataset` / `fabric` | `fabric.query` scoped to a dataset or fabric-wide; record summaries are used. |
| `cap` | Any capability; its result is flattened to text. |
| `url` | Fetched (20 s timeout); HTML is stripped to text. |

The combined context is bounded (about 9,000 characters), so summarise or section very large inputs first.

### 4.2 Speakers, styles and length

`speakers` (up to **6**): `[{name, voice, speed, persona}]`. Voices come from `tts.voices` (the Kokoro catalogue). Defaults: **Nova** (`af_heart`, "warm, curious host…") and **Rex** (`am_michael`, "analytical co-host…").

| Style | Shape |
|---|---|
| `conversational` (default) | Relaxed back-and-forth with follow-up questions. |
| `interview` | First speaker asks; the others answer as experts. |
| `news` | Tight briefing, clear hand-offs. |
| `deep-dive` | Define the topic, 3–4 angles, implications. |
| `debate` | Opposing stances, then common ground. |
| `story` | Narrative with scene-setting and pay-off. |

Length: the script targets about `minutes × 150` words (minimum 120). The script writer is told to ground every claim in the source material and quote numbers exactly.

### 4.3 Settings and defaults

Stored in `vera/podcast/generated/settings.json`:

| Key | Default |
|---|---|
| `speakers` | Nova + Rex |
| `style` | `conversational` |
| `minutes` | `3` |
| `gap_ms` | `350` |
| `engine` | `""` (TTS server default, Kokoro) |
| `format` | `mp3_if_possible` |
| `show_name` | `Vera Signal` |
| `intro` | `true` (scripted intro and sign-off) |

---

## 5. Generation stages

Podcast generation is a staged media job: resolve topic and supplied context,
author a speaker-structured script, synthesise segments, assemble audio,
persist the artifact, and expose it through list/get. Progress stages are
`gather → script → voice → stitch → done` with a percentage.

Keep script generation and speech synthesis separately inspectable. A good
script can fail at voice lookup or audio assembly, while successful TTS can
faithfully render a poorly attributed script. Use `podcast.script` (or the
composer's **Script preview**) to check attribution before spending TTS time.
Voice settings and segment order are recorded with the episode, so a
regeneration is deterministic enough to diagnose.

---

## 6. Outputs and storage

A finished episode returns `{episode_id, title, duration_s, audio_url, audio_file, script_file, segments, …}`:

| Location | Content |
|---|---|
| `vera/podcast/generated/<id>.wav` / `.mp3`, `<id>.json` | Durable local library; the JSON sidecar holds `episode_id`, title, description, topic, style, requested minutes, duration, speakers, segments (the script), source labels, format, size, audio URL, session and creation time. |
| Session artifact workspace | `<slug>.<fmt>` (audio) and `<slug>.script.md` (readable script). These **are** the deliverables — callers should reference them rather than save again. |
| Object store `podcasts/<id>.<fmt>` | Mirror when the fabric object store is configured; `/podcast/audio/<id>` falls back to it if the local file is missing. |
| Fabric dataset `podcasts.episodes` | Index record, so Dream, Research and other systems can discover episodes. |
| `GET /podcast/audio/{episode_id}` | Raw audio for players. |

The job table is in memory (last 50 jobs) — `podcast.status` does not survive a restart, but episodes do.

---

## 7. Chat composer

In `chat_panel.html`:

- The **Pod** toolbar button toggles `#podcastBar` above the input bar.
- Fields: topic, style, minutes; speaker rows (name, voice dropdown, speed, persona); sources — a *this chat* checkbox (recent messages become a text source), a fabric dataset picker, and URL / note / web-search adders.
- **Script preview** writes the script into an editable textarea (`Name: line` format); **Generate** then uses the edited text verbatim.
- Generation polls `podcast.status` and shows a progress bar; the finished episode becomes a chat card with an `<audio>` player, a transcript toggle and a download link. **Episodes** re-inserts past episodes.
- **Defaults** persists the current speaker and style configuration server-side (`podcast.settings.set`), so agent-initiated generations use the same voices.

---

## 8. Other entry points

| Caller | How |
|---|---|
| Agentic loop / agents | Call `podcast.generate` with `wait=true` and a file or prior output as `sources`. The `podcast-producer` default agent specialises in this. |
| Dream | Add `podcast` to a trigger's `deliver_to`; the report is shaped with the `audio` output format and sent as one text source (`wait=false`). See [Dream §13](./17-dream.md#13-delivery-channels). |
| Re-delivery | `output.redeliver {channel: "podcast"}` turns any existing output into an episode. See [Render](./28-render.md#5-re-delivery-outputredeliver). |

---

## 9. Events

`podcast.progress` — `{job_id, stage, message, pct, …}` — emitted at each stage and mirrored in the in-memory job table read by `podcast.status`.

---

## 10. Worked examples

Generate from a workspace file and wait for the result:

```json
{"name": "podcast.generate", "arguments": {
  "sources": "./research.md", "style": "deep-dive", "minutes": 5, "wait": true}}
```

Two speakers on a fabric dataset, returned as a job to poll:

```bash
curl -s -X POST "$VERA/podcast/generate" -H 'Content-Type: application/json' -d '{
  "topic": "This week on the sensor mesh",
  "sources": [{"type": "dataset", "dataset_id": "mesh.esp32-1.temp", "top_k": 12}],
  "speakers": [{"name": "Nova", "voice": "af_heart"}, {"name": "Rex", "voice": "am_michael"}],
  "style": "news"}'
curl -s "$VERA/podcast/status?job_id=<job_id>"
```

Write the script first, edit it, then voice it:

```json
{"name": "podcast.script", "arguments": {"topic": "Home Assistant 2026.10", "minutes": 3}}
{"name": "podcast.generate", "arguments": {"script": {"segments": [
  {"speaker": "Nova", "text": "Welcome back…"}, {"speaker": "Rex", "text": "…"}]}}}
```

---

## 11. Failure modes and troubleshooting

| Symptom | Check |
|---|---|
| `script generation failed` | The LLM did not return parseable JSON twice; try a shorter source or another model. |
| `TTS returned no audio` / HTTP errors | TTS media node down or voice/engine unknown; check `tts.voices` and [LLM Cluster](./04-ollama-cluster.md) media routing. |
| `channel/width mismatch` while stitching | Segments came back in different channel or sample-width formats (mixed engines); use one engine per episode. |
| Episode is WAV not MP3 | `ffmpeg` not installed. |
| `podcast.status` unknown job | Process restarted (job table is in memory); use `podcast.list`. |
| Speakers wrong or merged | Speaker names in the script must match the speaker list; preview with `podcast.script`. |

Never embed credentials or private source material unless the resulting
persistent audio is authorised to contain it: episodes are stored durably, in
the fabric index and possibly in the object store.

---

## See also

- [Media & Characters](./38-media-characters.md) — TTS/STT, voices
- [Dream](./17-dream.md) — the `podcast` delivery channel
- [Render](./28-render.md) — `output.redeliver`
- [Agents & Chat](./19-agents-chat.md) — the chat surface hosting the composer
- [Data Fabric](./06-data-fabric.md) — `podcasts.episodes` and dataset sources

## Screenshots

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
