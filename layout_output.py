"""Writes a Word file that reproduces the source PDF page by page (page size, tables, borders, pictures,
text positions, fonts), with the text replaced by its translation.
Tables become real Word tables, free text becomes flowing paragraphs or floating text boxes, and pictures
are placed at their original coordinates. Usage: write_replica(pdf_path, out_path, translate), where
translate(text, location) -> (translated_text, needs_review); needs_review text is highlighted yellow."""
import io
import math
import re
from xml.sax.saxutils import escape

import pdfplumber
import pymupdf
from docx import Document
from docx.oxml import parse_xml
from docx.shared import Pt

SNAP = 1.5          # pt: edges closer than this are the same edge
EMU = 12700         # EMU per point
W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')
SERIF, SANS = "Times New Roman", "Calibri"
HIGHLIGHT = "FFFF00"


def _tw(pt):
    return int(round(pt * 20))


def _snap(values):
    out = []
    for v in sorted(values):
        if out and v - out[-1] <= SNAP:
            continue
        out.append(v)
    return out


def _nearest(v, grid):
    return min(range(len(grid)), key=lambda i: abs(grid[i] - v))


def clean_page(page):
    """Drop the second copy of every glyph that the PDF prints twice (fake bold: 'PPaaggee').
    A genuine double letter ('Philosophy') advances by a character width, so it is kept."""
    return page.dedupe_chars(tolerance=1)


def _rot_dirs(page):
    """Reading direction of rotated text: matrix (0,1,..) = turned 90 deg anticlockwise = bottom-to-top."""
    rot = [c for c in page.chars if not c.get("upright", True)]
    ccw = sum(1 for c in rot if c["matrix"][1] > 0)
    return ("btt", "ltr") if ccw >= len(rot) - ccw else ("ttb", "rtl")


def page_words(page):
    """Words with rotated text read in the right order (pdfplumber would return it reversed)."""
    cd, ld = _rot_dirs(page)
    return page.extract_words(extra_attrs=["size"], char_dir_rotated=cd, line_dir_rotated=ld)


def _cell_rotated(page, bb):
    chars = [c for c in page.crop(bb, strict=False).chars if c["text"].strip()]
    return bool(chars) and sum(1 for c in chars if not c.get("upright", True)) > len(chars) / 2


def table_data(page, table):
    """table.extract(), with the text of rotated cells read the right way round."""
    data = table.extract()
    cd, ld = _rot_dirs(page)
    for r, row in enumerate(table.rows):
        for c, bb in enumerate(row.cells):
            if bb is not None and r < len(data) and c < len(data[r]) and _cell_rotated(page, bb):
                txt = page.crop(bb, strict=False).extract_text(char_dir_rotated=cd, line_dir_rotated=ld)
                data[r][c] = re.sub(r"\s*\n\s*", " ", txt or "")      # a turned header wraps one word per line
    return data


_FONT_REG = {}


def _installed_fonts():
    """{display name (no '(TrueType)'): file} from the Windows registry; empty elsewhere."""
    if not _FONT_REG:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, val, _ = winreg.EnumValue(k, i)
                _FONT_REG[re.sub(r"\s*\((TrueType|OpenType)\)$", "", name)] = val
        except Exception:
            _FONT_REG["_none"] = ""
    return _FONT_REG


def _families():
    fams = set()
    for n in _installed_fonts():
        for part in n.split(" & "):
            fams.add(re.sub(r"\s+(Bold|Italic|Regular|Light|Semibold|Black|Condensed)(\s.*)?$", "", part).strip())
    return fams


def _resolve_family(raw, flags):
    """Real family name when that font is installed, else a serif/sans stand-in."""
    norm = re.sub(r"[^a-z]", "", raw.split("+")[-1].lower())
    best = ""
    for f in _families():
        nf = re.sub(r"[^a-z]", "", f.lower())
        if nf and norm.startswith(nf) and len(nf) > len(re.sub(r"[^a-z]", "", best.lower())):
            best = f
    return best or (SERIF if flags & 4 else SANS)


class _Spans:
    """Font facts per text span, so family/size/bold/italic follow the original."""

    def __init__(self, mupage):
        self.spans = []
        for b in mupage.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    if s["text"].strip():
                        self.spans.append(s)

    def style(self, bbox):
        x0, t, x1, b = bbox
        best = None
        for s in self.spans:
            cx, cy = (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
            if x0 - 1 <= cx <= x1 + 1 and t - 1 <= cy <= b + 1:
                if best is None or len(s["text"]) > len(best["text"]):
                    best = s
        if best is None:
            return 10.0, False, SANS, False
        f = best["flags"]
        bold = bool(f & 16) or bool(re.search(r"bold|black|heavy", best["font"], re.I))
        return round(best["size"], 1), bold, _resolve_family(best["font"], f), bool(f & 2)


def _run(text, size, bold, serif, italic=False, hl=False, underline=False):
    """One run per script: symbol-font / dingbat characters get a font that actually has them."""
    parts = re.findall(r"[\uf000-\uf8ff]+|[\u2190-\u2bff\u25a0-\u25ff]+|[^\uf000-\uf8ff\u2190-\u2bff]+", text)
    if len(parts) > 1 or (parts and re.match(r"[\uf000-\uf8ff\u2190-\u2bff]", parts[0])):
        return "".join(_run1(p, size, bold, serif, italic, hl,
                             "Symbol" if re.match(r"[\uf000-\uf8ff]", p) else
                             "Segoe UI Symbol" if re.match(r"[\u2190-\u2bff\u25a0-\u25ff]", p) else None,
                             underline)
                       for p in parts)
    return _run1(text, size, bold, serif, italic, hl, None, underline)


def _run1(text, size, bold, serif, italic, hl, force_font, underline=False):
    font = force_font or (serif if isinstance(serif, str) else (SERIF if serif else SANS))
    rpr = (f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}" w:eastAsia="{font}"/>'
           f'{"<w:b/><w:bCs/>" if bold else ""}{"<w:i/><w:iCs/>" if italic else ""}{"<w:u w:val=\"single\"/>" if underline else ""}'
           f'<w:sz w:val="{int(round(size * 2))}"/><w:szCs w:val="{int(round(size * 2))}"/>'
           f'{f"<w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"{HIGHLIGHT}\"/>" if hl else ""}'
           f'<w:lang w:val="ru-RU"/>')
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _para(text, size, bold, serif, italic, align, hl, extra_ppr="", page_break=False, after=0.0, line_pt=None,
          bullet=None, underline=False):
    line = _tw(line_pt or size * 1.2)
    ppr = (f'{"<w:pageBreakBefore/>" if page_break else ""}{extra_ppr}'
           f'<w:spacing w:before="0" w:after="{_tw(after)}" w:line="{line}" w:lineRule="exact"/>'
           f'<w:jc w:val="{align}"/>')
    lead = (_run(bullet, size, bold, serif, italic, False) + "<w:r><w:tab/></w:r>") if bullet else ""
    return f'<w:p><w:pPr>{ppr}</w:pPr>{lead}{_run(text, size, bold, serif, italic, hl, underline) if text else ""}</w:p>'


_font_cache = {}


def _font_file(family, bold):
    reg = _installed_fonts()
    want = family + (" Bold" if bold else "")
    for n, f in reg.items():
        if n == want or (not bold and (n == family or n.startswith(family + " &"))):
            return f
    for n, f in reg.items():
        if n.startswith(family) and (("Bold" in n and "Italic" not in n) == bold) and "Light" not in n:
            return f
    return "timesbd.ttf" if bold else "times.ttf"


def _text_width(text, size, bold, serif):
    try:
        from PIL import ImageFont
        fam = serif if isinstance(serif, str) else (SERIF if serif else SANS)
        key = (fam, bold)
        if key not in _font_cache:
            f = _font_file(fam, bold)
            _font_cache[key] = ImageFont.truetype(f if "\\" in f or "/" in f else "C:/Windows/Fonts/" + f, 100)
        return _font_cache[key].getlength(text) * size / 100.0
    except Exception:
        return len(text) * size * 0.5


