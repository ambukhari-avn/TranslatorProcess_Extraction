"""Independent second-model check of every translation.

A different model (config.verifier_model) is shown each English source next to its Russian translation and
scores it strictly on meaning, completeness, terminology and grammar. Formatting is not scored. The scores become a confidence
per segment and for the whole document; segments below config.verify_threshold are flagged for review.
"""
import hashlib
import json
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

from llm_client import OPENROUTER_URL, FatalAPIError
from masking import mask
from qa_checks import QAIssue

DIMENSIONS = ("meaning", "completeness", "terminology", "grammar")
WEIGHTS = {"meaning": 0.40, "completeness": 0.25, "terminology": 0.15, "grammar": 0.20}
WATCH_BELOW = 95   # calibration on the approved documents: below 95 diverges about twice as often as 98+
SKIPPED_MATCHES = ("passthrough", "rule", "skipped", "exact")     # "exact" = already approved translation memory

RUBRIC = """You are a strict bilingual (English -> {language}) technical-translation auditor. For every numbered item,
compare the English source with its translation and score each dimension from 0 to 100 (100 = flawless).
Be harsh: most real translations have small flaws, so 100 is rare.

Dimensions
- meaning: the translation says exactly what the source says; nothing mistranslated, softened or reversed.
- completeness: nothing omitted and nothing added (words, qualifiers, list items, sentences, headings).
- terminology: correct technical terms, consistent with the required terms listed for the item.
- grammar: correct {language}: case, number, gender, agreement, punctuation and natural technical style.

Judge the translation itself, never its formatting. Do NOT lower any score, and do not mention in "issues", for:
line breaks, spacing, capitalisation, punctuation spacing (a space before a colon), list numbering style, the
look of codes, tags, numbers and units (they are checked separately by software), or Chinese text. Chinese is
deliberately NOT translated, so a Chinese span that is identical in source and translation is correct.
Equipment tags and names such as PUMP-A, ACT-B, QM-TRIP or Q1-RUN are kept in Latin on purpose: never flag
them as untranslated. Writing a unit name in Cyrillic (Barg -> БАРГ) is accepted house style.
Writing a Latin acronym letter by letter in Cyrillic (NSS -> НСС, UOG -> УОГ) is a terminology error: an acronym
is either kept in Latin or replaced by the proper Russian term.

Scoring guide: 95-100 no real defect; 85-94 only a stylistic nit; 70-84 a noticeable flaw; 50-69 a clear error;
below 50 wrong, missing or untranslated. English left in the translation (other than codes, names and acronyms)
is a completeness and meaning defect. "confidence" is your overall confidence that the translation can be released
as it is, and must not exceed your lowest dimension by more than 20.

Reply with JSON only, no commentary, in exactly this shape:
{{"results": [{{"id": 1, "meaning": 0, "completeness": 0, "terminology": 0, "grammar": 0,
"confidence": 0, "verdict": "ok|minor|major", "issues": "short concrete description of each defect, or empty"}}]}}
Include every id exactly once. Write "issues" in English."""


def _terms_for(kb, text):
    scan = re.sub(r"\[\[P\d+\]\]", " ", mask(text).masked_text)     # the glossary view used when translating
    terms = kb.find_glossary_terms(scan) if kb else []
    return "; ".join(f'"{t.en_term}" -> "{t.target_term}"' for t in terms)


def _prompt(batch, kb, config):
    lines = [RUBRIC.format(language=config.language_name), "", "Items:"]
    for n, (source, translation) in enumerate(batch, start=1):
        source, translation = (re.sub("\u27e6[0-9tb]+\u27e7", " ", t) for t in (source, translation))   # Word formatting markers
        lines.append(f"\n### {n}\nEnglish: {source}\n{config.language_name}: {translation}")
        terms = _terms_for(kb, source)
        if terms:
            lines.append(f"Required terms: {terms}")
    return "\n".join(lines)


