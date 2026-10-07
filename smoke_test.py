"""Offline check of masking, knowledge-base lookups, QA checks and review-document output (no API calls).
Run: python smoke_test.py"""

from masking import mask, unmask
from knowledge_base import KnowledgeBase, TMEntry
from qa_checks import run_checks
from word_output import write_review_document
from extraction import Segment

print("=== 1. Masking ===")
sample = "Operating Pressure: 220 Barg, Model STG79S-E1G000-1-I-DHT-13C-A-30A0, Tag RZK-NSS-PT-MI-U10-001/2"
result = mask(sample)
print("Masked:", result.masked_text)
restored, missing = unmask(result.masked_text, result.mapping)
assert restored == sample, f"Round-trip failed!\n  got:      {restored}\n  expected: {sample}"
assert not missing
print("Round-trip OK, nothing missing.\n")

print("=== 2. Knowledge base (exact + fuzzy) ===")
kb = KnowledgeBase(tm_path="data/translation_memory.json", glossary_path="data/glossary.json")
kb.tm.append(TMEntry(source_text="Maximum Overpressure", target_text="Максимальное избыточное давление"))

exact = kb.exact_match("Operating Pressure")
print("Exact match:", exact.target_text if exact else None)
assert exact and exact.target_text == "Рабочее давление"

fuzzy = kb.fuzzy_match("Operating Pressures", threshold=0.8)
print("Fuzzy match found:", [e.target_text for _s, e in fuzzy])
assert fuzzy, "Expected a fuzzy match for a near-identical string"

terms = kb.find_glossary_terms("The Honeywell pressure transmitter uses a Hastelloy diaphragm.")
print("Glossary terms found:", [t.en_term for t in terms])
assert len(terms) >= 2
print()

print("=== 3. QA checks ===")
issues = run_checks(
    original_text="Document Number RZK-NSS-AVN-DAT-INS-90010-00-0",
    translated_text="Құжат нөмірі",  # placeholder dropped on purpose to test detection
    unmasked_missing_placeholders=["[[P0]]"],
    glossary_terms_expected=[],
)
print("Issues found:", [(i.rule, i.severity) for i in issues])
assert any(i.rule == "missing_placeholder" and i.severity == "critical" for i in issues)
print("Critical issue correctly detected for a dropped placeholder.\n")

print("=== 4. Word output generation ===")
fake_segments = [
    {
        "segment": Segment(text="Operating Pressure", page=1, kind="paragraph", location="page1/para1"),
        "translated_text": "Жұмыс қысымы",
        "match_type": "exact",
        "qa_issues": [],
    },
    {
        "segment": Segment(text="Document Number RZK-NSS-AVN-DAT-INS-90010-00-0", page=1,
                            kind="paragraph", location="page1/para2"),
        "translated_text": "Құжат нөмірі",
        "match_type": "llm",
        "qa_issues": issues,  # the critical issue from above, so we can see it highlighted
    },
    {
        "segment": Segment(text="Scanned stamp text", page=2, kind="paragraph",
                            location="page2/para1", from_ocr=True),
        "translated_text": "Сканерленген мөр мәтіні",
        "match_type": "llm",
        "qa_issues": [],
    },
]
write_review_document(fake_segments, "output/smoke_test_review.docx", doc_title="Smoke Test")
print("Wrote output/smoke_test_review.docx — open it and confirm:")
print("  - row 1 (exact match) is unhighlighted")
print("  - row 2 (critical QA issue) is highlighted strong red")
print("  - row 3 (from OCR) is highlighted pale yellow")

print("\nAll smoke tests passed.")