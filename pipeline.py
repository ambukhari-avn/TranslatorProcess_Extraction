"""Orchestrator: PDF -> extract -> TM lookup -> mask -> LLM -> unmask -> QA -> Word.
Usage: python pipeline.py path/to/document.pdf"""
import sys
import re
import json
import threading
import os
import ast
import hashlib
import html
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

from config import PipelineConfig
from extraction import extract_pdf, Segment
from masking import mask, unmask, CHINESE_RE
from knowledge_base import KnowledgeBase
import llm_client
import verifier
from qa_checks import run_checks, QAIssue, latin_leftovers, _cyrillic_ratio, _latin_whitelist
from word_output import write_review_document
from layout_output import write_replica
import docx_io

OCR_STUB_PREFIX = "[OCR FAILED:"
NOT_TRANSLATED = "[НЕ ПЕРЕВЕДЕНО]"
LEAK_RE = re.compile(r"\[\[\s*P\d+\s*\]\]")
PAGE_RE = re.compile(r"p{1,2}age\s*(\d+)\s*[of\s]{1,6}(\d+)", re.IGNORECASE)   # also the jumbled "PPage 15o fo 105"
FLAG_RULES = {"latin_words_in_output", "possibly_untranslated", "mixed_script_token", "low_confidence",
              "verification_unavailable"}


class JobCancelled(Exception):
    """Raised from the progress callback to stop a run; everything translated so far is already cached."""


class ReviewRequiredError(RuntimeError):
    """Outputs were saved, but unresolved QA issues prevent release."""


def _cache_key(config: PipelineConfig, text: str) -> str:
    model = config.openrouter_model_name
    return f"{model}||{text}"


def _scan_text(text: str) -> str:
    """Text for glossary matching: protected names and device tags (QM-TRIP, PUMP-A, a person's name)
    stay as written, so a glossary word inside them ("TRIP") must not count."""
    return re.sub(r"\[\[P\d+\]\]", " ", mask(text).masked_text)


def _code_fingerprint(path: Path) -> bytes:
    """The code of a module without comments or docstrings, so editing those never invalidates the cache."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:] or [ast.Pass()]
    return ast.dump(tree).encode()


@lru_cache(maxsize=1)
def _policy_digest() -> str:
    names = ("masking.py", "llm_client.py", "knowledge_base.py")
    version = b"english-to-russian-preserve-chinese-v2"
    return hashlib.sha256(version + b"".join(_code_fingerprint(Path(__file__).with_name(name)) for name in names)).hexdigest()


def _gloss_sig(kb, text: str, config: PipelineConfig = None) -> str:
    """Invalidate translations when translation policy, terminology, memory, or model settings change."""
    config = config or PipelineConfig()
    terms = kb.find_glossary_terms(_scan_text(text))
    protected = mask(text)
    payload = {
        "policy": _policy_digest(), "source": text,
        "masked": protected.masked_text, "mapping": protected.mapping,
        "terms": [term.__dict__ for term in terms],
        "tm": [entry.__dict__ for entry in kb.tm if entry.status == "approved"],
        "settings": {name: getattr(config, name) for name in (
            "target_language", "openrouter_model_name", "fallback_model_name",
            "temperature", "max_output_tokens", "fuzzy_match_threshold", "reasoning_off")},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _load_cache(path: str) -> dict:
    """Translations already paid for. Delete the file to force a fresh run
    (do that after changing the glossary, TM, protected terms or model)."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


_CACHE_LOCK = threading.Lock()


def _save_cache(path: str, cache: dict):
    """Written atomically and merged with the file's current content, so runs sharing one cache never lose each other's entries."""
    with _CACHE_LOCK:
        merged = _load_cache(path)
        merged.update(cache)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)
        os.replace(tmp, path)


def _safe_write_docx(results, docx_path, title, verification_lines=None):
    """Never lose a finished run to a locked file (e.g. the old .docx open in Word)."""
    try:
        write_review_document(results, docx_path, doc_title=title, verification_lines=verification_lines)
        return docx_path
    except PermissionError:
        stamp = datetime.now().strftime("%H%M%S")
        alt = docx_path.replace(".docx", f"_{stamp}.docx")
        print(f"      !! {docx_path} is locked (open in Word?). Saving to {alt} instead.")
        write_review_document(results, alt, doc_title=title, verification_lines=verification_lines)
        return alt


