"""Unit tests for classifier: sanitization."""
import unittest

from bot.classifier import _sanitize_note_for_classifier, MAX_NOTE_LENGTH


class TestSanitizeNote(unittest.TestCase):
    def test_truncate(self):
        long = "а" * (MAX_NOTE_LENGTH + 100)
        out = _sanitize_note_for_classifier(long)
        self.assertEqual(len(out), MAX_NOTE_LENGTH)

    def test_strip_and_collapse_whitespace(self):
        out = _sanitize_note_for_classifier("  one   two  \n  three  ")
        self.assertEqual(out, "one two three")

    def test_empty(self):
        self.assertEqual(_sanitize_note_for_classifier(""), "")
        self.assertEqual(_sanitize_note_for_classifier("   "), "")
        self.assertEqual(_sanitize_note_for_classifier(None), "")


if __name__ == "__main__":
    unittest.main()