def _fit(text, size, bold, serif, avail, single_line, floor=0.8):
    """Shrink a little (never below `floor`) so a longer translation keeps the original line structure."""
    words = text.split()
    if not words or avail <= 0:
        return size

    avail *= 0.96   # Word wraps on hair-thin differences; keep a margin

    def fits(sz, whole=True):
        if any(_text_width(w, sz, bold, serif) > avail for w in words):
            return False
        return not (single_line and whole) or _text_width(text, sz, bold, serif) <= avail

    sz = size
    while not fits(sz) and sz > size * floor:
        sz -= 0.25
    if fits(sz):
        return sz
    # the whole line cannot fit on one line, but a single word must never break mid-word
    sz = size
    while not fits(sz, whole=False) and sz > size * 0.65:
        sz -= 0.25
    return sz if fits(sz, whole=False) else size


def _n_lines(text, size, bold, serif, avail):
    """Greedy word-wrap estimate of how many lines `text` needs."""
    n, cur = 1, 0.0
    space = _text_width(" ", size, bold, serif)
    for w in text.split():
        ww = _text_width(w, size, bold, serif)
        if cur and cur + space + ww > avail * 0.97:
            n, cur = n + 1, ww
        else:
            cur += (space if cur else 0.0) + ww
    return n


def _split_paras(text):
    """Keep a line break only where the source has a real one (after a colon or before a numbered item)."""
    out = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        new_item = re.match(r"^\d+[.)]\s", ln) or re.match(r"^[^\W\d_][^:\n]{0,25}:", ln)   # "3. ..." or "Дата: ..."
        if out and not new_item and not out[-1].endswith(":"):
            out[-1] += " " + ln
        else:
            out.append(ln)
    return out


# ---------------------------------------------------------------- tables
def _edge_cover(edges, orient, fixed, lo, hi):
    """Fraction of [lo, hi] covered by edges of this orientation lying on `fixed`."""
    total = 0.0
    for e in edges:
        if e["orientation"] != orient:
            continue
        pos = e["top"] if orient == "h" else e["x0"]
        if abs(pos - fixed) > SNAP:
            continue
        a, b = (e["x0"], e["x1"]) if orient == "h" else (e["top"], e["bottom"])
        total += max(0.0, min(b, hi) - max(a, lo))
    return total / max(hi - lo, 0.01)


def _edge_width(edges, orient, fixed, lo, hi):
    ws = [e.get("linewidth") or 0.5 for e in edges if e["orientation"] == orient
          and abs((e["top"] if orient == "h" else e["x0"]) - fixed) <= SNAP
          and min(e["x1"] if orient == "h" else e["bottom"], hi) - max(e["x0"] if orient == "h" else e["top"], lo) > 0]
    return max(ws) if ws else 0.5