def process_segment(seg: Segment, kb: KnowledgeBase, config: PipelineConfig,
                    call_log: list) -> dict:
    # 0. Scanned page with no real text: don't send the stub to the LLM.
    if seg.text.startswith(OCR_STUB_PREFIX):
        return {
            "segment": seg,
            "translated_text": "",
            "match_type": "skipped",
            "qa_issues": [QAIssue("ocr_not_available", "critical",
                                  f"Scanned page was NOT translated: {seg.text}")],
        }

    # 0b. "Page N of M" is a fixed pattern: no AI call, and no chance of it being scrambled.
    pm = PAGE_RE.fullmatch(seg.text.strip())
    if pm and config.target_language == "ru":
        return {"segment": seg, "translated_text": f"Страница {pm.group(1)} из {pm.group(2)}",
                "match_type": "rule", "qa_issues": []}

    mask_result = mask(seg.text)
    letters_left = re.sub(r"\[\[P\d+\]\]", "", mask_result.masked_text)

    def checked(target, match_type):
        return {"segment": seg, "translated_text": target, "match_type": match_type,
                "qa_issues": run_checks(seg.text, target, [], kb.find_glossary_terms(_scan_text(seg.text)),
                                        list(mask_result.mapping.values()))}

    if not re.search(r"[A-Za-z]", letters_left):
        return checked(seg.text, "passthrough")

    # 1. Exact match from approved TM.
    exact = kb.exact_match(seg.text)
    if exact:
        target = exact.target_text
        if seg.text.rstrip().endswith(":") and not target.rstrip().endswith(":"):
            target = target.rstrip() + ":"
        return checked(target, "exact")

    # 1b. The whole cell IS a glossary term: use it directly, in the case the source uses.
    gx = kb.glossary_exact(seg.text)
    if gx and not CHINESE_RE.search(seg.text):
        src, target = seg.text.strip(), gx.target_term
        letters = [ch for ch in src if ch.isalpha()]
        if letters and all(ch.isupper() for ch in letters) and len(letters) > 1:
            target = target.upper()
        elif src[:1].isupper() and target[:1].islower():
            target = target[0].upper() + target[1:]
        if src.rstrip().endswith(":") and not target.rstrip().endswith(":"):
            target = target.rstrip() + ":"
        return checked(target, "glossary")

    # 2. Mask first: lets us detect segments with nothing to translate.
    if re.fullmatch(r"[A-Z]\.?", seg.text.strip()):
        return checked(seg.text, "passthrough")

    # 3. Context: fuzzy examples + glossary terms.
    fuzzy_matches = kb.fuzzy_match(seg.text, threshold=config.fuzzy_match_threshold)
    similar_examples = [entry for _score, entry in fuzzy_matches]
    glossary_terms = kb.find_glossary_terms(_scan_text(seg.text))

    # 4+5. Translate, restore numbers/codes. A bad reply (placeholder dropped, invented or left in,
    # or a runaway length) is retried once before it is flagged; an API failure is flagged, not fatal.
    llm_module = llm_client
    prev_raw, prev_cjk, retry_extra = "", False, ""
    for attempt in (1, 2, 3, 4):
        # attempt 2 says what went wrong; attempt 3 drops the glossary/examples and every instruction
        # that could be misread as a template (a model once answered with invented [[P..]] tokens).
        note = (retry_extra or
                ("Your previous reply was rejected: it contained [[P..]] tokens that are not in the text below, lost some "
                 "that are, or invented Chinese characters. Translate only English; keep a [[P..]] token only "
                 "if it literally appears in the text below.")) if attempt > 1 else ""
        try:
            llm_result = llm_module.translate_segment(
                masked_text=mask_result.masked_text, config=config,
                glossary_terms=glossary_terms, similar_examples=similar_examples,
                retry_note=note, minimal=(attempt == 3),
                **({"model": config.fallback_model_name} if attempt == 4 else {}),
                **({"repair_text": prev_raw} if attempt == 4 and prev_cjk else {}),
            )
        except Exception as e:
            if type(e).__name__ == "FatalAPIError":
                raise            # expired key / no credit: retrying cannot help, the whole run must stop
            if attempt < 4:      # empty reply / transient error: try again before giving up
                continue
            return {"segment": seg, "translated_text": "", "match_type": "llm",
                    "qa_issues": [QAIssue("api_error", "critical",
                                          f"Translation call failed after 4 attempts: {type(e).__name__}: {e}")]}
        call_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "location": seg.location,
            "attempt": attempt,
            "model": llm_result["model"],
            "prompt": llm_result["prompt_sent"],
            "response": llm_result["translated_text"],   # raw model output, before unmasking
            "input_tokens": llm_result["input_tokens"],
            "output_tokens": llm_result["output_tokens"],
        })
        final_text, missing_placeholders = unmask(llm_result["translated_text"], mask_result.mapping)
        runaway = len(llm_result["translated_text"]) > 6 * len(mask_result.masked_text) + 200
        # English left in the Russian, or Russian written in Latin letters: one corrective retry, then accept and flag
        english_left = [w for w in latin_leftovers(final_text, list(mask_result.mapping.values())) if not w.isupper()]
        words_in = [w for w in re.findall(r"[A-Za-z]{3,}", re.sub(r"\[\[P\d+\]\]", "", mask_result.masked_text))
                    if not w.isupper()]
        romanised = len(words_in) >= 2 and _cyrillic_ratio(final_text) < 0.15
        soft_bad = bool(english_left) or romanised
        stray_cjk = bool(CHINESE_RE.search(llm_result["translated_text"]))
        # a mostly-Russian draft with a few stray Chinese characters is repaired, not thrown away
        prev_raw = llm_result["translated_text"]
        prev_cjk = stray_cjk and _cyrillic_ratio(re.sub(r"\[\[P\d+\]\]|[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]", "", prev_raw)) > 0.6
        qa_issues = run_checks(seg.text, final_text, missing_placeholders, glossary_terms,
                       list(mask_result.mapping.values()))
        hard_bad = bool(runaway or stray_cjk or any(issue.severity == "critical" for issue in qa_issues))
        soft_bad = soft_bad or any(issue.severity == "moderate" for issue in qa_issues)
        if soft_bad and attempt < 4 and not hard_bad:
            left = ", ".join(english_left[:6])
            retry_extra = ("Your previous reply left English words untranslated" + (f" ({left})" if left else "")
                           + " or wrote Russian in Latin letters. Translate every English word into Russian and "
                           "preserve every placeholder exactly. Use the required glossary terminology.")
            continue                       # one corrective retry; a second miss is accepted and flagged by QA
        if not hard_bad and not soft_bad:
            break
        if attempt == 3 and not config.fallback_model_name:
            break

    # 6. QA.
    qa_issues = run_checks(
        original_text=seg.text, translated_text=final_text,
        unmasked_missing_placeholders=missing_placeholders,
        glossary_terms_expected=glossary_terms,
        kept_tokens=list(mask_result.mapping.values()),
    )
    return {"segment": seg, "translated_text": final_text,
            "match_type": "fuzzy" if similar_examples else "llm",
            "qa_issues": qa_issues}


