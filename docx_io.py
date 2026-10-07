"""Word in, Word out: the translation goes into the original .docx, so styles, tables, numbering, headers and
footers stay exactly as the client made them and the longer text reflows like any edit made in Word.

A unit is a stretch of consecutive text runs of one paragraph (runs split by a picture, a field or a footnote
mark are separate units). Formatting changes inside a unit travel as markers the translator must keep:
"⟦2⟧" starts the 2nd formatting group, "⟦t⟧" is a tab, "⟦b⟧" a line break. Masking protects them like any
other placeholder, so a dropped or duplicated marker is a QA flag, not a silent formatting loss."""
import copy
import io
import re
import zipfile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
EMU_PT = 12700
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PART_RE = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
MARKER = re.compile("⟦([0-9tb]+)⟧")
LETTER = re.compile(r"[A-Za-z]")


def q(tag):
    return f"{{{W}}}{tag}"


_TEXT_CHILDREN = {q("rPr"), q("t"), q("tab"), q("br")}
_SKIPPED = {q("proofErr"), q("bookmarkStart"), q("bookmarkEnd"), q("permStart"), q("permEnd"),
            q("commentRangeStart"), q("commentRangeEnd"), q("pPr"), q("sdtPr"), q("sdtEndPr")}
_CONTAINERS = {q("hyperlink"), q("ins"), q("smartTag"), q("customXml"), q("sdt"), q("sdtContent")}
_RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow",
              "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
              "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs",
              "em", "lang", "eastAsianLayout", "specVanish", "oMath"]
_SETTINGS_AFTER = {"hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars", "rsids", "mathPr",
                   "attachedSchema", "themeFontLang", "clrSchemeMapping", "decimalSymbol", "listSeparator"}


def _clean_revisions(root):
    """Tracked changes are accepted: deleted text goes, inserted text becomes ordinary text."""
    for tag in ("del", "moveFrom", "rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange"):
        for el in list(root.iter(q(tag))):
            el.getparent().remove(el)
    for tag in ("ins", "moveTo"):
        for el in list(root.iter(q(tag))):
            parent = el.getparent()
            if parent is None or parent.tag in (q("trPr"), q("rPr"), q("pPr")):
                if parent is not None:
                    parent.remove(el)
                continue
            at = parent.index(el)
            for child in list(el):
                parent.insert(at, child)
                at += 1
            parent.remove(el)


