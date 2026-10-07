"""Text normalisation for speech (pure, no dependencies).

Kokoro reads text through espeak's grapheme-to-phoneme rules, which say
"£1.2bn" as "pound one point two b n", "7-9%" as "seven nine percent",
"2026-10-05" digit by digit and "BoE" as a word. TTS front ends solve this with
a text-normalisation (TN) pass that turns written forms into spoken words before
synthesis - the job NVIDIA NeMo's `nemo_text_processing` (WFST grammars over
pynini) does for its own TTS. This module is that pass, written as ordered rules
for British English news and analysis text:

  money       £1.2bn, $45m, €3.50, £250,000, US$2 trillion
  percent     7.5%, 7-9%, -3%, 25bps / basis points
  dates       2026-10-05, 05/10/2026, 5 Oct 2026, October 5th 2026, 5th of May
  times       10:50, 09:05, 14:00, 6pm, 6.30pm
  periods     Q3 2026, H1, FY26, FY2025/26, 1990s, mid-2020s
  numbers     12,000; 0.25; 3x; 1st/22nd; 1,234.5; ranges 2024-2026; 10+
  units       km, kg, mph, GW, MW, kWh, sq ft, m2, °C, GB, TB
  symbols     &, ~, ≈, +, /, →, #, @, %
  short forms e.g., i.e., vs, etc., approx., No. 5, YoY, QoQ, MoM, YTD
  acronyms    spelled out (UK, AI, GPU, CEO) unless they are said as words (NATO,
              REIT, FTSE, NASA) or have a spoken form (BoE, ECB, S&P)
  links       a URL becomes its site ("forbes dot com"); emails are dropped

`normalize(text)` is the only entry point. GPU_inference calls it after it has
stripped markdown, so the rules never see `**` or `[link](url)`. When NeMo's
normaliser is installed and TTS_NORMALIZER=nemo, it runs first and these rules
tidy what it leaves (acronyms, links, the house vocabulary). Line breaks are kept.

`speech_plan(text)` then cuts the normalised text into the pieces a person would
breathe between - sentences, lines, paragraphs, and long sentences at their
clauses - each with the pause that should follow it. Kokoro trims the silence
off what it renders and only pauses where its own batches join, so a heading
ran straight into the line below it and streamed sentences had no gap.
"""
from __future__ import annotations

import re
import zlib
from typing import Callable, List, Optional, Tuple

# ── numbers to words ─────────────────────────────────────────────────────────

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")]
_ORD_IRREG = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
              "nine": "ninth", "twelve": "twelfth"}


def _under_1000(n: int) -> str:
    out: List[str] = []
    h, r = divmod(n, 100)
    if h:
        out.append(f"{_ONES[h]} hundred")
    if r:
        if h:
            out.append("and")
        if r < 20:
            out.append(_ONES[r])
        else:
            t, o = divmod(r, 10)
            out.append(_TENS[t] + (f"-{_ONES[o]}" if o else ""))
    return " ".join(out)


def cardinal(n: int) -> str:
    """12345 -> 'twelve thousand three hundred and forty-five' (British 'and')."""
    if n < 0:
        return "minus " + cardinal(-n)
    if n < 1000:
        return _under_1000(n) if n else "zero"
    out: List[str] = []
    rest = n
    for size, word in _SCALES:
        if rest >= size:
            q, rest = divmod(rest, size)
            out.append(f"{cardinal(q)} {word}")
    if rest:
        out.append(("and " if rest < 100 else "") + _under_1000(rest))
    return " ".join(out)


def ordinal(n: int) -> str:
    """22 -> 'twenty-second'."""
    words = cardinal(n)
    head, sep, last = words.rpartition(" ")
    pre, dash, tail = last.rpartition("-")
    if tail in _ORD_IRREG:
        tail = _ORD_IRREG[tail]
    elif tail.endswith("y"):
        tail = tail[:-1] + "ieth"
    else:
        tail += "th"
    last = f"{pre}{dash}{tail}" if dash else tail
    return f"{head}{sep}{last}" if sep else last