def _write_replica(pdf_path, results, kb, config, call_log, output_dir, base_name, ocr_pages=None, text_fixes=None):
    """Layout-identical Word file. Table cells reuse the translations above; free-text blocks
    (titles, footers, headings) are translated here through the same TM/glossary/AI path."""
    by_text = {}
    for r in results:
        if r["translated_text"] and not any(q.severity == "critical" for q in r["qa_issues"]):
            risky = any(q.rule in FLAG_RULES or q.rule == "inconsistent_translation" for q in r["qa_issues"])
            by_text.setdefault(r["segment"].text, (r["translated_text"], risky))
        else:
            by_text.setdefault(r["segment"].text, (r["segment"].text, True))
    block_cache = {}

    def translate(text, location):
        if text in by_text:
            return by_text[text]
        pm = PAGE_RE.fullmatch(text.strip())
        if pm and config.target_language == "ru":
            return f"Страница {pm.group(1)} из {pm.group(2)}", False
        if text not in block_cache:
            page = re.match(r"page(\d+)/", location)
            seg = Segment(text=text, page=int(page.group(1)) if page else 0, kind="paragraph", location=location)
            res = process_segment(seg, kb, config, call_log)
            out = res["translated_text"]
            bad = ((not out) or LEAK_RE.search(out) or any(q.severity == "critical" for q in res["qa_issues"])
                   or any(q.rule in FLAG_RULES for q in res["qa_issues"]))
            unusable = LEAK_RE.search(out or "") or any(q.severity == "critical" for q in res["qa_issues"])
            shown = (text if unusable else out) or text
            results.append(res)
            block_cache[text] = (shown, bool(bad or unusable or shown == NOT_TRANSLATED))
        return block_cache[text]

    path = os.path.join(output_dir, f"{base_name}_{config.target_language}_replica.docx")
    if pdf_path.lower().endswith(".docx"):
        try:
            stats = docx_io.translate_docx(pdf_path, path, translate)
        except PermissionError:
            path = path.replace(".docx", f"_{datetime.now().strftime('%H%M%S')}.docx")
            stats = docx_io.translate_docx(pdf_path, path, translate)
        if stats["text_boxes"]:
            print(f"      note: {stats['text_boxes']} text box(es) in the document do not grow with longer text: check them in the output")
        if stats["has_fields"]:
            print("      note: the table of contents and page numbers refresh when Word opens the file (choose Yes to update fields)")
        print("      note: pictures are copied unchanged")
        return path
    try:
        write_replica(pdf_path, path, translate, ocr_pages, text_fixes)
    except PermissionError:
        path = path.replace(".docx", f"_{datetime.now().strftime('%H%M%S')}.docx")
        write_replica(pdf_path, path, translate, ocr_pages, text_fixes)
    return path


