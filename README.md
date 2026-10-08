# Extraction: English to Russian technical document translation

Translates English PDF and Word (`.docx`) technical documents into Russian (Kazakh is supported by a one-line setting)
and writes the result as an editable Word file that keeps the look of the original, plus a bilingual review file that
shows every translated segment next to its source, with the items a person should check.

It is used two ways:

- **From the command line**, for one document at a time (`python pipeline.py ...`).
- **As an internal HTTP service** (FastAPI, in `app/`) that the .NET backend calls. The backend handles sign-in,
  uploads, progress, downloads and history; this repository does the translation.

```
Frontend (Angular)  ->  Backend (ASP.NET Core)  ->  Extraction service (FastAPI, this repo)  ->  OpenRouter models
   upload / progress      sign-in, files, jobs        runs the pipeline, one job per document
```

## Contents

1. [Quick start](#quick-start)
2. [What goes in and what comes out](#what-goes-in-and-what-comes-out)
3. [How a translation works](#how-a-translation-works)
4. [Configuration](#configuration)
5. [Terminology: glossary, translation memory, protected terms](#terminology-glossary-translation-memory-protected-terms)
6. [Quality control](#quality-control)
7. [Word input and PDF input](#word-input-and-pdf-input)
8. [Caching and cost](#caching-and-cost)
9. [OCR and scanned pages](#ocr-and-scanned-pages)
10. [The HTTP service](#the-http-service)
11. [Repository layout](#repository-layout)
12. [Tests](#tests)
13. [Troubleshooting](#troubleshooting)
14. [Known limitations](#known-limitations)

---

## Quick start

Requirements: Python 3 (developed and tested on 3.14), an [OpenRouter](https://openrouter.ai) API key with credit, and
optionally the Tesseract OCR program (only needed to translate scanned pages, see [OCR](#ocr-and-scanned-pages)).

```powershell
cd C:\Users\ambhukari\Documents\RussianTranslator\Extraction
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt            # add -r requirements-dev.txt to run the service tests

$env:OPENROUTER_API_KEY = "<your key>"      # the key is read from the environment, never from a file in the repo
python pipeline.py "C:\path\to\source.docx"
```

Results are written to `output\` (see below). A PDF works the same way: `python pipeline.py "C:\path\to\source.pdf"`.

Command line options:

| Option | Meaning |
|---|---|
| `pdf` (positional) | Path to the English `.pdf` or `.docx`. The argument keeps its old name `pdf`, but Word files are accepted. |
| `--output-dir <folder>` | Write every file to this folder instead of `output\`. Use a new folder to keep an earlier run intact. |

To reuse what an earlier run already paid for, copy its `*_translation_cache.json` and `*_verification_cache.json`
into the new output folder before you start. See [Caching and cost](#caching-and-cost).

**Exit status.** `0` means the run finished with nothing flagged. A non-zero status after the files were saved means
*review required*: critical or moderate issues remain. The files are drafts, not approved deliverables (see
[Release gate](#release-gate)). A rejected API key or no credit stops the run immediately with a clear message.

## What goes in and what comes out

**Input:** one `.pdf` or `.docx`. Chinese text that already sits in the source (for example Chinese next to English in
the same table cell) is kept exactly as written; Chinese the model invents is flagged as an error.

**Output**, in `output\` (or `--output-dir`), where `<name>` is the source file name without its extension and `ru` is
the target language code:

| File | What it is |
|---|---|
| `<name>_ru_replica.docx` | **The deliverable.** The translated document as a Word file. For a Word source it is the original file edited in place; for a PDF source it is rebuilt page by page. Flagged text is highlighted yellow. |
| `<name>_ru_review.docx` | The bilingual review file: one row per segment (English, Russian, confidence, QA flags, verifier explanation) and a summary at the top. Editing it does **not** update the replica. |
| `<name>_verification.json` | Every verifier score and reason, plus the document confidence. |
| `<name>_llm_call_log.json` | Every model call (model, attempt, tokens, outcome): the audit trail and the cost evidence. |
| `<name>_translation_cache.json`, `<name>_verification_cache.json` | Caches that make a rerun free for unchanged segments. |

Under the HTTP service the same files are written into the job's output folder, and the caches are shared by all jobs in
`<state>/cache/`.

## How a translation works

The orchestrator is `pipeline.py` (`run_pipeline`, called through `translate_file`). In order:

1. **Extract** (`extraction.py` for PDF, `docx_io.py` for Word). The document becomes *segments*: paragraphs and table
   cells, each with its page and position. Progress stage: `Extracting`.
2. **Translation memory.** If an approved translation for the exact source text exists (`data/translation_memory.json`),
   it is used and no model call is made. Matching ignores case, spacing and a trailing colon or period.
3. **Glossary.** Matching glossary terms are sent to the model with the segment, so approved terminology wins.
4. **Masking** (`masking.py`). Numbers, units, codes, standards, URLs, Chinese spans and protected names are replaced by
   placeholders such as `[[P3]]` before the text leaves the machine, then restored afterwards. A placeholder the model
   drops, changes, duplicates or reorders is a QA flag, so identifiers cannot silently change.
5. **Translate** (`llm_client.py`). The text goes to the main model through OpenRouter, with up to 8 calls in parallel.
   Progress stage: `Translating`. Each segment has a **retry ladder** of four attempts:
   - attempt 1: normal prompt;
   - attempt 2: says what went wrong the first time (English left over, Chinese characters, a lost placeholder);
   - attempt 3: a minimal prompt without the glossary and examples;
   - attempt 4: the fallback model.

   An expired key or no credit is not retried: it stops the whole run, because retrying cannot help.
6. **QA** (`qa_checks.py`). Mechanical checks on every segment, listed under [Quality control](#quality-control).
7. **Independent verification and repair** (`verifier.py`). A *different* model scores every translation against its
   English source from 0 to 100; weak segments are re-translated once using the verifier's findings. Progress stage:
   `Verifying`. Can be switched off.
8. **Harmonise.** The same source text gets the same translation across the whole document.
9. **Write the files** (`layout_output.py` for the PDF replica, `docx_io.py` for the Word path, `word_output.py` for the
   review file), then the checks described under [Release gate](#release-gate). Progress stage: `Writing`, then `Done`.

## Configuration

All pipeline settings are in `config.py` (`PipelineConfig`). Environment variables override the ones that change between
machines:

| Setting | Default | Purpose |
|---|---|---|
| `target_language` | `"ru"` | `"ru"` for Russian, `"kk"` for Kazakh. Output file names carry this code. |
| `OPENROUTER_API_KEY` | none | **Required.** Read from the environment. |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash` | Main translation model (any slug from openrouter.ai/models). |
| `OPENROUTER_FALLBACK_MODEL` | `google/gemini-3-flash-preview` | Last resort for a segment the main model keeps getting wrong. |
| `OPENROUTER_VERIFIER_MODEL` | `google/gemini-3-flash-preview` | The second model that scores translations. |
| `TRANSLATION_WORKERS` | `8` | Parallel model calls (`1` = one at a time). |
| `verify_translations` | `True` | Run the independent verification stage. |
| `verify_threshold` | `85` | Segments scored below this are flagged `low_confidence`. |
| `verify_repair` | `True` | Re-translate flagged segments once with the verifier's feedback. |
| `enforce_release_gate` | `True` | Exit non-zero when critical or moderate issues remain. |
| `ocr_scanned_pages` | `False` | `False`: scanned pages are copied as images. `True`: OCR and translate them. |
| `require_zero_data_retention` | `True` | Asks OpenRouter to route only to providers that do not retain prompts. |
| `request_timeout`, `max_output_tokens`, `temperature` | `120`, `4000`, `0.2` | Model call limits. |
| `glossary_path`, `tm_path` | `data/glossary.json`, `data/translation_memory.json` | Terminology files. |

Service settings are listed under [The HTTP service](#the-http-service).

**Important:** the service and the command line read real environment variables. They do **not** read the `.env` file by
themselves. To load a `.env` into your PowerShell session before starting (nothing is printed):

```powershell
Get-Content .env | ForEach-Object { if ($_ -match '^\s*([^#=\s][^=]*)=(.*)$') { Set-Item "env:$($matches[1].Trim())" $matches[2].Trim().Trim('"') } }
```

`.env` is git-ignored. Keys never go into the repository or into chat.

## Terminology: glossary, translation memory, protected terms

All terminology lives in `data/` as plain JSON, so it can be reviewed and versioned:

| File | Purpose |
|---|---|
| `glossary.json` | About 240 approved term pairs (`en_term`, `target_term`, optional `notes` sent to the model, `internal_note` for humans only, `exact_only` for generic words such as "Approved" or "Date" that apply only when the whole cell is that word). |
| `translation_memory.json` | Approved whole-sentence translations (`source_text`, `target_text`, `status`, `origin`). Exact matches skip the model entirely. |
| `protected_terms.json` | Names, codes and abbreviations that must never be translated (people, company names, tag names such as `QM-ON`). They are masked like any identifier. |
| `latin_whitelist.json` | Latin words that are *allowed* to remain in the Russian output (standards such as ATEX and units such as ppm); anything else in Latin letters is flagged. |

Rules that apply: an **approved wording always wins** over the model's choice, and the model never invents Chinese. Latin
text that was in the English source (identifiers, product names) is preserved as is, not converted.
The `*.csv`, `*.prev*.json` and `*.backup_*.json` files next to them are analysis snapshots and are git-ignored; only the
four live files above are part of the repository.

The source abbreviation `OFT` is retained rather than corrected to `DFT`: confirm the apparent source typo with the
responsible engineer.

## Quality control

### Mechanical checks (`qa_checks.py`)

| Severity | Flags |
|---|---|
| critical | `api_error` (segment could not be translated), `empty_output`, `numeric_mismatch`, `missing_placeholder`, `leaked_placeholder`, `protected_token_mismatch`, `chinese_mismatch`, `homoglyph_substitution`, `suspiciously_short_output`, `suspiciously_long_output`, `ocr_not_available` |
| moderate | `missing_glossary_term`, `inconsistent_translation`, `possibly_untranslated`, `latin_words_in_output`, `mixed_script_token`, `ocr_replica_review`, `low_confidence` and `verification_unavailable` (from the verifier) |

### Independent verification

A second model (`verifier_model`) scores meaning, completeness, terminology and grammar strictly from 0 to 100.
Formatting (line breaks, spacing, capitalisation, how codes or numbers look) is **not** scored; software checks it. A
segment's confidence is the lowest of the weighted score, the model's own figure and the weakest dimension plus 25. The
document confidence is the length-weighted average of the segments. Segments below `verify_threshold` are flagged,
highlighted yellow in the replica and listed in the review file with the verifier's explanation. A mechanical check
also compares the page, table and picture counts of the PDF and its replica.

### Release gate

Unless `enforce_release_gate = False`, any remaining critical or moderate issue makes the command line exit non-zero
**after** the review and replica files are saved. Those files are drafts. Resolve the flagged rows (fix the glossary,
the translation memory or the source cause, then rerun) rather than editing the review file.

Two further checks protect the client-facing file:

- A leftover `[[P..]]` placeholder in the **replica** is treated as a failure ("do NOT send this file"). In the review
  file it is shown on purpose, flagged for the reviewer.
- If any segment could not be translated (`api_error`), the run is reported as **failed**, and what was translated is
  cached so a rerun does not pay for it twice.

Through the HTTP service the gate does not fail the job: a finished translation with flagged rows is `Done` with
`reviewRequired: true`, and the number of flagged items is returned so the frontend can show "N items need review".

## Word input and PDF input

**Word (`.docx`): preferred when you have it.** The translation is written into a copy of the original file, so
styles, tables, numbering, headers, footers and pictures stay exactly as the client made them, and longer Russian text
reflows the way it would in Word. What the Word path does:

- Fixed ("exactly") table row heights become "at least", so longer text is not clipped.
- Tracked changes are accepted.
- The table of contents and page-number fields refresh when Word opens the file.
- Header and footer tables that sit inside fixed text boxes are unwrapped so they cannot overflow; text boxes autofit.
- A paragraph whose formatting changes inside (a bold lead-in) carries markers that the translator must keep: `⟦2⟧`
  (a formatting group), `⟦t⟧` (a tab) and `⟦b⟧` (a line break). They are masked like any placeholder, so a lost marker
  is a QA flag.
- Pictures are copied unchanged. The run prints a note for each text box and picture.

**PDF.** For clients who send only a PDF, the replica is **rebuilt** page by page (`layout_output.py`): page size and
orientation, tables as real Word tables, free text as flowing paragraphs or floating text boxes, borderless grids,
hanging markers and label columns, pictures as spacer paragraphs, and the repeated header and footer of each page as
real Word header and footer stories. Text that would overlap a footer or runs onto the next page moves down instead of
overlapping. This rebuild is an approximation of the original layout, so a Word source always gives the better result.

**Pictures.** Every picture is copied unchanged. Text inside pictures is not translated.

## Caching and cost

Translating costs money (one model call per uncached segment, plus the verifier). Two caches keep reruns cheap:

- the **translation cache** (`*_translation_cache.json`, or `<state>/cache/translation_cache.json` under the service);
- the **verification cache** (`*_verification_cache.json`).

Entries are keyed by a fingerprint of the translation code, the relevant terminology, the approved translation memory,
the protected content and the model settings, so a cached entry is invalidated automatically when any of those change,
and **stale entries are retried on the next run (which can cost money)**. QA-only and reporting-only edits do not
invalidate the translation cache. Existing caches and logs are never deleted. Finish related terminology or policy changes
before rerunning a large document, to avoid paying twice.

Under the HTTP service the cache is shared by all jobs, so a sentence translated for one document is free in the next.

## OCR and scanned pages

By default (`ocr_scanned_pages = False`) a page with no extractable text is copied into the replica as an image and is
not translated. With `ocr_scanned_pages = True`, such pages are rendered and read with Tesseract, translated, and the
translation appears in the review file (the replica keeps the page image and flags it for manual replacement or
annotation before release). OCR is page-level: text inside pictures on a page that does have text is not read.

OCR needs the separate **Tesseract program**, not just the `pytesseract` Python package, with English language data and
`chi_sim` / `chi_tra` for scanned Chinese. The pipeline looks on `PATH`, in `TESSERACT_CMD` and in the standard Windows
folders:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Missing programs, missing language data, rendering failures and empty OCR output are reported with their real cause.
Disagreements between the Chinese and English in the source are kept, not resolved by guessing.

## The HTTP service

The FastAPI service in `app/` runs translations for the backend. Run it **from the repository root** (the pipeline reads
`data/` relative to it) and keep it off the public network: only the backend should reach it, using a shared secret in
the `X-API-Key` header.

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002
```

Use any free port and set the same address as `Extraction:BaseUrl` in the backend (`http://127.0.0.1:8002` by default
there). Interactive API docs are at `http://127.0.0.1:8002/docs`.

### Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | none | The model key (also read by the pipeline). |
| `EXTRACTION_API_KEY` | empty | Shared secret the backend sends in `X-API-Key`. **Empty means no check: development only.** Must equal the backend's `Extraction:ApiKey`. |
| `EXTRACTION_FILES_ROOT` | `<repo>\files` | Every input and output path a client sends must lie inside this folder, and it must be **the same folder as the backend's `Storage:Root`**. |
| `EXTRACTION_STATE_DIR` | `<repo>\state` | Job state files (`jobs\*.json`) and the shared translation cache. |
| `EXTRACTION_MAX_JOBS` | `2` | Jobs that run at the same time; the rest wait as `Queued`. |

Generate a key with `[Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }))`.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Start a job: `{jobId, inputPath, outputDir, options:{language, verify}}`, answered `202` with status `Queued`. The same `jobId` again returns the existing job. |
| `GET /jobs/{jobId}` | `{status, progress, stage, critical, moderate, documentScore, reviewRequired, error, files, createdAt, startedAt, finishedAt}` where `files` holds the paths of the output, review, report and log files. |
| `GET /jobs?status=Running` | List jobs, newest first. |
| `POST /jobs/{jobId}/cancel` | Cancel a queued job at once, or a running one at its next progress report. |
| `GET /health` | `{status, running, queued, maxConcurrent, openrouterKeyConfigured}`. Needs no key. |

Statuses: `Queued`, `Running`, `Done`, `Failed`, `Cancelled`. Stages reported while running: `Extracting`,
`Translating`, `Verifying`, `Writing`, `Done`, with a percentage from 0 to 100.

Every error is `{"code": "...", "message": "..."}`. Codes: `UNAUTHORIZED`, `VALIDATION_ERROR`, `PATH_OUTSIDE_ROOT`,
`INPUT_NOT_FOUND`, `UNSUPPORTED_FILE_TYPE`, `JOB_NOT_FOUND`, `JOB_EXISTS`, `JOB_FINISHED`, `INTERNAL_ERROR`.

### Behaviour worth knowing

- A job's state is saved to `<state>\jobs\<jobId>.json`. A job left `Queued` or `Running` by a restart is marked
  `Failed` on start-up (it is not resumed; the cache means a rerun is cheap).
- A run in which segments could not be translated is `Failed` (for example no API key or no credit); flagged rows alone
  are not a failure.
- Paths are checked to be inside `EXTRACTION_FILES_ROOT`, so the service cannot be made to read or write elsewhere.
- Code layout: `app/api/routes` (endpoints), `app/schemas` (request and response models), `app/services` (job manager and
  the translator wrapper), `app/core` (settings and errors).
- Calling it from Python without the HTTP layer: `pipeline.translate_file(input_path, output_dir, on_progress, config, cache_dir)`.

## Repository layout

```
pipeline.py          orchestrator and command line (run_pipeline, translate_file)
config.py            every pipeline setting (PipelineConfig)
extraction.py        PDF -> segments (pdfplumber, PyMuPDF; OCR for scanned pages)
docx_io.py           Word reader and in-place writer
layout_output.py     PDF replica writer (page-by-page Word rebuild)
word_output.py       review document writer
masking.py           placeholders for numbers, codes, Chinese, protected names
knowledge_base.py    glossary and translation memory
llm_client.py        OpenRouter calls and prompts
qa_checks.py         mechanical checks
verifier.py          independent second-model scoring and repair
app/                 the FastAPI service (see above)
data/                glossary, translation memory, protected terms, Latin whitelist
tests/               service tests (job manager, translate_file, API)
test_*.py, smoke_test.py   pipeline tests
input/, files/, output*/, state/   working folders (git-ignored)
```

## Tests

Nothing below calls the paid API: the model is simulated.

```powershell
python -m unittest test_corrections test_verifier test_docx -v      # pipeline: masking, QA, glossary, Word path, verifier (49 tests)
python -m unittest discover -s tests -t .                           # service: job manager, translate_file, API (23 tests; needs requirements-dev.txt)
python smoke_test.py                                                # quick offline check of masking, knowledge base, QA and review output
```

They cover Chinese preservation, English translation against simulated responses, technical identifiers, numeric signs,
glossary scopes, cache reuse, Word generation, release blocking, the job lifecycle (queue, cancel, restart recovery) and
every API endpoint including key and path checks.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `uvicorn` is "not recognized" | Use `python -m uvicorn ...` so it runs from the Python you installed the packages into. |
| "Port ... is already in use" or the service answers on a port you did not start | An earlier copy is still running. Find it with `Get-NetTCPConnection -LocalPort <port> -State Listen`, or use another port and update the backend's `Extraction:BaseUrl`. |
| Backend job fails with `INPUT_NOT_FOUND`, or Done but "file not found in the shared storage folder" | `EXTRACTION_FILES_ROOT` and the backend's `Storage:Root` are not the same folder. |
| Calls return `401 UNAUTHORIZED` | `EXTRACTION_API_KEY` and the backend's `Extraction:ApiKey` differ, or the service was started before the variable was set (restart it). |
| Setting a key in `.env` has no effect | The service does not read `.env`; load it into the session first (see [Configuration](#configuration)). |
| `/health` shows `openrouterKeyConfigured: false`, or a run ends with "could not be translated" | `OPENROUTER_API_KEY` is not set in the terminal that started the service, or the key has no credit. |
| A run is `Failed` after a restart | Expected: unfinished jobs are marked `Failed` on start-up. Start the translation again; the cache keeps the cost low. |
| Non-zero exit but files exist | Review required (critical or moderate issues). Open the review file, fix the cause, rerun. |
| Chinese or Latin text in the Russian output | Check `latin_whitelist.json` and `protected_terms.json`; unexpected Latin is flagged `latin_words_in_output`. |

## Known limitations

- **PDF layout is rebuilt, not preserved.** Dense pages (for example a requirement-matrix page with many overlapping
  words), filled shapes, spaced digits in a table of contents and static footer page numbers can still differ from the
  original, and Russian is longer than English so the page count can grow. A Word source avoids all of this.
- **Layout rules were tuned on this company's document templates.** Other templates may need further adjustments.
- **Pictures are not translated**, and scanned pages are translated only with `ocr_scanned_pages = True` and Tesseract.
- **The Word path** has been verified on small documents and the company templates; run a new client template through it
  and review the result before relying on it.
- **Terminology decisions are a business matter**: the glossary, the NDT term list, wording such as "Pump Datasheet" and
  the FAT wording need the responsible engineer's approval; the pipeline applies whatever is approved.
- **Translation quality is a model output.** The checks catch numbers, identifiers, placeholders and weak segments, but
  a person should review flagged rows before a document is released to a client.
