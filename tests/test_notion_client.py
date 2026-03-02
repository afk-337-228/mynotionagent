"""Unit tests for notion_client: normalize_category, extract_url_from_text."""
import unittest

from bot.notion_client import (
    CATEGORIES,
    normalize_category,
    extract_url_from_text,
)


class TestNormalizeCategory(unittest.TestCase):
    def test_exact_match(self):
        for cat in CATEGORIES:
            self.assertEqual(normalize_category(cat), cat)
            self.assertEqual(normalize_category(cat.lower()), cat)

    def test_aliases(self):
        self.assertEqual(normalize_category("книги"), "Книги к прочтению")
        self.assertEqual(normalize_category("крипта"), "Крипта")
        self.assertEqual(normalize_category("крипту"), "Крипта")
        self.assertEqual(normalize_category("задачи"), "Задачи на сегодня/завтра")
        self.assertEqual(normalize_category("спорт"), "Спорт")

    def test_empty_or_unknown(self):
        self.assertIsNone(normalize_category(""))
        self.assertIsNone(normalize_category("   "))
        self.assertIsNone(normalize_category("несуществующая категория xyz"))

    def test_substring(self):
        self.assertEqual(normalize_category("книг"), "Книги к прочтению")


class TestExtractUrlFromText(unittest.TestCase):
    def test_single_url(self):
        self.assertEqual(extract_url_from_text("Check https://example.com/page"), "https://example.com/page")
        self.assertEqual(extract_url_from_text("https://ya.ru"), "https://ya.ru")

    def test_url_with_trailing_punctuation(self):
        s = "Ссылка: https://example.com/article."
        self.assertEqual(extract_url_from_text(s), "https://example.com/article")

    def test_no_url(self):
        self.assertIsNone(extract_url_from_text("Just text"))
        self.assertIsNone(extract_url_from_text(""))

    def test_http(self):
        self.assertEqual(extract_url_from_text("http://test.org"), "http://test.org")


if __name__ == "__main__":
    unittest.main()