def _repair_flagged(results, kb, config, call_log, disk_cache, cache_path, canonical, verify_cache):
    """One corrective pass: segments the verifier scored below the threshold are translated again with the
    reviewer's findings, and the new text replaces the old one only if the verifier scores it higher."""
    weak = {}
    for r in results:
        v = r.get("verification") or {}
        if (v.get("confidence") is not None and v["confidence"] < config.verify_threshold and v.get("issues")
                and r["match_type"] in ("llm", "fuzzy") and not r["segment"].from_ocr):
            weak.setdefault((r["segment"].text, r["translated_text"]), []).append(r)
    if not weak:
        return 0
    llm_module = llm_client

    def redo(pair):
        text, _old = pair
        masked = mask(text)
        terms = kb.find_glossary_terms(_scan_text(text))
        note = ("A strict reviewer found these problems in an earlier translation of this text: "
                + weak[pair][0]["verification"]["issues"] + " Write a corrected translation that fixes them and "
                "changes nothing that was already right. If a problem is a typo or error in the English source, "
                "translate the source as written. Keep every [[P..]] placeholder exactly once.")
        try:
            reply = llm_module.translate_segment(masked_text=masked.masked_text, config=config, glossary_terms=terms,
                                                 similar_examples=[], retry_note=note)
        except Exception:
            return pair, None
        call_log.append({"timestamp": datetime.now(timezone.utc).isoformat(), "location": weak[pair][0]["segment"].location,
                         "attempt": "repair", "model": reply["model"], "prompt": reply["prompt_sent"],
                         "response": reply["translated_text"], "input_tokens": reply["input_tokens"],
                         "output_tokens": reply["output_tokens"]})
        final, missing = unmask(reply["translated_text"], masked.mapping)
        issues = run_checks(text, final, missing, terms, list(masked.mapping.values()))
        if any(issue.severity == "critical" for issue in issues):
            return pair, None
        return pair, {"segment": weak[pair][0]["segment"], "translated_text": final,
                      "match_type": weak[pair][0]["match_type"], "qa_issues": issues}

    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        drafts = {pair: new for pair, new in pool.map(redo, list(weak)) if new}
    if not drafts:
        return 0
    verifier.verify_results(list(drafts.values()), kb, config, verify_cache)
    improved = 0
    for pair, new in drafts.items():
        old_conf = weak[pair][0]["verification"]["confidence"]
        new_conf = (new.get("verification") or {}).get("confidence")
        if new_conf is None or new_conf <= old_conf:
            continue
        improved += 1
        for r in weak[pair]:
            r.update(translated_text=new["translated_text"], qa_issues=list(new["qa_issues"]),
                     verification=new["verification"], repaired=True)
        text = pair[0]
        if text in canonical:
            disk_cache[_cache_key(config, text)] = {
                "translated_text": new["translated_text"], "match_type": new["match_type"],
                "qa_issues": [issue.__dict__ for issue in new["qa_issues"]], "sig": _gloss_sig(kb, text, config)}
    _save_cache(cache_path, dict(disk_cache))
    print(f"      -> {improved} of {len(weak)} weak translation(s) improved after the verifier's feedback")
    return improved


def _harmonise(results):
    """Short labels that differ only by case, spacing or a trailing colon/period get one translation:
    the approved TM entry, else the one the verifier scored highest."""
    def norm(t):
        return re.sub(r"[\s.:]+", " ", t.lower()).strip()

    def core(t):
        return t.rstrip(" :.;")

    groups = {}
    for r in results:
        src = r["segment"].text
        if r["translated_text"] and r["match_type"] in ("llm", "fuzzy", "exact") and len(src) <= 80 and "\n" not in src:
            groups.setdefault(norm(src), []).append(r)
    for rs in groups.values():
        if len({norm(r["translated_text"]) for r in rs}) < 2:
            continue
        best = max(rs, key=lambda r: (r["match_type"] == "exact", (r.get("verification") or {}).get("confidence") or 0))
        for r in rs:
            if norm(r["translated_text"]) == norm(best["translated_text"]):
                continue
            src = r["segment"].text
            new = core(best["translated_text"]) + src[len(core(src)):]
            r["translated_text"] = new.upper() if src.isupper() and not best["segment"].text.isupper() else new
            r["qa_issues"] = [q for q in r["qa_issues"] if q.rule not in ("low_confidence", "inconsistent_translation")]
            r["verification"] = best.get("verification") or r.get("verification")
            r["harmonised"] = True

