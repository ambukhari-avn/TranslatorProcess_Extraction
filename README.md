# Extraction: English to Russian technical document translation

Translates English PDF and Word (`.docx`) technical documents into Russian and delivers the result as an editable Word
file that keeps the look of the original, together with a bilingual review file that shows every translated segment
next to its source and marks the items a person should check.

## What you give it, what you get

**In:** one `.pdf` or `.docx`. Chinese text already present in the source (for example next to English in the same table
cell) is kept exactly as written.

**Out**, where `<name>` is the source file name and `ru` the target language:

| File | What it is |
|---|---|
| `<name>_ru_replica.docx` | **The deliverable.** The translated document as a Word file. A Word source is edited in place; a PDF source is rebuilt page by page. Text that needs checking is highlighted yellow. |
| `<name>_ru_review.docx` | The bilingual review file: one row per segment with the English, the Russian, a confidence score, any flags, and the verifier's explanation, plus a summary at the top. |
| `<name>_verification.json` | Every confidence score with its reason, and the document score. |
| `<name>_llm_call_log.json` | A record of every model call: the audit trail. |

Kazakh is also supported as a target language.

## How a translation works

1. **Read the document** into paragraphs and table cells, each with its page and position.
2. **Approved translations first.** A sentence that already has an approved translation (the translation memory) is used
   as is, with no model involved. Approved glossary terms are given to the model so the company's wording wins.
3. **Protect what must not change.** Numbers, units, codes, standards, web addresses, names and any Chinese text are
   replaced by placeholders before translation and put back afterwards. If one is lost, changed, duplicated or
   reordered, it is flagged instead of being silently accepted.
4. **Translate.** Each segment goes to the translation model. A segment that comes back wrong (English left over,
   invented Chinese characters, a lost placeholder) is retried with a clearer instruction, then a simpler one, then a
   second model.
5. **Check.** Mechanical checks run on every segment (see below).
6. **Verify.** A different model compares each Russian segment with its English source and scores it. Weak segments are
   translated again once using the verifier's findings.
7. **Make the files.** The same source text gets the same translation across the document, then the Word and review
   files are written and checked once more.

## Quality control

**Mechanical checks.** Every segment is checked for: a missing or empty translation, a changed number, a lost or leaked
placeholder, a changed protected name or code, Chinese that is not in the source, look-alike letters from another
alphabet, an output that is far too short or too long, an approved glossary term that was not used, the same source
translated two different ways, English left untranslated, and mixed alphabets inside one word.

**Independent verification.** A second model scores meaning, completeness, terminology and grammar from 0 to 100.
Formatting (line breaks, spacing, capital letters, how codes or numbers look) is not scored; the mechanical checks cover
it. Each segment gets a confidence, and the document gets one overall score weighted by length. Segments below the
threshold (85) are highlighted in the Word file and listed in the review file with an explanation.

**Release gate.** Anything critical or moderate that remains makes the result a **draft**: the files are saved, but the
command exits with a warning status, and the service reports `reviewRequired` with the number of flagged items so the
frontend can show "N items need review". A leftover placeholder in the client-facing Word file is treated as a failure
and the file must not be sent. If any segment could not be translated at all, the run is reported as failed.

Editing the review file does not change the translated Word file. Fix the cause (the glossary, the approved translation
memory, or the source) and translate again.

## Word and PDF sources

**Word (`.docx`) is the preferred source.** The translation is written into a copy of the original file, so styles,
tables, numbering, headers, footers and pictures stay exactly as the client made them, and longer Russian text reflows as
it would in Word. Fixed row heights become "at least" so text is not clipped, tracked changes are accepted, and the table
of contents and page numbers refresh when the file is opened in Word.

**PDF.** When only a PDF exists, the Word file is rebuilt page by page: page size and orientation, tables as real Word
tables, flowing paragraphs and text boxes, repeated headers and footers, and pictures in place. Text that would run into
a footer or past the page moves down instead of overlapping. The rebuild is an approximation of the original layout, so a
Word source always gives the better result.

Pictures are copied unchanged and the text inside them is not translated. Scanned pages (a PDF page with no text) can be
read with OCR and appear in the review file; the Word file keeps the page image and flags it for manual replacement.

## Terminology

Approved wording is kept in plain JSON files in `data/`, so it can be reviewed and versioned:

- `glossary.json`: approved English to Russian term pairs (about 240).
- `translation_memory.json`: approved whole-sentence translations.
- `protected_terms.json`: names, codes and abbreviations that are never translated.
- `latin_whitelist.json`: Latin words allowed to stay in the Russian text, such as standards and units.

Approved wording always wins over the model's choice. The model never invents Chinese. Latin text from the English
source (identifiers, product names) is preserved as is.

## Cost

Each segment that is not already approved or cached costs one translation call, plus the verifier. Translations are
cached, so translating the same document again, or a document that shares sentences with an earlier one, costs little. A
cached entry is refreshed automatically when the terminology or translation settings it depended on change.

## The service

The pipeline runs behind a small HTTP service that only the backend calls. It accepts a
translation job for a file, reports progress and status (`Queued`, `Running`, `Done`, `Failed`, `Cancelled`), and can
cancel a job. A finished translation with flagged rows is `Done` with `reviewRequired` set. Interactive API documentation
is served at `/docs` while the service runs.

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Start a translation job |
| `GET /jobs/{jobId}` | Status, progress, stage, counts and the output file paths |
| `GET /jobs` | List jobs |
| `POST /jobs/{jobId}/cancel` | Cancel a job |
| `GET /health` | Service health |

## Known limitations

- A PDF is rebuilt, not preserved: very dense pages, filled shapes, spaced digits in a table of contents and static
  footer page numbers can still differ from the original, and Russian text is longer, so the page count can grow.
- The layout rules were tuned on this company's document templates; other templates may need adjusting.
- Text inside pictures is not translated.
- Translation quality is a model output. The checks catch numbers, identifiers, placeholders and weak segments, but a
  person should review flagged rows before a document is released to a client.
- Terminology decisions (the glossary, the NDT term list, wording such as "Pump Datasheet" and the FAT wording) belong to
  the responsible engineer; the pipeline applies whatever has been approved.
