"""
config.py
Every setting that changes between a Kazakh run and a Russian run lives here.
"""
import os
from dataclasses import dataclass


@dataclass
class PipelineConfig:
    target_language: str = "ru"  # "kk" = Kazakh, "ru" = Russian

    openrouter_api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
    # copy other slugs from openrouter.ai/models; set OPENROUTER_MODEL to override without editing this file
    openrouter_model_name: str = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
    # last resort for a segment the main model keeps getting wrong (e.g. Chinese characters, empty replies)
    fallback_model_name: str = os.environ.get("OPENROUTER_FALLBACK_MODEL", "google/gemini-3-flash-preview")
    require_zero_data_retention: bool = True
    max_output_tokens: int = 4000
    workers: int = int(os.environ.get("TRANSLATION_WORKERS", "8"))   # parallel API calls; 1 = one at a time
    request_timeout: int = 120   # seconds per API call
    reasoning_off: bool = True   # ask OpenRouter not to spend tokens 'thinking' (faster, no empty replies)
    temperature: float = 0.2

    verify_translations: bool = True   # second model scores every translation against its English source
    verifier_model: str = os.environ.get("OPENROUTER_VERIFIER_MODEL", "google/gemini-3-flash-preview")
    verify_threshold: int = 85         # segments below this confidence are flagged for review
    verify_repair: bool = True         # re-translate flagged segments once, using the verifier's findings
    verify_batch_size: int = 15
    verify_batch_chars: int = 6000
    verify_max_tokens: int = 8000
    ocr_scanned_pages: bool = False  # False: scanned pages are copied as images; True: OCR and translate them
    fuzzy_match_threshold: float = 0.85
    enforce_release_gate: bool = True

    glossary_path: str = "data/glossary.json"
    tm_path: str = "data/translation_memory.json"

    @property
    def language_name(self) -> str:
        return {"kk": "Kazakh", "ru": "Russian"}.get(self.target_language, self.target_language)

    @property
    def system_prompt_language_line(self) -> str:
        return (f"Translate the English text in this technical document into {self.language_name}. "
            "Preserve Chinese source text exactly as written; do not translate, remove, or duplicate it. "
            "Preserve identifiers, standards, names, and placeholders exactly.")