def _flag_inconsistent(results):
    """Same English text, different Russian: flag every occurrence (case and spacing ignored)."""
    def norm(t):
        return re.sub(r"[\s.:]+", " ", t.lower()).strip()
    groups = {}
    for r in results:
        if r["translated_text"] and r["match_type"] in ("llm", "fuzzy", "exact"):
            groups.setdefault(norm(r["segment"].text), []).append(r)
    for rs in groups.values():
        variants = {norm(r["translated_text"]) for r in rs}
        if len(variants) > 1:
            shown = " | ".join(sorted({r["translated_text"][:40] for r in rs})[:3])
            for r in rs:
                r["qa_issues"].append(QAIssue("inconsistent_translation", "moderate",
                                              f"Same English translated {len(variants)} different ways: {shown}"))


def _docx_xml(path):
    with zipfile.ZipFile(path) as z:
        return " ".join(z.read(n).decode("utf8", "ignore") for n in z.namelist()
                        if n.startswith("word/") and n.endswith(".xml"))


def _docx_text(path):
    return html.unescape(re.sub(r"<[^>]+>", " ", _docx_xml(path)))


def _stray_latin_in_docx(path):
    """Latin words left in a finished .docx (acronyms, codes, units, addresses and whitelisted words ignored)."""
    text = re.sub(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", _docx_text(path))
    wl = _latin_whitelist() | {"ma", "mm", "kg", "barg", "bar", "psi", "ppm", "hz", "kv", "kw", "ft", "sst"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text) if w.lower() not in wl and not w.isupper()]
    return Counter(words)


def _cjk_in_docx(path):
    return "".join(CHINESE_RE.findall(_docx_text(path)))


def _leaks_in_docx(path):
    """Count leftover [[Pn]] tokens in a finished .docx (body, tables, text boxes)."""
    return len(LEAK_RE.findall(re.sub(r"<[^>]+>", "", _docx_xml(path))))