def _build_table(page, table, t_idx, p_no, words, spans, edges, translate, pics):
    data = table_data(page, table)
    cells = []  # (r, c, bbox, text)
    for r, row in enumerate(table.rows):
        for c, bb in enumerate(row.cells):
            if bb is not None:
                txt = data[r][c] if r < len(data) and c < len(data[r]) and data[r][c] else ""
                cells.append((r, c, bb, txt.strip()))
    xs = _snap([v for _, _, bb, _ in cells for v in (bb[0], bb[2])])
    ys = _snap([v for _, _, bb, _ in cells for v in (bb[1], bb[3])])
    n_r, n_c = len(ys) - 1, len(xs) - 1
    occ = [[None] * n_c for _ in range(n_r)]
    placed = []
    for k, (r, c, bb, txt) in enumerate(cells):
        c0, c1 = _nearest(bb[0], xs), _nearest(bb[2], xs)
        r0, r1 = _nearest(bb[1], ys), _nearest(bb[3], ys)
        if c1 <= c0 or r1 <= r0:
            continue
        placed.append((k, r, c, bb, txt, c0, c1, r0, r1))
        for rr in range(r0, r1):
            for cc in range(c0, c1):
                occ[rr][cc] = len(placed) - 1

    # Widen narrow columns so that no translated word breaks mid-word. The width is borrowed from roomy
    # columns of the same table, so the table keeps its overall width. (Cells are translated here once.)
    tr_cache, need = {}, [0.0] * n_c
    for (_, r, c, bb, txt, c0, c1, r0, r1) in placed:
        if not txt or c1 - c0 != 1 or _cell_rotated(page, bb):
            continue
        out_, flag_ = translate(txt, f"page{p_no}/table{t_idx}/row{r}/col{c}")
        tr_cache[(r, c)] = (out_, flag_)
        sz_, b_, se_, _it = spans.style(bb)
        longest = [w_ for pp in _split_paras(out_) for w_ in pp.split()]
        if longest:      # measured at the smallest font we are willing to use (80%)
            need[c0] = max(need[c0], max(_text_width(w_, sz_ * 0.8, b_, se_) for w_ in longest) + 8)
    widths = [xs[i + 1] - xs[i] for i in range(n_c)]
    deficit = [max(0.0, need[i] - widths[i]) for i in range(n_c)]
    if sum(deficit) > 0:
        slack = [max(0.0, widths[i] - max(need[i], 20.0)) for i in range(n_c)]
        take = min(sum(deficit), sum(slack) * 0.8)
        if take > 0:
            new_w = [widths[i] + deficit[i] * take / sum(deficit) - take * slack[i] / sum(slack) for i in range(n_c)]
            xs = [xs[0]]
            for w_ in new_w:
                xs.append(xs[-1] + w_)

    grid = "".join(f'<w:gridCol w:w="{_tw(xs[i + 1] - xs[i])}"/>' for i in range(n_c))
    rows_xml = []
    need_h = [0.0] * n_r
    for gr in range(n_r):
        tcs, gc = [], 0
        while gc < n_c:
            idx = occ[gr][gc]
            if idx is None:
                tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{_tw(xs[gc + 1] - xs[gc])}" w:type="dxa"/>'
                           '<w:tcBorders><w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="nil"/>'
                           '<w:right w:val="nil"/></w:tcBorders></w:tcPr><w:p><w:pPr><w:spacing w:before="0" '
                           'w:after="0" w:line="20" w:lineRule="exact"/></w:pPr></w:p></w:tc>')
                gc += 1
                continue
            _, r, c, bb, txt, c0, c1, r0, r1 = placed[idx]
            span = c1 - c0
            width = xs[c1] - xs[c0]
            vm = ""
            if r1 - r0 > 1:
                vm = '<w:vMerge w:val="restart"/>' if gr == r0 else "<w:vMerge/>"
            bx = {"top": _edge_cover(edges, "h", bb[1], bb[0], bb[2]) > 0.6,
                  "bottom": _edge_cover(edges, "h", bb[3], bb[0], bb[2]) > 0.6,
                  "left": _edge_cover(edges, "v", bb[0], bb[1], bb[3]) > 0.6,
                  "right": _edge_cover(edges, "v", bb[2], bb[1], bb[3]) > 0.6}
            wd = {"top": _edge_width(edges, "h", bb[1], bb[0], bb[2]),
                  "bottom": _edge_width(edges, "h", bb[3], bb[0], bb[2]),
                  "left": _edge_width(edges, "v", bb[0], bb[1], bb[3]),
                  "right": _edge_width(edges, "v", bb[2], bb[1], bb[3])}
            borders = "".join(
                (f'<w:{s} w:val="single" w:sz="{max(2, min(18, int(round(wd[s] * 8))))}" w:space="0" w:color="000000"/>'
                 if bx[s] else f'<w:{s} w:val="nil"/>') for s in ("top", "left", "bottom", "right"))
            body = '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/></w:pPr></w:p>'
            mar_l, mar_r, valign, text_dir = 2.0, 2.0, "center", ""
            inside = []
            if gr == r0 and txt:
                inside = [w for w in words if bb[0] - 1 <= (w["x0"] + w["x1"]) / 2 <= bb[2] + 1
                          and bb[1] - 1 <= (w["top"] + w["bottom"]) / 2 <= bb[3] + 1]
                size, bold, serif, italic = spans.style(bb)
                align = "left"
                rotated = _cell_rotated(page, bb)
                if rotated:
                    align, text_dir = "center", '<w:textDirection w:val="btLr"/>'
                if inside and not rotated:
                    first_top = min(w["top"] for w in inside)
                    line1 = [w for w in inside if abs(w["top"] - first_top) < 3]
                    lg, rg = min(w["x0"] for w in line1) - bb[0], bb[2] - max(w["x1"] for w in line1)
                    if abs(lg - rg) <= max(8.0, 0.06 * (bb[2] - bb[0])):
                        align = "center"
                    elif rg <= 6 and lg > 20:
                        align = "right"
                    else:
                        mar_l = max(1.0, min(10.0, lg))
                    tg = first_top - bb[1]
                    bg = bb[3] - max(w["bottom"] for w in inside)
                    valign = "center" if abs(tg - bg) <= 3 else ("bottom" if tg > bg else "top")
                out, flag = tr_cache.get((r, c)) or translate(txt, f"page{p_no}/table{t_idx}/row{r}/col{c}")
                paras = _split_paras(out) or [""]
                tops = sorted({round(w["top"]) for w in inside})
                one_line = len(tops) <= 1
                # first source line vs the rest can differ in weight ("General Notes:" is bold, the items are not)
                first_st = rest_st = (size, bold, serif, italic)
                if len(tops) > 1 and len(paras) > 1:
                    first_st = spans.style((bb[0], tops[0] - 1, bb[2], tops[1] - 2))
                    rest_st = spans.style((bb[0], tops[1] - 1, bb[2], bb[3]))
                # visible gap between source lines (e.g. signature block) -> space after each paragraph
                pitch = min((b - a for a, b in zip(tops, tops[1:])), default=0)
                gap = max(0.0, pitch - size * 1.2) if pitch > size * 1.5 else 0.0
                avail = width - mar_l - mar_r          # width after the column rebalancing
                parts, need = [], 0.0
                for n, pp in enumerate(paras):
                    sz_, b_, se_, it_ = first_st if n == 0 else rest_st
                    fs = _fit(pp, sz_, b_, se_, avail, one_line)
                    parts.append(_para(pp, fs, b_, se_, it_, align, flag, after=gap if n < len(paras) - 1 else 0.0))
                    need += _n_lines(pp, fs, b_, se_, avail) * fs * 1.2 + (gap if n < len(paras) - 1 else 0.0)
                if r1 - r0 == 1:
                    need_h[gr] = max(need_h[gr], need)
                body = "".join(parts)
            mine = [pc for pc in pics if bb[0] <= (pc[0][0] + pc[0][2]) / 2 <= bb[2]
                    and bb[1] <= (pc[0][1] + pc[0][3]) / 2 <= bb[3]]
            if mine and gr == r0:
                if not txt:
                    valign = "top"
                text_bottom = max((w["bottom"] for w in inside), default=None) if txt else None
                holders, after = "", ""
                for (ib, rid) in mine:
                    _next_id[0] += 1
                    pw_, ph_ = ib[2] - ib[0], ib[3] - ib[1]
                    if text_bottom is not None and ib[1] >= text_bottom - 1:
                        # below the text: it follows the text, so a longer translation pushes it down
                        valign = "top"
                        after += (f'<w:p><w:pPr><w:spacing w:before="{_tw(max(ib[1] - text_bottom, 0))}" w:after="0" '
                                  f'w:line="240" w:lineRule="auto"/><w:ind w:left="{_tw(max(ib[0] - (bb[0] + mar_l), 0))}"/></w:pPr>'
                                  '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
                                  f'<wp:extent cx="{int(pw_ * EMU)}" cy="{int(ph_ * EMU)}"/><wp:docPr id="{_next_id[0]}" name="pic{_next_id[0]}"/>'
                                  f'<wp:cNvGraphicFramePr/>{_picture(rid, pw_, ph_, _next_id[0])}</wp:inline></w:drawing></w:r></w:p>')
                    else:
                        holders += _anchor(ib[0] - (bb[0] + mar_l), max(ib[1] - bb[1], 0.0), pw_, ph_,   # never above its own cell
                                           _picture(rid, pw_, ph_, _next_id[0]), "pic", rel_h="column", rel_v="paragraph")
                lead_par = (f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
                            f'</w:pPr>{holders}</w:p>') if holders or not txt else ""
                body = lead_par + (body if txt else "") + after
            tcs.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{_tw(width)}" w:type="dxa"/>'
                f'{f"<w:gridSpan w:val=\"{span}\"/>" if span > 1 else ""}{vm}'
                f'<w:tcBorders>{borders}</w:tcBorders>{text_dir}<w:vAlign w:val="{valign}"/>'
                f'<w:tcMar><w:top w:w="0" w:type="dxa"/><w:left w:w="{_tw(mar_l)}" w:type="dxa"/>'
                f'<w:bottom w:w="0" w:type="dxa"/><w:right w:w="{_tw(mar_r)}" w:type="dxa"/></w:tcMar>'
                f'</w:tcPr>{body}</w:tc>')
            gc += span
        rows_xml.append(f'<w:tr><w:trPr><w:cantSplit/><w:trHeight w:val="{_tw(ys[gr + 1] - ys[gr])}" '
                        f'w:hRule="atLeast"/></w:trPr>{"".join(tcs)}</w:tr>')
    tbl = (f'<w:tbl {W_NS}><w:tblPr><w:tblW w:w="{_tw(xs[-1] - xs[0])}" w:type="dxa"/>'
           f'<w:tblInd w:w="{_tw(xs[0])}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
           '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/>'
           '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar></w:tblPr>'
           f'<w:tblGrid>{grid}</w:tblGrid>{"".join(rows_xml)}</w:tbl>')
    growth = sum(max(0.0, need_h[g] - (ys[g + 1] - ys[g])) for g in range(n_r))
    return tbl, ys[0], ys[-1], growth


# ------------------------------------------------------- floating objects
_next_id = [100]


def _anchor(x, y, w, h, graphic, name, rel_h="page", rel_v="page"):
    _next_id[0] += 1
    i = _next_id[0]
    return (f'<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
            f'relativeHeight="{i}" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
            '<wp:simplePos x="0" y="0"/>'
            f'<wp:positionH relativeFrom="{rel_h}"><wp:posOffset>{int(x * EMU)}</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="{rel_v}"><wp:posOffset>{int(y * EMU)}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{int(w * EMU)}" cy="{int(h * EMU)}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'<wp:wrapNone/><wp:docPr id="{i}" name="{name}{i}"/><wp:cNvGraphicFramePr/>{graphic}'
            '</wp:anchor></w:drawing></w:r>')


def _picture(rid, w, h, i):
    return ('<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{i}" name="img{i}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{int(w * EMU)}" cy="{int(h * EMU)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>')


def _textbox(x, y, w, h, para_xml, vert="horz"):
    g = ('<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
         '<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/>'
         f'<a:ext cx="{int(w * EMU)}" cy="{int(h * EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
         '<a:noFill/><a:ln><a:noFill/></a:ln></wps:spPr>'
         f'<wps:txbx><w:txbxContent>{para_xml}</w:txbxContent></wps:txbx>'
         f'<wps:bodyPr rot="0" vert="{vert}" wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t" '
         'anchorCtr="0"><a:noAutofit/></wps:bodyPr></wps:wsp></a:graphicData></a:graphic>')
    return _anchor(x, y, w, h, g, "text")


def _hline(x, y, w, lw):
    g = ('<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
         '<wps:wsp><wps:cNvCnPr/><wps:spPr><a:xfrm><a:off x="0" y="0"/>'
         f'<a:ext cx="{int(w * EMU)}" cy="0"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
         f'<a:ln w="{int(lw * EMU)}"><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln></wps:spPr>'
         '<wps:bodyPr/></wps:wsp></a:graphicData></a:graphic>')
    return _anchor(x, y, w, 0, g, "rule")


