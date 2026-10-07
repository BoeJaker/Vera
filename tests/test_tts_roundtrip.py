"""The speech round-trip harness scores what it hears fairly: the same normaliser
on both sides, "and" optional, and silent gaps measured inside the audio only."""
import importlib.util
import io
import os
import wave
from array import array

ROOT = os.path.join(os.path.dirname(__file__), "..")
_spec = importlib.util.spec_from_file_location("roundtrip", os.path.join(ROOT, "tools", "tts-roundtrip", "roundtrip.py"))
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


def _wav(parts, sr=24000):
    pcm = array("h")
    for kind, seconds in parts:
        n = int(sr * seconds)
        pcm.extend([8000 if (i // 20) % 2 else -8000 for i in range(n)] if kind == "tone" else [0] * n)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def test_gaps_are_the_silences_between_speech():
    wav = _wav([("quiet", 0.3), ("tone", 0.5), ("quiet", 0.5), ("tone", 0.4), ("quiet", 0.1), ("tone", 0.3),
                ("quiet", 0.4)])
    assert rt.gaps(wav) == [0.5]                       # leading/trailing silence and a 0.1 s dip do not count


def test_word_error_rate_reads_digits_and_words_alike():
    assert rt.wer(rt.words("one hundred and eight to three hundred"), rt.words("108 to 300")) == 0
    assert rt.wer(rt.words("the F O M C met"), rt.words("the FOMC met")) == 0
    assert rt.wer(rt.words("Arsenal won three nil"), rt.words("Arsenal 1-3-0")) > 0.5


def test_the_corpus_ships_with_the_harness():
    # **/*.jsonl is ignored repo-wide; the corpus is excepted, and every line parses
    import json
    path = os.path.join(ROOT, "tools", "tts-roundtrip", "corpus.jsonl")
    lines = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip() and not x.startswith("#")]
    assert len(lines) >= 20 and all({"id", "text", "expect"} <= set(x) for x in lines)
    assert "!tools/tts-roundtrip/corpus.jsonl" in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