def run_pipeline(pdf_path: str, config: PipelineConfig, output_dir: str = "output",
                 on_progress=None, cache_dir: str = None, outputs: dict = None):
    """Translate one PDF or Word file. on_progress(stage, percent) is called as the run advances and may raise
    JobCancelled to stop it. cache_dir shares the translation and verification caches between documents
    (default: caches live in output_dir). outputs, when given, receives the paths of the files written and the
    document confidence."""
    outputs = outputs if outputs is not None else {}

    def _progress(stage, percent):
        if on_progress:
            on_progress(stage, max(0, min(99, round(percent))))

    os.makedirs(output_dir, exist_ok=True)
    _progress("Extracting", 0)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    print(f"Output folder: {os.path.abspath(output_dir)}")
    print("Pictures: copied unchanged")
    print(f"[1/5] Extracting text from {pdf_path} ...")
    is_word = pdf_path.lower().endswith(".docx")
    segments = docx_io.extract_docx(pdf_path) if is_word else extract_pdf(pdf_path, config.ocr_scanned_pages)
    print(f"      -> {len(set(s.text for s in segments))} unique strings among {len(segments)} segments; "
          f"each unique string is translated once and reused")
    print(f"      -> {len(segments)} segments found "
          f"({sum(1 for s in segments if s.from_ocr)} from OCR)")
    _progress("Extracting", 5)
    chinese_segments = sum(bool(CHINESE_RE.search(seg.text)) for seg in segments)
    if chinese_segments:
        print(f"      -> Chinese detected in {chinese_segments} segment(s): preserved unchanged; English translated.")

    print(f"[2/5] Loading knowledge base ({config.tm_path}, {config.glossary_path}) ...")
    kb = KnowledgeBase(tm_path=config.tm_path, glossary_path=config.glossary_path)
    print(f"      -> {len(kb.tm)} approved TM entries, {len(kb.glossary)} glossary terms")

    print(f"[3/5] Translating {len(segments)} segments into {config.language_name} "
          f"via {config.openrouter_model_name} ...")
    call_log, results = [], []
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir or output_dir, "translation_cache.json" if cache_dir else f"{base_name}_translation_cache.json")
    disk_cache = _load_cache(cache_path)
    if disk_cache:
        print(f"      (resuming: {len(disk_cache)} translations found in {cache_path})")
    exact_count = fuzzy_count = llm_count = 0

    # Same text apart from capitals / line breaks ("U10 METHANOL INJECTION PACKAGE" vs "...Package")
    # is translated ONCE; every variant reuses that translation (re-capitalised), so one English
    # phrase can never come out as several different Russian ones.
    def _norm_key(t):
        if mask(t).mapping:
            return t
        return re.sub(r"\s+", " ", t).strip().lower()

    canon, seg_for_text = {}, {}
    for seg in segments:
        seg_for_text.setdefault(seg.text, seg)
        cur = canon.get(_norm_key(seg.text))
        if cur is None or (cur.isupper() and not seg.text.isupper()):
            canon[_norm_key(seg.text)] = seg.text

    def _match_case(src, base_text, tr):
        if src.isupper() and not base_text.isupper():
            return tr.upper()
        if src[:1].isalpha() and base_text[:1].isalpha() and src[0].isupper() != base_text[0].isupper() and tr[:1].isalpha():
            if src[0].isupper():
                return tr[0].upper() + tr[1:]
            if len(tr) > 1 and tr[1].islower():
                return tr[0].lower() + tr[1:]
        return tr

    # Translate the distinct texts in parallel first (one API call at a time was the bottleneck).
    # Everything is then assembled in document order below.
    prefetched = {}
    def _cache_ok(text):
        c = disk_cache.get(_cache_key(config, text))
        if c is None:
            return False
        if c.get("sig") != _gloss_sig(kb, text, config):
            return False
        issues = run_checks(text, c["translated_text"], [], kb.find_glossary_terms(_scan_text(text)),
                            list(mask(text).mapping.values()))
        return not any(issue.severity in ("critical", "moderate") for issue in issues)

    todo = [seg_for_text[t] for t in dict.fromkeys(canon.values()) if not _cache_ok(t)]
    stale = sum(1 for t in dict.fromkeys(canon.values())
                if _cache_key(config, t) in disk_cache and not _cache_ok(t))
    if stale:
        print(f"      {stale} stale or failed-QA cached translation(s): processing them again under current policy")
    if todo and config.workers > 1:
        print(f"      translating {len(todo)} distinct texts with {config.workers} parallel workers ...")
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            futures = {pool.submit(process_segment, s, kb, config, call_log): s for s in todo}
            for n, fut in enumerate(as_completed(futures), start=1):
                try:
                    _progress("Translating", 6 + 54 * n / len(todo))
                except JobCancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    _save_cache(cache_path, dict(disk_cache))
                    raise
                s = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:          # never lose the run to one bad segment
                    if type(e).__name__ == "FatalAPIError":
                        pool.shutdown(wait=False, cancel_futures=True)
                        _save_cache(cache_path, dict(disk_cache))
                        raise RuntimeError(
                            "STOPPED: the translation service rejected the request (" + str(e)[:200] + "). "
                            "Fix the API key / credit and run again - everything translated so far is saved "
                            "and will not be paid for twice.") from None
                    res = {"segment": s, "translated_text": "", "match_type": "llm",
                           "qa_issues": [QAIssue("api_error", "critical", f"{type(e).__name__}: {e}")]}
                key = _cache_key(config, s.text)
                prefetched[s.text] = res
                if (res["match_type"] in ("llm", "fuzzy")
                        and not any(q.rule == "api_error" for q in res["qa_issues"])):
                    disk_cache[key] = {"translated_text": res["translated_text"],
                                       "match_type": res["match_type"],
                                       "qa_issues": [q.__dict__ for q in res["qa_issues"]],
                                       "sig": _gloss_sig(kb, s.text, config)}
                if n % 25 == 0 or n == len(todo):
                    _save_cache(cache_path, dict(disk_cache))
                    print(f"      -> {n}/{len(todo)} distinct texts translated")

    memo = {}

    def _resolve(seg):
        """Result for a canonical text: disk cache, then the parallel pass, else translate now."""
        if seg.text in memo:
            return memo[seg.text]
        key = _cache_key(config, seg.text)
        if seg.text in prefetched:
            res = prefetched[seg.text]
        elif _cache_ok(seg.text):
            c = disk_cache[key]
            # QA is re-run on the cached text so today's rules apply, not the rules of the day it was cached
            qa = run_checks(original_text=seg.text, translated_text=c["translated_text"],
                            unmasked_missing_placeholders=[],
                            glossary_terms_expected=kb.find_glossary_terms(_scan_text(seg.text)),
                            kept_tokens=list(mask(seg.text).mapping.values()))
            res = {"segment": seg, "translated_text": c["translated_text"], "match_type": c["match_type"],
                   "qa_issues": qa}
        else:
            try:
                res = process_segment(seg, kb, config, call_log)
            except Exception as e:
                if type(e).__name__ == "FatalAPIError":
                    _save_cache(cache_path, dict(disk_cache))
                    raise RuntimeError("STOPPED: the translation service rejected the request (" + str(e)[:200] +
                                       "). Fix the API key / credit and run again - progress is saved.") from None
                raise
            # cache only real API results; failed calls are retried next run
            if (res["match_type"] in ("llm", "fuzzy")
                    and not any(q.rule == "api_error" for q in res["qa_issues"])):
                disk_cache[key] = {"translated_text": res["translated_text"], "match_type": res["match_type"],
                                   "qa_issues": [q.__dict__ for q in res["qa_issues"]],
                                   "sig": _gloss_sig(kb, seg.text, config)}
        memo[seg.text] = res
        return res

    for i, seg in enumerate(segments, start=1):
        base_text = canon[_norm_key(seg.text)]
        res = _resolve(seg_for_text[base_text])
        tr = res["translated_text"]
        if base_text != seg.text and tr:
            tr = _match_case(seg.text, base_text, tr)
        result = {**res, "segment": seg, "translated_text": tr, "qa_issues": list(res["qa_issues"])}
        results.append(result)
        if i % 20 == 0:
            _progress("Translating", 60 + 5 * i / len(segments))

        if result["match_type"] == "exact":
            exact_count += 1
        elif result["match_type"] == "fuzzy":
            fuzzy_count += 1
        else:
            llm_count += 1
        if i % 10 == 0 or i == len(segments):
            _save_cache(cache_path, disk_cache)
            print(f"      -> {i}/{len(segments)} done "
                  f"(exact: {exact_count}, fuzzy-guided: {fuzzy_count}, llm/other: {llm_count})")

    ocr_pages, flagged_ocr = {}, set()
    for result in results:
        seg = result["segment"]
        if seg.from_ocr and result["translated_text"] and not seg.text.startswith(OCR_STUB_PREFIX):
            ocr_pages.setdefault(seg.page, []).append(result["translated_text"])
            if seg.page not in flagged_ocr:
                flagged_ocr.add(seg.page)
                result["qa_issues"].append(QAIssue(
                    "ocr_replica_review", "moderate",
                    f"Page {seg.page} is a scan: it stays an image in the replica and its OCR text is translated on the "
                    "next page. OCR can misread words, so check it against the scan before release."))
    verify_cache = os.path.join(cache_dir or output_dir, "verification_cache.json" if cache_dir else f"{base_name}_verification_cache.json")
    fatal = None
    if config.verify_translations:
        print(f"      Verifying translations with {config.verifier_model} ...")
        _progress("Verifying", 65)
        fatal = verifier.verify_results(results, kb, config, verify_cache)
        _progress("Verifying", 75)
        if config.verify_repair and not fatal:
            _repair_flagged(results, kb, config, call_log, disk_cache, cache_path, set(canon.values()), verify_cache)
        _progress("Verifying", 82)
    _harmonise(results)
    _flag_inconsistent(results)
    print("      Writing the translation into a copy of the Word file ..." if is_word
          else "      Building layout replica (same structure as the PDF) ...")
    _progress("Writing", 83)
    replica_path = _write_replica(pdf_path, results, kb, config, call_log, output_dir, base_name, ocr_pages,
                                   {s.raw_text: s.text for s in segments if s.raw_text})
    print(f"      -> {replica_path}  (yellow = needs human review)")
    outputs["output"] = replica_path
    _progress("Writing", 90)
    verification_lines = None
    if config.verify_translations:
        fatal = verifier.verify_results(results, kb, config, verify_cache) or fatal     # blocks translated while building the replica
        summary, verification_lines = verifier.summarise(results, config, fatal,
                                                         verifier.structure_report(pdf_path, replica_path))
        report_path = os.path.join(output_dir, f"{base_name}_verification.json")
        verifier.write_report(results, summary, report_path)
        outputs["report"], outputs["documentScore"] = report_path, summary["document_confidence"]
        print(f"      -> {report_path}")
        print("      " + verification_lines[0])
        for line in verification_lines[1:]:
            print("      " + line)

    _progress("Writing", 93)
    print("[4/5] Writing audit log ...")
    log_path = os.path.join(output_dir, f"{base_name}_llm_call_log.json")
    if os.path.exists(log_path):  # keep earlier calls when resuming from the cache
        try:
            with open(log_path, encoding="utf-8") as f:
                call_log = json.load(f) + call_log
        except Exception:
            pass
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(call_log, f, ensure_ascii=False, indent=2)
    print(f"      -> {log_path}")
    outputs["log"] = log_path
    _progress("Writing", 96)

    print("[5/5] Writing Word review document ...")
    docx_path = os.path.join(output_dir, f"{base_name}_{config.target_language}_review.docx")
    docx_path = _safe_write_docx(results, docx_path,
                                 f"{base_name} — {config.language_name} translation review", verification_lines)
    print(f"      -> {docx_path}")
    outputs["review"] = docx_path

    review_leaks = _leaks_in_docx(docx_path)
    if review_leaks:   # shown to the reviewer on purpose (flagged critical rows)
        print(f"      note: {review_leaks} leftover [[P..]] token(s) in the review file, flagged for the reviewer")
    stray = _stray_latin_in_docx(replica_path)
    if stray:
        top = ", ".join(f"{w}x{n}" for w, n in stray.most_common(12))
        print(f"      note: {sum(stray.values())} Latin word(s) left in the replica (highlighted when flagged): {top}")
    replica_leaks = _leaks_in_docx(replica_path)
    if replica_leaks:   # the client-facing file must never contain one
        print(f"\n!! FAILED: {replica_leaks} leftover [[P..]] placeholder(s) in {replica_path} - do NOT send this file.")
        raise RuntimeError("placeholder leak in the replica document; see message above")

    failed = [r for r in results if any(q.rule == "api_error" for q in r["qa_issues"])]
    if failed:
        print(f"\n!! INCOMPLETE: {len(failed)} segment(s) could not be translated (see api_error rows); "
              f"the files contain the English source or blanks there. Fix the cause and run again - the "
              f"others are cached and will not be paid for twice.")
    outputs["failed"] = len(failed)
    critical = sum(1 for r in results for qa in r["qa_issues"] if qa.severity == "critical")
    moderate = sum(1 for r in results for qa in r["qa_issues"] if qa.severity == "moderate")
    print(f"\nFiles saved. {critical} critical issue(s), {moderate} moderate issue(s) flagged for review.")
    outputs["critical"], outputs["moderate"] = critical, moderate
    if config.enforce_release_gate and (critical or moderate):
        error = ReviewRequiredError(
            f"REVIEW REQUIRED: {critical} critical and {moderate} moderate issue(s). "
            "Audit and Word files were saved, but the replica is NOT approved for release. "
            "Resolve the flagged rows and regenerate both documents.")
        error.results = results
        raise error
    return results


