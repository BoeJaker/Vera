# 38 · Media, Voice, Characters, and Sprites

This domain covers image generation and transformation, speech recognition and
synthesis, voice pipelines, character definitions, sprite animation, galleries,
and Babblefish language/voice workflows. Rendered documents remain described in
[Render and media](28-render.md); this guide focuses on generative assets and
reusable personas.

## Asset lifecycle

1. Resolve the model/backend and its health.
2. Normalize prompt, source asset, dimensions, and generation settings.
3. Run generation or transformation as a job when it may be long-running.
4. Validate media type, dimensions/duration, and non-empty output.
5. Store the artifact and its provenance.
6. Register it in a gallery, character, animation, or downstream record.

Always retain source/reference identities, model, seed when available, relevant
settings, and the producing capability. A gallery thumbnail is not the source
artifact. Deleting a gallery entry should not accidentally delete an asset that
another character or business record still references.

## Characters and sprites

A character definition owns descriptive traits, voice/presentation settings,
and selected assets. Sprite generation adds a grid/cell contract: frame size,
alignment, direction/state names, timing, and transparent background. Generate
base identity before animation variants, and compare frames for scale/anchor
drift before building a sheet or package.

Model output is proposed media. Review likeness, intellectual-property rights,
unsafe content, text accuracy, and continuity before publication.

## Audio and voice

STT transforms audio into timestamped text; TTS transforms reviewed text into
audio. A duplex or Babblefish pipeline composes listening, language/model work,
translation when configured, and synthesis. Diagnose each stage separately.
Feedback loops, wrong device/sample rate, missing voice, language mismatch, and
GPU contention are distinct failures.

## Source map

- `vera/images/` and `vera/media/` — image/media operations and nodes.
- `vera/character/` — character definitions and assets.
- `vera/spritegen/` — frame, sheet, animation, and package workflows.
- `vera/babblefish/` — voice/language orchestration.
- `vera/render/` — galleries and general artifact presentation.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
