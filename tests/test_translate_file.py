"""translate_file: the single entry point of the API, run offline with a stand-in for the model."""
import os
import tempfile
import unittest

from docx import Document

import llm_client
import pipeline
from config import PipelineConfig


class TranslateFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "sample.docx")
        doc = Document()
        for text in ("Maintenance procedure", "Inspection of the pump", "Safety instructions", "Cleaning of the filter"):
            doc.add_paragraph(text)
        doc.save(self.src)
        self.calls = []
        self._original = llm_client.translate_segment

        def stub(masked_text, config, glossary_terms, similar_examples, retry_note="", minimal=False, model="", repair_text=""):
            self.calls.append(masked_text)
            return {"model": "stub", "prompt_sent": "", "translated_text": "Процедура технического обслуживания насоса",
                    "input_tokens": 0, "output_tokens": 0}
        llm_client.translate_segment = stub
        self.config = PipelineConfig(verify_translations=False)

    def tearDown(self):
        llm_client.translate_segment = self._original

    def test_the_result_lists_the_files_counts_and_reports_rising_progress(self):
        seen = []
        result = pipeline.translate_file(self.src, os.path.join(self.dir, "out"), lambda stage, pct: seen.append((stage, pct)),
                                         self.config)
        percents = [p for _, p in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(seen[-1], ("Done", 100))
        self.assertTrue(all(p < 100 for p in percents[:-1]))
        for key in ("output", "review", "log"):
            self.assertTrue(os.path.isfile(result["files"][key]), key)
        self.assertIn("reviewRequired", result)
        self.assertEqual(result["critical"] + result["moderate"] > 0, result["reviewRequired"])

    def test_flagged_rows_are_a_result_not_an_error(self):
        result = pipeline.translate_file(self.src, os.path.join(self.dir, "flag"), None, self.config)
        self.assertTrue(os.path.isfile(result["files"]["output"]))

    def test_unsupported_files_are_refused(self):
        with self.assertRaises(ValueError):
            pipeline.translate_file(os.path.join(self.dir, "notes.txt"), self.dir, None, self.config)

    def test_a_run_can_be_cancelled_from_the_progress_callback(self):
        def cancel(stage, pct):
            if stage == "Translating":
                raise pipeline.JobCancelled()
        with self.assertRaises(pipeline.JobCancelled):
            pipeline.translate_file(self.src, os.path.join(self.dir, "cancel"), cancel, self.config)

    def test_a_shared_cache_dir_saves_the_second_run_its_model_calls(self):
        cache = os.path.join(self.dir, "cache")
        pipeline.translate_file(self.src, os.path.join(self.dir, "one"), None, self.config, cache_dir=cache)
        first = len(self.calls)
        self.assertGreater(first, 0)
        pipeline.translate_file(self.src, os.path.join(self.dir, "two"), None, self.config, cache_dir=cache)
        self.assertEqual(len(self.calls), first)
        self.assertTrue(os.path.isfile(os.path.join(cache, "translation_cache.json")))

    def test_segments_that_could_not_be_translated_make_the_run_fail(self):
        def broken(masked_text, config, glossary_terms, similar_examples, retry_note="", minimal=False, model="", repair_text=""):
            raise ConnectionError("no network")
        llm_client.translate_segment = broken
        with self.assertRaises(RuntimeError) as caught:
            pipeline.translate_file(self.src, os.path.join(self.dir, "broken"), None, self.config)
        self.assertIn("could not be translated", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
