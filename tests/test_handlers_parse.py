"""Unit tests for handler parsing: explicit category, move command."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.handlers import _parse_explicit_category, _parse_move_command


class TestParseExplicitCategory(unittest.TestCase):
    def test_zapishi_v(self):
        r = _parse_explicit_category("запиши в спорт: план на неделю")
        self.assertIsNotNone(r)
        cat, note = r
        self.assertEqual(cat, "Спорт")
        self.assertEqual(note, "план на неделю")

    def test_v_kategoriju(self):
        r = _parse_explicit_category("в крипту: купить btc")
        self.assertIsNotNone(r)
        cat, note = r
        self.assertEqual(cat, "Крипта")
        self.assertEqual(note, "купить btc")

    def test_not_explicit(self):
        self.assertIsNone(_parse_explicit_category("просто текст"))
        self.assertIsNone(_parse_explicit_category("запиши в: без категории"))

    def test_empty_note(self):
        self.assertIsNone(_parse_explicit_category("запиши в разное:   "))


class TestParseMoveCommand(unittest.TestCase):
    def test_perenesi_poslednjuju(self):
        r = _parse_move_command("перенеси последнюю заметку в разное")
        self.assertIsNotNone(r)
        fragment, cat = r
        self.assertIsNone(fragment)
        self.assertEqual(cat, "Разное")

    def test_peremesti_fragment(self):
        r = _parse_move_command("перемести Купить молоко в финансы")
        self.assertIsNotNone(r)
        fragment, cat = r
        self.assertEqual(fragment, "Купить молоко")
        self.assertEqual(cat, "Финансы")

    def test_not_move(self):
        self.assertIsNone(_parse_move_command("просто текст"))
        self.assertIsNone(_parse_move_command("перенеси что-то"))


if __name__ == "__main__":
    unittest.main()