# ------------------------------------------------------------ text blocks
def _rotated_blocks(words, spans):
    """Rotated free text (side labels): one block per column of words, kept in reading order."""
    cols = []
    for w in sorted(words, key=lambda w: w["x0"]):
        if cols and abs(cols[-1][0]["x0"] - w["x0"]) < 3:
            cols[-1].append(w)
        else:
            cols.append([w])
    out = []
    for col in cols:
        x0, x1 = min(w["x0"] for w in col), max(w["x1"] for w in col)
        top, bottom = min(w["top"] for w in col), max(w["bottom"] for w in col)
        out.append({"text": " ".join(w["text"] for w in col), "x0": x0, "x1": x1, "top": top, "bottom": bottom,
                    "style": spans.style((x0, top, x1, bottom)), "nlines": 1, "x0s": [x0], "pitch": None,
                    "col_right": x1, "rotated": True})
    return out


def _is_bullet(ch):
    """List markers: private-use symbol-font glyphs (Wingdings/Symbol) and common bullet characters."""
    return 0xF000 <= ord(ch) <= 0xF8FF or ch in "•●▪■‣➢➔→◦⁃"


_NUMBERED = re.compile(r"^(\d+(?:\.\d+)+\.?|\d+[.)]|[A-Za-z][.)]|(?:[ivxl]{2,5}|[IVXL]{2,5})[.)])$")   # 6.0 4.1.2 3. a) ii.


def _label_columns(lines, spans):
    """A run of rows made of a short label, a wide gap and a text (a reference list: "ASME B31.1   2020 - Piping")
    keeps its label as a hanging marker, so a longer translation wraps inside its own column instead of
    overlapping the next row. Needs at least two aligned rows in a row; wrapped lines of the text continue the run."""
    pair = {}                                         # index of the text line -> index of its label line
    for m, text in enumerate(lines):
        if text["bullet"] or len(text["text"]) < 8:
            continue
        for n, label in enumerate(lines):
            if (n != m and not label["bullet"] and abs(label["top"] - text["top"]) < 2.5
                    and 18 < text["x0"] - label["x1"] < 160 and len(label["words"]) <= 4 and len(label["text"]) <= 22
                    and not any(k["top"] > label["top"] - 2.5 and abs(k["top"] - label["top"]) < 2.5 and k["x1"] < label["x0"] - 8
                                for k in lines)
                    and not any(abs(k["top"] - text["top"]) < 2.5 and label["x1"] <= k["x0"] and k["x1"] <= text["x0"]
                                for k in lines)):         # a label is the first cell of its row and touches the text
                if m not in pair or lines[pair[m]]["x1"] < label["x1"]:
                    pair[m] = n
    labels = set(pair.values())
    numberish = lambda m: bool(re.fullmatch(r"[\d.\s]+", lines[pair[m]]["text"]) and re.search(r"\d", lines[pair[m]]["text"]))

    def keep(run):
        return len(run) >= 2 or (len(run) == 1 and numberish(run[0]))        # "1 3.2" is a section number even alone

    runs, current = [], []
    for m, ln in enumerate(lines):
        if m in labels:
            continue
        if m in pair:
            n = pair[m]
            if current and abs(lines[pair[current[-1]]]["x0"] - lines[n]["x0"]) < 3 and abs(lines[current[-1]]["x0"] - ln["x0"]) < 3:
                current.append(m)
            else:
                if keep(current):
                    runs.append(current)
                current = [m]
        elif (current and not ln["bullet"] and -3 < ln["x0"] - lines[current[0]]["x0"] < 60
              and 0 < ln["top"] - lines[m - 1]["top"] < 2.2 * (ln["bottom"] - ln["top"])):
            continue                                  # a wrapped line of the text column (it may be indented further)
        else:
            if keep(current):
                runs.append(current)
            current = []
    if keep(current):
        runs.append(current)
    for run in runs:
        for m in run:
            label = lines[pair[m]]
            lines[m]["bullet"], lines[m]["bx"] = label["text"], label["x0"]
            label["gone"] = True
    return [ln for ln in lines if not ln.get("gone")]


def _blocks(words, table_bboxes, spans):
    """Words outside tables -> lines -> blocks.  Wrapped lines of one paragraph merge (even with
    double line spacing or a bold phrase inside); a bullet or number starts a new block and is kept
    apart from the text, so it is never sent for translation and can be re-attached as a real bullet."""
    def in_table(w):
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in table_bboxes)

    outside = [w for w in words if not in_table(w)]
    rotated_blocks = _rotated_blocks([w for w in outside if not w.get("upright", True)], spans)
    free = sorted((w for w in outside if w.get("upright", True)), key=lambda w: (round(w["top"]), w["x0"]))
    lines = []
    for w in free:
        for ln in lines:
            starts_counter = re.fullmatch(r"p{1,2}age", w["text"], re.IGNORECASE) and ln["words"]   # footer "Page N of M"
            same_line = abs(ln["top"] - w["top"]) < 2.5 or abs(ln["base"] - w["bottom"]) < 1.5     # or the same baseline
            marker = len(ln["words"]) == 1 and _NUMBERED.match(ln["words"][0]["text"])    # "6.0" then a wide gap, then the title
            if same_line and w["x0"] - ln["x1"] < (60 if marker else 14) and (ln["x0"] - w["x1"] < 14 or abs(ln["top"] - w["top"]) >= 1.0 or _NUMBERED.match(w["text"]) or _is_bullet(w["text"][0])) and not starts_counter:
                ln["words"].append(w)
                ln["x1"] = max(ln["x1"], w["x1"])
                ln["bottom"] = max(ln["bottom"], w["bottom"])
                break
        else:
            lines.append({"top": w["top"], "bottom": w["bottom"], "base": w["bottom"], "x0": w["x0"], "x1": w["x1"],
                          "words": [w]})
    for ln in lines:
        ws = sorted(ln["words"], key=lambda w: w["x0"])
        ln["bullet"], ln["bx"] = None, None
        first = ws[0]["text"]
        if first and _is_bullet(first[0]):
            ln["bullet"], ln["bx"] = first[0], ws[0]["x0"]
            if len(first) > 1:
                ws[0] = {**ws[0], "text": first[1:]}
            else:
                ws = ws[1:]
        elif _NUMBERED.match(first) and len(ws) > 1 and ws[1]["x0"] - ws[0]["x1"] > 6:
            ln["bullet"], ln["bx"] = first, ws[0]["x0"]
            ws = ws[1:]
        elif (re.fullmatch(r"\\d{1,2}", first) and len(ws) > 2 and re.fullmatch(r"\\.?\\d+(?:\\.\\d+)*\\.?", ws[1]["text"])
              and ws[1]["x0"] - ws[0]["x1"] < 6 and ws[2]["x0"] - ws[1]["x1"] > 6):
            ln["bullet"], ln["bx"] = first + ws[1]["text"], ws[0]["x0"]       # a number split by kerning: "1 3.2"
            ws = ws[2:]
        if not ws:
            ln["text"] = ""
            continue
        ln["words"] = ws
        ln["x0"] = min(w["x0"] for w in ws)
        ln["text"] = " ".join(w["text"] for w in ws)
        ln["style"] = spans.style((ln["x0"], ln["top"], ln["x1"], ln["bottom"]))
    lines = [l for l in lines if l["text"]]
    lines.sort(key=lambda l: (l["top"], l["x0"]))
    lines = _label_columns(lines, spans)
    col_right = max((l["x1"] for l in lines), default=0)
    col_left = min((l["x0"] for l in lines), default=0)
    full = col_left + 0.75 * (col_right - col_left)      # a paragraph line that reaches here is not the last one
    blocks = []
    for ln in lines:
        target = None
        in_row = any(o is not ln and abs(o["top"] - ln["top"]) < 2.5 and o["x1"] < ln["x0"] - 8 for o in lines)   # a cell, not a wrapped line
        if not ln["bullet"] and not in_row:
            for b in blocks:
                last = b["lines"][-1]
                size = b["style"][0]
                same_col = (abs(last["x0"] - ln["x0"]) < 3
                            or abs((last["x0"] + last["x1"]) - (ln["x0"] + ln["x1"])) < 6)
                hanging = ln["x0"] > last["x0"] + 3 and ln["x0"] - last["x0"] < 40
                step = ln["top"] - last["top"]
                pitch_ok = len(b["lines"]) < 2 or abs(step - (last["top"] - b["lines"][-2]["top"])) < 0.35 * step
                if (abs(size - ln["style"][0]) < 0.6 and b["style"][2] == ln["style"][2]
                        and 0 <= ln["top"] - last["bottom"] < size * 1.0 and step > 1 and pitch_ok
                        and last["x1"] > full and (same_col or hanging)):
                    target = b
                    break
        if target:
            target["lines"].append(ln)
        else:
            blocks.append({"lines": [ln], "style": ln["style"]})
    out = []
    for b in blocks:
        ls = b["lines"]
        longest = max(ls, key=lambda l: len(l["text"]))     # block style = its dominant line
        out.append({"text": " ".join(l["text"] for l in ls), "x0": min(l["x0"] for l in ls),
                    "x1": max(l["x1"] for l in ls), "top": ls[0]["top"], "bottom": ls[-1]["bottom"],
                    "style": longest["style"], "nlines": len(ls), "x0s": [l["x0"] for l in ls],
                    "x1s": [l["x1"] for l in ls],
                    "bullet": ls[0]["bullet"], "bx": ls[0]["bx"],
                    "pitch": (ls[-1]["top"] - ls[0]["top"]) / (len(ls) - 1) if len(ls) > 1 else None,
                    "col_right": col_right, "col_left": col_left})
    return out + rotated_blocks


