"""Text normalisation for speech (pure, no dependencies).

Kokoro reads text through espeak's grapheme-to-phoneme rules, which say
"£1.2bn" as "pound one point two b n", "7-9%" as "seven nine percent",
"2026-10-05" digit by digit and "BoE" as a word. TTS front ends solve this with
a text-normalisation (TN) pass that turns written forms into spoken words before
synthesis - the job NVIDIA NeMo's `nemo_text_processing` (WFST grammars over
pynini) does for its own TTS. This module is that pass, written as ordered rules
for British English news and analysis text:

  money       £1.2bn, $45m, €3.50, £250,000, US$2 trillion, 50p, GBP 100
  ranges      108-300, £5-10m, $1.2-1.5bn, 3-4kg, 2-3pp, 7-9%, 2020-21, 9am-5pm,
              12:30-14:00, 1-3 June, June 1-3, Jan-Mar, Mon-Fri, Q1-Q3, 1st-2nd;
              any dash (- ‐ ‑ – — −) and "to"; a unit or scale said once, at the end
  percent     7.5%, -3%, +2%, 25bps / basis points, 2pp / percentage points
  dates       2026-10-05, 05/10/2026, 5 Oct 2026, October 5th 2026, 5th of May
  times       10:50, 09:05, 14:00, 6pm, 6.30pm
  periods     Q3 2026, H1, FY26, FY2025/26, 2024/25, 1990s, 1980's, the 80s, mid-2020s
  numbers     12,000; 0.25; 3x; 1st/22nd; 10+; 100s; years only where a year is meant
              ("in 1500" vs "1500 people"); scores (won 3-0 -> three nil)
  fractions   1/2, 3/4, 2½, 24/7, 50/50, 8/10 (out of)
  units       km, kg, mph, GW, MWh, sq ft, m², °C, GB, ms, hrs, mins, lbs, ft, 10in
  codes       phone numbers digit by digit, version numbers (3.11.2), IP addresses
  symbols     &, ~, ≈, +, /, →, #, @, %
  short forms e.g., i.e., vs, etc., approx., pp., fig., No. 5, YoY, QoQ, MoM, YTD
  acronyms    spelled out (UK, AI, GPU, CEO, U.S.) unless they are said as words
              (NATO, FIFA, a SHOUTED word) or have a spoken form (BoE, ECB, S&P) -
              the house vocabulary is data, in tts_lexicon.json beside this file
  markup      <sub alias>, <say-as>, <break> from a writer who knows what it meant
              (resolve_speech_markup, before everything else)
  links       a URL becomes its site ("forbes dot com"); emails are dropped
  pauses      a prose dash or a bracketed aside becomes a comma, so the voice pauses

`normalize(text)` is the entry point; it keeps line breaks. GPU_inference calls
it after it has stripped markdown, so the rules never see `**` or `[link](url)`.
When NeMo's normaliser is installed and TTS_NORMALIZER=nemo, it runs first and
these rules tidy what it leaves (acronyms, links, the house vocabulary).

`speech_plan(text)` then cuts the normalised text into the pieces a person would
breathe between - sentences, lines, paragraphs, and long sentences at their
clauses - each with the pause that should follow it. Kokoro trims the silence
off everything it renders and only pauses where its own batches join, so without
this a paragraph comes out as one breathless run and a heading runs straight
into the line below it.
"""
from __future__ import annotations

import json
import os
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


def _pairs(n: int) -> str:
    """1080 -> 'ten eighty', 1920 -> 'nineteen twenty': how a screen size or a model number is said."""
    hi, lo = divmod(n, 100)
    if not 10 <= hi <= 99:
        return cardinal(n)
    if not lo:
        return f"{cardinal(hi)} hundred"
    return f"{cardinal(hi)} {'oh ' + _ONES[lo] if lo < 10 else cardinal(lo)}"


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


def _digits(s: str) -> str:
    """'0800' -> 'oh eight oh oh': how a phone number or an address is read."""
    return " ".join("oh" if d == "0" else _ONES[int(d)] for d in s if d.isdigit())


# ── vocabulary ───────────────────────────────────────────────────────────────

MONTHS = {m[:3].lower(): m for m in ("January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December")}
MONTHS["sept"] = "September"
_MONTH_RE = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
        "sat": "Saturday", "sun": "Sunday"}
_DAY_RE = r"(?:Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"

CURRENCY = {"£": ("pound", "pounds", "penny", "pence"), "$": ("dollar", "dollars", "cent", "cents"),
            "€": ("euro", "euros", "cent", "cents"), "¥": ("yen", "yen", "", ""), "₹": ("rupee", "rupees", "", "")}
_CODES = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "INR": "₹"}
_MULT = {"k": "thousand", "K": "thousand", "m": "million", "M": "million", "mn": "million", "bn": "billion",
         "b": "billion", "B": "billion", "tn": "trillion", "trn": "trillion", "T": "trillion",
         "thousand": "thousand", "million": "million", "billion": "billion", "trillion": "trillion"}

# unit -> (singular, plural)
UNITS = {"km": ("kilometre", "kilometres"), "kg": ("kilogram", "kilograms"), "mph": ("mile per hour", "miles per hour"),
         "kph": ("kilometre per hour", "kilometres per hour"), "km/h": ("kilometre per hour", "kilometres per hour"),
         "gw": ("gigawatt", "gigawatts"), "mw": ("megawatt", "megawatts"), "kw": ("kilowatt", "kilowatts"),
         "kwh": ("kilowatt hour", "kilowatt hours"), "mwh": ("megawatt hour", "megawatt hours"),
         "gwh": ("gigawatt hour", "gigawatt hours"), "sq ft": ("square foot", "square feet"),
         "sqft": ("square foot", "square feet"), "m2": ("square metre", "square metres"),
         "m²": ("square metre", "square metres"), "km2": ("square kilometre", "square kilometres"),
         "km²": ("square kilometre", "square kilometres"), "ha": ("hectare", "hectares"),
         "gb": ("gigabyte", "gigabytes"), "tb": ("terabyte", "terabytes"), "mb": ("megabyte", "megabytes"),
         "kb": ("kilobyte", "kilobytes"), "pb": ("petabyte", "petabytes"),
         "ghz": ("gigahertz", "gigahertz"), "mhz": ("megahertz", "megahertz"), "khz": ("kilohertz", "kilohertz"),
         "hz": ("hertz", "hertz"), "ms": ("millisecond", "milliseconds"),
         "°c": ("degree Celsius", "degrees Celsius"), "°f": ("degree Fahrenheit", "degrees Fahrenheit"),
         "bps": ("basis point", "basis points"), "bp": ("basis point", "basis points"),
         "pp": ("percentage point", "percentage points"), "ppt": ("percentage point", "percentage points"),
         "ppts": ("percentage point", "percentage points"), "tok/s": ("token per second", "tokens per second"),
         "rpm": ("revolution per minute", "revolutions per minute"), "cm": ("centimetre", "centimetres"),
         "mm": ("millimetre", "millimetres"), "ml": ("millilitre", "millilitres"), "lb": ("pound", "pounds"),
         "lbs": ("pound", "pounds"), "oz": ("ounce", "ounces"), "ft": ("foot", "feet"),
         "hr": ("hour", "hours"), "hrs": ("hour", "hours"), "min": ("minute", "minutes"),
         "mins": ("minute", "minutes"), "sec": ("second", "seconds"), "secs": ("second", "seconds"),
         "yr": ("year", "years"), "yrs": ("year", "years"), "wk": ("week", "weeks"), "wks": ("week", "weeks"),
         "mbps": ("megabit per second", "megabits per second"), "gbps": ("gigabit per second", "gigabits per second"),
         "fps": ("frame per second", "frames per second"), "mah": ("milliamp hour", "milliamp hours"),
         "nm": ("nanometre", "nanometres"), "tonnes": ("tonne", "tonnes"), "t": ("tonne", "tonnes"),
         # said only when written against the number: 24h, 10in, 500g, 60W
         "h": ("hour", "hours"), "in": ("inch", "inches"), "g": ("gram", "grams"), "w": ("watt", "watts")}
