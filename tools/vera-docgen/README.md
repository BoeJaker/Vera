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

- `documentation/assets/<domain>/*.png` — panel screenshots (GitHub-relative).
- `documentation/assets/manifest.json` — what was captured.
- `documentation/NN-*.md` — managed auto-blocks (`<!-- VERA:AUTO:… -->`) refreshed
  in place; **authored prose is preserved**.
- `documentation/README.md` — the gallery index.

## How it maps to capabilities

| CLI | Capability |
|---|---|
| `docgen run` | `docs.build` → `operator.mission.run("documentation")` |
| `docgen gallery` | `docs.gallery` |
| `docgen test` | `operator.test.run` |

Everything the CLI does is also callable directly (MCP / HTTP / the Operator
Studio panel), because the harness *is* Vera capabilities — usable as one
framework or as singular tools.