def year(n: int) -> str:
    """2026 -> 'twenty twenty-six', 2005 -> 'two thousand and five', 1990 -> 'nineteen ninety'."""
    if 2000 <= n <= 2009:
        return "two thousand" + (f" and {_ONES[n - 2000]}" if n > 2000 else "")
    if 1100 <= n <= 2099 and n % 100:
        hi, lo = divmod(n, 100)
        return f"{cardinal(hi)} {'oh ' + _ONES[lo] if lo < 10 else cardinal(lo)}"
    if 1100 <= n <= 2099:
        return f"{cardinal(n // 100)} hundred"
    return cardinal(n)


def decimal(s: str) -> str:
    """'1.25' -> 'one point two five'; '0.5' -> 'nought point five'."""
    s = s.replace(",", "")
    neg = s.startswith("-")
    s = s.lstrip("-")
    whole, _, frac = s.partition(".")
    w = cardinal(int(whole)) if whole else "nought"
    if whole in ("0", ""):
        w = "nought"
    out = w + (" point " + " ".join(_ONES[int(d)] for d in frac) if frac else "")
    return ("minus " if neg else "") + out


def number(s: str) -> str:
    s = s.replace(",", "")
    return decimal(s) if "." in s else cardinal(int(s))


# ── vocabulary ───────────────────────────────────────────────────────────────

MONTHS = {m[:3].lower(): m for m in ("January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December")}
MONTHS["sept"] = "September"
_MONTH_RE = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

CURRENCY = {"£": ("pound", "pounds", "penny", "pence"), "$": ("dollar", "dollars", "cent", "cents"),
            "€": ("euro", "euros", "cent", "cents"), "¥": ("yen", "yen", "", ""), "₹": ("rupee", "rupees", "", "")}
_MULT = {"k": "thousand", "K": "thousand", "m": "million", "M": "million", "mn": "million", "bn": "billion",
         "b": "billion", "B": "billion", "tn": "trillion", "trn": "trillion", "T": "trillion",
         "thousand": "thousand", "million": "million", "billion": "billion", "trillion": "trillion"}

UNITS = {"km": "kilometres", "kg": "kilograms", "mph": "miles per hour", "kph": "kilometres per hour",
         "gw": "gigawatts", "mw": "megawatts", "kw": "kilowatts", "kwh": "kilowatt hours", "mwh": "megawatt hours",
         "gwh": "gigawatt hours", "sq ft": "square feet", "sqft": "square feet", "m2": "square metres",
         "m²": "square metres", "km2": "square kilometres", "km²": "square kilometres", "ha": "hectares",
         "gb": "gigabytes", "tb": "terabytes", "mb": "megabytes", "ghz": "gigahertz", "mhz": "megahertz",
         "ms": "milliseconds", "°c": "degrees Celsius", "°f": "degrees Fahrenheit", "bps": "basis points",
         "bp": "basis points", "tok/s": "tokens per second", "rpm": "revolutions per minute"}

# Letters people say as letters, even inside a SHOUTED HEADLINE.
ACRONYMS = {"UK", "US", "EU", "UN", "AI", "ML", "GPU", "CPU", "LLM", "API", "CEO", "CFO", "CTO", "COO", "GDP", "CPI",
            "IPO", "ETF", "VAT", "NHS", "BBC", "ITV", "FCA", "PRA", "ONS", "OBR", "IMF", "WHO", "WTO", "USA", "UAE",
            "IT", "HR", "PR", "TV", "PC", "VR", "AR", "IOT", "SME", "RICS", "CRE", "ESG", "KPI", "ROI", "SDK",
            "MCP", "RAG", "NLP", "OCR", "IDP", "HMRC", "DWP", "NASA", "NATO", "REIT", "FTSE", "ECB"}

# Said as one word - left alone.
WORDS = {"NATO", "NASA", "REIT", "REITS", "FTSE", "UNESCO", "OPEC", "ASEAN", "NASDAQ", "SWIFT", "COVID", "ISIS",
         "LIDAR", "RADAR", "LASER", "SCUBA", "GIF", "JPEG", "PIN", "OFCOM", "OFGEM", "OFWAT", "OFSTED", "UNICEF",
         "NIMBY", "SAAS", "PROPTECH", "ISA", "AIM", "DAX", "CAC", "NICE", "FOMO", "WAN", "LAN", "RAM", "ROM",
         "SIM", "SPAC", "ESMA", "IFRS", "GAAP", "ACAS", "HMRC"}
