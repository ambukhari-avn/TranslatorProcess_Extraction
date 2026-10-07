"""Mechanical checks on each translated segment."""
import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import List
from knowledge_base import GlossaryTerm
from masking import CHINESE_RE


@dataclass
class QAIssue:
    rule: str
    severity: str  # "critical" | "moderate" | "minor"
    message: str


_HOMOGLYPHS = {"A": "\u0410", "B": "\u0412", "C": "\u0421", "E": "\u0415", "H": "\u041d",
               "K": "\u041a", "M": "\u041c", "O": "\u041e", "P": "\u0420", "T": "\u0422",
               "X": "\u0425"}
_NUM = re.compile(r"(?<![A-Za-z\u0400-\u04ff0-9_])(?<!\d\.)[-+\u2212]?\d+(?:[.,]\d+)?")
_LEAK = re.compile(r"\[\[\s*P\d+\s*\]\]")


def _nums(s: str) -> Counter:
    normalized = unicodedata.normalize("NFKC", s)
    return Counter(n.replace("\u2212", "-").replace(",", ".") for n in _NUM.findall(normalized))



def _term_present(target_term: str, translated_text: str) -> bool:
    hay = translated_text.lower()
    for w in re.findall(r"\w+", target_term.lower()):
        if len(w) <= 3:
            stem = w
        elif len(w) == 4:
            stem = w[:-1]                       # рама ~ рамы
        elif len(w) <= 6:
            stem = w[:len(w) - 2]               # бирки ~ бирок
        elif len(w) <= 8:
            stem = w[:max(5, len(w) - 3)]       # отключение ~ отключают, прибор ~ приборов
        else:
            stem = w[:6]                        # long words: the root is enough
        if stem not in hay:
            return False
    return True


def _cyrillic_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha() and not CHINESE_RE.fullmatch(c)]
    if not letters:
        return 1.0
    return sum(1 for c in letters if "\u0400" <= c <= "\u04FF") / len(letters)


LATIN_WHITELIST_PATH = "data/latin_whitelist.json"
_DEFAULT_WHITELIST = ["HART", "ATEX", "IECEx", "NPT", "DC", "AC", "SST", "IIB", "LCD", "LED",
                      "API", "ANSI", "ASME", "ISO", "IEC", "ITP", "ROK", "HTS", "PDF"]


def _latin_whitelist() -> set:
    words = _DEFAULT_WHITELIST
    if os.path.exists(LATIN_WHITELIST_PATH):
        try:
            with open(LATIN_WHITELIST_PATH, encoding="utf-8") as f:
                words = json.load(f)
        except Exception:
            pass
    return {w.lower() for w in words}


def latin_leftovers(translated_text: str, kept_tokens=None) -> List[str]:
    """Latin words (3+ letters) left in a translation, ignoring kept codes/names and the whitelist."""
    rest = translated_text
    for tok in sorted(kept_tokens or [], key=len, reverse=True):
        rest = rest.replace(tok, " ")
    wl = _latin_whitelist()
    out = []
    for w in re.findall(r"[A-Za-z]{3,}", rest):
        if w.lower() not in wl and w not in out:
            out.append(w)
    return out


