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
