"""Replaces numbers, units, codes, URLs, Chinese spans and protected terms with [[Pn]] placeholders
before the text goes to the LLM, then restores them afterwards."""
import json
import os
import re
from dataclasses import dataclass
from typing import List, Tuple

PROTECTED_PATH = "data/protected_terms.json"
_SIGN = r"(?<![A-Za-z0-9_.])[-+\u2212]?"
CHINESE_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]")
_CHINESE_SPAN = r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002ffff\u3000-\u303f\uff01-\uff60]+"

_BASE_PATTERNS = [
    r"\u27e6[0-9tb]+\u27e7",                       # formatting / tab / line-break markers of a Word unit (docx_io)
    _CHINESE_SPAN,
    r"(?:https?://|www\.)[^\s,;]+[^\s,;.]|[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    r"(?<![A-Za-z0-9_])(?:AVANCEAON|RZH|ESI|MSDS|JOTUN|SSPC|Testex|WFT|DFT|DEPAMU)(?![A-Za-z0-9_])",
    r"(?<![A-Za-z0-9_])(?!(?:HIGH-HIGH|LOW-LOW|ON-OFF|OFF-ON)\b)[A-Z][A-Z0-9]*(?:-[ \t\r\n]*[A-Z0-9_/]+)+(?![A-Za-z0-9_])",
    r"\b(?:EN|ISO|IEC|ASTM|ASME|ANSI|API|NACE|AWS|NAMUR)[ \t]*[A-Z]?[0-9]+(?:[./-][A-Z0-9]+)*\b",
    r"\b(?:HART|SST|NPT|WNRF|RF|LL|HH|LALL|LAL|LAH|PAH|PAHH|ICSS|LCP|ITP|ROK|FAT|WPS|PQR|PMI|DFT|MTC|MTCs|IIB|IECEx|ATEX|NAMUR|NE43)\b",
    r"\b[HL](?=[ \t]*\n[ \t]*(?:[0-9]+|-))",
    # whole dates: 01.10.2025, 01/10/2025, 01-Oct-2025
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
    r"\b\d{1,2}[-\s](?i:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s]\d{2,4}\b",
    # part / tag codes containing a digit: RZK-NSS-PT-MI-U10-001/2
    r"\b(?!(?:ANNEXURE|ANNEX|APPENDIX|SECTION|CLAUSE|FIGURE|TABLE)\b)(?=[A-Z\-\/]*\d)[A-Z]{2,}[A-Z0-9\-\/]{3,}\b",
    # hyphenated tag chains even without a digit: RZK-NSS-PT-MI
    r"\b[A-Z]{2,5}(?:-[A-Z0-9]{1,6}){3,}\b",
    # model numbers with letters AND digits: STG79S-E1G000-1-I-DHT-13C-A-30A0
    r"\b(?!(?:ANNEXURE|ANNEX|APPENDIX|SECTION|CLAUSE|FIGURE|TABLE)\b)(?=[A-Z0-9\-]*[A-Z])(?=[A-Z0-9\-]*\d)[A-Z0-9]{4,}[A-Z0-9\-]*[A-Z0-9]\b",
    # short alphanumerics: M20, T4, IP66, 316L
    r"\b(?:[A-Z]{1,2}\d+[A-Z]?|\d+[A-Z]{1,2})\b",
    # number + unit, sign included: -30°C, 5 bar, 66%
    _SIGN + r"\d+(?:\.\d+)?\s?(?i:bar|psi|°C|°F|mm|NPT|%)(?![A-Za-z])",
    # plain numbers, sign included
    _SIGN + r"\d+(?:[.,]\d+)?(?![0-9])",
]

_cache = {}


def _protected_terms(path: str = PROTECTED_PATH) -> List[str]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


def _term_regex(term: str) -> str:
    esc = re.escape(term).replace("'", "['\u2019]")
    lead = r"(?<!\w)" if re.match(r"\w", term[0]) else ""    # no boundary needed next to symbols like "+"
    trail = r"(?!\w)" if re.match(r"\w", term[-1]) else ""   # so "+/-" still matches in "+/-0.075"
    return lead + esc + trail


def _compiled(protected: Tuple[str, ...]):
    if protected not in _cache:
        parts = []
        if protected:
            ordered = sorted(protected, key=len, reverse=True)
            parts.append("|".join(_term_regex(t) for t in ordered))
        parts += _BASE_PATTERNS
        _cache[protected] = re.compile("|".join(f"(?:{p})" for p in parts))
    return _cache[protected]


@dataclass
class MaskResult:
    masked_text: str
    mapping: dict


def mask(text: str, protected_terms: List[str] = None) -> MaskResult:
    if protected_terms is None:
        protected_terms = _protected_terms()
    regex = _compiled(tuple(protected_terms))
    text = re.sub(r"(?<=\d)(?=(?:Feet|Foot|Inches|Inch)\b)", " ", text)   # "40Feet" -> "40 Feet"
    mapping = {}

    def _replace(m):
        placeholder = f"[[P{len(mapping)}]]"
        mapping[placeholder] = m.group(0)
        return placeholder

    return MaskResult(masked_text=regex.sub(_replace, text), mapping=mapping)


def unmask(translated_text: str, mapping: dict) -> Tuple[str, List[str]]:
    result = translated_text
    missing = []
    found_order = re.findall(r"\[\[\s*P(\d+)\s*\]\]", translated_text)
    expected_order = [placeholder[3:-2] for placeholder in mapping]
    order_changed = found_order != expected_order
    for placeholder, original in mapping.items():
        n = placeholder[3:-2]  # "[[P12]]" -> "12"
        pat = re.compile(rf"\[\[\s*P{n}\s*\]\]")
        count = len(pat.findall(result))
        if count:
            result = pat.sub(lambda _m: original, result)
        if count != 1 or order_changed:
            missing.append(placeholder)
    return result, missing