"""Text normalisation for the speech server: written forms become the words a
person would say (British English news and analysis text), and nothing ever
makes synthesis fail."""
import ast
import importlib.util
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
_spec = importlib.util.spec_from_file_location("tts_text_core", os.path.join(ROOT, "edge", "tts_text_core.py"))
tn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tn)

CASES = [
    ("House prices rose 0.2% in August.", "House prices rose nought point two per cent in August."),
    ("Landsec sold a £1.2bn stake", "Landsec sold a one point two billion pound stake"),
    ("JLL raised $45m in Q3 2026.", "J L L raised forty-five million dollars in the third quarter of twenty twenty-six."),
    ("Rates may fall 7-9% by 2026-10-05", "Rates may fall seven to nine per cent by the fifth of October twenty twenty-six"),
    ("on 05/10/2026", "on the fifth of October twenty twenty-six"),
    ("The BoE held at 4.25% on 5 Oct 2026, 25bps above the ECB.",
     "The Bank of England held at four point two five per cent on the fifth of October twenty twenty-six, "
     "twenty-five basis points above the E C B."),
    ("Meeting at 10:50, 09:05 and 6.30pm; doors 9am", "Meeting at ten fifty, nine oh five and six thirty p m; doors nine a m"),
    ("Revenue hit £250,000 vs. £3.50 per share", "Revenue hit two hundred and fifty thousand pounds versus three pounds fifty per share"),
    ("approx. 3x last year", "approximately three times last year"),
    ("In the 1990s and the mid-2020s", "In the nineteen nineties and the mid twenty twenties"),
    ("the 1900s", "the nineteen hundreds"),
    ("FY2025/26 and H1 2026", "the financial year twenty twenty-five to twenty-six and the first half of twenty twenty-six"),
    ("YoY growth of 12,345 units", "year on year growth of twelve thousand three hundred and forty-five units"),
    ("The 22nd attempt, 1st place", "The twenty-second attempt, first place"),
    ("UK HOUSING MARKET NEWS AND UPDATES", "U K Housing Market News And Updates"),
    ("AI, GPU and LLM demand", "A I, G P U and L L M demand"),
    ("NATO and REITs, S&P 500 and FTSE 100", "NATO and reets, S and P five hundred and footsie one hundred"),
    ("5m homes, 300k users, 10+ years, 2024-2026", "five million homes, three hundred thousand users, ten plus years, "
                                                   "twenty twenty-four to twenty twenty-six"),
    ("See https://www.forbes.com/sites/x/2026/these-startups now", "See forbes dot com now"),
    ("temps of 21°C; 50 sq ft; 4.5 GW", "temps of twenty-one degrees Celsius; fifty square feet; four point five gigawatts"),
    ("COVID-19 cases fell -3% in March 2024", "COVID-nineteen cases fell minus three per cent in March twenty twenty-four"),
    ("e.g. the EU fines of €45m", "for example the E U fines of forty-five million euros"),
    ("Q3 '26", "the third quarter of twenty twenty-six"),
    ("£0.50 each", "fifty pence each"),
    ("1 km", "one kilometre"),
]


def test_written_forms_become_spoken_words():
    for written, spoken in CASES:
        assert tn.normalize(written) == spoken, (written, tn.normalize(written))


def test_line_breaks_survive_normalisation():
    assert tn.normalize("Key points\n\n5 homes\n6 flats") == "Key points\n\nfive homes\nsix flats"


def test_the_speech_plan_pauses_like_a_reader():
    plan = tn.speech_plan("U K House Prices\nPrices rose. Did they? Yes!\n\nNext paragraph here")
    texts = [t for t, _ in plan]
    assert texts == ["U K House Prices.", "Prices rose.", "Did they?", "Yes!", "Next paragraph here."]
    pause = dict(plan)
    # a heading is a statement with a line pause; a paragraph break is the longest gap
    assert pause["U K House Prices."] >= tn.PAUSES["line"] * 0.9
    assert pause["Yes!"] >= tn.PAUSES["paragraph"] * 0.9 > pause["Prices rose."]
    assert pause["Did they?"] > pause["Prices rose."] * 0.9 > 0
    # the last piece keeps its pause: the streaming routes synthesise one sentence at a time
    assert plan[-1][1] > 0