def translate_file(input_path: str, output_dir: str, on_progress=None, config: PipelineConfig = None,
                   cache_dir: str = None) -> dict:
    """Translate a PDF or Word file; the single entry point for callers other than the command line.
    Flagged rows are not an error: the files are written and `reviewRequired` is set. Anything that stops the run
    (a rejected API key, a cancelled job) is raised: JobCancelled, RuntimeError."""
    if not input_path.lower().endswith((".pdf", ".docx")):
        raise ValueError("Only .pdf and .docx files can be translated.")
    config = config or PipelineConfig()
    outputs = {}
    try:
        run_pipeline(input_path, config, output_dir, on_progress=on_progress, cache_dir=cache_dir, outputs=outputs)
    except ReviewRequiredError:
        pass
    if outputs.get("failed"):
        raise RuntimeError(f"{outputs['failed']} segment(s) could not be translated (see the api_error rows in the review file). "
                           "Check the API key and credit and run again: what was translated is cached.")
    flagged = outputs.get("critical", 0) + outputs.get("moderate", 0)
    if on_progress:
        on_progress("Done", 100)
    return {"reviewRequired": bool(flagged), "critical": outputs.get("critical", 0), "moderate": outputs.get("moderate", 0),
            "documentScore": outputs.get("documentScore"),
            "files": {k: outputs.get(k) for k in ("output", "review", "report", "log")}}


def _main(argv):
    import argparse
    parser = argparse.ArgumentParser(description="Translate an English PDF or Word file into Russian (Word output).")
    parser.add_argument("pdf", help="path to the English PDF, or to the English .docx (translated in place, keeping its formatting)")
    parser.add_argument("--output-dir", default="output",
                        help="folder for every file this run writes (default: output); a new folder never overwrites an old one")
    args = parser.parse_args(argv)
    config = PipelineConfig()
    try:
        run_pipeline(args.pdf, config, args.output_dir)
    except ReviewRequiredError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
