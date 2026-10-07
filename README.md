# PDF Extraction

## Translation Pipeline

```powershell
python pipeline.py "path\to\source.pdf"
```

Option: `--output-dir <folder>` writes every file to another folder, so an earlier run is never overwritten.
To reuse what an earlier run already paid for, copy its `*_translation_cache.json` and
`*_verification_cache.json` into the new folder first.

## Word input (preferred when you have the .docx)

```powershell
python pipeline.py "path	o\source.docx"
```

The translation is written into a copy of the original Word file: styles, tables, numbering, headers, footers and
pictures stay exactly as the client made them, and longer Russian text reflows as it would in Word. Fixed ("exactly")
row heights become "at least", tracked changes are accepted, and the table of contents and page numbers refresh when
Word opens the file. A paragraph whose formatting changes inside (a bold lead-in) carries markers `⟦2⟧` (formatting
group), `⟦t⟧` (tab), `⟦b⟧` (line break) that the translator must keep; they are masked like any placeholder, so a lost
one is a QA flag. Text boxes do not grow with their text and pictures are copied unchanged: the run prints a note for
each. The PDF path below remains for clients who send only a PDF. `docx_io.py` holds the Word reader and writer.

The pipeline writes a bilingual review DOCX, a layout replica DOCX, an API call log,
and a translation cache to `output`. Configure the model and target language in
`config.py`; API keys are read from environment variables.

English is translated into Russian by default. Chinese source text is copied
unchanged, including Chinese alongside English in the same cell. Chinese-only
segments require no model call. Chinese spans, technical identifiers, standards,
alarm codes, numbers, and protected names are masked before translation and restored
afterwards. Added, changed, missing, duplicated, or reordered protected content is
flagged rather than silently accepted.

Cache entries are invalidated automatically when translation code, relevant
terminology, approved translation memory, protected content, or model settings
change. Existing caches and logs are not deleted. The first run after these
corrections will reprocess stale entries and may incur API charges.
QA-only and reporting-only edits no longer invalidate the entire translation
cache; current QA is still rerun and failed entries are retried. The migration to
this narrower fingerprint invalidates older signatures once. Finish related
translation-policy changes before rerunning large documents to avoid repeated costs.
The source abbreviation `OFT` is retained, not automatically corrected to `DFT`;
confirm the apparent source typo with the responsible engineer.

By default, unresolved critical or moderate QA issues cause a nonzero exit status
after audit and review files have been saved. Those files are drafts, not approved
deliverables. Chinese preserved from the source is allowed; Chinese invented by
the model is not. Editing a review DOCX does not update its replica or cache.
Correct the terminology/TM or translation cause, then regenerate both files.

## Independent Verification

After translation, a second model (`verifier_model`, default `google/gemini-3-flash-preview` through
OpenRouter; override with `OPENROUTER_VERIFIER_MODEL`) compares every English segment with its Russian
translation and scores it strictly from 0 to 100 on meaning, completeness, terminology and grammar.
Formatting (line breaks, spacing, capitalisation, how codes or numbers look) is not scored; software checks it. The segment confidence is the lowest of the weighted score, the model's own figure and the weakest
dimension plus 25. The document confidence averages the segments, weighted by length.

- Segments below `verify_threshold` (default 85) are flagged `low_confidence` (moderate), highlighted yellow in
  the replica and listed in the review with the verifier's explanation.
- Segments the verifier could not score are flagged `verification_unavailable`; an expired key stops
  verification and says so.
- A mechanical check compares page, table and picture counts between the PDF and the replica.
- Outputs: a confidence per row and a summary at the top of the review DOCX, plus
  `<name>_verification.json` (every score) and `<name>_verification_cache.json` (reused on reruns).
- Set `verify_translations = False` in `config.py` to skip the stage. Tests: `python -m unittest test_verifier -v`.

## Pictures

Every picture is copied unchanged; text inside pictures is not translated.

## Offline Checks

```powershell
python -m unittest test_corrections test_verifier test_docx -v
python smoke_test.py
```

Regression tests exercise Chinese preservation, English translation with simulated
HTTP responses, technical identifiers, numeric signs, glossary scopes, cache reuse,
Word generation, and release blocking without paid API calls.

## OCR Prerequisites

OCR requires the separate Tesseract executable, not only the `pytesseract` Python
package. Install English language data and `chi_sim`/`chi_tra` for scanned Chinese.
The pipeline checks PATH, `TESSERACT_CMD`, and standard Windows installation folders.

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Missing executables, language data, rendering failures, and empty OCR output are
reported with their actual cause. Scanned pages remain original images in the
replica; OCR translations appear in the review and require manual page replacement
or annotation before release. Source Chinese/English disagreements are retained,
not resolved by guessing engineering requirements.

This standalone module extracts positioned text and detected tables from PDFs. Pages with no extractable text are rendered and passed to Tesseract OCR.

## Entry point

```python
from extract_document import extract_document

blocks = extract_document("document.pdf")
```

The result is a list of dictionaries. Each block has a one-based `page`, `text`, PDF-point coordinates (`x0`, `top`, `x1`, `bottom`), `font_size` when available, `kind` (`text` or `table`), and `source` (`pdfplumber` or `ocr`). Table blocks also contain a `cells` matrix. OCR blocks have no font size.

OCR is page-level: it runs when a page has no extractable words. Text inside image regions on an otherwise text-bearing page is not separately OCRed.

## Run standalone

Install the Python dependencies and the Tesseract OCR executable, with the desired language data installed and Tesseract available on `PATH`:

```powershell
python -m pip install -r requirements.txt
python -c "from extract_document import extract_document; print(extract_document(r'document.pdf'))"
```

Set `ocr_language` to a Tesseract language code when needed, for example `extract_document("document.pdf", ocr_language="rus")`. `dpi` controls the raster resolution used for OCR and defaults to 300.