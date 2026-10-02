# 38 · Media, Voice, Characters, and Sprites

This domain covers Vera's generative and perceptual media: image generation
and transformation (Stable Diffusion on the media node), the image archive,
reference-image search and illustration for chats and reports, machine vision
for text-only agents, speech (TTS/STT), animated **characters** (companions for
agents), and the **spritegen** pixel-art sprite pipeline. Rendered documents and
HTML reports are covered in [Render](./28-render.md), and podcasts in
[Podcast](./31-podcast.md).

| Area | Source | Cap groups |
|---|---|---|
| Media backends (SD, TTS, STT) | `vera/capabilities/capabilities.py` | `image.*`, `sd.*`, `tts.*`, `stt.*` |
| Illustration, image search, vision | `vera/media/media_capabilities.py` | `media.*`, `vision.*` |
| Image archive and Image Studio | `vera/images/image_fabric.py`, `image_studio_panel.html`, `thumbnail_panel.html` | `images.*` |
| Characters / companions | `vera/character/character_capabilities.py`, `character_element.js`, `character_panel.html`, `desktop/vera_companion.py` | `character.*` |
| Sprite pipeline | `vera/spritegen/` | `spritegen.*` |

**Status:** stable but hardware-dependent. Every path probes which tiers are
live (GPU SD endpoint, ControlNet, IP-Adapter, rembg, ESRGAN, vision models,
search backends) and degrades gracefully, returning `{error: …}` rather than
raising. Diffusion can be slow when SD runs on CPU.

## Contents

