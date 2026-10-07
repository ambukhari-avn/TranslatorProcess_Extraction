"""Offline tests for the verification stage (the verifier model is simulated; no API calls)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

import llm_client
import pipeline
import verifier
from config import PipelineConfig
from extraction import Segment
from knowledge_base import KnowledgeBase
import layout_output
from layout_output import spaced_text_fixes, write_replica
from llm_client import FatalAPIError
from qa_checks import QAIssue


def reply(rows):
    return json.dumps({"results": [{"id": i, "meaning": m, "completeness": c, "terminology": t, "grammar": g,
                                    "confidence": conf, "verdict": v, "issues": issues}
                                   for i, (m, c, t, g, conf, v, issues) in enumerate(rows, start=1)]})


def result(text, translation, match_type="llm", loc="page1/block0"):
    return {"segment": Segment(text=text, page=1, kind="paragraph", location=loc),
            "translated_text": translation, "match_type": match_type, "qa_issues": []}


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.config = PipelineConfig(workers=1, verify_threshold=85)
        self.kb = KnowledgeBase("data/translation_memory.json", "data/glossary.json")

    def test_confidence_is_capped_by_the_weakest_dimension_and_the_models_own_figure(self):
        found = verifier._parse(reply([(100, 100, 100, 100, 100, "ok", ""),
                                       (100, 100, 100, 40, 98, "major", "bad grammar"),
                                       (90, 90, 90, 90, 70, "minor", "")]), 3)
        self.assertEqual(found[1]["confidence"], 100)
        self.assertEqual(found[2]["confidence"], 65)       # lowest dimension 40 + 25
        self.assertEqual(found[3]["confidence"], 70)       # the model's own, lower figure wins

    def test_unusable_replies_are_ignored(self):
        self.assertEqual(verifier._parse("not json", 2), {})
        self.assertEqual(verifier._parse('{"results": [{"id": 9, "meaning": 1}]}', 2), {})
        fenced = "```json\n" + reply([(90, 90, 90, 90, 90, "ok", "")]) + "\n```"
        self.assertEqual(verifier._parse(fenced, 1)[1]["confidence"], 90)

    def test_weak_segments_are_flagged_and_results_are_cached(self):
        items = [result("Pressure Transmitter", "Датчик давления", loc="page1/block0"),
                 result("Open the valve slowly.", "Откройте клапан.", loc="page1/block1"),
                 result("1.5", "1.5", "passthrough", loc="page1/block2")]
        answer = reply([(98, 98, 98, 98, 98, "ok", ""), (60, 40, 90, 90, 60, "major", "'slowly' is missing")])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(verifier, "_call", return_value=answer) as call:
            cache = str(Path(tmp) / "v.json")
            verifier.verify_results(items, self.kb, self.config, cache)
            self.assertEqual(call.call_count, 1)
            self.assertNotIn("verification", items[2])           # nothing to verify in a bare number
            self.assertEqual([q.rule for q in items[0]["qa_issues"]], [])
            self.assertEqual([q.rule for q in items[1]["qa_issues"]], ["low_confidence"])
            self.assertIn("slowly", items[1]["qa_issues"][0].message)
            again = [result("Pressure Transmitter", "Датчик давления"), result("Open the valve slowly.", "Откройте клапан.")]
            verifier.verify_results(again, self.kb, self.config, cache)
            self.assertEqual(call.call_count, 1)                  # second run reads the cache
            self.assertEqual(again[1]["verification"]["confidence"], items[1]["verification"]["confidence"])

    def test_items_the_model_skipped_are_asked_for_again(self):
        items = [result("First sentence.", "Первое предложение.", loc="a"), result("Second sentence.", "Второе предложение.", loc="b")]
        replies = [reply([(95, 95, 95, 95, 95, "ok", "")]), reply([(92, 92, 92, 92, 92, "ok", "")])]
        with mock.patch.object(verifier, "_call", side_effect=replies) as call:
            verifier.verify_results(items, self.kb, self.config)
        self.assertEqual(call.call_count, 2)
        self.assertEqual([r["verification"]["confidence"] for r in items], [95, 92])

    def test_a_refused_key_leaves_segments_unverified_and_flagged(self):
        items = [result("Open the valve.", "Откройте клапан.")]
        with mock.patch.object(verifier, "_call", side_effect=FatalAPIError("HTTP 401")):
            fatal = verifier.verify_results(items, self.kb, self.config)
        self.assertIn("401", fatal)
        self.assertIsNone(items[0]["verification"]["confidence"])
        self.assertEqual(items[0]["qa_issues"][0].rule, "verification_unavailable")

    def test_document_confidence_weights_long_segments_more(self):
        short = result("Yes", "Да"); long = result("x" * 90, "y" * 90)
        short["verification"] = {"confidence": 50}; long["verification"] = {"confidence": 100}
        summary, lines = verifier.summarise([short, long], self.config)
        self.assertEqual(summary["document_confidence"], 95.0)    # (10*50 + 90*100) / 100
        self.assertEqual(summary["below_threshold"], 1)
        self.assertIn("95.0/100", lines[0])

    def test_structure_report_compares_pages_tables_and_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf, docx = str(Path(tmp) / "a.pdf"), str(Path(tmp) / "a.docx")
            doc = pymupdf.open()
            for n in (1, 2):
                doc.new_page(width=595, height=842).insert_text((72, 100), f"Page text {n}")
            doc.save(pdf)
            write_replica(pdf, docx, lambda text, loc: (text, False))
            report = verifier.structure_report(pdf, docx)
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(report["expected"]["pages"], 2)

    def _weak(self, confidence=60):
        item = result("Approved for its Purpose", "Утверждено для его цели")
        item["verification"] = {"confidence": confidence, "verdict": "major", "issues": "unnatural wording"}
        item["qa_issues"].append(QAIssue("low_confidence", "moderate", "weak"))
        return item

    def _repair(self, item, new_confidence):
        cache = {}
        better = {"model": "m", "prompt_sent": "", "translated_text": "Одобрено для своей цели",
                  "input_tokens": 1, "output_tokens": 1}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm_client, "translate_segment", return_value=better) as translate, \
                mock.patch.object(verifier, "_call", return_value=reply([(new_confidence,) * 4 + (new_confidence, "ok", "")])):
            done = pipeline._repair_flagged([item], self.kb, self.config, [], cache, str(Path(tmp) / "c.json"),
                                            {"Approved for its Purpose"}, None)
        return done, cache, translate

    def test_a_weak_translation_is_redone_with_the_reviewers_findings(self):
        item = self._weak()
        done, cache, translate = self._repair(item, 96)
        self.assertEqual(done, 1)
        self.assertEqual(item["translated_text"], "Одобрено для своей цели")
        self.assertEqual(item["verification"]["confidence"], 96)
        self.assertEqual([q.rule for q in item["qa_issues"]], [])
        self.assertIn("unnatural wording", translate.call_args.kwargs["retry_note"])
        self.assertEqual(next(iter(cache.values()))["translated_text"], "Одобрено для своей цели")

    def test_a_redo_that_does_not_score_higher_is_discarded(self):
        item = self._weak(confidence=70)
        done, cache, _ = self._repair(item, 60)
        self.assertEqual(done, 0)
        self.assertEqual(item["translated_text"], "Утверждено для его цели")
        self.assertEqual(cache, {})

    def test_label_variants_get_one_translation_with_their_own_punctuation(self):
        weak = result("Tested Pressure :", "Испытанное давление :", loc="a")
        good = result("Tested Pressure", "Испытательное давление", loc="b")
        weak["verification"], good["verification"] = {"confidence": 70}, {"confidence": 95}
        weak["qa_issues"].append(QAIssue("low_confidence", "moderate", "x"))
        pipeline._harmonise([weak, good])
        self.assertEqual(weak["translated_text"], "Испытательное давление :")
        self.assertEqual(good["translated_text"], "Испытательное давление")
        self.assertEqual(weak["qa_issues"], [])

    def test_revision_label_is_not_the_column_header(self):
        from masking import mask
        scan = lambda text: __import__("re").sub(r"\[\[P\d+\]\]", " ", mask(text).masked_text)
        self.assertEqual([t.target_term for t in self.kb.find_glossary_terms(scan("Revision 00"))], ["Редакция"])
        self.assertEqual(self.kb.exact_match("REVISION").target_text, "РЕДАКЦИИ")

    def test_letter_spaced_lines_are_rebuilt_and_their_variants_made_equal(self):
        body = ["This document shall be kept in the project file.", "The written permission of the owner is required.",
                "Reproduced material shall not be shared in part or in full without this notice."]
        good = "T H I S D O C UM E NT S H A L L NO T B E R EP RO D U C ED IN P A RT O R F UL L W I T H O UT"
        bad = "T H I S D O C UM E NT S H A L L NO T B E J7 EP J7O D U C ED IN P A RT O R F UL L W I T H O UT"
        fixes = spaced_text_fixes(body + [good] * 5 + [bad])
        self.assertEqual(fixes[good], fixes[bad])
        self.assertIn("DOCUMENT SHALL NOT BE", fixes[good])
        self.assertEqual(spaced_text_fixes(body), {})

    def test_section_numbers_and_roman_numerals_are_list_markers(self):
        for marker in ("6.0", "6.1", "4.1.2", "3.", "a)", "ii.", "iv.", "A."):
            self.assertTrue(layout_output._NUMBERED.match(marker), marker)
        for word in ("mm.", "No.", "2020", "Fig."):
            self.assertFalse(layout_output._NUMBERED.match(word), word)

    def test_a_long_paragraph_reaching_the_page_edge_is_body_text_not_a_footer(self):
        footer = {"top": 760, "bottom": 770, "nlines": 1, "text": "Page 1 of 4"}
        paragraph = {"top": 640, "bottom": 735, "nlines": 9, "text": "Welds having indications " * 20}
        self.assertTrue(layout_output._is_pinned(footer, 792, set()))
        self.assertFalse(layout_output._is_pinned(paragraph, 792, set()))

    def test_a_reference_list_keeps_its_labels_beside_the_text(self):
        def line(top, label, text):
            lab = {"top": top, "bottom": top + 9, "x0": 90, "x1": 130, "bullet": None, "text": label,
                   "words": [{"text": label, "x0": 90, "x1": 130}]}
            body = {"top": top, "bottom": top + 9, "x0": 181, "x1": 400, "bullet": None, "text": text,
                    "words": [{"text": text, "x0": 181, "x1": 400}]}
            return [lab, body]
        rows = line(100, "ASME B31", "2020 - Power Piping") + line(111, "API 650", "2020 - Weld Steel Tanks")
        rows.sort(key=lambda l: (l["top"], l["x0"]))
        kept = layout_output._label_columns(rows, None)
        self.assertEqual([(l["bullet"], l["text"]) for l in kept],
                         [("ASME B31", "2020 - Power Piping"), ("API 650", "2020 - Weld Steel Tanks")])

    def test_aligned_rows_of_cells_become_a_grid(self):
        def block(top, x0, x1, text):
            return {"top": top, "bottom": top + 9, "x0": x0, "x1": x1, "text": text, "nlines": 1, "bullet": None,
                    "style": (9, False, True, False), "pitch": None, "col_left": 90, "col_right": 520}
        blocks = []
        for n, (a, y, d) in enumerate([("ASME B31.1", "2020", "- Power Piping"), ("API 650", "2020", "- Weld Steel Tanks"),
                                       ("AWS D1.1", "2021", "- Structural Welding Code")]):
            top = 300 + n * 11
            blocks += [block(top, 92, 140, a), block(top, 173, 194, y), block(top, 210, 330, d)]
        blocks.append(block(311 + 22, 213, 400, "wrapped"))                  # a line under the last column continues the row
        blocks.append(block(100, 90, 500, "A heading that is alone on its row"))
        grids = layout_output._grids(blocks, 792, set())
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0]["cols"]), 3)
        self.assertEqual(len(grids[0]["rows"]), 3)
        self.assertEqual([c[1]["text"] for c in grids[0]["rows"][2]["cont"]], ["wrapped"])
        self.assertEqual(layout_output._grids(blocks[:6], 792, set()), [])    # two rows are not a grid


if __name__ == "__main__":
    unittest.main()
