import os
import tempfile
import unittest
from pathlib import Path

from app.core.env import load_env_file


class LoadEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".env"
        self._saved = {k: os.environ.get(k) for k in ("T_ONE", "T_QUOTED", "T_KEEP", "T_SPACED")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.dir.cleanup()

    def test_loads_values_and_skips_comments_blanks_and_junk(self):
        self.path.write_text('# a comment\n\nT_ONE=1\nnot a setting\nT_QUOTED="two words"\n  T_SPACED = x=y \n', encoding="utf-8")
        self.assertEqual(load_env_file(self.path), 3)
        self.assertEqual(os.environ["T_ONE"], "1")
        self.assertEqual(os.environ["T_QUOTED"], "two words")
        self.assertEqual(os.environ["T_SPACED"], "x=y")      # only the first "=" splits

    def test_a_real_environment_variable_wins_over_the_file(self):
        os.environ["T_KEEP"] = "from-environment"
        self.path.write_text("T_KEEP=from-file\n", encoding="utf-8")
        self.assertEqual(load_env_file(self.path), 0)
        self.assertEqual(os.environ["T_KEEP"], "from-environment")

    def test_a_missing_file_is_fine(self):
        self.assertEqual(load_env_file(Path(self.dir.name) / "nope.env"), 0)

    def test_a_byte_order_mark_does_not_break_the_first_key(self):
        self.path.write_bytes("﻿T_ONE=1\n".encode("utf-8"))
        load_env_file(self.path)
        self.assertEqual(os.environ.get("T_ONE"), "1")


if __name__ == "__main__":
    unittest.main()