def test_the_speech_plan_joins_wrapped_lines_and_cuts_long_sentences():
    assert [t for t, _ in tn.speech_plan("This sentence is hard\nwrapped across lines.")] == \
        ["This sentence is hard wrapped across lines."]
    long = ", ".join(f"clause number {word}" for word in ("one", "two", "three", "four") * 12) + "."
    pieces = tn.speech_plan(long)
    assert len(pieces) > 1 and all(len(t) <= tn.MAX_SEGMENT for t, _ in pieces)
    assert " ".join(t for t, _ in pieces) == long
    assert tn.speech_plan("") == [] and tn.speech_plan("…") == []


def test_number_words():
    assert tn.cardinal(1_234_567) == "one million two hundred and thirty-four thousand five hundred and sixty-seven"
    assert tn.cardinal(1005) == "one thousand and five" and tn.cardinal(-12) == "minus twelve"
    assert tn.ordinal(21) == "twenty-first" and tn.ordinal(12) == "twelfth" and tn.ordinal(100) == "one hundredth"
    assert tn.year(2005) == "two thousand and five" and tn.year(1907) == "nineteen oh seven" and tn.year(2000) == "two thousand"
    assert tn.decimal("0.05") == "nought point nought five".replace("nought five", "zero five")


def test_never_fails_and_leaves_plain_text_alone():
    for odd in ("", "99:99", "31/31/2026", "2026-13-45", "£", "Q9", "12:", "1e10", "x" * 5000, "€" * 10):
        tn.normalize(odd)                           # no exception, whatever comes in
    plain = "The market was quiet, and nobody moved."
    assert tn.normalize(plain) == plain


def test_the_speech_server_normalises_before_synthesis():
    src = open(os.path.join(ROOT, "edge", "GPU_inference.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_tts_synthesize")
    body = ast.get_source_segment(src, fn)
    assert "_tn.normalize_best" in body and body.index("_clean_for_tts") < body.index("normalize_best")
    assert 'os.getenv("TTS_NORMALIZER"' in src and "import tts_text_core as _tn" in src


def test_the_media_server_deploy_ships_the_normaliser():
    src = open(os.path.join(ROOT, "vera", "provisioning", "components_capabilities.py"), encoding="utf-8").read()
    assert '("edge/tts_text_core.py", "tts_text_core.py")' in src


def _gpu_src():
    return open(os.path.join(ROOT, "edge", "GPU_inference.py"), encoding="utf-8").read()


def test_the_accent_follows_the_voice():
    src = _gpu_src()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_voice_lang")
    ns = {"KOKORO_LANG": "en-us"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "x", "exec"), ns)
    vl = ns["_voice_lang"]
    assert vl("bf_emma") == "en-gb" and vl("bm_george") == "en-gb" and vl("af_heart") == "en-us"
    assert vl("bf_emma", "en-us") == "en-us" and vl("") == "en-us"


def test_the_speech_server_takes_its_defaults_from_vera():
    src = _gpu_src()
    assert '@app.get("/tts/config")' in src and '@app.post("/tts/config")' in src
    model = src[src.index("class TTSRequest"):src.index("class TTSConfig")]
    assert "normalize: Optional[bool]" in model and "speed:    Optional[float] = None" in model
    kok = src[src.index("def _synthesize_kokoro"):src.index("def _synthesize_coqui")]
    assert '_TTS_CFG.get("voice")' in kok and "lang=lang" in kok and "_voice_lang(" in kok
    caps = open(os.path.join(ROOT, "vera", "capabilities", "capabilities.py"), encoding="utf-8").read()
    assert '"tts.config.set"' in caps and '"tts.config.get"' in caps


def test_the_speech_server_paces_sentence_by_sentence():
    src = _gpu_src()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_tts_synthesize")
    body = ast.get_source_segment(src, fn)
    # normalise, then plan, then one synthesis per piece with its silence after it
    assert body.index("normalize_best") < body.index("speech_plan") < body.index("_synthesize_piece")
    assert "np.zeros(int(sr * pause * pacing / speed)" in body
    assert '"pacing"' in src[src.index("_TTS_CFG = {"):src.index("_TTS_CFG = {") + 300]
    assert "pacing:" in src[src.index("class TTSRequest"):src.index("class TTSConfig")]
    assert "pacing:" in src[src.index("class TTSConfig"):src.index('@app.get("/tts/config")')]
    caps = open(os.path.join(ROOT, "vera", "capabilities", "capabilities.py"), encoding="utf-8").read()
    setter = caps[caps.index('"tts.config.set"'):]
    assert 'body["pacing"]' in setter[:3000]


