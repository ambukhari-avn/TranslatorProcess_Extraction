"""OpenRouter translation call and prompt construction."""
import time
import requests
from typing import List
from config import PipelineConfig
from knowledge_base import GlossaryTerm, TMEntry


class FatalAPIError(RuntimeError):
    """The key is invalid/expired or the account has no credit: every further call would fail the same way."""


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def build_prompt(masked_text: str, config: PipelineConfig,
                 glossary_terms: List[GlossaryTerm],
                 similar_examples: List[TMEntry],
                 retry_note: str = "", minimal: bool = False, repair_text: str = "") -> str:
    lines = [config.system_prompt_language_line]
    cjk_rule = ("Chinese source text is protected by placeholders. Preserve every placeholder exactly once, "
                "in its original order. Translate every unprotected English word, including English next to "
                "Chinese. Do not introduce Chinese characters or translate protected source text.")
    if repair_text:  # fix only the stray non-Russian words in an otherwise good draft
        lines.append("Below is the source text and a draft translation. Correct untranslated English or invented "
                 "Chinese in the draft, changing as little else as possible. " + cjk_rule
                     + "Keep any [[P..]] tokens exactly as they are.")
        if glossary_terms:
            lines.append("Required terms: " + "; ".join(f'"{t.en_term}" -> "{t.target_term}"' for t in glossary_terms))
        lines.append(f"\nEnglish text:\n{masked_text}\n\nDraft translation:\n{repair_text}")
        lines.append("\nRespond with ONLY the corrected translation.")
        return "\n".join(lines)
    if minimal:      # last-resort retry
        lines.append("Translate every unprotected English word. " + cjk_rule + "Reply with the translation only.")
        lines.append(f"\nText to translate:\n{masked_text}")
        return "\n".join(lines)
    has_placeholders = "[[P" in masked_text
    if has_placeholders:
        lines.append(
            "Preserve the structure. Do NOT translate, alter, reorder, or remove any token that looks like "
            "[[P0]], [[P1]], etc. — leave every one of these placeholders exactly as-is in your output; "
            "they represent Chinese source text, numbers, units, names, and codes that must not change. "
            "Never invent a placeholder that is not in the input."
        )
    else:
        lines.append("Preserve the structure.")
    lines.append(
        "Do not translate abbreviations, initials, revision letters or "
        "alphanumeric identifiers; keep Latin letters Latin. Do not add or "
        "remove words (keep modifiers such as 'inline') and keep the same line breaks as the source. Translate every English word "
        "except identifiers" + (" and placeholders" if has_placeholders else "") + ". "
        + ("Do not add quotation marks or other punctuation next to a placeholder that is not in the source. "
           if has_placeholders else "")
        + "Translate terminology consistently."
    )
    lines.append("Uppercase English is not automatically an abbreviation: translate roles such as CONTRACTOR "
                 "and MANUFACTURER, and translate ordinary quoted labels such as Document title. "
                 "In coating defect lists, runs means paint runs, not work or operation. "
                 "Do not invent headings, dates, or other words absent from the source.")
    if glossary_terms:
        lines.append(
            "\nGlossary. The word choice is MANDATORY, but the glossary gives dictionary forms: change case, number and gender endings so the sentence is grammatical "
            '(e.g. "Датчик давления" becomes "датчика давления" after "паспорт"):')
        for t in glossary_terms:
            note = f" ({t.notes})" if t.notes else ""
            lines.append(f'- "{t.en_term}" -> "{t.target_term}"{note}')
    if similar_examples:
        lines.append("\nFor reference, here is how similar approved sentences were translated:")
        for entry in similar_examples:
            lines.append(f'- EN: "{entry.source_text}"')
            lines.append(f'  Approved: "{entry.target_text}"')
    lines.append(cjk_rule.strip())
    if retry_note:
        lines.append("\n" + retry_note)
    lines.append(f"\nText to translate:\n{masked_text}")
    lines.append("\nRespond with ONLY the translation. No preamble, no explanation.")
    return "\n".join(lines)


def translate_segment(masked_text: str, config: PipelineConfig,
                      glossary_terms: List[GlossaryTerm],
                      similar_examples: List[TMEntry],
                      retry_note: str = "", minimal: bool = False, model: str = "",
                      repair_text: str = "") -> dict:
    model = model or config.openrouter_model_name
    prompt = build_prompt(masked_text, config, glossary_terms, similar_examples, retry_note, minimal, repair_text)
    headers = {
        "Authorization": f"Bearer {config.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
    }
    if config.require_zero_data_retention:
        payload["provider"] = {"data_collection": "deny"}
    if config.reasoning_off:
        payload["reasoning"] = {"enabled": False}

    if not config.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Create one at https://openrouter.ai/keys")
    for wait in (2, 5, 15, None):        # rate limits (429), server errors and timeouts get a few retries
        try:
            response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=config.request_timeout)
        except (requests.Timeout, requests.ConnectionError):
            if wait is None:
                raise
            time.sleep(wait)
            continue
        if response.status_code == 400 and "reasoning" in payload and "reasoning" in response.text.lower():
            payload.pop("reasoning")          # this model/provider cannot switch thinking off: ask without it
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
    if data.get("error"):                       # some providers answer HTTP 200 with an error body
        raise RuntimeError(f"OpenRouter error: {str(data['error'])[:300]}")
    choice = (data.get("choices") or [{}])[0]
    translated = ((choice.get("message") or {}).get("content") or "").strip()
    if not translated:                          # null/empty content (e.g. the token budget went on thinking)
        raise RuntimeError(f"model returned no text (finish_reason={choice.get('finish_reason')}, "
                           f"completion_tokens={(data.get('usage') or {}).get('completion_tokens')})")
    usage = data.get("usage", {})
    return {
        "translated_text": translated,
        "prompt_sent": prompt,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "model": model,
    }