WORDS -= {"HMRC"}                                     # said as letters
# Spoken forms that are neither letters nor the written word.
SPOKEN = {"BoE": "the Bank of England", "ECB": "the E C B", "S&P": "S and P",
          "FTSE": "footsie", "REIT": "reet", "REITs": "reets", "SaaS": "sass", "PropTech": "prop tech",
          "proptech": "prop tech", "FinTech": "fin tech", "fintech": "fin tech", "LLMs": "L L Ms", "GPUs": "G P Us",
          "APIs": "A P Is", "CEOs": "C E Os", "AIs": "A Is", "YoY": "year on year", "QoQ": "quarter on quarter",
          "MoM": "month on month", "YTD": "year to date", "WoW": "week on week", "ESG": "E S G", "M&A": "M and A",
          "R&D": "R and D", "Q&A": "Q and A", "B2B": "B to B", "B2C": "B to C", "P2P": "peer to peer",
          "MoE": "mixture of experts", "GenAI": "gen A I", "OpenAI": "open A I", "macOS": "mac O S", "iOS": "i O S",
          "GitHub": "git hub", "PwC": "P W C", "EY": "E Y", "KPMG": "K P M G", "JLL": "J L L", "CBRE": "C B R E"}
ABBREV = [(r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "et cetera"),
          (r"\bvs\.?(?=\s)", "versus"), (r"\bv\.(?=\s)", "versus"), (r"\bapprox\.?(?=\s)", "approximately"),
          (r"\bca\.(?=\s*\d)", "about"), (r"\bc\.(?=\s*\d)", "about"), (r"\bNo\.(?=\s*\d)", "number"),
          (r"\bno\.(?=\s*\d)", "number"), (r"\bst\.(?=\s+[A-Z])", "Saint"), (r"\bDr\.(?=\s)", "Doctor"),
          (r"\bMr\.?(?=\s)", "Mister"), (r"\bMrs\.?(?=\s)", "Missus"), (r"\bMs\.?(?=\s)", "Ms"),
          (r"\bProf\.(?=\s)", "Professor"), (r"\bInc\.", "Inc"), (r"\bLtd\.", "limited"), (r"\bplc\b", "P L C"),
          (r"\bPLC\b", "P L C"), (r"\bpa\b", "a year"), (r"\bp\.a\.", "a year"), (r"\bmin\.(?=\s)", "minimum"),
          (r"\bmax\.(?=\s)", "maximum"), (r"\best\.(?=\s)", "estimated"), (r"\bdept\.(?=\s)", "department"),
          (r"\bGovt\b", "government"), (r"\bgovt\b", "government"), (r"\bw/o\b", "without"), (r"\bw/(?=\s)", "with")]


# ── the rules, in order ──────────────────────────────────────────────────────

Rule = Tuple[str, Callable[["re.Match[str]"], str]]


def _money(m: "re.Match[str]") -> str:
    sym, amt, mult = m.group("sym"), m.group("amt"), m.group("mult") or ""
    one, many, sub1, subn = CURRENCY.get(sym[-1], ("", "", "", ""))
    prefix = "U S " if sym.upper().startswith("US") else ""
    clean = amt.replace(",", "")
    if mult:
        return f"{number(clean)} {_MULT.get(mult, mult)} {prefix}{many}".strip()
    whole, _, frac = clean.partition(".")
    w = int(whole or 0)
    out = f"{cardinal(w)} {prefix}{one if w == 1 else many}"
    if frac and sub1 and int(frac[:2].ljust(2, "0")):
        p = int(frac[:2].ljust(2, "0"))
        out += f" {cardinal(p)}" if w else ""
        if not w:
            out = f"{cardinal(p)} {sub1 if p == 1 else subn}"
    return out


def _pct(m: "re.Match[str]") -> str:
    a, b = m.group("a"), m.group("b")
    if b:
        return f"{number(a)} to {number(b)} per cent"
    return f"{decimal(a) if '.' in a else cardinal(int(a.replace(',', '')))} per cent"


