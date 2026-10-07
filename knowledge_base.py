"""Translation memory and glossary stored as JSON."""
import json
import os
import re
import difflib
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class TMEntry:
    source_text: str
    target_text: str
    status: str = "approved"
    origin: str = "customer-approved"


@dataclass
class GlossaryTerm:
    en_term: str
    target_term: str
    notes: str = ""          # sent to the model
    internal_note: str = ""  # for humans only, never sent to the model
    exact_only: bool = False  # generic word ("Approved", "Date"): applies only when the whole cell is this term


class KnowledgeBase:
    def __init__(self, tm_path: str, glossary_path: str):
        self.tm_path = tm_path
        self.glossary_path = glossary_path
        self.tm: List[TMEntry] = []
        self.glossary: List[GlossaryTerm] = []
        self._load()

    def _load(self):
        if os.path.exists(self.tm_path):
            with open(self.tm_path, encoding="utf-8") as f:
                self.tm = [TMEntry(**row) for row in json.load(f)]
        if os.path.exists(self.glossary_path):
            with open(self.glossary_path, encoding="utf-8") as f:
                self.glossary = [GlossaryTerm(**row) for row in json.load(f)]

    def save(self):
        os.makedirs(os.path.dirname(self.tm_path) or ".", exist_ok=True)
        with open(self.tm_path, "w", encoding="utf-8") as f:
            json.dump([e.__dict__ for e in self.tm], f, ensure_ascii=False, indent=2)
        with open(self.glossary_path, "w", encoding="utf-8") as f:
            json.dump([t.__dict__ for t in self.glossary], f, ensure_ascii=False, indent=2)

    @staticmethod
    def _key(s: str, loose: bool = False) -> str:
        k = " ".join(s.split()).strip().lower()
        return k.rstrip(" :.") if loose else k.rstrip(" :")

    def exact_match(self, source_text: str) -> Optional[TMEntry]:
        """Exact match ignoring case, spacing and a trailing colon; a second pass also ignores a trailing period."""
        for loose in (False, True):
            key = self._key(source_text, loose)
            for entry in self.tm:
                if entry.status == "approved" and self._key(entry.source_text, loose) == key:
                    return entry
        return None

    def fuzzy_match(self, source_text: str, threshold: float = 0.85, top_n: int = 3) -> List[Tuple[float, TMEntry]]:
        scored = []
        for entry in self.tm:
            if entry.status != "approved":
                continue
            ratio = difflib.SequenceMatcher(None, source_text.lower(), entry.source_text.lower()).ratio()
            if ratio >= threshold:
                scored.append((ratio, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_n]

    @staticmethod
    def _loose(s: str) -> str:
        return re.sub(r"\s+", " ", s.replace("&", "and")).strip().lower().rstrip(" :.;,")

    def glossary_exact(self, text: str) -> Optional[GlossaryTerm]:
        """The glossary term when the whole segment IS that term ("Data sheet", "Level Transmitter")."""
        raw = re.sub(r"\s+", " ", text).strip().lower()
        for t in self.glossary:          # same punctuation first ("Project Name" is not "Project Name:")
            if re.sub(r"\s+", " ", t.en_term).strip().lower() == raw:
                return t
        key = self._loose(text)
        for t in self.glossary:
            if self._loose(t.en_term) == key:
                return t
        return None

    def find_glossary_terms(self, text: str) -> List[GlossaryTerm]:
        """Whole-term matches; a term inside a longer matched term is dropped so the model gets no conflicting rules."""
        found = []
        text = re.sub(r"\s+", " ", text).replace(" & ", " and ")   # a line break or "&" must not hide a term
        whole = self._loose(text)
        for t in self.glossary:
            if t.exact_only and self._loose(t.en_term) != whole:
                continue                # a generic word only counts when it is the whole cell
            term = t.en_term.replace(" & ", " and ")
            plural = r"(?:s|es)?" if term[-1:].isalpha() else ""      # "Pulsation Dampeners", "injection packages"
            m = re.search(rf"(?<!\w){re.escape(term)}{plural}(?!\w)", text, re.IGNORECASE)
            if m:
                found.append((m.start(), m.end(), t))
        keep = []
        for s, e, t in found:
            if any(s2 <= s and e <= e2 and (e2 - s2) > (e - s) for s2, e2, _ in found):
                continue
            keep.append(t)
        return keep