def _call(prompt, config):
    """One chat completion from the verifier model, returned as text."""
    headers = {"Authorization": f"Bearer {config.openrouter_api_key}", "Content-Type": "application/json"}
    payload = {"model": config.verifier_model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "max_tokens": config.verify_max_tokens,
               "response_format": {"type": "json_object"}}
    if config.require_zero_data_retention:
        payload["provider"] = {"data_collection": "deny"}
    if not config.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Create one at https://openrouter.ai/keys")
    for wait in (2, 5, 15, None):
        try:
            response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=config.request_timeout)
        except (requests.Timeout, requests.ConnectionError):
            if wait is None:
                raise
            time.sleep(wait)
            continue
        if response.status_code == 400 and "response_format" in payload and "response_format" in response.text.lower():
            payload.pop("response_format")
            continue
        if response.status_code in (429, 500, 502, 503, 504) and wait is not None:
            time.sleep(wait)
            continue
        break
    if response.status_code in (401, 402, 403):
        raise FatalAPIError(f"OpenRouter HTTP {response.status_code}: {response.text[:300]}")
    if not response.ok:
        raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"OpenRouter error: {str(data['error'])[:300]}")
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("verifier returned no text")
    return text


def _score(value):
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _parse(text, count):
    """{item number: record} from the model's JSON; items it did not score are simply absent."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        rows = json.loads(text[start:end + 1]).get("results", [])
    except (ValueError, AttributeError):
        return {}
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            n = int(row.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        scores = {d: _score(row.get(d)) for d in DIMENSIONS}
        if not 1 <= n <= count or any(v is None for v in scores.values()):
            continue
        weighted = sum(WEIGHTS[d] * scores[d] for d in DIMENSIONS)
        stated = _score(row.get("confidence"))
        confidence = min(weighted, stated if stated is not None else weighted, min(scores.values()) + 25)
        verdict = str(row.get("verdict", "")).lower()
        out[n] = {**scores, "confidence": int(round(confidence)),
                  "verdict": verdict if verdict in ("ok", "minor", "major") else "minor",
                  "issues": str(row.get("issues") or "").strip()[:400]}
    return out


RUBRIC_ID = hashlib.sha256((RUBRIC + json.dumps(WEIGHTS)).encode("utf-8")).hexdigest()[:12]


def _verify_pairs(pairs, kb, config, cache, cache_path):
    """{(source, translation): record or None}. Cached pairs are free; the rest go in parallel batches."""
    def key(pair):
        return hashlib.sha256(json.dumps([RUBRIC_ID, config.verifier_model, *pair, _terms_for(kb, pair[0])],
                                         ensure_ascii=False).encode("utf-8")).hexdigest()

    out = {p: cache[key(p)] for p in pairs if key(p) in cache}
    todo = [p for p in pairs if p not in out]
    batches, cur, size = [], [], 0
    for pair in todo:
        if cur and (len(cur) >= config.verify_batch_size or size + len(pair[0]) + len(pair[1]) > config.verify_batch_chars):
            batches.append(cur)
            cur, size = [], 0
        cur.append(pair)
        size += len(pair[0]) + len(pair[1])
    if cur:
        batches.append(cur)

    state = {"fatal": None}

    def run(batch):
        if state["fatal"]:
            return batch, {}
        for attempt in (1, 2):
            try:
                found = _parse(_call(_prompt(batch, kb, config), config), len(batch))
            except FatalAPIError as error:
                state["fatal"] = str(error)[:200]
                return batch, {}
            except Exception:
                found = {}
            if len(found) == len(batch):
                break
            if attempt == 1 and found:           # ask again only for the items that were not scored
                missing = [p for n, p in enumerate(batch, start=1) if n not in found]
                ids = [n for n in range(1, len(batch) + 1) if n not in found]
                try:
                    again = _parse(_call(_prompt(missing, kb, config), config), len(missing))
                except FatalAPIError as error:
                    state["fatal"] = str(error)[:200]
                    again = {}
                except Exception:
                    again = {}
                found.update({ids[i - 1]: rec for i, rec in again.items()})
                break
        return batch, found

    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        for batch, found in pool.map(run, batches):
            for n, pair in enumerate(batch, start=1):
                if n in found:
                    out[pair] = cache[key(pair)] = found[n]
            if cache_path and found:
                _save(cache_path, cache)
    return out, state["fatal"]


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path, cache):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, path)


def verify_results(results, kb, config, cache_path=None):
    """Score every translated result that has not been verified yet; flag the weak ones.
    Sets result["verification"] and appends QA issues. Returns an error message when the service refused us."""
    pending = [r for r in results if "verification" not in r and r["translated_text"]
               and r["match_type"] not in SKIPPED_MATCHES
               and not any(q.rule == "api_error" for q in r["qa_issues"])
               and re.search(r"[A-Za-z]", re.sub(r"\[\[P\d+\]\]", "", mask(r["segment"].text).masked_text))]
    if not pending:
        return None
    cache = _load(cache_path) if cache_path else {}
    pairs = list(dict.fromkeys((r["segment"].text, r["translated_text"]) for r in pending))
    scored, fatal = _verify_pairs(pairs, kb, config, cache, cache_path)
    for r in pending:
        rec = scored.get((r["segment"].text, r["translated_text"]))
        if rec is None:
            r["verification"] = {"confidence": None}
            r["qa_issues"].append(QAIssue("verification_unavailable", "moderate",
                                          "The verifier model did not score this segment; check it manually."))
            continue
        r["verification"] = rec
        if rec["confidence"] < config.verify_threshold:
            r["qa_issues"].append(QAIssue(
                "low_confidence", "moderate",
                f"Verifier confidence {rec['confidence']}/100 ({rec['verdict']}): {rec['issues'] or 'no detail given'}"))
    return fatal


def structure_report(pdf_path, docx_path):
    """Mechanical page / table / picture comparison between the source PDF and the replica
    (for a Word source: tables, pictures and paragraphs of the original against the translated copy)."""
    if pdf_path.lower().endswith(".docx"):
        import docx_io
        expected, found = docx_io.structure_counts(pdf_path), docx_io.structure_counts(docx_path)
        return {"expected": expected, "replica": found,
                "mismatches": [f"{k}: source {expected[k]} vs translation {found[k]}" for k in expected if expected[k] != found[k]]}
    import pdfplumber
    from layout_output import clean_page, page_words
    pages = tables = pictures = 0
    with pdfplumber.open(pdf_path) as pdf:
        import pymupdf
        with pymupdf.open(pdf_path) as mu:
            for page, mpage in zip(pdf.pages, mu):
                pages += 1
                clean = clean_page(page)
                if page_words(clean):
                    tables += len(clean.find_tables())
                    pictures += sum(1 for i in mpage.get_image_info(xrefs=True) if i["xref"])
                else:
                    pictures += 1                     # a drawing/scan page is copied as one picture
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    found = {"pages": xml.count("<w:sectPr"), "tables": len(re.findall(r"<w:tbl[ >]", xml)),
             "pictures": xml.count("<pic:pic>") + xml.count("<pic:pic ")}
    expected = {"pages": pages, "tables": tables, "pictures": pictures}
    return {"expected": expected, "replica": found,
            "mismatches": [f"{k}: PDF {expected[k]} vs replica {found[k]}" for k in expected if expected[k] != found[k]]}


def summarise(results, config, fatal=None, structure=None):
    """Document-level confidence and the lines shown at the top of the review."""
    scored = [r for r in results if (r.get("verification") or {}).get("confidence") is not None]
    unscored = [r for r in results if "verification" in r and r["verification"].get("confidence") is None]
    weights = [max(10, len(r["segment"].text)) for r in scored]
    total = sum(weights)
    document = round(sum(w * r["verification"]["confidence"] for w, r in zip(weights, scored)) / total, 1) if total else None
    low = sorted((r for r in scored if r["verification"]["confidence"] < config.verify_threshold),
                 key=lambda r: r["verification"]["confidence"])
    watch = sum(1 for r in scored if config.verify_threshold <= r["verification"]["confidence"] < WATCH_BELOW)
    summary = {"model": config.verifier_model, "threshold": config.verify_threshold, "verified": len(scored),
               "unverified": len(unscored), "document_confidence": document,
               "lowest_segment": min((r["verification"]["confidence"] for r in scored), default=None),
               "below_threshold": len(low), "watch_band": watch, "stopped_by": fatal, "structure": structure}
    lines = [f"Independent verification by {config.verifier_model}: "
             + (f"document confidence {document}/100" if document is not None else "no segment could be scored")
             + f" over {len(scored)} segment(s); {len(low)} below the {config.verify_threshold} threshold"
             + (f"; {len(unscored)} could not be scored" if unscored else "") + "."
             + (f" A further {watch} scored {config.verify_threshold}-{WATCH_BELOW - 1}: worth a look, not blocking." if watch else "")]
    if fatal:
        lines.append(f"Verification stopped early: {fatal}")
    if structure:
        lines.append("Structure check (PDF vs replica): "
                     + ("identical page, table and picture counts." if not structure["mismatches"]
                        else "; ".join(structure["mismatches"]) + "."))
    return summary, lines


def write_report(results, summary, path):
    rows = [{"location": r["segment"].location, "source": r["segment"].text, "translation": r["translated_text"],
             **r["verification"]} for r in results if "verification" in r]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "segments": rows}, f, ensure_ascii=False, indent=2)