def _iso_date(m: "re.Match[str]") -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"the {ordinal(d)} of {list(MONTHS.values())[mo - 1]} {year(y)}"


def _uk_date(m: "re.Match[str]") -> str:
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    yy = int(y) + (2000 if len(y) == 2 else 0)
    return f"the {ordinal(d)} of {list(MONTHS.values())[mo - 1]} {year(yy)}"


def _month_name(tok: str) -> str:
    return MONTHS.get(tok.lower()[:4] if tok.lower().startswith("sept") else tok.lower()[:3], tok)


def _day_month(m: "re.Match[str]") -> str:
    d, mon, y = int(m.group("d")), _month_name(m.group("mon")), m.group("y")
    return f"the {ordinal(d)} of {mon}" + (f" {year(int(y))}" if y else "")


def _month_day(m: "re.Match[str]") -> str:
    d, mon, y = int(m.group("d")), _month_name(m.group("mon")), m.group("y")
    return f"{mon} the {ordinal(d)}" + (f", {year(int(y))}" if y else "")


def _month_year(m: "re.Match[str]") -> str:
    return f"{_month_name(m.group('mon'))} {year(int(m.group('y')))}"


def _time(m: "re.Match[str]") -> str:
    g = m.groupdict()
    h, mi, ap = int(g["h"]), g.get("m") or "", (g.get("ap") or "").lower().replace(".", "")
    if h > 23 or (mi and int(mi) > 59):
        return m.group(0)
    words = cardinal(h if (ap or h <= 12) else h - 12) if h else "twelve"
    if mi and int(mi):
        words += " " + ("oh " + _ONES[int(mi)] if int(mi) < 10 else cardinal(int(mi)))
    elif not ap:
        words += " o'clock" if h <= 12 else ""
    if ap:
        words += " a m" if ap.startswith("a") else " p m"
    elif h > 12:
        words += " p m"
    return words


def _quarter(m: "re.Match[str]") -> str:
    q, y = int(m.group("q")), (m.group("y") or "").lstrip("'")
    out = f"the {ordinal(q)} quarter"
    if y:
        yy = int(y) + (2000 if len(y) == 2 else 0)
        out += f" of {year(yy)}"
    return out


def _half(m: "re.Match[str]") -> str:
    h, y = int(m.group("h")), m.group("y")
    out = f"the {'first' if h == 1 else 'second'} half"
    if y:
        out += f" of {year(int(y) + (2000 if len(y) == 2 else 0))}"
    return out


def _fy(m: "re.Match[str]") -> str:
    a, b = m.group("a"), m.group("b")
    ya = int(a) + (2000 if len(a) == 2 else 0)
    if b:
        yb = int(b) + (2000 if len(b) == 2 else 0)
        return f"the financial year {year(ya)} to {cardinal(yb % 100) if len(b) == 2 else year(yb)}"
    return f"financial year {year(ya)}"


def _decade(m: "re.Match[str]") -> str:
    """1990s -> 'the nineteen nineties', 1900s -> 'the nineteen hundreds', mid-2020s -> 'the mid twenty twenties'."""
    pre, y = (m.group("pre") or "").lower(), int(m.group("y"))
    if y % 100 == 0:
        w = f"{cardinal(y // 100)} hundreds"
    else:
        w = year(y)
        w = w[:-1] + "ies" if w.endswith("y") else w + "s"
    return f"the {pre + ' ' if pre else ''}{w}"


def _range(m: "re.Match[str]") -> str:
    a, b = m.group("a"), m.group("b")
    if len(a) == 4 and len(b) == 4 and 1800 <= int(a) <= 2100 and 1800 <= int(b) <= 2100:
        return f"{year(int(a))} to {year(int(b))}"
    return f"{number(a)} to {number(b)}"


def _times(m: "re.Match[str]") -> str:
    return f"{number(m.group('n'))} times"


def _ord(m: "re.Match[str]") -> str:
    return ordinal(int(m.group("n")))


