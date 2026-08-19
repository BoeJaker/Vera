# 28 · Render — Document Export

> **Doc status:** concise reference for `render/`. Expand as the surface grows.

`render/render_capabilities.py` turns Markdown/text answers into real files — DOCX, PDF, HTML, ODT, PPTX, … — so the standardised output-format profiles in `vera.output_formats` have somewhere to land. Conversion shells out to the `pandoc` binary (installed in the Docker image); pure-text targets (`md`/`txt`) are written directly and need no binary.

| Cap | Purpose |
|---|---|
| `render.formats` | Which target formats are live (gated on `pandoc` / a PDF engine being present) |
| `render.export` | Markdown/text → a file in a target format; returns a download URL |
| `render.dream_export` | Pull a [dream](./17-dream.md) cycle's report and export it |

Generated files are written under `_out/` next to the module and served read-only via `GET /render/download?name=…` (the filename is validated to stay inside the output dir).

`render.dream_export` is the bridge to Dream: a long autonomous review or synthesis can be exported straight to a shareable DOCX/PDF. The `REVIEW_STYLES` / output-format profiles shared with chat and dream (`vera.output_formats`) define what those documents look like.

---

## See also

- [Dream](./17-dream.md) — `render.dream_export` consumes dream reports
- [Agents & Chat](./19-agents-chat.md) — shares the `output_formats` profiles
- [Docker](./13-docker.md) — the image that ships the `pandoc` binary

## Screenshots

## Render pipeline and artifacts

Rendering converts structured Vera output into a presentation artifact. Format
discovery selects a supported renderer; export resolves content and options;
the renderer writes an artifact; gallery registration makes it discoverable.
HTML, Mermaid, charts, reports, and document exports share this lifecycle even
though their engines differ.

Inputs are content, not trusted code. Sanitize HTML, constrain file paths, and
review Mermaid/templating features before rendering model-produced text. A
successful render should return the artifact identity, media type, and size;
do not infer success from a zero exit code if the expected file is absent.

Failures usually fall into missing external binaries/fonts, malformed source,
unsupported format/options, unwritable artifact storage, or browser/PDF engine
startup. Preserve source plus render settings alongside important artifacts so
they can be reproduced.

<!-- VERA:AUTO:screenshots START -->
_No screenshots captured yet — run `docs.build` (or `operator.mission.run documentation`)._
<!-- VERA:AUTO:screenshots END -->

## Capabilities

<!-- VERA:AUTO:capabilities START -->
_No capabilities resolved for this domain._
<!-- VERA:AUTO:capabilities END -->