def _field_runs(root):
    """Runs that sit inside a field (a table of contents, a page number, a cross-reference): Word refreshes them."""
    inside, depth = set(), 0
    for run in root.iter(q("r")):
        if depth:
            inside.add(run)
        for fc in run.findall(q("fldChar")):
            kind = fc.get(q("fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "end" and depth:
                depth -= 1
    return inside


def _is_text_run(run):
    for child in run:
        if child.tag not in _TEXT_CHILDREN:
            return False
        if child.tag == q("br") and child.get(q("type")) not in (None, "textWrapping"):
            return False
    return True


_LOOK = {"rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "color", "sz", "highlight",
         "u", "shd", "vertAlign"}


def _rpr_key(rpr):
    """What the reader can see: weight, slant, underline, colour, size, font. Letter spacing and language do not count."""
    if rpr is None:
        return ""
    items = []
    for c in rpr:
        name = etree.QName(c).localname
        if name in _LOOK:
            attrs = {etree.QName(k).localname: v for k, v in c.attrib.items()}
            if name in ("b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike") and attrs.get("val") in ("0", "false"):
                continue
            if name == "u" and attrs.get("val") == "none":
                continue
            items.append((name, tuple(sorted(attrs.items()))))
    return repr(sorted(items))


def _spans(paragraph, skip):
    spans, cur = [], []

    def flush():
        if cur:
            spans.append(list(cur))
            cur.clear()

    def walk(node):
        for child in node:
            if child.tag == q("r"):
                if _is_text_run(child) and child not in skip:
                    cur.append(child)
                else:
                    flush()
            elif child.tag in _SKIPPED:
                continue
            elif child.tag in _CONTAINERS:
                flush()
                walk(child)
                flush()
            else:
                flush()

    walk(paragraph)
    flush()
    return spans


def _tokens(runs):
    out = []
    for run in runs:
        rpr = run.find(q("rPr"))
        key = _rpr_key(rpr)
        for child in run:
            if child.tag == q("t"):
                out.append(("text", child.text or "", key, rpr))
            elif child.tag == q("tab"):
                out.append(("tab", "", key, rpr))
            elif child.tag == q("br"):
                out.append(("br", "", key, rpr))
    return out


def _groups(tokens):
    """Formatting groups of a unit: [(key, rPr)] and, for each token, the group it belongs to."""
    groups, assigned = [], []
    for kind, text, key, rpr in tokens:
        neutral = kind != "text" or not text.strip()
        if not groups or (not neutral and groups[-1][0] != key):
            groups.append((key, rpr))
        assigned.append(len(groups) - 1)
    return groups, assigned


def _unit_text(tokens, groups, assigned):
    parts, last = [], -1
    for (kind, text, _k, _r), g in zip(tokens, assigned):
        if len(groups) > 1 and g != last:
            parts.append(f"⟦{g + 1}⟧")
            last = g
        parts.append(text if kind == "text" else "⟦t⟧" if kind == "tab" else "⟦b⟧")
    return "".join(parts)


def _units(root):
    """Every translatable unit of a part, in document order."""
    skip = _field_runs(root)
    tables = {t: n for n, t in enumerate(root.iter(q("tbl")))}
    units = []
    for p_idx, paragraph in enumerate(root.iter(q("p"))):
        in_fallback = any(a.tag == f"{{{MC}}}Fallback" for a in paragraph.iterancestors())
        for s_idx, runs in enumerate(_spans(paragraph, skip)):
            tokens = _tokens(runs)
            groups, assigned = _groups(tokens)
            raw = _unit_text(tokens, groups, assigned)
            text = raw.strip()
            if not LETTER.search(MARKER.sub("", text)):
                continue
            tc = next(paragraph.iterancestors(q("tc")), None)
            where = f"p{p_idx}" + (f".{s_idx}" if s_idx else "")
            if tc is not None:
                tr, tbl = tc.getparent(), next(tc.iterancestors(q("tbl")), None)
                where = f"tbl{tables.get(tbl, 0)}/row{tr.getparent().index(tr)}/col{tr.index(tc)}/" + where
            units.append({"runs": runs, "groups": groups, "text": text, "where": where, "fallback": in_fallback,
                          "lead": raw[:len(raw) - len(raw.lstrip())], "trail": raw[len(raw.rstrip()):],
                          "kind": "table_cell" if tc is not None else "paragraph"})
    return units


def _load(blob):
    root = etree.fromstring(blob)
    _clean_revisions(root)
    return root


def extract_docx(path):
    """Segments for the pipeline: one per distinct unit position (the duplicate copy of a text box is not repeated)."""
    from extraction import Segment
    segments = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            m = PART_RE.match(name)
            if not m:
                continue
            for unit in _units(_load(z.read(name))):
                if unit["fallback"]:
                    continue
                segments.append(Segment(text=unit["text"], page=0, kind=unit["kind"],
                                        location=f"{m.group(1)}/{unit['where']}"))
    return segments


def _put_rpr(rpr, tag, **attrs):
    for old in rpr.findall(q(tag)):
        rpr.remove(old)
    el = etree.SubElement(rpr, q(tag))
    for k, v in attrs.items():
        el.set(q(k), v)
    rpr.remove(el)
    order = _RPR_ORDER.index(tag)
    for i, child in enumerate(rpr):
        name = etree.QName(child).localname
        if name in _RPR_ORDER and _RPR_ORDER.index(name) > order:
            rpr.insert(i, el)
            return
    rpr.append(el)


def _run(group, flagged, text=None, tab=False, br=False):
    run = etree.Element(q("r"))
    rpr = copy.deepcopy(group[1]) if group[1] is not None else etree.Element(q("rPr"))
    _put_rpr(rpr, "lang", val="ru-RU")
    if flagged:
        _put_rpr(rpr, "highlight", val="yellow")
    run.append(rpr)
    if tab:
        etree.SubElement(run, q("tab"))
    elif br:
        etree.SubElement(run, q("br"))
    else:
        t = etree.SubElement(run, q("t"))
        t.text = text
        t.set(XML_SPACE, "preserve")
    return run


def _rebuild(unit, translated, flagged):
    runs, group = [], 0
    chunks = MARKER.split(unit["lead"] + translated + unit["trail"])
    for i, chunk in enumerate(chunks):
        if i % 2 == 0:
            if chunk:
                runs.append(_run(unit["groups"][group], flagged, text=chunk))
        elif chunk == "t":
            runs.append(_run(unit["groups"][group], flagged, tab=True))
        elif chunk == "b":
            runs.append(_run(unit["groups"][group], flagged, br=True))
        elif 0 <= int(chunk) - 1 < len(unit["groups"]):
            group = int(chunk) - 1
    first = unit["runs"][0]
    parent, at = first.getparent(), first.getparent().index(first)
    for old in unit["runs"]:
        parent.remove(old)
    for k, new in enumerate(runs):
        parent.insert(at + k, new)


def _unwrap_table_box(root):
    """A header or footer whose table sits in a fixed-size floating text box clips the longer translation. The table is
    moved into the header / footer itself, which grows with its content. -> (top, bottom, left) of the box in points."""
    boxes = []
    for anchor in root.iter(f"{{{WP}}}anchor"):
        if any(a.tag == f"{{{MC}}}Fallback" for a in anchor.iterancestors()):
            continue
        content = anchor.find(f".//{{{WPS}}}txbx/{q('txbxContent')}")
        pv, ph_, ext = (anchor.find(f"{{{WP}}}{n}") for n in ("positionV", "positionH", "extent"))
        if (content is None or content.find(q("tbl")) is None or pv is None or ph_ is None or ext is None
                or pv.get("relativeFrom") != "page" or ph_.get("relativeFrom") != "page"
                or pv.find(f"{{{WP}}}posOffset") is None or ph_.find(f"{{{WP}}}posOffset") is None):
            continue
        boxes.append((anchor, content, pv, ph_, ext))
    if len(boxes) != 1:
        return None
    anchor, content, pv, ph_, ext = boxes[0]
    top = int(pv.find(f"{{{WP}}}posOffset").text) / EMU_PT
    left = int(ph_.find(f"{{{WP}}}posOffset").text) / EMU_PT
    height = int(ext.get("cy")) / EMU_PT
    holder = next(anchor.iterancestors(f"{{{MC}}}AlternateContent"), None)
    run = (holder if holder is not None else anchor).getparent()
    while run is not None and run.tag != q("r"):
        run = run.getparent()
    paragraph = run.getparent() if run is not None else None
    if paragraph is None or paragraph.tag != q("p"):
        return None
    at = paragraph.index(run)
    moved = list(content)
    paragraph.remove(run)
    for k, el in enumerate(moved):
        if el.tag == q("p"):                                  # the paragraph Word requires after a table: make it a hairline
            for old in el.findall(q("pPr")):
                el.remove(old)
            ppr = etree.SubElement(el, q("pPr"))
            el.remove(ppr)
            el.insert(0, ppr)
            sp = etree.SubElement(ppr, q("spacing"))
            for k_, v in (("before", "0"), ("after", "0"), ("line", "20"), ("lineRule", "exact")):
                sp.set(q(k_), v)
        paragraph.addprevious(el)
    hairline = paragraph
    ppr = hairline.find(q("pPr"))
    if ppr is None:
        ppr = etree.Element(q("pPr"))
        hairline.insert(0, ppr)
    for old in ppr.findall(q("spacing")):
        ppr.remove(old)
    sp = etree.SubElement(ppr, q("spacing"))
    for k_, v in (("before", "0"), ("after", "0"), ("line", "20"), ("lineRule", "exact")):
        sp.set(q(k_), v)
    for tbl in (el for el in moved if el.tag == q("tbl")):
        tblpr = tbl.find(q("tblPr"))
        if tblpr is not None:
            for old in tblpr.findall(q("tblInd")):
                tblpr.remove(old)
            ind = etree.Element(q("tblInd"))
            ind.set(q("w"), str(int(left * 20)))              # corrected for the page margin in _set_distances
            ind.set(q("type"), "dxa")
            tblpr.append(ind)
    return top, top + height, left


def _autofit_boxes(root):
    """Text boxes resize to their text, so a longer translation is not cut off."""
    for body_pr in root.iter(f"{{{WPS}}}bodyPr"):
        for old in list(body_pr):
            if old.tag in (f"{{{DML}}}noAutofit", f"{{{DML}}}normAutofit", f"{{{DML}}}spAutoFit"):
                body_pr.remove(old)
        etree.SubElement(body_pr, f"{{{DML}}}spAutoFit")


def _set_distances(doc_root, rels, infos):
    """Header / footer distances and table indents of the sections that use an unwrapped header or footer."""
    for sect in doc_root.iter(q("sectPr")):
        mar, size = sect.find(q("pgMar")), sect.find(q("pgSz"))
        if mar is None or size is None:
            continue
        left, page_h = int(mar.get(q("left"), "0")) / 20, int(size.get(q("h"), "0")) / 20
        for kind in ("header", "footer"):
            for ref in sect.findall(q(f"{kind}Reference")):
                name = rels.get(ref.get(f"{{{REL}}}id"))
                info = infos.get(name)
                if info is None:
                    continue
                if kind == "header":
                    mar.set(q("header"), str(max(int(info["top"] * 20), 0)))
                else:
                    mar.set(q("footer"), str(max(int((page_h - info["bottom"]) * 20), 0)))
                for tbl in info["tables"]:
                    ind = tbl.find(f"{q('tblPr')}/{q('tblInd')}")
                    if ind is not None:
                        ind.set(q("w"), str(int((info["left"] - left) * 20)))


def translate_docx(src_path, out_path, translate):
    """Copy of src_path with every unit replaced by translate(text, location) -> (text, needs_review).
    Returns what a reviewer should know about: units written, text boxes, fields to refresh."""
    stats = {"units": 0, "text_boxes": 0, "has_fields": False, "unwrapped": 0}
    roots, infos = {}, {}
    with zipfile.ZipFile(src_path) as zin, zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        rels = {}
        if "word/_rels/document.xml.rels" in zin.namelist():
            for rel in etree.fromstring(zin.read("word/_rels/document.xml.rels")):
                rels[rel.get("Id")] = "word/" + rel.get("Target").split("/")[-1]
        pending = []
        for item in zin.infolist():
            blob = zin.read(item.filename)
            m = PART_RE.match(item.filename)
            if m:
                root = _load(blob)
                stats["text_boxes"] += sum(1 for el in root.iter(q("txbxContent"))
                                           if not any(a.tag == f"{{{MC}}}Fallback" for a in el.iterancestors()))
                stats["has_fields"] |= any(fc.get(q("fldCharType")) == "separate" for fc in root.iter(q("fldChar")))
                for el in root.iter(q("trHeight")):                  # a fixed row height would clip the longer text
                    if el.get(q("hRule")) == "exact":
                        el.set(q("hRule"), "atLeast")
                for unit in _units(root):
                    out, flagged = translate(unit["text"], f"{m.group(1)}/{unit['where']}")
                    _rebuild(unit, out, flagged)
                    stats["units"] += 1
                if m.group(1).startswith(("header", "footer")):
                    box = _unwrap_table_box(root)
                    if box:
                        infos[item.filename] = {"top": box[0], "bottom": box[1], "left": box[2],
                                                "tables": [t for t in root.iter(q("tbl"))]}
                        stats["unwrapped"] += 1
                _autofit_boxes(root)
                roots[item.filename] = root
                pending.append(item)
            else:
                zout.writestr(item, blob)
        if "word/document.xml" in roots and infos:
            _set_distances(roots["word/document.xml"], rels, infos)
        for item in pending:
            zout.writestr(item, etree.tostring(roots[item.filename], xml_declaration=True, encoding="UTF-8", standalone=True))
    if stats["has_fields"]:
        _refresh_fields_on_open(out_path)
    return stats


def _refresh_fields_on_open(path):
    """A table of contents, page numbers and cross-references are rebuilt by Word when the file is opened."""
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            blob = zin.read(item.filename)
            if item.filename == "word/settings.xml":
                root = etree.fromstring(blob)
                if root.find(q("updateFields")) is None:
                    el = etree.Element(q("updateFields"))
                    el.set(q("val"), "true")
                    for i, child in enumerate(root):
                        if etree.QName(child).localname in _SETTINGS_AFTER:
                            root.insert(i, el)
                            break
                    else:
                        root.append(el)
                blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, blob)
    with open(path, "wb") as f:
        f.write(buf.getvalue())


def structure_counts(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", "ignore")
    return {"tables": len(re.findall(r"<w:tbl[ >]", xml)), "pictures": xml.count("<pic:pic>") + xml.count("<pic:pic "),
            "paragraphs": len(re.findall(r"<w:p[ >]", xml))}
