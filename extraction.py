"""Turns a PDF into Segment objects (paragraphs and table cells) with page and position info."""

from dataclasses import dataclass
from typing import List, Optional
import pdfplumber


@dataclass
class Segment:
    text: str
    page: int
    kind: str  # "paragraph" | "table_cell"
    location: str  # e.g. "page3/table1/row2/col1"
    font_size: Optional[float] = None
    from_ocr: bool = False
    table_id: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None
    raw_text: Optional[str] = None   # the text as read from the PDF, when it had to be repaired


def extract_pdf(pdf_path: str, ocr_scanned: bool = False) -> List[Segment]:
    """Table cells and text blocks of every page. A page without extractable text (a scan) is skipped, so the
    replica copies it as an image, unless ocr_scanned is set, which reads and translates it with OCR.
    Blocks come from layout_output._blocks, the splitter the Word replica uses, so what is translated is what gets placed."""
    import pymupdf
    from layout_output import clean_page, page_words, table_data, _blocks, _Spans, spaced_text_fixes

    segments: List[Segment] = []
    with pdfplumber.open(pdf_path) as pdf, pymupdf.open(pdf_path) as mu:
        for page_num, (raw_page, mpage) in enumerate(zip(pdf.pages, mu), start=1):
            page = clean_page(raw_page)
            page_text = page.extract_text() or ""

            if len(page_text.strip()) < 10:
                if ocr_scanned:
                    segments.extend(_ocr_fallback(raw_page, page_num))
                else:
                    print(f"      -> page {page_num} is a scan: copied into the replica as an image, not translated")
                continue

            tables = page.find_tables()
            for t_idx, table in enumerate(tables):
                for r_idx, row in enumerate(table_data(page, table)):
                    for c_idx, cell_text in enumerate(row):
                        if cell_text and cell_text.strip():
                            segments.append(Segment(
                                text=cell_text.strip(),
                                page=page_num,
                                kind="table_cell",
                                location=f"page{page_num}/table{t_idx}/row{r_idx}/col{c_idx}",
                                table_id=t_idx, row=r_idx, col=c_idx,
                            ))

            blocks = _blocks(page_words(page), [t.bbox for t in tables], _Spans(mpage))
            for i, bl in enumerate(blocks):
                if bl["text"].strip():
                    segments.append(Segment(
                        text=bl["text"].strip(), page=page_num, kind="paragraph",
                        location=f"page{page_num}/block{i}", font_size=bl["style"][0],
                    ))

    fixes = spaced_text_fixes([seg.text for seg in segments])
    for seg in segments:
        if seg.text in fixes:
            seg.raw_text, seg.text = seg.text, fixes[seg.text]
    return segments


def _ocr_fallback(page, page_num) -> List[Segment]:
    """OCR a scanned page; if OCR is unavailable, return a visible failure segment with the cause."""
    try:
        import os
        import shutil
        from pathlib import Path
        import pytesseract
        command = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
        if not command:
            candidates = (Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                          Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"))
            command = next((str(path) for path in candidates if path.is_file()), None)
        if not command:
            raise RuntimeError("Tesseract executable is missing. Install Tesseract and set PATH or TESSERACT_CMD.")
        pytesseract.pytesseract.tesseract_cmd = command
        languages = pytesseract.get_languages(config="")
        selected = [language for language in ("eng", "chi_sim", "chi_tra") if language in languages]
        if "eng" not in selected:
            raise RuntimeError("Tesseract English language data (eng) is missing.")
        image = page.to_image(resolution=300).original
        data = pytesseract.image_to_data(image.convert("L"), lang="+".join(selected), config="--psm 11",
                                         output_type=pytesseract.Output.DICT)
        blocks = {}                       # (block, paragraph, line) -> words; low-confidence noise (lines, hatching) dropped
        for i, word in enumerate(data["text"]):
            word = word.strip()
            if word and float(data["conf"][i]) >= 60 and sum(ch.isalnum() for ch in word) >= 1:
                blocks.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(word)
        pieces = [" ".join(words) for _, words in sorted(blocks.items())]
        pieces = [p for p in pieces if sum(ch.isalpha() for ch in p) >= 3]
        if not pieces:
            raise RuntimeError("Tesseract returned no readable text for this scanned page.")
    except Exception as error:
        return [Segment(text=f"[OCR FAILED: {type(error).__name__}: {error}]",
                        page=page_num, kind="paragraph", location=f"page{page_num}/ocr", from_ocr=True)]
    return [Segment(text=p, page=page_num, kind="paragraph", location=f"page{page_num}/ocr{i}", from_ocr=True)
            for i, p in enumerate(pieces)]