# written either way: "50 sq ft" and "50sq ft"
_UNIT_RE = (r"sq\s?ft|km/h|kWh|MWh|GWh|km²|km2|m²|m2|tok/s|Mbps|Gbps|mAh|bps|bp|ppts|ppt|pp|km|kg|mph|kph|GW|MW|kW|"
            r"GB|TB|MB|KB|kB|PB|GHz|MHz|kHz|Hz|ms|°C|°F|ha|rpm|cm|mm|ml|lbs|lb|oz|ft|hrs|hr|mins|min|secs|sec|"
            r"yrs|yr|wks|wk|fps|nm")
# only against the number, never after a space - "5 in 10 people", "a 4 g signal"
_UNIT_TIGHT_RE = r"h|in|g|W|t"

# The house vocabulary - what to say for BoE, which capitals are spelled out (UK, FOMC,
# AAPL) and which are words (NATO, FTSE) - is data: tts_lexicon.json beside this file,
# deployed with it. Missing or unreadable, the rules still run without it.
_LEXICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_lexicon.json")


def load_lexicon(path: str = _LEXICON_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            lex = json.load(f)
        return lex if isinstance(lex, dict) else {}
    except (OSError, ValueError):
        return {}


_LEXICON = load_lexicon()
# Spoken forms that are neither letters nor the written word.
SPOKEN = {str(k): str(v) for k, v in (_LEXICON.get("say") or {}).items()}
# Letters people say as letters, even inside a SHOUTED HEADLINE.
ACRONYMS = {str(w) for w in _LEXICON.get("spell") or []}
# Said as one word - left alone.
WORDS = {str(w) for w in _LEXICON.get("say_as_word") or []}
ABBREV = [(r"\be\.g\.", "for example"), (r"\bi\.e\.", "that is"), (r"\betc\.", "et cetera"),
          (r"\bvs\.?(?=\s)", "versus"), (r"\bv\.(?=\s)", "versus"), (r"\bapprox\.?(?=\s)", "approximately"),
          (r"\bca\.(?=\s*\d)", "about"), (r"\bc\.(?=\s*\d)", "about"), (r"\bNo\.(?=\s*\d)", "number"),
          (r"\bno\.(?=\s*\d)", "number"), (r"\bNos\.(?=\s*\d)", "numbers"), (r"\bst\.(?=\s+[A-Z])", "Saint"),
          (r"\bDr\.(?=\s)", "Doctor"), (r"\bMr\.?(?=\s)", "Mister"), (r"\bMrs\.?(?=\s)", "Missus"),
          (r"\bMs\.?(?=\s)", "Ms"), (r"\bProf\.(?=\s)", "Professor"), (r"\bJr\.", "Junior"), (r"\bSr\.", "Senior"),
          (r"\bInc\.", "Inc"), (r"\bLtd\.", "limited"), (r"\bCorp\.", "Corp"), (r"\bplc\b", "P L C"),
          (r"\bPLC\b", "P L C"), (r"\bpa\b", "a year"), (r"\bp\.a\.", "a year"), (r"(?<!\d)(?<!\d )\bmin\.(?=\s)", "minimum"),
          (r"\bmax\.(?=\s)", "maximum"), (r"\best\.(?=\s)", "estimated"), (r"\bdept\.(?=\s)", "department"),
          (r"\bGovt\b", "government"), (r"\bgovt\b", "government"), (r"\bw/o\b", "without"), (r"\bw/(?=\s)", "with"),
          (r"\bpp\.(?=\s*\d)", "pages"), (r"\bp\.(?=\s*\d)", "page"), (r"\b[Ff]ig\.(?=\s*\d)", "figure"),
          (r"\b[Cc]h\.(?=\s*\d)", "chapter"), (r"\b[Vv]ol\.(?=\s*\d)", "volume"), (r"\b[Ss]ec\.(?=\s*\d)", "section"),
          (r"\b[Pp]ara\.(?=\s*\d)", "paragraph"), (r"\bincl\.(?=\s)", "including"), (r"\bexcl\.(?=\s)", "excluding"),
          (r"\bavg\.?(?=\s)", "average"), (r"\bAve\.(?=\s)", "Avenue"), (r"\bRd\.(?=\s)", "Road"),
          (r"\bmo\.(?=\s)", "month"), (r"\bmos\.(?=\s)", "months")]

# ── shapes the rules share ───────────────────────────────────────────────────

# thousands grouped properly, so "£5, then" keeps its comma (and its pause)
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_DASH = r"\s?(?:-|–|—)\s?"                       # the hyphen look-alikes are folded to "-" first
_DASH_TO = r"(?:\s?(?:-|–|—)\s?|\s+to\s+)"
_CUR = r"(?:US|A|C|HK|NZ)?[£$€¥₹]"
_MULT_RE = r"bn|trn|tn|mn|k|K|m|M|b|B|T|thousand|million|billion|trillion"
_SCALE_RE = r"bn|trn|tn|mn|k|m"
_AMPM = r"[ap]\.?m\.?"


# ── the rules, in order ──────────────────────────────────────────────────────

Rule = Tuple[str, Callable[["re.Match[str]"], str]]


def _say_money(sym: str, amt: str, mult: str = "") -> str:
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


def _money(m: "re.Match[str]") -> str:
    return _say_money(m.group("sym"), m.group("amt"), m.group("mult") or "")


def _money_range(m: "re.Match[str]") -> str:
    """£5-10m -> 'five to ten million pounds'; $1.2-1.5bn; £3.50-£4.20 -> each amount in full."""
    s1, a, b = m.group("s1"), m.group("a"), m.group("b")
    m1, m2 = m.group("m1") or "", m.group("m2") or ""
    if not (m1 or m2) and ("." in a or "." in b):
        return f"{_say_money(s1, a)} to {_say_money(m.group('s2') or s1, b)}"
    _, many, _, _ = CURRENCY.get(s1[-1], ("", "", "", ""))
    prefix = "U S " if s1.upper().startswith("US") else ""
    w1, w2 = _MULT.get(m1, ""), _MULT.get(m2 or m1, "")
    left = number(a) + (f" {w1}" if w1 and w1 != w2 else "")
    right = number(b) + (f" {w2}" if w2 else "")
    return f"{left} to {right} {prefix}{many}".strip()


def _code_money(m: "re.Match[str]") -> str:
    """GBP 100, 100 USD, USD 5m -> the symbol form the money rules read."""
    g = m.groupdict()
    return f"{_CODES[g['c']]}{g['n']}{g.get('mult') or ''}"


def _pence(m: "re.Match[str]") -> str:
    n = m.group("n")
    return f"{number(n)} {'penny' if n == '1' else 'pence'}"


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


def _day_name(tok: str) -> str:
    return DAYS.get(tok.lower()[:3], tok)


def _day_month(m: "re.Match[str]") -> str:
    d, mon, y = int(m.group("d")), _month_name(m.group("mon")), m.group("y")
    return f"the {ordinal(d)} of {mon}" + (f" {year(int(y))}" if y else "")


def _month_day(m: "re.Match[str]") -> str:
    d, mon, y = int(m.group("d")), _month_name(m.group("mon")), m.group("y")
    return f"{mon} the {ordinal(d)}" + (f", {year(int(y))}" if y else "")


def _month_year(m: "re.Match[str]") -> str:
    return f"{_month_name(m.group('mon'))} {year(int(m.group('y')))}"


def _day_range_month(m: "re.Match[str]") -> str:
    """1-3 June 2026 -> 'the first to the third of June twenty twenty-six'."""
    d1, d2, mon, y = int(m.group("d1")), int(m.group("d2")), _month_name(m.group("mon")), m.group("y")
    if not (1 <= d1 <= 31 and 1 <= d2 <= 31):
        return m.group(0)
    return f"the {ordinal(d1)} to the {ordinal(d2)} of {mon}" + (f" {year(int(y))}" if y else "")


def _month_day_range(m: "re.Match[str]") -> str:
    """June 1-3 -> 'June the first to the third'."""
    d1, d2, mon, y = int(m.group("d1")), int(m.group("d2")), _month_name(m.group("mon")), m.group("y")
    if not (1 <= d1 <= 31 and 1 <= d2 <= 31):
        return m.group(0)
    return f"{mon} the {ordinal(d1)} to the {ordinal(d2)}" + (f", {year(int(y))}" if y else "")


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


def _time_range(m: "re.Match[str]") -> str:
    """9-5pm, 9.30-11am, 12:30-14:00 -> each side a time, 'to' between (the time rules read them next)."""
    a, b = m.group("a"), m.group("b")
    if re.search(_AMPM + r"$", b) and not re.search(_AMPM + r"$", a):
        a = a.replace(".", ":")                    # 9.30 before an "am" is a time, not a decimal
    return f"{a} to {b}"


def _quarter(m: "re.Match[str]") -> str:
    q, y = int(m.group("q")), (m.group("y") or "").lstrip("'")
    out = f"the {ordinal(q)} quarter"
    if y:
        yy = int(y) + (2000 if len(y) == 2 else 0)
        out += f" of {year(yy)}"
    return out


def _quarter_range(m: "re.Match[str]") -> str:
    a, b, y = int(m.group("a")), int(m.group("b")), (m.group("y") or "").lstrip("'")
    out = f"the {ordinal(a)} to the {ordinal(b)} quarter"
    if y:
        out += f" of {year(int(y) + (2000 if len(y) == 2 else 0))}"
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


def _plural_word(w: str) -> str:
    return w[:-1] + "ies" if w.endswith("y") else w + "s"


def _decade(m: "re.Match[str]") -> str:
    """1990s -> 'the nineteen nineties', 1900s -> 'the nineteen hundreds', mid-2020s -> 'the mid twenty twenties'."""
    pre, y = (m.group("pre") or "").lower(), int(m.group("y"))
    if y % 100 == 0:
        w = f"{cardinal(y // 100)} hundreds"
    else:
        w = _plural_word(year(y))
    return f"the {pre + ' ' if pre else ''}{w}"


def _short_decade(m: "re.Match[str]") -> str:
    """the 80s / '90s / 80's -> 'the eighties'; over 50s -> 'over fifties'."""
    return _plural_word(cardinal(int(m.group("d") or m.group("d2") or m.group("d3"))))


_ROUND_PLURAL = {"10": "tens", "100": "hundreds", "1000": "thousands", "10000": "tens of thousands",
                 "100000": "hundreds of thousands", "1000000": "millions"}


def _year_tail(a: int, b: str) -> str:
    """The second year of a span, said the short way where people say it so (2020-21 -> 'twenty-one')."""
    if len(b) == 4:
        return year(int(b))
    yb = a // 100 * 100 + int(b)
    if 2000 <= a <= 2009 or yb < a:
        return year(yb)
    return cardinal(int(b)) if int(b) >= 10 else "oh " + _ONES[int(b)]


def _year_range(m: "re.Match[str]") -> str:
    a, b = int(m.group("a")), m.group("b")
    if len(b) == 4 and (not 1100 <= int(b) <= 2099 or _COUNT_AFTER.match(m.string, m.end())):
        return f"{number(m.group('a'))} to {number(b)}"          # 1800-2000 people: a count, not a span
    if len(b) == 2 and _COUNT_AFTER.match(m.string, m.end()):
        return f"{number(m.group('a'))} to {number(b)}"
    return f"{year(a)} to {_year_tail(a, b)}"


def _range(m: "re.Match[str]") -> str:
    return f"{number(m.group('a'))} to {number(m.group('b'))}"


def _say_unit(n: str, u: str) -> str:
    key = re.sub(r"\s+", " ", u.lower()).replace("sq ft", "sq ft")
    if key == "sqft":
        key = "sq ft"
    one, many = UNITS.get(key, (u, u))
    return one if n.replace(",", "") == "1" else many


def _unit(m: "re.Match[str]") -> str:
    n, u = m.group("n"), m.group("u")
    return f"{number(n)} {_say_unit(n, u)}"


def _seconds(m: "re.Match[str]") -> str:
    """30s -> 'thirty seconds'; 'a 30s timeout' -> 'a thirty second timeout' (an amount as an adjective is singular)."""
    n = m.group("n")
    adjective = re.search(r"\b[Aa]n?[ \t]+$", m.string[:m.start()]) is not None
    return f"{number(n)} {'second' if n == '1' or adjective else 'seconds'}"


def _unit_range(m: "re.Match[str]") -> str:
    """3-4kg -> 'three to four kilograms'; 5ft-6ft -> 'five to six feet' (the unit said once)."""
    a, b, u1, u = m.group("a"), m.group("b"), m.group("u1") or "", m.group("u")
    left = number(a) + (f" {_say_unit(a, u1)}" if u1 and _say_unit("2", u1) != _say_unit("2", u) else "")
    return f"{left} to {number(b)} {_say_unit(b, u)}"


def _scale_range(m: "re.Match[str]") -> str:
    """5-10m homes -> 'five to ten million homes'."""
    a, b, m1, m2 = m.group("a"), m.group("b"), m.group("m1") or "", m.group("m")
    w1, w2 = _MULT.get(m1, ""), _MULT[m2]
    return f"{number(a)}{' ' + w1 if w1 and w1 != w2 else ''} to {number(b)} {w2}"


def _times(m: "re.Match[str]") -> str:
    return f"{number(m.group('n'))} times"


def _by(m: "re.Match[str]") -> str:
    """1920x1080 -> 'nineteen twenty by ten eighty'; 4 x 4 -> 'four by four'."""
    def say(s: str) -> str:
        n = int(s)
        return _pairs(n) if 1000 <= n <= 9999 and n % 100 else cardinal(n)
    return f"{say(m.group('a'))} by {say(m.group('b'))}"


def _ord(m: "re.Match[str]") -> str:
    return ordinal(int(m.group("n")))


def _ord_range(m: "re.Match[str]") -> str:
    return f"{ordinal(int(m.group('a')))} to {ordinal(int(m.group('b')))}"


def _score(m: "re.Match[str]") -> str:
    """won 3-0 -> 'won three nil'."""
    say = lambda s: "nil" if int(s) == 0 else cardinal(int(s))  # noqa: E731
    return f"{m.group('ctx')}{say(m.group('a'))} {say(m.group('b'))}"


_FRACTION = {2: ("half", "halves"), 3: ("third", "thirds"), 4: ("quarter", "quarters"), 8: ("eighth", "eighths")}


def _slash(m: "re.Match[str]") -> str:
    """3/4 -> 'three quarters', 50/50 -> 'fifty fifty', 8/10 -> 'eight out of ten'."""
    a, b = int(m.group("a")), int(m.group("b"))
    if b in _FRACTION and 0 < a < b:
        one, many = _FRACTION[b]
        return f"{cardinal(a)} {one if a == 1 else many}"
    if a == b:
        return f"{cardinal(a)} {cardinal(b)}"
    return f"{cardinal(a)} out of {cardinal(b)}"


_VULGAR = {"½": "a half", "¼": "a quarter", "¾": "three quarters", "⅓": "a third", "⅔": "two thirds", "⅛": "an eighth"}


def _mixed_fraction(m: "re.Match[str]") -> str:
    return f"{cardinal(int(m.group('n')))} and {_VULGAR[m.group('f')]}"


def _phone(m: "re.Match[str]") -> str:
    s = m.group(0)
    digits = re.sub(r"\D", "", s)
    if not 10 <= len(digits) <= 13:
        return s
    groups = [g for g in re.split(r"[\s\-()]+", s.strip()) if g]
    spoken = []
    for g in groups:
        if g.startswith("+"):
            spoken.append("plus " + _digits(g))
        else:
            spoken.append(_digits(g))
    return ", ".join(spoken)


def _version(m: "re.Match[str]") -> str:
    """Python 3.12 -> 'Python three point twelve'; v2.0.1 -> 'version two point oh point one'."""
    parts = m.group("v").split(".")
    said = " point ".join(cardinal(int(p)) if not (p.startswith("0") and len(p) > 1) else _digits(p) for p in parts)
    pre = m.groupdict().get("pre")
    if pre is None:                                # the "v" form
        return f"version {said}"
    return f"{pre}{said}"


def _ip(m: "re.Match[str]") -> str:
    parts = m.group(0).split(".")
    if not all(0 <= int(p) <= 255 for p in parts):
        return m.group(0)
    return " dot ".join(_digits(p) for p in parts)


# Words that, just before a 4-digit number, mean it is a year ("in 1500"), and words
# around it that mean it is a count ("over 1500", "1500 people").
_YEAR_BEFORE = {"in", "since", "by", "from", "until", "till", "to", "of", "during", "before", "after", "circa",
                "year", "early", "late", "mid", "summer", "winter", "spring", "autumn", "fall", "between", "and",
                "through", "for", "into", "fy", "season", "class", "vintage", "c"}
_COUNT_BEFORE = {"over", "nearly", "almost", "some", "than", "roughly", "approximately", "exactly", "only", "just",
                 "another", "extra", "additional", "top", "least", "most", "under", "below", "above", "total",
                 "up", "of the", "costs", "cost", "paid", "pay", "employs", "employed", "hired", "sold", "built",
                 "about", "around", "the first", "the last", "the next"}
_COUNT_AFTER = re.compile(
    r"\s+(?:people|persons|units|homes|houses|properties|jobs|staff|employees|workers|users|customers|clients|"
    r"members|students|patients|cases|deaths|votes|words|pages|items|tokens|rows|files|lines|times|points|shares|"
    r"cars|vehicles|flats|beds|bedrooms|rooms|acres|tonnes|tons|litres|calories|steps|visitors|followers|"
    r"subscribers|views|downloads|companies|firms|businesses|sites|stores|shops|branches|new|more|other|of|"
    r"miles|metres|meters|feet|yards|km|kg|dollars|pounds|euros|samples|images|requests|calls|records|"
    r"documents|emails|messages|lots|plots|transactions|sales|deals|loans|applications|seats|tickets)\b")


def _bare_number(m: "re.Match[str]") -> str:
    s = m.group(0)
    clean = s.replace(",", "")
    if "." not in clean and "," not in s and len(clean.lstrip("-")) == 4 and not clean.startswith("-"):
        n = int(clean)
        if 1100 <= n <= 2099:
            before = m.string[:m.start()].lower().rstrip(" ~(")
            prev = re.findall(r"[a-z]+(?: [a-z]+)?$", before)
            prev_word = before.split()[-1] if before.split() else ""
            prev_two = prev[0] if prev else ""
            if _COUNT_AFTER.match(m.string, m.end()):
                return cardinal(n)
            if prev_word in _YEAR_BEFORE:
                return year(n)
            if prev_word in _COUNT_BEFORE or prev_two in _COUNT_BEFORE:
                return cardinal(n)
            return year(n) if n >= 1900 else cardinal(n)
    return number(s)


def _url(m: "re.Match[str]") -> str:
    host = re.sub(r"^www\.", "", m.group("host").lower())
    return host.replace(".", " dot ")


def _dotted_acronym(m: "re.Match[str]") -> str:
    """U.S. -> 'U S'; keeps a full stop when the abbreviation also ended the sentence."""
    letters = " ".join(c.upper() for c in m.group(0) if c.isalpha())
    nxt = m.string[m.end():m.end() + 2]
    ends_sentence = nxt == "" or nxt == "\n" or (len(nxt) == 2 and nxt[0] == " " and nxt[1].isupper())
    return letters + ("." if ends_sentence else "")


_ACR_RE = re.compile(r"\b([A-Z]{2,6})(s?)\b")
_VOWELS = set("AEIOUY")
_ONSETS = {"STR", "SPR", "SCR", "SPL", "THR", "SHR", "CHR", "SCH", "PHR"}


def _sayable(w: str) -> bool:
    """A capitalised word read as a word, not letters: FIFA, NVIDIA, VERY - but not HTML, GCSE, IPCC."""
    if len(w) < 4 or not (_VOWELS & set(w[1:]) or w[0] in "AEIOU"):
        return False
    runs = re.findall(r"[^AEIOUY]+", w)
    for i, run in enumerate(runs):
        if len(run) >= 3 and not (i == 0 and w.startswith(run) and run in _ONSETS):
            return False
    # and it must end the way English words end: FOMC, AAPL, CAGR, OECD are letters
    coda = re.search(r"[^AEIOUY]+$", w)
    return not coda or len(coda.group(0)) == 1 or coda.group(0) in _CODAS


_CODAS = {"ST", "ND", "NT", "NK", "NG", "RT", "RD", "RN", "RM", "RS", "LT", "LD", "LK", "LM", "LP", "MP", "SK",
          "SP", "CT", "PT", "FT", "XT", "TH", "SH", "CH", "CK", "LL", "SS", "FF", "GH", "PH", "RK", "RL", "RP",
          "NS", "TS", "DS", "MS", "LS", "KS", "PS", "GS", "NC", "RC", "RB", "RF", "RG", "RV", "LF", "LV", "ZZ",
          "TT", "NN", "MM", "PP", "RR", "DD", "BB", "GG"}


_ROMAN = {"II": 2, "III": 3, "IV": 4, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "XI": 11, "XII": 12, "XIII": 13,
          "XIV": 14, "XV": 15, "XVI": 16}
# after these a numeral is a number ("World War two"); after any other name, a regnal one ("Elizabeth the second")
_ROMAN_COUNTED = {"War", "Part", "Chapter", "Phase", "Stage", "Act", "Volume", "Book", "Season", "Episode", "Mark",
                  "Type", "Class", "Level", "Tier", "Gen", "Generation", "Series", "Model", "Round", "Step", "Super",
                  "Bowl", "Rocky", "Apollo", "Vatican", "Grade", "Category", "Schedule", "Annex", "Article"}


def _acronym(m: "re.Match[str]") -> str:
    w, plural = m.group(1), m.group(2)
    if w in WORDS:
        return m.group(0)
    if w in _ROMAN and not plural:
        prev = re.search(r"\b([A-Z][a-z]+)[ \t]+$", m.string[:m.start()])
        if prev:
            n = _ROMAN[w]
            return cardinal(n) if prev.group(1) in _ROMAN_COUNTED else f"the {ordinal(n)}"
    if w not in ACRONYMS and _sayable(w):
        return w.capitalize() + plural
    return " ".join(w) + ("s" if plural else "")


RULES: List[Rule] = [
    # links and addresses first, so their digits are not read
    (r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", lambda m: ""),
    (r"\bhttps?://(?:www\.)?(?P<host>[^/\s)]+)[^\s)]*", _url),
    (r"\bwww\.(?P<host>[^/\s)]+)[^\s)]*", _url),
    (r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])", _ip),
    # codes read digit by digit, before any of their digits look like amounts
    (r"(?<![\w+])(?:\+\d{1,3}[\s-]?(?:\(0\)\s?)?\d|0\d)[\d\s-]{8,15}\d(?![\w])", _phone),
    (r"(?P<pre>\b(?:[Vv]ersion|Python|Node|iOS|Android|macOS|Windows|Ubuntu|Debian|Fedora|Chrome|Firefox|"
     r"[Rr]elease|[Uu]pdate|[Ff]irmware|[Kk]ernel|[Pp]atch)\s)(?P<v>\d+(?:\.\d+)+)\b", _version),
    (r"(?<![\w.])[vV](?P<v>\d+(?:\.\d+)+)\b", _version),
    (r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", _uk_date),
    (r"(?<![\w.])(?P<pre>)(?P<v>\d+\.\d+\.\d+)(?![\w.]|\.\d)", _version),
    # currency codes -> symbols, so one set of money rules reads both
    (r"\b(?P<c>GBP|USD|EUR|JPY|INR)\s?(?P<n>" + _NUM + r")(?:\s?(?P<mult>" + _SCALE_RE + r")\b)?", _code_money),
    (r"(?<![\w.])(?P<n>" + _NUM + r")(?:\s?(?P<mult>" + _SCALE_RE + r"))?\s(?P<c>GBP|USD|EUR|JPY|INR)\b", _code_money),
    # signs: "+3.5%" -> "plus", "-5°C" / "between -5 and -10" -> "minus"
    (r"(?<![\w+])\+(?=\d)", lambda m: "plus "),
    (r"(?:(?<=\s)|(?<=^)|(?<=\()|(?<=~))-(?=\d)", lambda m: "minus "),
    # money ranges, then money: £5-10m, $1.2-1.5bn, £3.50-£4.20, then £1.2bn, US$2 trillion, $45m, €3.50
    (r"(?P<s1>" + _CUR + r")\s?(?P<a>" + _NUM + r")(?:\s?(?P<m1>" + _MULT_RE + r")\b)?" + _DASH_TO
     + r"(?P<s2>" + _CUR + r")?\s?(?P<b>" + _NUM + r")(?:\s?(?P<m2>" + _MULT_RE + r")\b)?", _money_range),
    (r"(?P<sym>" + _CUR + r")\s?(?P<amt>" + _NUM + r")(?:\s?(?P<mult>" + _MULT_RE + r")\b)?", _money),
    (r"\b(?P<n>480|720|1080|1440|2160)p\b", lambda m: f"{_pairs(int(m.group('n')))} p"),
    (r"(?<![\w.£$€])(?P<n>\d+(?:\.\d+)?)p\b", _pence),
    # percentages, with ranges (7-9%, 7%-9%, 7 to 9%)
    (r"(?P<a>-?" + _NUM + r")\s?%?" + _DASH_TO + r"(?P<b>" + _NUM + r")\s?%", _pct),
    (r"(?P<a>-?" + _NUM + r")(?P<b>)\s?%", _pct),
    # dates, with day and month ranges
    (r"\b(\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+Z?)?\b", _iso_date),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b", _uk_date),
    (r"\b(?P<d1>\d{1,2})(?:st|nd|rd|th)?" + _DASH_TO + r"(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s(?:of\s)?(?P<mon>"
     + _MONTH_RE + r")\.?(?:,?\s(?P<y>\d{4}))?\b", _day_range_month),
    (r"\b(?P<mon>" + _MONTH_RE + r")\.?\s(?P<d1>\d{1,2})(?:st|nd|rd|th)?" + _DASH + r"(?P<d2>\d{1,2})(?:st|nd|rd|th)?\b"
     r"(?:,?\s(?P<y>\d{4}))?", _month_day_range),
    (r"\b(?P<a>" + _MONTH_RE + r")\.?" + _DASH + r"(?P<b>" + _MONTH_RE + r")\b",
     lambda m: f"{_month_name(m.group('a'))} to {_month_name(m.group('b'))}"),
    (r"\b(?P<a>" + _DAY_RE + r")\.?" + _DASH + r"(?P<b>" + _DAY_RE + r")\b",
     lambda m: f"{_day_name(m.group('a'))} to {_day_name(m.group('b'))}"),
    (r"\b(?P<a>" + _DAY_RE + r")\.?(?=,?\s+\d{1,2}(?:st|nd|rd|th)?\s(?:of\s)?" + _MONTH_RE + r")",
     lambda m: _day_name(m.group("a"))),
    (r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s(?:of\s)?(?P<mon>" + _MONTH_RE + r")\.?(?:,?\s(?P<y>\d{4}))?\b", _day_month),
    (r"\b(?P<mon>" + _MONTH_RE + r")\.?\s(?P<d>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s(?P<y>\d{4}))?", _month_day),
    (r"\b(?P<mon>" + _MONTH_RE + r")\.?\s(?P<y>\d{4})\b", _month_year),
    # 1 June - 3 June: a dash before another date is "to"
    (r"[ \t]?(?:-|–|—)[ \t]?(?=the (?:[a-z]+-)?[a-z]+ of [A-Z])", lambda m: " to "),
    # periods
    (r"\bQ(?P<a>[1-4])" + _DASH + r"Q?(?P<b>[1-4])(?:\s?(?P<y>\d{4}|'\d{2}))?\b", _quarter_range),
    (r"\bQ(?P<q>[1-4])(?:\s?(?P<y>\d{4}|'\d{2}))?\b", _quarter),
    (r"\bH(?P<h>[12])(?:\s(?P<y>\d{4}))?\b", _half),
    (r"\bFY\s?(?P<a>\d{4}|\d{2})(?:/(?P<b>\d{4}|\d{2}))?\b", _fy),
    (r"\b(?P<pre>[Mm]id|[Ee]arly|[Ll]ate)?-?(?P<y>1[89]\d0|20\d0)'?s\b", _decade),
    # the 80s, '90s, 80's, over 50s - a decade only where one is meant; "30s" alone is seconds
    (r"(?:(?<=\bthe )|(?<=\bhis )|(?<=\bher )|(?<=\btheir )|(?<=\bmy )|(?<=\byour )|(?<=\bour )|(?<=\bin )|"
     r"(?<=\bover )|(?<=\bunder )|(?<=\bmid )|(?<=\bearly )|(?<=\blate )|(?<=\bmid-)|(?<=\bearly-)|(?<=\blate-))"
     r"'?(?P<d>[2-9]0)'?s\b|(?<![\w£$€.,])'(?P<d2>[2-9]0)s\b|(?<![\w£$€.,'])(?P<d3>[2-9]0)'s\b", _short_decade),
    (r"(?<![\w£$€.,])(?P<n>10|100|1,?000|10,?000|100,?000|1,?000,?000)s\b",
     lambda m: _ROUND_PLURAL[m.group("n").replace(",", "")]),
    # times, with ranges (9am-5pm, 9-5pm, 12:30-14:00)
    (r"\b(?P<a>\d{1,2}(?:[:.]\d{2})?(?:\s?" + _AMPM + r")?)" + _DASH + r"(?P<b>\d{1,2}(?:[:.]\d{2})?\s?" + _AMPM
     + r")(?=\W|$)", _time_range),
    (r"\b(?P<a>\d{1,2}:\d{2})" + _DASH + r"(?P<b>\d{1,2}:\d{2})\b", _time_range),
    (r"\b(?P<h>\d{1,2})[:.](?P<m>\d{2})\s?(?P<ap>" + _AMPM + r")(?=\W|$)", _time),
    (r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b", _time),
    (r"\b(?P<h>\d{1,2})(?P<m>)\s?(?P<ap>" + _AMPM + r")(?=\W|$)", _time),
    # units, with ranges: 3-4kg, 50-60 sq ft, 2-3pp
    (r"\b(?P<a>" + _NUM + r")\s?(?P<u1>" + _UNIT_RE + r")?" + _DASH_TO + r"(?P<b>" + _NUM + r")\s?(?P<u>"
     + _UNIT_RE + r")(?![\w/])", _unit_range),
    (r"\b(?P<a>" + _NUM + r")(?P<u1>" + _UNIT_TIGHT_RE + r")?" + _DASH + r"(?P<b>" + _NUM + r")(?P<u>"
     + _UNIT_TIGHT_RE + r")(?![\w/])", _unit_range),
    (r"\b(?P<n>" + _NUM + r")\s?(?P<u>" + _UNIT_RE + r")(?![\w/])(?:\.(?=[ \t]+[a-z]))?", _unit),
    (r"\b(?P<n>" + _NUM + r")(?P<u>" + _UNIT_TIGHT_RE + r")(?![\w/'])", _unit),
    (r"\b(?P<n>\d+(?:\.\d+)?)s\b", _seconds),
    # scales with ranges (5-10m homes), sizes (1920x1080), multipliers (3x)
    (r"\b(?P<a>" + _NUM + r")\s?(?P<m1>" + _SCALE_RE + r")?" + _DASH + r"(?P<b>" + _NUM + r")\s?(?P<m>" + _SCALE_RE
     + r")\b", _scale_range),
    (r"\b(?P<a>\d+)\s?[x×]\s?(?P<b>\d+)\b", _by),
    (r"\b(?P<n>\d+(?:\.\d+)?)[x×]\b", _times),
    # 5m homes, 300k users, 1.2bn: a scale with no currency
    (r"\b(?P<n>" + _NUM + r")\s?(?P<mult>" + _SCALE_RE + r")\b", lambda m: f"{number(m.group('n'))} {_MULT[m.group('mult')]}"),
    (r"\b(?P<a>\d+)(?:st|nd|rd|th)" + _DASH + r"(?P<b>\d+)(?:st|nd|rd|th)\b", _ord_range),
    (r"\b(?P<n>\d+)(?:st|nd|rd|th)\b", _ord),
    # scores, year spans, slashes, then every other range
    (r"(?P<ctx>\b(?:won|lost|beat|beaten|drew|draw|drawn|scored?|scoreline|victory|defeat|win|loss|lead|led|"
     r"trail(?:ed|ing)?|finished|ended)\b[^.\n\d]{0,30}?)\b(?P<a>\d{1,2})" + _DASH + r"(?P<b>\d{1,2})\b(?!\.\d)", _score),
    (r"\b(?P<a>1[89]\d{2}|20\d{2})(?:" + _DASH + r"|/)(?P<b>1[1-9]\d{2}|20\d{2}|\d{2})\b(?![.,]?\d)", _year_range),
    (r"(?<![\w/.])(?P<n>\d+)\s?(?P<f>[½¼¾⅓⅔⅛])", _mixed_fraction),
    (r"(?<![\w/.])(?P<mo>0?[1-9]|1[0-2])/(?P<y>(?:19|20)\d{2})(?![\w/])",
     lambda m: f"{list(MONTHS.values())[int(m.group('mo')) - 1]} {year(int(m.group('y')))}"),
    (r"(?<![\w/.])(?P<a>\d{1,4})/(?P<b>\d{1,4})(?![\w/])", _slash),
    (r"(?<![\w.])(?P<a>" + _NUM + r")" + _DASH + r"(?P<b>" + _NUM + r")(?![\w])", _range),
    (r"\b(?P<n>\d[\d,]*)\+", lambda m: f"{number(m.group('n'))} plus"),
    # what is left: plain numbers
    (r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.])-?\d+(?:\.\d+)?(?![\w])", _bare_number),
]
_COMPILED = [(re.compile(p), f) for p, f in RULES]

SYMBOLS = [("&", " and "), ("≈", " about "), ("~", " about "), ("→", " to "), ("->", " to "), ("<=", " at most "),
           (">=", " at least "), ("±", " plus or minus "), ("#", " number "), ("/", " or "), ("@", " at "),
           ("§", " section "), ("°", " degrees "), ("½", " a half"), ("¼", " a quarter"), ("¾", " three quarters"),
           ("⅓", " a third"), ("⅔", " two thirds"), ("⅛", " an eighth"), ("×", " times "), ("€", " euros "),
           ("£", " pounds "), ("%", " per cent")]

# Characters that look alike but read differently: every hyphen and minus becomes "-",
# every odd space a space, so one set of rules matches what an LLM or a feed writes.
_FOLD = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "−": "-", "﹣": "-", "－": "-",
                       " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
                       " ": " ", "⁠": "", "­": "", "′": "'", "’": "'", "‘": "'",
                       "“": '"', "”": '"', "•": "\n", "▪": "\n", "◦": "\n", "‣": "\n"})


def _safe(fn: Callable[["re.Match[str]"], str]) -> Callable[["re.Match[str]"], str]:
    """A rule that fails on odd input leaves that piece as written - speech must never fail on a number."""
    def run(m: "re.Match[str]") -> str:
        try:
            return fn(m)
        except Exception:  # noqa: BLE001
            return m.group(0)
    return run


_SAFE = [(rx, _safe(fn)) for rx, fn in _COMPILED]
_SPOKEN_RX = [(re.compile(r"(?<![\w&])" + re.escape(k) + r"(?![\w&])"), v)
              for k, v in sorted(SPOKEN.items(), key=lambda kv: -len(kv[0]))]


def _normalize_line(t: str) -> str:
    # SHOUTED HEADLINES (three or more capitalised words in a row) are words, not acronyms
    t = re.sub(r"\b[A-Z]{2,}(?:[ \t,:\-–]+[A-Z]{2,}){2,}\b",
               lambda m: " ".join(w if (w in SPOKEN or w in WORDS or w.strip(",:") in ACRONYMS)
                                  else w.capitalize() for w in m.group(0).split(" ")), t)
    for rx, v in _SPOKEN_RX:
        t = rx.sub(v, t)
    for pat, rep in ABBREV:
        t = re.sub(pat, rep, t)
    t = t.replace("...", "…")
    for rx, fn in _SAFE:
        t = rx.sub(fn, t)
    for a, b in SYMBOLS:
        t = t.replace(a, b)
    t = re.sub(r"\b(?:[A-Za-z]\.){2,}(?![\w])", _dotted_acronym, t)
    t = _ACR_RE.sub(_acronym, t)
    # an amount used as an adjective: "a £1.2bn stake" -> "a one point two billion pound stake"
    t = re.sub(r"\b([Aa]n?) ((?:[a-z-]+ )*?(?:thousand|million|billion|trillion) )(pound|dollar|euro)s\b", r"\1 \2\3", t)
    t = re.sub(r"\b([Tt]he)\s+the\b", r"\1", t)          # "the BoE" -> "the the Bank of England"
    # a prose dash or a bracketed aside is a pause: say it as a comma
    t = re.sub(r"[ \t]+(?:-|–|—)+[ \t]+|(?<=\w)—(?=\w)", ", ", t)
    t = re.sub(r"^\s*(?:-|–|—)+\s*", "", t)
    t = re.sub(r"[ \t]*[(\[][ \t]*", ", ", t)
    t = re.sub(r"[ \t]*[)\]][ \t]*(?=[\w\"'])", ", ", t)
    t = re.sub(r"[ \t]*[)\]]", "", t)
    t = re.sub(r",[ \t]*(?=,)", "", t)
    t = re.sub(r",[ \t]*([.!?;:…])", r"\1", t)
    t = re.sub(r"^[ \t]*,[ \t]*", "", t)
    t = re.sub(r"[ \t]+([,.;:!?])", r"\1", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def normalize(text: str) -> str:
    """Written text -> text a TTS voice reads naturally (British English). Line breaks are kept."""
    if not text:
        return ""
    t = str(text).translate(_FOLD).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_normalize_line(line) for line in t.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


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
                # line by line, so its output keeps the breaks the pacing needs
                text = "\n".join(nm.normalize(line, verbose=False, punct_post_process=True) if line.strip() else ""
                                 for line in str(text).split("\n"))
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
    explicit = set()                                    # pieces whose pause a <break> set exactly
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", text or "") if p.strip(PAUSE_MARK + " \t\n")]
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
            if not re.search(r"[.!?…:;,][\"')\]]*$", line.rstrip(PAUSE_MARK + " \t")):
                line = re.sub("([" + PAUSE_MARK + r" \t]*)$", r".\1", line, count=1)
            # an explicit <break> (a run of PAUSE_MARK) cuts the line there, with exactly that pause
            for chunk in re.split("(" + PAUSE_MARK + "+)", line):
                if chunk.startswith(PAUSE_MARK):
                    if plan:
                        plan[-1] = (plan[-1][0], max(plan[-1][1], len(chunk) * 0.1))
                        explicit.add(len(plan) - 1)
                    continue
                for sentence in _sentences(chunk.strip()):
                    for clause in _clauses(sentence):
                        if not re.search(r"\w", clause):      # a stray mark: its pause goes to what came before
                            if plan:
                                plan[-1] = (plan[-1][0], max(plan[-1][1], _end_pause(clause)))
                            continue
                        plan.append((clause, _end_pause(clause)))
            if plan and li < len(lines) - 1:
                plan[-1] = (plan[-1][0], max(plan[-1][1], PAUSES["line"]))
        if plan and pi < len(paragraphs) - 1:
            plan[-1] = (plan[-1][0], max(plan[-1][1], PAUSES["paragraph"]))
    return [(seg, pause if i in explicit else _vary(seg, pause)) for i, (seg, pause) in enumerate(plan)]


# ── speech markup: an SSML subset the speaking models may write ──────────────
#
# Only the writer knows whether "3-2" is a score, "1500" a year, "FOMC" spelled out.
# So a model whose words will be spoken may mark what it means, in standard SSML:
#   <sub alias="one hundred and eight to three hundred">108-300</sub>
#   <say-as interpret-as="characters">FOMC</say-as>   (characters|digits|telephone|cardinal|ordinal)
#   <break time="600ms"/>   <break strength="strong"/>
# Anything else - an unknown tag, a broken one, one left open - is dropped to its
# text, never spoken as markup. Untagged text goes through normalize() as before.

PAUSE_MARK = ""          # one per 100 ms of an explicit pause; private use, so no rule reads it
_BREAK_STRENGTH = {"none": 0.0, "x-weak": 0.1, "weak": 0.2, "medium": 0.4, "strong": 0.7, "x-strong": 1.0}
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>/]+))""")
# a tag starts "<name" or "</name" with no space after "<", so "a < b" is never taken for markup
_ANY_TAG = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>")
_PAIRED = re.compile(r"<(?P<name>sub|say-as|phoneme)\b(?P<attrs>[^<>]*)>(?P<inner>.*?)</(?P=name)\s*>",
                     re.IGNORECASE | re.DOTALL)
_BREAK = re.compile(r"<break\b(?P<attrs>[^<>]*?)/?>(?:\s*</break\s*>)?", re.IGNORECASE)
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&#39;": "'"}
_OPENERS = re.compile(r"<(sub|say-as|phoneme)\b[^<>]*>", re.IGNORECASE)


def _attrs(s: str) -> dict:
    return {k.lower(): (a or b or c) for k, a, b, c in _ATTR.findall(s or "")}


def _break_seconds(attrs: dict) -> float:
    t = (attrs.get("time") or "").strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s)?", t)
    if m:
        v = float(m.group(1))
        sec = v if m.group(2) == "s" else v / 1000.0
    else:
        sec = _BREAK_STRENGTH.get((attrs.get("strength") or "medium").lower(), 0.4)
    return max(0.0, min(3.0, sec))


def _say_as(kind: str, inner: str) -> str:
    k = (kind or "").lower()
    text = inner.strip()
    if k in ("characters", "spell-out", "letters", "verbatim"):
        return " ".join(c.upper() if c.isalpha() else c for c in text if not c.isspace())
    if k in ("digits", "telephone"):
        groups = [g for g in re.split(r"[^\d+]+", text) if g]
        said = [("plus " if g.startswith("+") else "")
                + " ".join("oh" if d == "0" else _ONES[int(d)] for d in g if d.isdigit()) for g in groups]
        return ", ".join(s for s in said if s)
    if k in ("cardinal", "number") and re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", text):
        return number(text.replace("-", "")) if not text.startswith("-") else "minus " + number(text[1:])
    if k == "ordinal" and re.fullmatch(r"\d+(?:st|nd|rd|th)?", text):
        return ordinal(int(re.sub(r"\D", "", text)))
    return text                                           # date, time, unknown: the normaliser reads it


def _unescape(t: str) -> str:
    for k, v in _ENTITIES.items():
        t = t.replace(k, v)
    return t


def resolve_speech_markup(text: str) -> str:
    """Speech markup -> plain text for the voice (aliases said, breaks as PAUSE_MARK runs)."""
    if not text or "<" not in text:
        return _unescape(text or "")

    def paired(m: "re.Match[str]") -> str:
        name, attrs, inner = m.group("name").lower(), _attrs(m.group("attrs")), _ANY_TAG.sub("", m.group("inner"))
        if name == "sub":
            alias = attrs.get("alias")
            return alias if alias is not None else inner
        if name == "say-as":
            return _say_as(attrs.get("interpret-as") or attrs.get("interpret_as") or "", inner)
        return inner                                      # <phoneme>: its text, until the phonemiser can take ph=

    t = _PAIRED.sub(paired, text)
    t = _BREAK.sub(lambda m: " " + PAUSE_MARK * max(1, round(_break_seconds(_attrs(m.group("attrs"))) * 10)) + " "
                   if _break_seconds(_attrs(m.group("attrs"))) > 0 else " ", t)
    t = _ANY_TAG.sub("", t)                               # every other tag, known or not, to its text
    t = re.sub(r"<[A-Za-z/][^<>]*$", "", t)               # a tag cut off at the end
    return _unescape(t)


def strip_speech_markup(text: str) -> str:
    """Speech markup -> the written form, for showing the text (aliases dropped, breaks removed)."""
    if not text or "<" not in text:
        return _unescape(text or "")
    t = _BREAK.sub("", text)
    t = _ANY_TAG.sub("", t)
    t = re.sub(r"<[A-Za-z/][^<>]*$", "", t)
    return _unescape(re.sub(r"[ \t]{2,}", " ", t))


def markup_safe_cut(buf: str) -> int:
    """How much of a streaming buffer can be cut into sentences: up to the first
    tag that is still open (or cut off mid-tag), so a sentence never ends inside one."""
    cut = len(buf)
    partial = re.search(r"<[A-Za-z/][^<>]*$", buf)
    if partial:
        cut = partial.start()
    for m in _OPENERS.finditer(buf, 0, cut):
        closer = re.compile(r"</" + re.escape(m.group(1)) + r"\s*>", re.IGNORECASE)
        if not closer.search(buf, m.end()):
            return m.start()
    return cut


def markup_spans(text: str) -> List[Tuple[int, int]]:
    """Where the complete tags and tagged phrases are, so a sentence split skips them."""
    spans = [(m.start(), m.end()) for m in _PAIRED.finditer(text)]
    spans += [(m.start(), m.end()) for m in _ANY_TAG.finditer(text)]
    return spans
