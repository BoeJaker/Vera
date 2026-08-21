# vera-docgen

Thin CLI over Vera's **operator** (`operator.*` / `docs.*` capabilities). The
operator observes→thinks→acts on web UIs; `docgen` is the "singular tool" entry
point for its documentation mission: screenshot every Vera panel and regenerate
the docs.

## Install (one-time)

Screenshots need a real headless browser:

```bash
pip install -r requirements-operator.txt
playwright install chromium
```

## Usage

```bash
# Boot a loop-lab sandbox, screenshot every panel (seeded), write all docs.
# Requires a running orchestrator (it owns evolve.sandbox.*):
python tools/vera-docgen/docgen.py run --sandbox --orchestrator http://localhost:8999

# Drive a live Vera directly, in-process (no orchestrator round-trip):
python tools/vera-docgen/docgen.py run --base-url http://localhost:8999

# Only some domains:
python tools/vera-docgen/docgen.py run --base-url http://localhost:8999 --only markets,dream,operator

# Regenerate docs/tables without taking screenshots:
python tools/vera-docgen/docgen.py run --base-url http://localhost:8999 --no-capture

# Rebuild just the gallery index from the last manifest:
python tools/vera-docgen/docgen.py gallery --orchestrator http://localhost:8999

# Run the unit suite:
python tools/vera-docgen/docgen.py test
```

## Output

The generated visual index is documentation/GALLERY.md. It is replaceable and
is built from the capture manifest. The authored documentation/README.md is
never overwritten, including during selective domain or panel runs.

- `documentation/assets/<domain>/*.png` — panel screenshots (GitHub-relative).
- `documentation/assets/manifest.json` — what was captured.
- `documentation/NN-*.md` — managed auto-blocks (`<!-- VERA:AUTO:… -->`) refreshed
  in place; **authored prose is preserved**.
- `documentation/GALLERY.md` — the generated gallery index.

## Authoring representative captures

Default capture is appropriate when a panel's landing view is already useful.
Workbench panels should declare one or more capture_states in
vera/operator/docs/domain_map.py. A state identifies the subview selector to
click, a visible result selector, optional non-placeholder result text, and any
additional graph/chart settling time.

Prefer evidence that reflects user value. A button, empty canvas, iframe, or
loading shell is not readiness. Keep fixture seeds small, deterministic,
namespaced, and sandbox-only.

After capture, inspect every new image. Confirm the named feature is visible,
text is legible, loading indicators are gone, sensitive data is absent, and the
Markdown link resolves from the guide containing it.

## How it maps to capabilities

| CLI | Capability |
|---|---|
| `docgen run` | `docs.build` → `operator.mission.run("documentation")` |
| `docgen gallery` | `docs.gallery` |
| `docgen test` | `operator.test.run` |

Everything the CLI does is also callable directly (MCP / HTTP / the Operator
Studio panel), because the harness *is* Vera capabilities — usable as one
framework or as singular tools.