def test_headings_and_list_items_end_as_statements():
    src = _gpu_src()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_clean_for_tts")
    ns = {"re": __import__("re"), "_expand_roman_numerals": lambda t: t}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "x", "exec"), ns)
    out = ns["_clean_for_tts"]("## Key points\n- **Mortgage approvals** up\n- 5-year fixes\n1. Third item\nProse line.")
    assert out == "Key points.\nMortgage approvals up.\n5-year fixes.\nThird item.\nProse line.", out


# ── speech markup (SSML subset) ──────────────────────────────────────────────

def test_speech_markup_says_what_the_writer_meant():
    r = tn.resolve_speech_markup
    assert r('Rates of <sub alias="one hundred and eight to three hundred">108-300</sub> bps') == \
        "Rates of one hundred and eight to three hundred bps"
    assert r('The <say-as interpret-as="characters">FOMC</say-as> met') == "The F O M C met"
    assert r('Call <say-as interpret-as="telephone">0800 123</say-as>') == "Call oh eight oh oh, one two three"
    assert r('the <say-as interpret-as="ordinal">3</say-as> time') == "the third time"
    assert r("<sub alias='Bank of England'>BoE</sub> &amp; co") == "Bank of England & co"
    assert r('<phoneme alphabet="ipa" ph="x">Kokoro</phoneme> speaks') == "Kokoro speaks"


def test_speech_markup_never_reaches_the_voice_broken():
    r = tn.resolve_speech_markup
    assert r('An <emphasis level="strong">unknown</emphasis> tag') == "An unknown tag"
    assert r('Left open <sub alias="x">108') == "Left open 108"
    assert r("Cut off at the end <say-as interpret") == "Cut off at the end "
    assert r("maths: a < b and c > d") == "maths: a < b and c > d"       # not markup
    assert r("") == "" and r("plain text") == "plain text"


def test_breaks_become_exact_pauses_in_the_plan():
    t = tn.resolve_speech_markup('Well <break time="800ms"/> maybe. Then <break strength="weak"/> go.')
    assert t.count(tn.PAUSE_MARK) == 8 + 2
    plan = tn.speech_plan(tn.normalize(t))
    assert [p for p, _ in plan] == ["Well", "maybe.", "Then", "go."]
    assert plan[0][1] == 0.8 and plan[2][1] == 0.2


def test_the_written_form_drops_the_markup():
    s = tn.strip_speech_markup
    assert s('Rates of <sub alias="one hundred and eight to three hundred">108-300</sub>.<break time="1s"/>') == \
        "Rates of 108-300."
    assert s('The <say-as interpret-as="characters">FOMC</say-as> met') == "The FOMC met"


def _sentence_helpers():
    src = _gpu_src()
    names = {"_sentence_cuts", "_cut", "_extract_sentences", "_split_sentences"}
    fns = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name in names]
    sent_end = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "_SENT_END")
    ns = {"re": __import__("re"), "_tn": tn}
    flags = __import__("__future__").annotations.compiler_flag    # list[int] on older Pythons
    exec(compile(ast.Module(body=[sent_end] + fns, type_ignores=[]), "x", "exec", flags=flags), ns)
    return ns


def test_streaming_never_cuts_a_sentence_inside_markup():
    ns = _sentence_helpers()
    done, rest = ns["_extract_sentences"]('First one. Then <sub alias="U.S. rates. Up">US')
    assert done == ["First one."] and rest == 'Then <sub alias="U.S. rates. Up">US'
    done, rest = ns["_extract_sentences"](rest + ' rates</sub> rose. Next')
    assert done == ['Then <sub alias="U.S. rates. Up">US rates</sub> rose.'] and rest == "Next"
    done, rest = ns["_extract_sentences"]("Held here. Partial <say-a")
    assert done == ["Held here."] and rest == "Partial <say-a"
    assert ns["_split_sentences"]('A <sub alias="x. y">z</sub> b. C.') == ['A <sub alias="x. y">z</sub> b.', "C."]


def test_the_speech_server_resolves_markup_before_cleaning():
    src = _gpu_src()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_tts_synthesize")
    body = ast.get_source_segment(src, fn)
    assert body.index("resolve_speech_markup") < body.index("_clean_for_tts(")