# ------------------------------------------------------------- the writer
def _underlines(blocks, edges, tbs):
    """-> (ids of blocks that are underlined, ids of the rule edges that were underlines)."""
    marked, used = set(), set()
    for k, e in enumerate(edges):
        if e["orientation"] != "h" or any(b[0] - 2 <= e["x0"] and e["x1"] <= b[2] + 2
                                          and b[1] - 2 <= e["top"] <= b[3] + 2 for b in tbs):
            continue
        for i, bl in enumerate(blocks):
            if bl.get("rotated"):
                continue
            ov = min(e["x1"], bl["x1"]) - max(e["x0"], bl["x0"])
            width = bl["x1"] - bl["x0"]
            # a thin rule hugging the bottom of the text, lying within it (it may skip a leading "H.")
            if (-7 <= e["top"] - bl["bottom"] <= 5 and ov > 0.5 * width
                    and e["x0"] >= bl["x0"] - 8 and e["x1"] <= bl["x1"] + 30):
                marked.add(i)
                used.add(k)
    return marked, used


def _is_pinned(bl, page_h, repeats):
    """Header/footer text stays at its exact coordinates; everything else flows."""
    if (bl["top"] < page_h * 0.06 or bl["bottom"] > page_h * 0.92) and bl["nlines"] <= 2:
        return True                      # a long paragraph that merely reaches the page edge is body text and must flow
    return (re.sub(r"\d+", "#", bl["text"]), round(bl["top"] / 4)) in repeats


def _grids(blocks, page_h, repeats):
    """Rows of aligned cells that are not drawn as a table (a reference list "ASME B31.1 | 2020 | - Piping", a matrix
    of X marks) -> [{"cols": [x0 of each column], "rows": [{"cells": {col: (block id, block)}, "cont": [(id, block)]}]}].
    A single block under the last column of a row (a wrapped line) continues that row."""
    cand = sorted(((i, b) for i, b in enumerate(blocks) if not b.get("rotated") and not b["bullet"] and b["nlines"] <= 3
                   and not _is_pinned(b, page_h, repeats)), key=lambda t: (round(t[1]["top"]), t[1]["x0"]))
    rows = []
    for i, b in cand:
        if rows and abs(rows[-1][0][1]["top"] - b["top"]) < 2.5:
            rows[-1].append((i, b))
        else:
            rows.append([(i, b)])
    runs, cur = [], None
    for cells in rows:
        cells.sort(key=lambda t: t[1]["x0"])
        first = cells[0][1]
        multi = (len(cells) >= 2 and len(first["text"]) <= 45
                 and all(c2[1]["x0"] - c1[1]["x1"] > 8 for c1, c2 in zip(cells, cells[1:])))
        close = bool(cur) and first["top"] - max(b["bottom"] for _, b in cur[-1]["cells"] + cur[-1]["cont"]) < 2.0 * max(first["bottom"] - first["top"], 8)
        if multi and close:
            cur.append({"cells": cells, "cont": []})
        elif cur and len(cells) == 1 and close and first["x0"] >= cur[-1]["cells"][0][1]["x0"] - 3:
            cur[-1]["cont"].append(cells[0])
        else:
            if cur:
                runs.append(cur)
            cur = [{"cells": cells, "cont": []}] if multi else None
    if cur:
        runs.append(cur)
    grids = []
    for run in runs:
        if len(run) < 3:
            continue
        cols = []
        for x in sorted(b["x0"] for row in run for _, b in row["cells"]):
            if cols and x - cols[-1][-1] < 10:
                cols[-1].append(x)
            else:
                cols.append([x])
        starts = [min(c) for c in cols]
        if len(starts) > 8:
            continue
        placed, ok = [], True
        for row in run:
            cmap = {}
            for i, b in row["cells"]:
                k = min(range(len(starts)), key=lambda n: abs(starts[n] - b["x0"]))
                if k in cmap or abs(starts[k] - b["x0"]) > 12:
                    ok = False
                cmap[k] = (i, b)
            placed.append({"cells": cmap, "cont": row["cont"]})
        if ok and all(sum(1 for row in placed if k in row["cells"]) >= 2 for k in range(len(starts))):
            grids.append({"cols": starts, "rows": placed})
    return grids


def _grid_table(grid, p_no, g_idx, translate, page_w):
    """-> (top, bottom, xml, growth): a borderless Word table with one row per source row."""
    cols = grid["cols"]
    right = max(b["col_right"] for row in grid["rows"] for _, b in row["cells"].values())
    right = max(right, max(b["x1"] for row in grid["rows"] for _, b in row["cells"].values()))
    widths = [(cols[k + 1] if k + 1 < len(cols) else right) - cols[k] for k in range(len(cols))]
    tops = [min(b["top"] for _, b in row["cells"].values()) for row in grid["rows"]]
    rows_xml, growth = [], 0.0
    for r_i, row in enumerate(grid["rows"]):
        last = max([b["bottom"] for _, b in list(row["cells"].values()) + row["cont"]])
        height = (tops[r_i + 1] if r_i + 1 < len(tops) else last) - tops[r_i]
        need, tcs = 0.0, []
        for k, w in enumerate(widths):
            members = [row["cells"][k]] if k in row["cells"] else []
            members += [c for c in row["cont"] if min(range(len(cols)), key=lambda n: abs(cols[n] - c[1]["x0"])) == k]
            paras, cell_h = [], 0.0
            for n, (_, b) in enumerate(members):
                size, bold, serif, italic = b["style"]
                out, flag = translate(b["text"], f"page{p_no}/grid{g_idx}/row{r_i}/col{k}")
                pitch = b["pitch"] or size * 1.2
                avail = w - 4
                fs = _fit(out, size, bold, serif, avail, b["nlines"] == 1, floor=0.8) if k < len(widths) - 1 else size
                paras.append(_para(out, fs, bold, serif, italic, "left", flag, line_pt=pitch))
                cell_h += _n_lines(out, fs, bold, serif, avail) * pitch
            need = max(need, cell_h)
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{_tw(w)}" w:type="dxa"/><w:tcBorders>'
                       '<w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="nil"/><w:right w:val="nil"/></w:tcBorders>'
                       '<w:tcMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
                       f'<w:right w:w="{_tw(4)}" w:type="dxa"/></w:tcMar></w:tcPr>'
                       f'{"".join(paras) or "<w:p/>"}</w:tc>')
        growth += max(0.0, need - height)
        rows_xml.append(f'<w:tr><w:trPr><w:cantSplit/><w:trHeight w:val="{_tw(max(height, 1))}" w:hRule="atLeast"/></w:trPr>'
                        f'{"".join(tcs)}</w:tr>')
    grid_xml = "".join(f'<w:gridCol w:w="{_tw(w)}"/>' for w in widths)
    xml = (f'<w:tbl {W_NS}><w:tblPr><w:tblW w:w="{_tw(sum(widths))}" w:type="dxa"/>'
           f'<w:tblInd w:w="{_tw(cols[0])}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
           '<w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="0" w:type="dxa"/>'
           '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="0" w:type="dxa"/></w:tblCellMar></w:tblPr>'
           f'<w:tblGrid>{grid_xml}</w:tblGrid>{"".join(rows_xml)}</w:tbl>')
    bottom = max(b["bottom"] for row in grid["rows"] for _, b in list(row["cells"].values()) + row["cont"])
    return tops[0], bottom, xml, growth