- [1. Asset lifecycle](#1-asset-lifecycle)
- [2. Media backends](#2-media-backends)
- [3. Illustration, image search and vision](#3-illustration-image-search-and-vision)
- [4. Image archive and Image Studio](#4-image-archive-and-image-studio)
- [5. Characters (companions)](#5-characters-companions)
  - [5.1 Tiers](#51-tiers)
  - [5.2 Capabilities](#52-capabilities)
  - [5.3 `<vera-character>` and the desktop companion](#53-vera-character-and-the-desktop-companion)
- [6. Spritegen — the sprite pipeline](#6-spritegen--the-sprite-pipeline)
  - [6.1 Pipeline and tiers](#61-pipeline-and-tiers)
  - [6.2 Character definition](#62-character-definition)
  - [6.3 Capabilities](#63-capabilities)
  - [6.4 The exported package](#64-the-exported-package)
- [7. Audio and voice](#7-audio-and-voice)
- [8. UI panels and routes](#8-ui-panels-and-routes)
- [9. Events and storage](#9-events-and-storage)
- [10. Worked examples](#10-worked-examples)
- [11. Review, safety and troubleshooting](#11-review-safety-and-troubleshooting)
- [See also](#see-also)

---

## 1. Asset lifecycle

1. Resolve the model/backend and its health (`image.sd_capabilities`, `character.capabilities`, `spritegen.capabilities`, `vision.models`).
2. Normalise the prompt, source asset, dimensions and generation settings.
3. Run generation or transformation, as a job with progress events when it may be long.
4. Validate media type, dimensions or duration, and non-empty output.
5. Store the artifact and its provenance (prompt, model, device, seed, size, source, agent).
6. Register it in a gallery, character, animation or downstream record.

Always retain source/reference identities, model, seed when available,
relevant settings and the producing capability. A gallery thumbnail is not the
source artifact. Deleting a gallery entry should not accidentally delete an
asset that another character or business record still references.

---

## 2. Media backends

These capabilities (in `vera/capabilities/capabilities.py`) call the media node. The target is resolved per service by `media_base()` / `media_slot()` (least-busy media node, falling back to the configured GPU inference URL). See [LLM Cluster](./04-ollama-cluster.md).

| Cap | Route | Purpose |
|---|---|---|
| `image.generate` | `POST /image/generate` | Stable Diffusion txt2img. |
| `image.img2img` | `POST /image/img2img` | img2img (used for consistent frames). |
| `image.expression` | `POST /image/expression` | Expression variants for characters. |
| `image.pose` | `POST /image/pose` | ControlNet pose-guided generation. |
| `image.ipadapter` | `POST /image/ipadapter` | IP-Adapter identity-preserving generation. |
| `image.rembg` | `POST /image/rembg` | Background removal. |
| `image.upscale` | `POST /image/upscale` | ESRGAN upscale. |
| `image.thumbnail` | `POST /image/thumbnail` | Thumbnail generation. |
| `image.sd_capabilities` / `image.progress` | `/image/sd_capabilities`, `/image/progress` | Which SD features are live; generation progress. |
| `sd.loras`, `sd.lora_search`, `sd.lora_install`, `sd.lora_delete`, `sd.lora_store`, `sd.lora_store_delete` | `/sd/…` | LoRA management. |
| `tts.synthesize` / `tts.voices` | `POST /tts/synthesize`, `GET /tts/voices` | Text-to-speech; the voice catalogue (Kokoro). |
| `stt.transcribe` | `POST /stt/transcribe` | Speech-to-text (Whisper). |

Generated images are archived automatically into the image fabric ([§4](#4-image-archive-and-image-studio)).

---

## 3. Illustration, image search and vision

`vera/media/media_capabilities.py` provides three families designed to be called from chat, the agentic loop or a dream pipeline.

| Cap | Route | Purpose |
|---|---|---|
| `media.image.search` | `POST /media/image/search` | **Reference** images that exist (photos, diagrams, product shots). SearXNG image category first (`VERA_SEARXNG_URL`), then DuckDuckGo images; no API key. Inputs: `query`, `limit` (6), `engine` (`auto`/`searxng`/`ddg`), optional `session_id` + `title` to push a gallery into a chat. Output: `{results:[{title, image_url, thumbnail_url, page_url, engine}], count, engine_used, markdown}`. |
| `media.illustrate` | `POST /media/illustrate` | A **demonstrative** image for a concept. `mode` = `generate` (SD via `image.generate`, archived), `search` (web references) or `auto` (generate, fall back to search). Inputs: `subject`, `style`, `count` (search only), `width`/`height` (768), `negative_prompt`, optional `session_id` + `title` to push the image into chat via the `__chat_render__` bridge. Used by `report.html` illustrations. |
| `vision.models` | `GET /vision/models` | Multimodal models across Ollama instances (`{models:[{instance, model}], default}`). |
| `vision.describe` | `POST /vision/describe` | **Eyes for text-only agents**: route an image (`image_b64`, a `data:` URI, an `http(s)` URL or `/images/file/…`) plus a `prompt` (default "Describe this image in detail.") to the best available VL model (qwen-VL, llava, minicpm-v, moondream, llama3.2-vision, …). Output `{text, model, instance, elapsed_ms}`. |

The `visual-analyst` and `image-artist` default agents are built around these caps ([Agents & Chat §3](./19-agents-chat.md#default-agents)).

---

## 4. Image archive and Image Studio

`vera/images/image_fabric.py` persists every Stable Diffusion generation (txt2img and img2img, including character frames) so it can be browsed in the Fabric panel and reused elsewhere. Images are written to `vera/images/_store/` and served as `.png` URLs; a record in the fabric dataset **`images`** carries the URL plus generation metadata (prompt, model, device, seed, size, source, agent). Because the Fabric panel renders any image-URL value as an `<img>`, archived images show up there with no panel change.

| Cap / route | Purpose |
|---|---|
| `images.store` (`POST /images/store`) | Save a base64 PNG and ingest its fabric record. |
| `images.list` (`GET /images/list`) | Recent images, newest first, optionally filtered by agent. |
| `GET /images/file/{name}` | Serve a stored image (read-only; path-checked). |

The **Image Studio** tab (`image-studio`, `tab_order` 18, `/imagestudio/panel`) is the media hub, with panes for **Generate**, **LoRAs**, **Overview**, **Gallery**, **Thumbnails** (`/images/thumbnail_panel`), **Sprites** (`/spritegen/panel`) and **Companion** (`/character/panel`).

---

## 5. Characters (companions)

A **character** embodies an existing agent: a set of generated expression frames plus animation and voice configuration, so the agent can appear as an animated companion instead of an emoji avatar. A character is keyed by `agent_id`, so the agent registry is never modified — any agent can gain or lose a character without a schema change.

Expression states: `neutral`, `talking`, `thinking`, `happy` (default set), plus `working`, `error`, `listening`. `talking` (mouth open) and `neutral` (mouth closed) are the minimum for procedural lip-flap. Each state has a deliberately exaggerated default prompt suffix so states read distinctly.

### 5.1 Tiers

| Layer | Tiers (best → fallback) |
|---|---|
| SD generation | img2img (consistent frames) → seed-locked txt2img |
| Animation (`render_mode`) | `talkinghead` → `spritesheet` (frame-cycle from a generated sheet) → `procedural` (expression-still swap + idle bob/blink + TTS mouth-flap) → `emoji` |

`character.capabilities` probes which tiers are live (via `image.sd_capabilities`).

### 5.2 Capabilities

| Cap | Route | Purpose |
|---|---|---|
| `character.capabilities` | `GET /character/capabilities` | Live SD features, render modes, styles, states. |
| `character.describe` | `POST /character/describe` | **LLM**: freeform description → structured SD prompts. |
| `character.generate` | `POST /character/generate` | Generate expression frames (`agent_id`, `description` or `base_prompt`, `style`, `render_mode`, `states`, `seed`, `steps`, `keep_existing`). Emits `character.generate.*`. Expect roughly 30–60 s per frame on CPU SD. |
| `character.preview` | `POST /character/preview` | Generate one expression without saving. |
| `character.commit_frame` | `POST /character/commit_frame` | Accept a previewed image as a state's frame (`character.frame.committed`). |
| `character.get` / `set` / `list` / `delete` | `/character/…` | Read, update config (animation, voice, render mode) without regenerating, list, delete (record + frames). |
| `character.narrate` | `POST /character/narrate` | **LLM**: recent events → one short, in-persona status line to speak (`events`, `mood`). |
| `character.use_sprite` | `POST /character/use_sprite` | Back a companion with a spritegen character: links every built animation sheet and sets `render_mode=spritesheet` (`character.sprite.linked`). |

### 5.3 `<vera-character>` and the desktop companion

`<vera-character>` (`vera/character/character_element.js`, served at `/ui/elements/character.js`) renders a character in any panel: idle animation, talking while TTS plays, and STT input. The **Companion** tab (`character-studio`, `tab_order` 58) hosts the studio and companion UI at `/character/panel`.

`vera/character/desktop/vera_companion.py` is a small frameless, transparent, always-on-top desktop window (pywebview) that loads the **same** element from a running Vera backend, so animation, TTS/STT and event narration are identical to the web:

```bash
pip install pywebview
python vera/character/desktop/vera_companion.py --base http://localhost:8999 --agent assistant
# options: --size 280, --no-narrate; env VERA_BASE, VERA_COMPANION_AGENT
```

---

## 6. Spritegen — the sprite pipeline

`vera/spritegen/` produces game-ready pixel-art sprites from a text brief or an existing image.

| Module | Responsibility |
|---|---|
| `spritegen_capabilities.py` | `@capability` wrappers, asset route, panels (the only spritegen file in the module list). |
| `pipeline.py` | Stage orchestration with tier probing and fallback. |
| `definition.py` | `CharacterDefinition` store, default animations and visemes. |
| `prompts.py` | Prompt and negative-prompt composition. |
| `providers.py` | Generation provider probing. |
| `pixelize.py`, `palette.py` | Pixelisation and palette reduction (pure PIL). |
| `skeleton.py` | Pose skeletons for ControlNet. |
| `spritesheet.py` | Sheet packing, atlas, GIF preview. |
| `importer.py` | Importing sheets, frame lists and ZIP packages. |
| `package.py` | Building the exportable `Character/` package. |
| `sprite_element.js`, `spritegen_panel.html` | `<vera-sprite>` element (`/ui/elements/sprite.js`) and the Sprite Studio UI. |

### 6.1 Pipeline and tiers

| Stage | Tiers (best → fallback) |
|---|---|
| Base reference | `image.generate`, then `image.rembg` for transparency (chroma-key fallback) |
| Frames | `image.pose` (ControlNet) → `image.ipadapter` → `image.img2img` (seed-locked) → `image.generate` (seed-locked) |
| Post | Pixelize + palette reduction (always) |
| Sheet | Pack frames + atlas JSON + GIF (always) |
| Upscale | `image.upscale` (ESRGAN), else skipped (Lanczos downscale covers sizing) |

Raw hi-res frames are kept so you can re-pixelize at a different size or palette without re-running diffusion. Progress is broadcast as `spritegen.run.*` and `spritegen.stage.*` events.

### 6.2 Character definition

A definition (`CharacterDefinition`) is the pipeline's source of truth: `char_id`, `name`, freeform `brief`, identity fields (`base_prompt`, `base_pose`), style (`style` default `pixel`, `style_prompt`, `framing`), output settings (`sprite_size` default 64 px, `colors` default 32, `outline`, `dither`), and animations. Default animations (frames @ fps):

| Animation | Frames | FPS | Loop |
|---|---|---|---|
| `idle` | 4 | 6 | yes |
| `walk` (side view) | 8 | 10 | yes |
| `run` (side view) | 8 | 14 | yes |
| `attack` | 6 | 14 | no |
| `jump` | 5 | 12 | no |
| `cast` | 6 | 10 | no |
| `talk` | 4 | 8 | yes |
| `hurt` | 3 | 10 | no |
| `death` | 6 | 8 | no |

Visemes for lip-sync: `rest`, `closed`, `wide`, `round`, `teeth`, `smile`.

A sprite adds a grid/cell contract: frame size, alignment, direction and state names, timing and transparent background. Generate the base identity before animation variants, and compare frames for scale and anchor drift before building a sheet or package (`spritegen.align_cells` re-pads every animation onto one shared cell).

### 6.3 Capabilities

| Group | Caps |
|---|---|
| Discovery | `spritegen.capabilities` (live tiers and vocabulary), `spritegen.preview_prompt` (the exact prompts a stage would send) |
| Definitions | `spritegen.describe` (**LLM**: brief → identity fields), `spritegen.define`, `spritegen.get`, `spritegen.list`, `spritegen.set` (config without regenerating), `spritegen.delete` |
| Generation | `spritegen.generate_base`, `spritegen.generate_animation` (`char_id`, `anim`, `frames`, `portrait`), `spritegen.generate_frame` (one frame, for interactive UIs), `spritegen.edit_animation` (img2img edits of existing frames), `spritegen.repixelize` |
| Import & compose | `spritegen.import_sheet`, `spritegen.import_frames`, `spritegen.import_package` (ZIP), `spritegen.from_image` (any image → bg-removed, pixelized frame), `spritegen.splice` (new animation from existing frames) |
| Assembly | `spritegen.align_cells`, `spritegen.build_sheet` (`columns`), `spritegen.build_package` |
| Whole run | `spritegen.run_pipeline` (`char_id`, `animations`, `do_base` false = reuse reference, `do_package`) — slow on CPU SD |

All routes are `POST /spritegen/<name>` except `GET /spritegen/get`, `/spritegen/list` and `/spritegen/capabilities`. Assets are served via `GET /spritegen/asset?char_id=…&path=…`.

### 6.4 The exported package

`spritegen.build_package` assembles and zips an engine-friendly layout (Godot, Unity, GameMaker can combine animations at runtime):

```text
Character/
  definition.yaml       # source-of-truth definition
  reference.png         # bg-removed reference
  sprite/<anim>.png     # sprite sheets
  sprite/<anim>.json    # per-sheet atlas (frame rects + tag)
  preview/<anim>.gif    # animated preview
  visemes/<v>.png       # mouth shapes (companion package)
  metadata.json         # summary manifest
```

---

## 7. Audio and voice

STT transforms audio into timestamped text; TTS transforms reviewed text into
audio. In Vera:

- **Chat** uses STT for the mic and streams TTS sentence by sentence (`agent.chat_voice`, `tts:true` on `/agents/chat/stream`). See [Agents & Chat §14](./19-agents-chat.md#14-voice).
- **Characters** speak through TTS with mouth animation (`procedural` / `spritesheet` modes) and narrate events (`character.narrate`).
- **Podcasts** voice each script segment via the TTS media node ([Podcast](./31-podcast.md)).
- **Dream's Director** can speak proactive thoughts in the chat ([Dream §9](./17-dream.md#9-the-director)).

Diagnose each stage separately. Feedback loops, wrong device or sample rate,
missing voice, language mismatch and GPU contention are distinct failures.

> [!NOTE]
> Earlier versions of this page described **Babblefish** (`vera/babblefish/`) as a voice/language pipeline. It is not: Babblefish is a universal **network-protocol** translator that lets agents speak arbitrary wire protocols through pluggable protocol modules. It is outside this page's scope.

---

## 8. UI panels and routes

| Panel id | Tab / mode | Route |
|---|---|---|
| `image-studio` | Image Studio (tab) | `/imagestudio/panel` (hub with Generate, LoRAs, Overview, Gallery, Thumbnails, Sprites, Companion panes) |
| `character-studio` | Companion (tab) | `/character/panel` |
| `sprite-companion` | inject (dashboard widget; can float or pop out) | `/spritegen/companion` |
| — | Sprite Studio (Image Studio pane) | `/spritegen/panel` |
| — | Thumbnails (Image Studio pane) | `/images/thumbnail_panel` |

Element scripts: `/ui/elements/character.js` (`<vera-character>`), `/ui/elements/sprite.js` (`<vera-sprite>`).

---

## 9. Events and storage

| Store | Location |
|---|---|
| Image archive | `vera/images/_store/` + fabric dataset `images` |
| Characters | Redis `vera:characters:{agent_id}`, fabric dataset `characters` (id `char-{agent_id}`), frames in `vera/character/_chars/{agent_id}/{state}.png` (served via `GET /character/asset`) |
| Spritegen | Redis `vera:spritegen:*`, fabric dataset `spritegen`, assets under `vera/spritegen/_chars/<char_id>/` |

Events: `character.generate.start` / `progress` / `done` / `error`, `character.frame.committed`, `character.sprite.linked`, `spritegen.run.start` / `done` / `error`, `spritegen.stage.progress` / `done`.

---

## 10. Worked examples

Illustrate a concept into the current chat:

```json
{"name": "media.illustrate", "arguments": {
  "subject": "a mesh network of ESP32 sensors in a greenhouse",
  "style": "clean technical diagram", "mode": "auto",
  "session_id": "<chat session>", "title": "Sensor mesh"}}
```

Let a text-only agent read a screenshot:

```json
{"name": "vision.describe", "arguments": {
  "image_url": "/images/file/abc123.png",
  "prompt": "What error message is shown?"}}
```

Give the `assistant` agent an animated companion:

```json
{"name": "character.generate", "arguments": {
  "agent_id": "<assistant agent id>",
  "description": "friendly robot owl with teal accents", "style": "pixel",
  "render_mode": "procedural"}}
```

Build a sprite end to end and link it to the companion:

```json
{"name": "spritegen.define", "arguments": {"name": "Moss Knight",
  "brief": "small knight in mossy armour with a lantern"}}
{"name": "spritegen.run_pipeline", "arguments": {"char_id": "<char_id>",
  "animations": "idle,walk,talk", "do_base": true, "do_package": true}}
{"name": "character.use_sprite", "arguments": {"agent_id": "<agent id>", "char_id": "<char_id>"}}
```

---

## 11. Review, safety and troubleshooting

Model output is proposed media. Review likeness, intellectual-property rights,
unsafe content, text accuracy and continuity before publication.

| Symptom | Check |
|---|---|
| Generation errors or "not available" | `image.sd_capabilities` / `*.capabilities` — the SD endpoint or a feature (ControlNet, IP-Adapter, rembg, ESRGAN) is not live; the pipeline falls back a tier. |
| Very slow frames | SD on CPU (roughly 30–60 s per frame); prefer fewer frames or `spritegen.repixelize` instead of regenerating. |
| Frames drift in identity | Lower tiers (txt2img seed-lock) are least consistent; enable IP-Adapter or ControlNet on the media node. |
| Sprites misaligned across animations | Run `spritegen.align_cells` before `build_sheet`. |
| Transparent background missing | `rembg` unavailable; chroma-key fallback was used. |
| `vision.describe` returns `available: []` | No multimodal model on any Ollama instance (`vision.models`). |
| No image search results | SearXNG unreachable (`VERA_SEARXNG_URL`) and DuckDuckGo blocked. |
| Companion silent | TTS media node down or voice unknown (`tts.voices`). |

---

## See also

- [Render](./28-render.md) — `report.html` illustrations, artifacts gallery
- [Podcast](./31-podcast.md) — multi-voice audio episodes
- [LLM Cluster](./04-ollama-cluster.md) — media node routing, GPU STT/TTS
- [Agents & Chat](./19-agents-chat.md) — voice in chat; visual agents
- [Data Fabric](./06-data-fabric.md) — `images`, `characters`, `spritegen` datasets
- [Flow Builder & UI Elements](./20-flow-builder.md) — the reusable-element pattern

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