def _unit(m: "re.Match[str]") -> str:
    n, u = m.group("n"), m.group("u")
    words = UNITS.get(u.lower(), u)
    if number(n) == "one" and words.endswith("s") and u.lower() not in ("bps", "bp"):
        words = words[:-1]
    return f"{number(n)} {words}"


def _bare_number(m: "re.Match[str]") -> str:
    s = m.group(0)
    clean = s.replace(",", "")
    if "." not in clean and len(clean) == 4 and 1100 <= int(clean) <= 2099 and "," not in s:
        return year(int(clean))
    return number(s)


def _url(m: "re.Match[str]") -> str:
    host = re.sub(r"^www\.", "", m.group("host").lower())
    return host.replace(".", " dot ")


_ACR_RE = re.compile(r"\b([A-Z]{2,6})(s?)\b")


def _acronym(m: "re.Match[str]") -> str:
    w, plural = m.group(1), m.group(2)
    if w in WORDS:
        return m.group(0)
    return " ".join(w) + ("s" if plural else "")


RULES: List[Rule] = [
    # links and addresses first, so their digits are not read
    (r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", lambda m: ""),
    (r"\bhttps?://(?:www\.)?(?P<host>[^/\s)]+)[^\s)]*", _url),
    (r"\bwww\.(?P<host>[^/\s)]+)[^\s)]*", _url),
    # money: £1.2bn, US$2 trillion, $45m, €3.50
    (r"(?P<sym>(?:US|A|C|HK)?[£$€¥₹])\s?(?P<amt>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<mult>bn|trn|tn|mn|k|K|m|M|b|B|T|thousand|million|billion|trillion)\b)?", _money),
    # percentages and basis points, with ranges
    (r"(?P<a>-?\d[\d,]*(?:\.\d+)?)\s?(?:-|–|—|to)\s?(?P<b>\d[\d,]*(?:\.\d+)?)\s?%", _pct),
    (r"(?P<a>-?\d[\d,]*(?:\.\d+)?)(?P<b>)\s?%", _pct),
    # dates
    (r"\b(\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+Z?)?\b", _iso_date),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", _uk_date),
    (r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s(?:of\s)?(?P<mon>" + _MONTH_RE + r")\.?(?:,?\s(?P<y>\d{4}))?\b", _day_month),
    (r"\b(?P<mon>" + _MONTH_RE + r")\.?\s(?P<d>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s(?P<y>\d{4}))?", _month_day),
    (r"\b(?P<mon>" + _MONTH_RE + r")\.?\s(?P<y>\d{4})\b", _month_year),
    # periods
    (r"\bQ(?P<q>[1-4])(?:\s?(?P<y>\d{4}|'\d{2}))?\b", _quarter),
    (r"\bH(?P<h>[12])(?:\s(?P<y>\d{4}))?\b", _half),
    (r"\bFY\s?(?P<a>\d{4}|\d{2})(?:/(?P<b>\d{4}|\d{2}))?\b", _fy),
    (r"\b(?P<pre>[Mm]id|[Ee]arly|[Ll]ate)?-?(?P<y>1[89]\d0|20\d0)s\b", _decade),
    # times
    (r"\b(?P<h>\d{1,2})[:.](?P<m>\d{2})\s?(?P<ap>[ap]\.?m\.?)(?=\W|$)", _time),
    (r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b", _time),
    (r"\b(?P<h>\d{1,2})(?P<m>)\s?(?P<ap>[ap]\.?m\.?)(?=\W|$)", _time),
    # units, multipliers, ordinals, ranges
    (r"\b(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<u>sq\s?ft|kWh|MWh|GWh|km²|km2|m²|m2|tok/s|bps|bp|km|kg|mph|kph|GW|MW|kW|GB|TB|MB|GHz|MHz|ms|°C|°F|ha|rpm)(?![\w])", _unit),
    (r"\b(?P<n>\d+(?:\.\d+)?)[x×]\b", _times),
    # 5m homes, 300k users, 1.2bn: a scale with no currency
    (r"\b(?P<n>\d[\d,]*(?:\.\d+)?)\s?(?P<mult>bn|trn|tn|mn|k|m)\b", lambda m: f"{number(m.group('n'))} {_MULT[m.group('mult')]}"),
    (r"\b(?P<n>\d+)(?:st|nd|rd|th)\b", _ord),
    (r"\b(?P<a>\d[\d,]*(?:\.\d+)?)\s?(?:–|—|-)\s?(?P<b>\d[\d,]*(?:\.\d+)?)\b", _range),
    (r"\b(?P<n>\d[\d,]*)\+", lambda m: f"{number(m.group('n'))} plus"),
    # what is left: plain numbers
    (r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])-?\d+(?:\.\d+)?(?![\w])", _bare_number),
]
_COMPILED = [(re.compile(p), f) for p, f in RULES]

SYMBOLS = [("&", " and "), ("≈", " about "), ("~", " about "), ("→", " to "), ("->", " to "), ("<=", " at most "),
           (">=", " at least "), ("±", " plus or minus "), ("#", " number "), ("/", " or "), ("@", " at "),
           ("§", " section "), ("°", " degrees "), ("½", " a half"), ("¼", " a quarter"), ("¾", " three quarters")]


def _safe(fn: Callable[["re.Match[str]"], str]) -> Callable[["re.Match[str]"], str]:
    """A rule that fails on odd input leaves that piece as written - speech must never fail on a number."""
    def run(m: "re.Match[str]") -> str:
        try:
            return fn(m)
        except Exception:  # noqa: BLE001
            return m.group(0)
    return run


def normalize(text: str) -> str:
    """Written text -> text a TTS voice reads naturally (British English).

    Line by line, and the breaks are kept: speech_plan pauses on them."""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out = "\n".join(_normalize_line(line) for line in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _normalize_line(t: str) -> str:
    # SHOUTED HEADLINES (three or more capitalised words in a row) are words, not acronyms
    t = re.sub(r"\b[A-Z]{2,}(?:[ \t,:\-–]+[A-Z]{2,}){2,}\b",
               lambda m: " ".join(w if (w in SPOKEN or w in WORDS or w.strip(",:") in ACRONYMS)
                                  else w.capitalize() for w in m.group(0).split(" ")), t)
    for k, v in sorted(SPOKEN.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(r"(?<![\w&])" + re.escape(k) + r"(?![\w&])", v, t)
    for pat, rep in ABBREV:
        t = re.sub(pat, rep, t)
    for rx, fn in _COMPILED:
        t = rx.sub(_safe(fn), t)
    for a, b in SYMBOLS:
        t = t.replace(a, b)
    t = _ACR_RE.sub(_acronym, t)
    # an amount used as an adjective: "a £1.2bn stake" -> "a one point two billion pound stake"
    t = re.sub(r"\b([Aa]n?) ((?:[a-z-]+ )*?(?:thousand|million|billion|trillion) )(pound|dollar|euro)s\b", r"\1 \2\3", t)
    t = re.sub(r"\b([Tt]he)\s+the\b", r"\1", t)          # "the BoE" -> "the the Bank of England"
    t = re.sub(r"[ \t]+([,.;:!?])", r"\1", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


_NEMO: List[Optional[object]] = []


def normalize_best(text: str, prefer: str = "rules") -> str:
    """NeMo's WFST normaliser first when asked for and installed (pynini), then
    these rules for what it does not cover; the rules alone otherwise."""
    if prefer == "nemo":
        if not _NEMO:
            try:
                from nemo_text_processing.text_normalization.normalize import Normalizer  # type: ignore
                _NEMO.append(Normalizer(input_case="cased", lang="en"))
            except Exception:  # noqa: BLE001 - not installed, or its grammars failed to build
                _NEMO.append(None)
        nm = _NEMO[0]
        if nm is not None:
            try:
                text = nm.normalize(text, verbose=False, punct_post_process=True)
            except Exception:  # noqa: BLE001
                pass
    return normalize(text)


# ── pacing ───────────────────────────────────────────────────────────────────

# Seconds of silence after each kind of break, at speed 1.0, on top of the ~0.1 s
# quiet tail Kokoro leaves on a piece. Read British English pauses around 0.5 s
# between sentences and near double that between paragraphs; a clause break inside
# a sentence is a short catch of breath. (Measured on cpu-246, bf_emma: a sentence
# gap lands at 0.45-0.55 s, a paragraph at ~0.85 s.)
PAUSES = {"comma": 0.12, "clause": 0.2, "colon": 0.28, "sentence": 0.4, "exclaim": 0.42, "question": 0.48,
          "ellipsis": 0.55, "line": 0.48, "paragraph": 0.78}
# A sentence longer than this is cut at its clauses: one breath, and well inside
# the 510 phonemes Kokoro renders in one pass.
MAX_SEGMENT = 240
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s+[\"'(\[]?[A-Z0-9]|\s*$)")


def _sentences(line: str) -> List[str]:
    out, start = [], 0
    for m in _SENT_END.finditer(line):
        out.append(line[start:m.end()].strip())
        start = m.end()
    rest = line[start:].strip()
    if rest:
        out.append(rest)
    return [s for s in out if s]


def _clauses(sentence: str, limit: int = MAX_SEGMENT) -> List[str]:
    """A long sentence in breath-sized pieces, cut after ; : , - never mid-clause."""
    if len(sentence) <= limit:
        return [sentence]
    pieces = [p for p in re.split(r"(?<=[;:,])\s+", sentence) if p]
    out: List[str] = []
    for p in pieces:
        if out and len(out[-1]) + 1 + len(p) <= limit and not out[-1].endswith((";", ":")):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def _end_pause(seg: str) -> float:
    tail = seg.rstrip("\"')] ")
    if tail.endswith("…") or tail.endswith("..."):
        return PAUSES["ellipsis"]
    if tail.endswith("?"):
        return PAUSES["question"]
    if tail.endswith("!"):
        return PAUSES["exclaim"]
    if tail.endswith("."):
        return PAUSES["sentence"]
    if tail.endswith(":"):
        return PAUSES["colon"]
    if tail.endswith(";"):
        return PAUSES["clause"]
    if tail.endswith(","):
        return PAUSES["comma"]
    return 0.0


def _vary(seg: str, pause: float) -> float:
    """People are not metronomes: +-8%, fixed per piece of text so a replay sounds the same."""
    if pause <= 0:
        return 0.0
    k = (zlib.crc32(seg.encode("utf-8")) % 1000) / 1000.0
    return round(pause * (0.92 + 0.16 * k), 3)


def speech_plan(text: str) -> List[Tuple[str, float]]:
    """Normalised text -> [(piece to synthesise, seconds of silence after it)].

    One piece per sentence (long ones cut at clauses); a line with no closing
    punctuation - a heading, a list item - gets a full stop so it is said as a
    statement and is followed by a line pause; paragraphs get a longer one. The
    last piece keeps its own pause, so pieces synthesised one at a time (the
    streaming routes) still have a gap between them.
    """
    plan: List[Tuple[str, float]] = []
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", text or "") if p.strip()]
    for pi, para in enumerate(paragraphs):
        lines: List[str] = []
        for ln in (x.strip() for x in para.split("\n")):
            if not ln:
                continue
            # a hard-wrapped sentence carries on in lower case: one line, not two
            if lines and ln[0].islower() and not re.search(r"[.!?…:;][\"')\]]*$", lines[-1]):
                lines[-1] += " " + ln
            else:
                lines.append(ln)
        for li, line in enumerate(lines):
            if not re.search(r"[.!?…:;,][\"')\]]*$", line):
                line += "."
            for sentence in _sentences(line):
                for clause in _clauses(sentence):
                    if not re.search(r"\w", clause):          # a stray mark: its pause goes to what came before
                        if plan:
                            plan[-1] = (plan[-1][0], max(plan[-1][1], _end_pause(clause)))
                        continue
                    plan.append((clause, _end_pause(clause)))
            if plan and li < len(lines) - 1:
                plan[-1] = (plan[-1][0], max(plan[-1][1], PAUSES["line"]))
        if plan and pi < len(paragraphs) - 1:
            plan[-1] = (plan[-1][0], max(plan[-1][1], PAUSES["paragraph"]))
    return [(seg, _vary(seg, pause)) for seg, pause in plan]