def _side_by_side(bl, blocks, tbs):
    """True when something else sits beside this block at the same height (columns): cannot flow."""
    for o in blocks:
        if o is bl:
            continue
        ov = min(bl["bottom"], o["bottom"]) - max(bl["top"], o["top"])
        if ov > 0.3 * min(bl["bottom"] - bl["top"], o["bottom"] - o["top"]) and (o["x0"] >= bl["x1"] or o["x1"] <= bl["x0"]):
            return True
    for t in tbs:
        ov = min(bl["bottom"], t[3]) - max(bl["top"], t[1])
        if ov > 0 and (t[0] >= bl["x1"] or t[2] <= bl["x0"]):
            return True
    return False


def _is_letter_spaced(text):
    tokens = text.split()
    return len(tokens) >= 12 and sum(1 for t in tokens if len(t) <= 2 and t.isalpha()) >= 0.7 * len(tokens)


def spaced_text_fixes(block_texts):
    """{raw text: fixed text} for letter-spaced lines such as "T H I S D O C UM E NT": the words are rebuilt from
    the vocabulary of the document's other text, and variants of the same line (a mis-read letter) are made equal."""
    from collections import Counter
    from difflib import SequenceMatcher
    spaced = [t for t in dict.fromkeys(block_texts) if _is_letter_spaced(t)]
    if not spaced:
        return {}
    vocab = Counter(w.lower() for t in block_texts if not _is_letter_spaced(t) for w in re.findall(r"[A-Za-z]{2,}", t))

    total = max(1, sum(vocab.values()))

    def cost(piece):
        """Lower is better: common words of the document are cheap, unknown letter runs are expensive."""
        if vocab[piece] > 0 or piece in ("a", "i"):
            return math.log(total / max(vocab[piece], 1)) + 1
        return 12 + 3 * len(piece)

    def split_words(text):
        letters = re.sub(r"[^A-Za-z]", "", text)
        s, n = letters.lower(), len(letters)
        best = [(0.0, 0)] + [(float("inf"), 0)] * n
        for i in range(1, n + 1):
            for j in range(max(0, i - 20), i):
                score = best[j][0] + cost(s[j:i])
                if score < best[i][0]:
                    best[i] = (score, j)
        words, i = [], n
        while i > 0:
            j = best[i][1]
            words.append(letters[j:i])
            i = j
        return " ".join(reversed(words))

    rebuilt = {t: split_words(t) for t in spaced}
    counts = Counter(t for block in block_texts for t in [block] if block in rebuilt)
    groups = []                                       # variants of one line, most frequent first
    for t in sorted(rebuilt, key=lambda t: -counts[t]):
        for g in groups:
            if SequenceMatcher(None, rebuilt[t].lower(), rebuilt[g[0]].lower()).ratio() >= 0.85:
                g.append(t)
                break
        else:
            groups.append([t])
    return {t: rebuilt[g[0]] for g in groups for t in g}