def run_checks(original_text: str, translated_text: str,
               unmasked_missing_placeholders: List[str],
               glossary_terms_expected: List[GlossaryTerm],
               kept_tokens: List[str] = None) -> List[QAIssue]:
    issues: List[QAIssue] = []

    if unmasked_missing_placeholders:
        issues.append(QAIssue("missing_placeholder", "critical",
            f"{len(unmasked_missing_placeholders)} number/code token(s) were dropped "
            f"or altered by the model: {unmasked_missing_placeholders}"))

    if _LEAK.search(translated_text):
        issues.append(QAIssue("leaked_placeholder", "critical",
            "A [[Pn]] placeholder is still present in the output."))

    if CHINESE_RE.findall(original_text) != CHINESE_RE.findall(translated_text):
        issues.append(QAIssue("chinese_mismatch", "critical",
            "Chinese source characters were changed, removed, reordered, or added. Preserve the Chinese source exactly."))

    if kept_tokens is None:
        from masking import mask
        kept_tokens = list(mask(original_text).mapping.values())
    for token in set(kept_tokens):
        if token.startswith("⟦") or (not re.search(r"[A-Za-z]", token) and not CHINESE_RE.search(token)):
            continue        # Word format markers are guarded by the placeholder check
        pattern = (re.compile(re.escape(token)) if CHINESE_RE.search(token) else
               re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])"))
        if len(pattern.findall(original_text)) != len(pattern.findall(translated_text)):
            issues.append(QAIssue("protected_token_mismatch", "critical",
                f"Protected identifier/unit {token!r} was changed, removed, or duplicated."))

    src_n, out_n = _nums(original_text), _nums(translated_text)
    lost, added = src_n - out_n, out_n - src_n
    if lost or added:
        issues.append(QAIssue("numeric_mismatch", "critical",
            f"Numbers differ from source. Missing/changed: {sorted(lost)}; "
            f"unexpected: {sorted(added)}. Check signs (e.g. -30 vs 30)."))

    for term in glossary_terms_expected:
        if term.target_term and not _term_present(term.target_term, translated_text):
            issues.append(QAIssue("missing_glossary_term", "moderate",
                f'Expected glossary term "{term.target_term}" (for "{term.en_term}") '
                f"not found in output."))

    src_len = len(re.sub(r"[.\u2026_\s]{4,}", " ", original_text).strip())      # dot leaders carry no text
    if len(translated_text.strip()) < src_len * 0.3 and src_len > 20:
        issues.append(QAIssue("suspiciously_short_output", "critical",
            "Output is far shorter than the source; likely truncated or skipped."))

    if len(translated_text) > 6 * len(original_text) + 200:
        issues.append(QAIssue("suspiciously_long_output", "critical",
            "Output is far longer than the source; likely repetitive or invented content."))

    if not translated_text.strip():
        issues.append(QAIssue("empty_output", "critical", "Model returned an empty translation."))

    source_prose, target_prose = original_text, translated_text
    for token in sorted(set(kept_tokens), key=len, reverse=True):
        source_prose = source_prose.replace(token, " ")
        target_prose = target_prose.replace(token, " ")
    whitelist = _latin_whitelist()
    source_prose = re.sub(r"[A-Za-z]+", lambda match: " " if match.group().lower() in whitelist else match.group(), source_prose)
    target_prose = re.sub(r"[A-Za-z]+", lambda match: " " if match.group().lower() in whitelist else match.group(), target_prose)
    words = re.findall(r"[A-Za-z]{4,}", source_prose)
    if len(words) >= 3 and _cyrillic_ratio(target_prose) < 0.3:
        issues.append(QAIssue("possibly_untranslated", "moderate",
            "Output contains little Cyrillic; it may still be in English."))

    # Latin letter swapped for a look-alike Cyrillic one inside a code (C-276 -> С-276)
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9\-]*\d[\w\-]*", original_text):
        swapped = "".join(_HOMOGLYPHS.get(c, c) for c in tok)
        if swapped != tok and swapped in translated_text and tok not in translated_text:
            issues.append(QAIssue("homoglyph_substitution", "critical",
                f'Code "{tok}" was rewritten with Cyrillic look-alike letters.'))

    if kept_tokens is not None:
        leftovers = latin_leftovers(translated_text, kept_tokens)
        if leftovers:
            issues.append(QAIssue("latin_words_in_output", "moderate",
                f"Latin words left in the Russian text: {leftovers[:6]}. "
                f"Untranslated, or add to data/latin_whitelist.json if intentional."))

    for tok in re.findall(r"\w+", translated_text):
        if re.search(r"[A-Za-z]", tok) and re.search(r"[\u0400-\u04FF]", tok):
            issues.append(QAIssue("mixed_script_token", "moderate",
                f'"{tok}" mixes Latin and Cyrillic letters.'))
            break

    return issues