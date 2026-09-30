"""The v6/v7 runner measures its setup (census 2026-09-30: a silent 30-60 s before the tier pass)."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def test_setup_timing_is_emitted_before_the_tier_pass():
    ev = SRC.index('"type": "agent_loop_v6.setup_timing"')
    assert SRC.index("_setup_t0 = time.monotonic()") < ev
    assert ev < SRC.index("_tier_catalog_brief = ", ev)
    for k in ("artifact_dir_s", "stream_register_s", "models_s"):
        assert '"%s"' % k in SRC[ev:ev + 600]