def write_replica(pdf_path, out_path, translate, ocr_pages=None, text_fixes=None):
    """text_fixes maps text as read from the PDF to the repaired text that was translated (see spaced_text_fixes)."""
    ocr_pages = ocr_pages or {}
    if text_fixes:
        translate_raw = translate

        def translate(text, location):
            return translate_raw(text_fixes.get(text.strip(), text), location)
    doc = Document()
    sec = doc.sections[0]
    with pdfplumber.open(pdf_path) as pl, pymupdf.open(pdf_path) as mu:
        normal = doc.styles["Normal"]
        normal.font.name, normal.font.size = SANS, Pt(10)
        body = doc.element.body
        sect = body[-1]

        def put(xml):
            sect.addprevious(parse_xml(xml if " xmlns:w=" in xml[:200] else _wrap(xml)))

        # pre-pass: text that repeats at the same height on several pages is header/footer
        all_blocks, seen, tseen = [], {}, {}
        pages = [clean_page(p) for p in pl.pages]
        for page, mpage in zip(pages, mu):
            tbs = [t.bbox for t in page.find_tables()]
            for k in {tuple(round(v / 4) for v in bb) for bb in tbs}:
                tseen[k] = tseen.get(k, 0) + 1
            bls = _blocks(page_words(page), tbs, _Spans(mpage))
            all_blocks.append(bls)
            for k in {(re.sub(r"\d+", "#", b["text"]), round(b["top"] / 4)) for b in bls}:
                seen[k] = seen.get(k, 0) + 1
        repeats = {k for k, n in seen.items() if n >= 2 and n >= 0.6 * len(pl.pages)}
        repeat_tables = {k for k, n in tseen.items() if n >= 2 and n >= 0.6 * len(pl.pages)}
        stories = {}        # section index -> (header xml list, footer xml list)
        n_sections = [0]

        n_pages = len(pages)
        for p_no, (page, mpage) in enumerate(zip(pages, mu), start=1):
            pw, ph = page.width, page.height
            words = page_words(page)
            spans, edges = _Spans(mpage), page.edges
            tables = page.find_tables()
            objects = []  # floating things anchored on this page
            drawing_only = not words      # no text at all (a drawing or a scan): copied as one picture, as is
            if drawing_only:
                tables, edges = [], []
                rid, _ = doc.part.get_or_add_image(io.BytesIO(mpage.get_pixmap(dpi=170).tobytes("png")))
                _next_id[0] += 1
                objects.append((0, _anchor(0, 0, pw, ph, _picture(rid, pw, ph, _next_id[0]), "pic")))

            # pictures (alpha kept); those inside a table cell travel with the cell
            pics, tb = [], [t.bbox for t in tables]
            for info in ([] if drawing_only else mpage.get_image_info(xrefs=True)):
                if not info["xref"]:
                    continue
                pix = pymupdf.Pixmap(mu, info["xref"])
                try:
                    smask = mu.extract_image(info["xref"]).get("smask")
                    if smask:
                        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(mu, smask))
                    elif pix.alpha == 0 and pix.colorspace.n > 3:
                        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                    png = pix.tobytes("png")
                except Exception:
                    continue
                rid, _ = doc.part.get_or_add_image(io.BytesIO(png))
                pics.append((tuple(info["bbox"]), rid))
            in_cell = set()
            for t in tables:
                for row in t.rows:
                    for bb in row.cells:
                        if bb is None:
                            continue
                        for n, (ib, _) in enumerate(pics):
                            if bb[0] <= (ib[0] + ib[2]) / 2 <= bb[2] and bb[1] <= (ib[1] + ib[3]) / 2 <= bb[3]:
                                in_cell.add(n)
            movable = []    # body pictures sit in the text flow, so text above them growing pushes them down
            for n, (ib, rid) in enumerate(pics):
                if n in in_cell:
                    continue
                w_, h_ = ib[2] - ib[0], ib[3] - ib[1]
                if (ib[1] > 60 and ib[3] < ph - 50 and w_ * h_ < 0.6 * pw * ph and h_ > 2
                        and not any(b["x0"] < ib[2] - 2 and b["x1"] > ib[0] + 2 and b["top"] < ib[3] - 2
                                    and b["bottom"] > ib[1] + 2 for b in all_blocks[p_no - 1])):
                    movable.append((ib, rid))
                else:       # logos, page backgrounds, pictures with text on them stay where they are
                    _next_id[0] += 1
                    objects.append((ib[1], _anchor(ib[0], ib[1], w_, h_, _picture(rid, w_, h_, _next_id[0]), "pic")))

            # free text blocks, each translated on its own.  Body text flows (so a longer translation
            # pushes later content down instead of overlapping it); header/footer text and anything
            # standing beside other content (columns) floats at its exact coordinates.
            blocks = all_blocks[p_no - 1]
            underlined, used_rules = _underlines(blocks, edges, tb)
            flow = []   # (top, bottom, xml, growth)
            grids = _grids(blocks, ph, repeats)
            in_grid = {i for g in grids for row in g["rows"] for i, _ in list(row["cells"].values()) + row["cont"]}
            floaters = []   # fixed-position text boxes, placed once we know how far the flow above them grew
            for g_idx, g in enumerate(grids):
                flow.append(_grid_table(g, p_no, g_idx, translate, pw))
            for i, bl in enumerate(blocks):
                if i in in_grid:
                    continue
                size, bold, serif, italic = bl["style"]
                out, flag = translate(bl["text"], f"page{p_no}/block{i}")
                cx = (bl["x0"] + bl["x1"]) / 2
                ragged = bl["nlines"] == 1 or len({round(l) for l in bl["x0s"]}) > 1
                body_w = bl["col_right"] - bl["col_left"]
                centred = (abs(cx - pw / 2) < 12 and bl["x1"] - bl["x0"] > 60 and ragged
                           and bl["x1"] - bl["x0"] < 0.8 * body_w and not bl["bullet"])
                justified = bl["nlines"] > 1 and all(abs(x - bl["col_right"]) < 3 for x in bl["x1s"][:-1])
                pitch = bl["pitch"] or size * 1.2
                lead = (pitch - (bl["bottom"] - bl["top"]) / bl["nlines"]) / 2   # half-leading above the glyphs
                if bl.get("rotated"):
                    objects.append((bl["top"], _textbox(bl["x0"], bl["top"], (bl["x1"] - bl["x0"]) + 2, (bl["bottom"] - bl["top"]) * 1.4,
                                                        _para(out, size, bold, serif, italic, "left", flag), vert="vert270")))
                    continue
                if not (_is_pinned(bl, ph, repeats) or _side_by_side(bl, blocks, tb)):
                    if centred:
                        ind_l = ind_r = max(10.0, min(bl["x0"], pw - bl["x1"]) - 15)
                        ind = f'<w:ind w:left="{_tw(ind_l)}" w:right="{_tw(ind_r)}"/>'
                    else:
                        first = bl["x0s"][0]
                        rest = min(bl["x0s"][1:]) if bl["nlines"] > 1 else first
                        ind_l, ind_r = rest, max(10.0, pw - bl["col_right"])
                        diff = first - rest
                        extra = (f' w:firstLine="{_tw(diff)}"' if diff > 2
                                 else f' w:hanging="{_tw(-diff)}"' if diff < -2 else "")
                        if bl["bullet"]:    # the marker hangs to the left of the text
                            ind_l = first
                            extra = f' w:hanging="{_tw(max(first - bl["bx"], 6))}"'
                        ind = f'<w:ind w:left="{_tw(ind_l)}" w:right="{_tw(ind_r)}"{extra}/>'
                    xml = _para(out, size, bold, serif, italic,
                                "center" if centred else "both" if justified else "left", flag,
                                extra_ppr=ind, line_pt=pitch, bullet=bl["bullet"], underline=i in underlined)
                    top = bl["top"] - lead
                    est = _n_lines(out, size, bold, serif, pw - ind_l - ind_r)
                    flow.append((top, top + bl["nlines"] * pitch, xml, max(0, est - bl["nlines"]) * pitch))
                    continue
                nb = [o for o in blocks if o is not bl and o["top"] < bl["bottom"] and o["bottom"] > bl["top"]]
                right = min([o["x0"] for o in nb if o["x0"] > bl["x1"]] + [pw - 20]) - 6
                if centred:
                    half = min(cx - 20, pw - 20 - cx, 270)
                    x, w, align = cx - half, 2 * half, "center"
                else:
                    x, w, align = bl["x0"], max(right - bl["x0"], bl["x1"] - bl["x0"]), "left"
                h = max(bl["bottom"] - bl["top"], size * 1.2) * 2 + 6
                # fixed-position text cannot push its neighbours down, so shrink it to keep its original
                # line count (down to 65%) instead of letting a longer translation overlap the next line
                avail = w - (18 if bl["bullet"] else 0)
                fsize = _fit(out, size, bold, serif, avail, bl["nlines"] == 1, floor=0.65)
                if bl["nlines"] == 1 and _text_width(out, fsize, bold, serif) > avail * 0.96:
                    # a fixed line cannot push the next one down: go as small as 55% before letting it wrap
                    fsize = size
                    while fsize > size * 0.55 and _text_width(out, fsize, bold, serif) > avail * 0.96:
                        fsize -= 0.25
                floaters.append((x, bl["top"] - lead, w, h, bl["top"], _is_pinned(bl, ph, repeats),
                                 _para(out, fsize, bold, serif, italic, align, flag, line_pt=pitch,
                                       bullet=bl["bullet"], underline=i in underlined)))

            # stand-alone horizontal rules (outside every table)
            for k, e in enumerate(edges):
                if k in used_rules:
                    continue
                if e["orientation"] == "h" and e["x1"] - e["x0"] > 40 and not any(
                        b[0] - 2 <= e["x0"] and e["x1"] <= b[2] + 2 and b[1] - 2 <= e["top"] <= b[3] + 2 for b in tb):
                    objects.append((e["top"], _hline(e["x0"], e["top"], e["x1"] - e["x0"], e.get("linewidth") or 0.5)))

            # flow: spacer, item, spacer, item ...  (first spacer carries the floating objects)
            hdr_flow, ftr_flow = [], []
            for t_idx, t in enumerate(tables):
                tbl, top, bottom, growth = _build_table(page, t, t_idx, p_no, words, spans, edges, translate, pics)
                repeating = tuple(round(v / 4) for v in t.bbox) in repeat_tables and not drawing_only
                banded = bool(repeat_tables) and not drawing_only      # same place as the document's header / footer, though not identical
                if bottom < 0.3 * ph and (repeating or (banded and top < 0.15 * ph)):
                    hdr_flow.append((top, bottom, tbl, growth))
                elif top > 0.7 * ph and (repeating or (banded and top > 0.85 * ph and bottom > 0.93 * ph)):
                    ftr_flow.append((top, bottom, tbl, growth))
                else:
                    flow.append((top, bottom, tbl, growth))
            for ib, rid in movable:
                w_, h_ = ib[2] - ib[0], ib[3] - ib[1]
                _next_id[0] += 1
                holder = _anchor(ib[0], 0, w_, h_, _picture(rid, w_, h_, _next_id[0]), "pic", rel_v="paragraph")
                flow.append((ib[1], ib[3], f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{_tw(h_)}" '
                                           f'w:lineRule="exact"/></w:pPr>{holder}</w:p>', 0.0))
            head_bottom = max([f[1] for f in hdr_flow], default=0.0)
            foot_top = min([f[0] for f in ftr_flow], default=ph)
            hdr_floats, ftr_floats = [], []
            for fl in list(floaters):
                if fl[5] and (hdr_flow or ftr_flow):         # pinned header / footer text
                    (hdr_floats if fl[4] < ph / 2 else ftr_floats).append(fl)
            has_story = bool(hdr_flow or ftr_flow)
            if has_story:
                head_bottom = max([head_bottom] + [fl[1] + fl[3] / 2 for fl in hdr_floats])
                foot_top = min([foot_top] + [fl[1] for fl in ftr_floats])
                body_clash = (any(f[0] < head_bottom - 2 or f[1] > foot_top + 2 for f in flow)
                              or any(not fl[5] and (fl[4] < head_bottom - 2 or fl[4] > foot_top + 2) for fl in floaters))
                if body_clash:
                    flow += hdr_flow + ftr_flow         # cannot be separated cleanly: the page keeps the old behaviour
                    hdr_flow, ftr_flow, hdr_floats, ftr_floats = [], [], [], []
                    head_bottom, foot_top, has_story = 0.0, ph, False
            if has_story:
                floaters = [fl for fl in floaters if fl not in hdr_floats and fl not in ftr_floats]
                for story_flow, story_floats in ((hdr_flow, hdr_floats), (ftr_flow, ftr_floats)):
                    for fl in list(story_floats):
                        band = (fl[1], fl[1] + fl[3] / 2)
                        crowded = any(f[0] < band[1] and f[1] > band[0] for f in story_flow) or any(
                            o is not fl and o[1] < band[1] and o[1] + o[3] / 2 > band[0] for o in story_floats)
                        if crowded:
                            continue            # beside a table or another text: it keeps its exact position
                        indent = f'<w:ind w:left="{_tw(max(fl[0], 0))}" w:right="{_tw(max(pw - fl[0] - fl[2], 0))}"/>'
                        story_flow.append((band[0], band[1], _wrap(fl[6].replace("<w:pPr>", "<w:pPr>" + indent, 1)), 0.0))
                        story_floats.remove(fl)
            flow.sort(key=lambda f: f[0])
            if not flow:
                flow.append((0, 0, None, 0.0))
            # How far did the flow above each fixed-position block grow? (same arithmetic as the loop below.)
            # Header/footer text stays put; anything else moves down with the content it sat under.
            drift_at, cur_, slack_, drift_ = [], head_bottom, 0.0, 0.0
            for top_, bottom_, _x, growth_ in flow:
                gap_ = max(top_ - cur_, 1.0)
                absorb_ = min(gap_ - 1.0, slack_)
                slack_ += growth_ - absorb_
                drift_ += growth_ - absorb_
                cur_ = max(cur_, bottom_)
                drift_at.append((bottom_, max(0.0, drift_)))
            for fx, fy, fw, fh, orig_top, pinned_, fxml in floaters:
                shift = 0.0 if pinned_ else max([d for b_, d in drift_at if b_ <= orig_top + 2] or [0.0])
                objects.append((orig_top, _textbox(fx, min(fy + shift, foot_top - fh / 2 - 30) if shift else fy, fw, fh, fxml)))
            hdr_objs = [x for y, x in objects if has_story and y < head_bottom - 2] + [
                _textbox(fx, fy, fw, fh, fxml) for fx, fy, fw, fh, _o, _p, fxml in hdr_floats]
            ftr_objs = [x for y, x in objects if has_story and y > foot_top - 2] + [
                _textbox(fx, fy, fw, fh, fxml) for fx, fy, fw, fh, _o, _p, fxml in ftr_floats]
            body_objs = [x for y, x in objects if not (has_story and (y < head_bottom - 2 or y > foot_top - 2))]
            cursor, first_item, slack = head_bottom, True, 0.0
            for top, bottom, xml, growth in flow:
                gap = max(top - cursor, 1.0)
                absorb = min(gap - 1.0, slack)   # earlier rows grew: close up the gap so the page still ends in place
                gap, slack = gap - absorb, slack - absorb
                slack += growth
                holder = "".join(body_objs) if first_item else ""
                put(f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{_tw(gap)}" '
                    f'w:lineRule="exact"/></w:pPr>{holder}</w:p>')
                first_item = False
                if xml:
                    put(xml)
                cursor = max(cursor, bottom)

            # every page is its own section, so each keeps its own size and orientation
            orient = ' w:orient="landscape"' if pw > ph else ""
            sz = f'<w:pgSz w:w="{_tw(pw)}" w:h="{_tw(ph)}"{orient}/>'
            mar = (f'<w:pgMar w:top="{_tw(head_bottom)}" w:right="0" w:bottom="{_tw(ph - foot_top)}" w:left="0" '
                   'w:header="0" w:footer="0" w:gutter="0"/>')
            if has_story:
                stories[n_sections[0]] = (_story_xml(hdr_flow, hdr_objs, 0.0, None),
                                          _story_xml(ftr_flow, ftr_objs, foot_top, ph))
            tiny = '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
            extra = ocr_pages.get(p_no)
            if p_no < n_pages or extra:
                put(f'<w:p {W_NS}><w:pPr>{tiny}<w:sectPr>{sz}{mar}</w:sectPr></w:pPr></w:p>')
                n_sections[0] += 1
            if extra:       # the scan stays as an image; its OCR text, translated, goes on the next page
                from xml.sax.saxutils import escape as _esc
                ind = '<w:ind w:left="1000" w:right="1000"/>'
                put(f'<w:p {W_NS}><w:pPr><w:spacing w:before="1000" w:after="200"/>{ind}</w:pPr><w:r><w:rPr><w:b/>'
                    f'<w:highlight w:val="yellow"/></w:rPr><w:t>Перевод текста сканированной страницы {p_no} (OCR, требует проверки)'
                    '</w:t></w:r></w:p>')
                for line in extra:
                    put(f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="100"/>{ind}</w:pPr><w:r><w:t xml:space="preserve">'
                        f'{_esc(line)}</w:t></w:r></w:p>')
                if p_no < n_pages:
                    put(f'<w:p {W_NS}><w:pPr>{tiny}<w:sectPr>{sz}{mar}</w:sectPr></w:pPr></w:p>')
                    n_sections[0] += 1
            if p_no < n_pages:
                pass
            else:
                put(f'<w:p {W_NS}><w:pPr>{tiny}</w:pPr></w:p>')
                sec.page_width, sec.page_height = Pt(pw), Pt(ph)
                sec.orientation = 1 if pw > ph else 0
                for m in ("left_margin", "right_margin", "top_margin", "bottom_margin", "header_distance",
                          "footer_distance"):
                    setattr(sec, m, Pt(0))
                if has_story:
                    sec.top_margin, sec.bottom_margin = Pt(head_bottom), Pt(ph - foot_top)

        for idx in range(len(doc.sections)):                    # the repeating header / footer of each page's section
            if not stories or idx < min(stories):
                continue
            section = doc.sections[idx]
            head_xml, foot_xml = stories.get(idx, ([_story_xml([], [], 0.0, None)[-1]], [_story_xml([], [], 0.0, None)[-1]]))
            for story, xml_list in ((section.header, head_xml), (section.footer, foot_xml)):
                if not xml_list:
                    continue
                story.is_linked_to_previous = False
                element = story._element
                for child in list(element):
                    element.remove(child)
                for xml in xml_list:
                    element.append(parse_xml(_with_images(xml, doc, story.part)))

    settings = doc.settings.element
    settings.append(parse_xml(
        f'<w:compat {W_NS}><w:compatSetting w:name="compatibilityMode" '
        'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'))
    doc.core_properties.title = "Translated replica of " + pdf_path.replace("\\", "/").split("/")[-1]
    doc.save(out_path)
    return out_path


