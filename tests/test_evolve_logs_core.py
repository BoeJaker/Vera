"""Pure parsing for the Loop Lab sandbox log/perf collector
(vera/evolve/evolve_logs_core.py). No docker/redis/app needed."""

from vera.evolve import evolve_logs_core as core


def test_parse_log_line_with_and_without_timestamp():
    ts, text = core.parse_log_line("2026-08-07T20:11:44.013220Z INFO [vera.orch] ready")
    assert ts == "2026-08-07T20:11:44.013220Z"
    assert text == "INFO [vera.orch] ready"
    # a wrapped traceback line has no leading ts → ts empty, text preserved
    ts2, text2 = core.parse_log_line('    File "x.py", line 10, in f')
    assert ts2 == "" and text2 == '    File "x.py", line 10, in f'


def test_classify_level():
    assert core.classify_level("ERROR [vera.memory] ChromaBackend connect: '_type'") == "error"
    assert core.classify_level("Traceback (most recent call last):") == "error"
    assert core.classify_level("raise ValueError('boom')  # ValueError") == "error"
    assert core.classify_level("WARNING keep_alive low") == "warn"
    assert core.classify_level("INFO seeded agent") == "info"


def test_error_signature_collapses_volatile_bits():
    a = core.error_signature("ERROR trace_id=2dabd836f5b at foo.py:412 addr=0x7ffab12 t=2026-08-07T20:11:44Z")
    b = core.error_signature("ERROR trace_id=99aa11bb2cc at foo.py:88 addr=0x001 t=2026-08-07T21:59:02Z")
    assert a == b, f"same error should collapse: {a!r} != {b!r}"
    # genuinely different errors stay distinct
    assert core.error_signature("ERROR disk full") != core.error_signature("ERROR out of memory")


def test_parse_stats_line():
    r = core.parse_stats_line("vera-dev\t12.5%\t34.2%\t1.5GiB / 8GiB")
    assert r == {"name": "vera-dev", "cpu_pct": 12.5, "mem_pct": 34.2, "mem": "1.5GiB / 8GiB"}
    assert core.parse_stats_line("") is None
    assert core.parse_stats_line("only-one-field") is None
    # unparseable percentages degrade to None, never crash
    bad = core.parse_stats_line("vera-dev\t--\t--")
    assert bad["cpu_pct"] is None and bad["mem_pct"] is None
