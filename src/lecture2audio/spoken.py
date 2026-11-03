"""Deterministic written form -> spoken form.

The language model does the context-dependent work (is "2024" a year or a quantity?
what does "*" mean in this line of code?). This module is the safety net: whatever the
model left behind, no raw digit or unpronounceable symbol reaches the voice model.
It has no third-party dependency, so the same input always gives the same output.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- English numbers

_EN_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
             "seventeen", "eighteen", "nineteen"]
_EN_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy",
            8: "eighty", 9: "ninety"}
_EN_SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand")]


def _en_lt100(n: int) -> str:
    if n < 20:
        return _EN_UNITS[n]
    tens, unit = divmod(n, 10)
    return _EN_TENS[tens] + ("-" + _EN_UNITS[unit] if unit else "")


def _en_lt1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(_EN_UNITS[hundreds] + " hundred")
    if rest:
        parts.append(_en_lt100(rest))
    return " ".join(parts)


def en_cardinal(n: int) -> str:
    if n < 0:
        return "minus " + en_cardinal(-n)
    if n == 0:
        return "zero"
    parts = []
    for value, name in _EN_SCALES:
        if n >= value:
            quotient, n = divmod(n, value)
            parts.append(_en_lt1000(quotient) + " " + name)
    if n:
        parts.append(_en_lt1000(n))
    return " ".join(parts)


_EN_ORD_SPECIAL = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                   "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}


def en_ordinal(n: int) -> str:
    words = en_cardinal(n)
    match = re.search(r"([a-z]+)$", words)
    head, last = words[: match.start()], match.group(1)
    if last in _EN_ORD_SPECIAL:
        last = _EN_ORD_SPECIAL[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + last


# ----------------------------------------------------------------- French numbers

_FR_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
             "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize"]
_FR_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _fr_lt20(n: int) -> str:
    if n < 17:
        return _FR_UNITS[n]
    return "dix-" + _FR_UNITS[n - 10]


def _fr_lt100(n: int, plural80: bool) -> str:
    if n < 20:
        return _fr_lt20(n)
    if n < 70:
        tens, unit = divmod(n, 10)
        base = _FR_TENS[tens]
        if unit == 0:
            return base
        if unit == 1:
            return base + " et un"
        return base + "-" + _FR_UNITS[unit]
    if n < 80:
        rest = n - 60
        if rest == 11:
            return "soixante et onze"
        return "soixante-" + _fr_lt20(rest)
    rest = n - 80
    if rest == 0:
        return "quatre-vingts" if plural80 else "quatre-vingt"
    return "quatre-vingt-" + _fr_lt20(rest)


def _fr_lt1000(n: int, final: bool) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        word = "cent" if hundreds == 1 else _FR_UNITS[hundreds] + " cent"
        if rest == 0 and hundreds > 1 and final:
            word += "s"
        parts.append(word)
    if rest:
        parts.append(_fr_lt100(rest, plural80=final))
    return " ".join(parts)


def fr_cardinal(n: int) -> str:
    if n < 0:
        return "moins " + fr_cardinal(-n)
    if n == 0:
        return "zéro"
    billions, rest = divmod(n, 10**9)
    millions, rest = divmod(rest, 10**6)
    thousands, units = divmod(rest, 10**3)
    parts = []
    if billions:
        parts.append(_fr_lt1000(billions, False) + (" milliard" if billions == 1 else " milliards"))
    if millions:
        parts.append(_fr_lt1000(millions, False) + (" million" if millions == 1 else " millions"))
    if thousands:
        parts.append("mille" if thousands == 1 else _fr_lt1000(thousands, False) + " mille")
    if units:
        parts.append(_fr_lt1000(units, True))
    return " ".join(parts)


def fr_ordinal(n: int, feminine: bool = False) -> str:
    if n == 1:
        return "première" if feminine else "premier"
    words = fr_cardinal(n)
    match = re.search(r"([a-zéèêâîôûç]+)$", words)
    head, last = words[: match.start()], match.group(1)
    if last == "cinq":
        last = "cinqu"
    elif last == "neuf":
        last = "neuv"
    elif last in ("vingts", "cents"):
        last = last[:-1]
    elif last.endswith("e"):
        last = last[:-1]
    return head + last + "ième"


# ------------------------------------------------------------------ public helpers

def _lang(language: str) -> str:
    return "fr" if str(language).lower().startswith("fr") else "en"


def digits_to_words(digits: str, language: str = "en") -> str:
    table = _FR_UNITS if _lang(language) == "fr" else _EN_UNITS
    return " ".join(table[int(c)] for c in digits if c.isdigit())


def number_to_words(n: int, language: str = "en") -> str:
    """Cardinal number in words. Very large numbers are read digit by digit."""
    lang = _lang(language)
    limit = 10**12 if lang == "fr" else 10**15
    if abs(n) >= limit:
        return ("minus " if n < 0 and lang == "en" else "moins " if n < 0 else "") + \
            digits_to_words(str(abs(n)), lang)
    return fr_cardinal(n) if lang == "fr" else en_cardinal(n)


# ------------------------------------------------------------------ normalisation

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/(\d{1,2}))?(?!\d)")
_CIDR = re.compile(r"(?<![\w/])/(\d{1,2})(?![\w])")
_HEX = re.compile(r"\b0[xX]([0-9a-fA-F]+)\b")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s?%")
_ORD = {
    "en": re.compile(r"\b(\d+)(st|nd|rd|th)(?!\w)"),
    "fr": re.compile(r"\b(\d+)(ère|ème|er|re|e|è)(?!\w)"),
}
_THOUSANDS = {
    "en": re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?!\d)"),
    "fr": re.compile(r"(?<!\d)\d{1,3}(?:[ \u202f\u00a0]\d{3})+(?!\d)"),
}
_DECIMAL = {
    "en": re.compile(r"(?<![\w.])(\d+)\.(\d+)(?!\w)"),
    "fr": re.compile(r"(?<![\w.,])(\d+)[.,](\d+)(?!\w)"),
}
_NEGATIVE = re.compile(r"(^|[\s(\[])-(?=\d)")
_INTEGER = re.compile(r"\d+")
_SNAKE = re.compile(r"\b([A-Za-z]+(?:_[A-Za-z0-9]+)+)\b")
_CAMEL = re.compile(r"\b([a-z]+(?:[A-Z][a-z0-9]*)+)\b")
_FILE_EXT = re.compile(r"(?<=\w)\.(h|c|cpp|py|sh|js|json|txt|md|csv|exe|pdf)\b")

_UNITS = {
    "en": {
        "ms": ("millisecond", "milliseconds"), "µs": ("microsecond", "microseconds"),
        "ns": ("nanosecond", "nanoseconds"), "Hz": ("hertz", "hertz"),
        "kHz": ("kilohertz", "kilohertz"), "MHz": ("megahertz", "megahertz"),
        "GHz": ("gigahertz", "gigahertz"),
        "Kbps": ("kilobit per second", "kilobits per second"),
        "Mbps": ("megabit per second", "megabits per second"),
        "Gbps": ("gigabit per second", "gigabits per second"),
        "KB": ("kilobyte", "kilobytes"), "MB": ("megabyte", "megabytes"),
        "GB": ("gigabyte", "gigabytes"), "TB": ("terabyte", "terabytes"),
        "Kb": ("kilobit", "kilobits"), "Mb": ("megabit", "megabits"),
        "Gb": ("gigabit", "gigabits"),
        "km": ("kilometer", "kilometers"), "kg": ("kilogram", "kilograms"),
        "cm": ("centimeter", "centimeters"), "mm": ("millimeter", "millimeters"),
    },
    "fr": {
        "ms": ("milliseconde", "millisecondes"), "µs": ("microseconde", "microsecondes"),
        "ns": ("nanoseconde", "nanosecondes"), "Hz": ("hertz", "hertz"),
        "kHz": ("kilohertz", "kilohertz"), "MHz": ("mégahertz", "mégahertz"),
        "GHz": ("gigahertz", "gigahertz"),
        "Kbps": ("kilobit par seconde", "kilobits par seconde"),
        "Mbps": ("mégabit par seconde", "mégabits par seconde"),
        "Gbps": ("gigabit par seconde", "gigabits par seconde"),
        "Ko": ("kilooctet", "kilooctets"), "Mo": ("mégaoctet", "mégaoctets"),
        "Go": ("gigaoctet", "gigaoctets"), "To": ("téraoctet", "téraoctets"),
        "km": ("kilomètre", "kilomètres"), "kg": ("kilogramme", "kilogrammes"),
        "cm": ("centimètre", "centimètres"), "mm": ("millimètre", "millimètres"),
    },
}
_UNIT_RE = {
    lang: re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(%s)(?!\w)" % "|".join(
        re.escape(u) for u in sorted(table, key=len, reverse=True)))
    for lang, table in _UNITS.items()
}

# Symbols a voice cannot read. Longest first so "->" wins over "-" and ">".
_SYMBOLS = {
    "en": {
        "!=": "is not equal to", "==": "is equal to", "<=": "is less than or equal to",
        ">=": "is greater than or equal to", "&&": "and", "||": "or", "->": "arrow",
        "=>": "implies", "++": "plus plus", "=": "equals", "<": "is less than",
        ">": "is greater than", "+": "plus", "≠": "is not equal to",
        "≤": "is less than or equal to", "≥": "is greater than or equal to",
        "≈": "is approximately", "→": "gives", "⇒": "implies", "×": "times",
        "÷": "divided by", "±": "plus or minus", "∞": "infinity", "√": "square root of",
        "∈": "belongs to", "∀": "for all", "∃": "there exists", "∑": "the sum of",
        "∏": "the product of", "∫": "the integral of", "%": "percent", "&": "and",
        "@": "at", "#": "hash", "/": "slash", "\\": "backslash", "*": "star",
        "^": "to the power of", "~": "tilde", "|": "pipe",
    },
    "fr": {
        "!=": "est différent de", "==": "est égal à", "<=": "est inférieur ou égal à",
        ">=": "est supérieur ou égal à", "&&": "et", "||": "ou", "->": "flèche",
        "=>": "implique", "++": "plus plus", "=": "égale", "<": "est inférieur à",
        ">": "est supérieur à", "+": "plus", "≠": "est différent de",
        "≤": "est inférieur ou égal à", "≥": "est supérieur ou égal à",
        "≈": "est environ égal à", "→": "donne", "⇒": "implique", "×": "fois",
        "÷": "divisé par", "±": "plus ou moins", "∞": "l'infini", "√": "racine carrée de",
        "∈": "appartient à", "∀": "pour tout", "∃": "il existe", "∑": "la somme de",
        "∏": "le produit de", "∫": "l'intégrale de", "%": "pour cent", "&": "et",
        "@": "arobase", "#": "dièse", "/": "barre oblique", "\\": "antislash",
        "*": "étoile", "^": "puissance", "~": "tilde", "|": "barre verticale",
    },
}
_DROP = "`{}[]"
_RESIDUE_SYMBOLS = set("=<>+*/\\^_~|{}[]#@$&%`")


def _strip_markup(text: str) -> str:
    text = text.replace("\u2212", "-").replace("\u00a0", " ")
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    return text


def _ipv4(match: re.Match, lang: str) -> str:
    groups = [int(g) for g in match.groups()[:4]]
    if any(g > 255 for g in groups):
        return match.group(0)
    dot = "dot" if lang == "en" else "point"
    text = f" {dot} ".join(digits_to_words(g, lang) for g in match.groups()[:4])
    if match.group(5):
        text += " slash " + number_to_words(int(match.group(5)), lang)
    return " " + text + " "


def _hex(match: re.Match, lang: str) -> str:
    spoken = []
    for char in match.group(1):
        spoken.append(digits_to_words(char, lang) if char.isdigit() else char.upper())
    return (" hexadecimal " if lang == "en" else " hexadécimal ") + " ".join(spoken) + " "


def _unit(match: re.Match, lang: str) -> str:
    singular, plural = _UNITS[lang][match.group(2)]
    number = match.group(1)
    return f"{number} {singular if number == '1' else plural}"


def _ordinal(match: re.Match, lang: str) -> str:
    digits, suffix = match.group(1), match.group(2)
    if len(digits) > 1 and digits.startswith("0"):
        return match.group(0)
    n = int(digits)
    if lang == "fr":
        return " " + fr_ordinal(n, feminine=suffix in ("re", "ère")) + " "
    return " " + en_ordinal(n) + " "


def _decimal(match: re.Match, lang: str) -> str:
    point = "point" if lang == "en" else "virgule"
    return f" {number_to_words(int(match.group(1)), lang)} {point} {digits_to_words(match.group(2), lang)} "


def _integer(match: re.Match, lang: str) -> str:
    digits = match.group(0)
    if len(digits) > 1 and digits.startswith("0"):
        return " " + digits_to_words(digits, lang) + " "
    return " " + number_to_words(int(digits), lang) + " "


def _identifiers(text: str) -> str:
    text = _SNAKE.sub(lambda m: m.group(1).replace("_", " "), text)
    return _CAMEL.sub(
        lambda m: re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", m.group(1)).lower(), text)


def _symbols(text: str, lang: str) -> str:
    table = _SYMBOLS[lang]
    for symbol in sorted(table, key=len, reverse=True):
        text = text.replace(symbol, f" {table[symbol]} ")
    for char in _DROP:
        text = text.replace(char, " ")
    return text.replace("_", " ")


def find_residue(text: str) -> dict:
    """What a voice model would stumble on: digits and code-like symbols."""
    symbols: dict = {}
    for char in text:
        if char in _RESIDUE_SYMBOLS:
            symbols[char] = symbols.get(char, 0) + 1
    return {"digits": sum(c.isdigit() for c in text), "symbols": symbols}


def normalize_spoken(text: str, language: str = "en") -> str:
    """Replace every digit and unpronounceable symbol by words."""
    lang = _lang(language)
    t = _strip_markup(text)
    t = _identifiers(t)
    t = _IPV4.sub(lambda m: _ipv4(m, lang), t)
    t = _CIDR.sub(lambda m: " slash " + number_to_words(int(m.group(1)), lang) + " ", t)
    t = _HEX.sub(lambda m: _hex(m, lang), t)
    t = _PERCENT.sub(lambda m: m.group(1) + (" percent" if lang == "en" else " pour cent"), t)
    t = _UNIT_RE[lang].sub(lambda m: _unit(m, lang), t)
    t = _ORD[lang].sub(lambda m: _ordinal(m, lang), t)
    t = _THOUSANDS[lang].sub(lambda m: re.sub(r"\D", "", m.group(0)), t)
    t = _NEGATIVE.sub(lambda m: m.group(1) + ("minus " if lang == "en" else "moins "), t)
    t = _DECIMAL[lang].sub(lambda m: _decimal(m, lang), t)
    t = _INTEGER.sub(lambda m: _integer(m, lang), t)
    t = _FILE_EXT.sub(lambda m: " dot " + m.group(1), t)
    t = _symbols(t, lang)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s+([.,;:!?)\]])", r"\1", t)
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return t.strip()