def _story_xml(items, objects, start, end):
    """Paragraphs of a header or footer: spacers place every item at its page height, objects ride on the first one.
    A footer (end given) is padded to the page bottom so its content starts exactly at `start`."""
    out, cursor, first = [], start, True
    for top, bottom, xml, _growth in sorted(items, key=lambda f: f[0]):
        gap = max(top - cursor, 1.0)
        out.append(f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{_tw(gap)}" w:lineRule="exact"/></w:pPr>'
                   f'{"".join(objects) if first else ""}</w:p>')
        first = False
        out.append(xml)
        cursor = max(cursor, bottom)
    if first and objects:
        out.append(f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/></w:pPr>'
                   f'{"".join(objects)}</w:p>')
    tail = max((end - cursor) if end is not None else 0.0, 1.0)
    out.append(f'<w:p {W_NS}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{_tw(tail)}" w:lineRule="exact"/></w:pPr></w:p>')
    return out


def _with_images(xml, doc, part):
    """Picture relationships belong to one part: copy the images a header / footer uses into its own part."""
    def swap(m):
        blob = doc.part.related_parts[m.group(1)].blob
        return f'r:embed="{part.get_or_add_image(io.BytesIO(blob))[0]}"'
    return re.sub(r'r:embed="(rId\d+)"', swap, xml)


def _wrap(xml):
    return xml.replace(">", f" {W_NS}>", 1)
