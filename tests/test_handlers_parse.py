"""Unit tests for handler parsing: explicit category, move command."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.handlers import (
    _parse_explicit_category,
    _parse_move_command,
    _parse_done_command,
    _parse_delete_command,
    _parse_search_command,
    _resolve_due_date_from_intent,
    _heuristic_route,
    _should_use_intent_llm,
)


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


class TestParseDoneCommand(unittest.TestCase):
    def test_done_last(self):
        self.assertEqual(_parse_done_command("выполнено"), "last")
        self.assertEqual(_parse_done_command("сделано"), "last")
        self.assertEqual(_parse_done_command("готово"), "last")
        self.assertEqual(_parse_done_command("отметь последнюю как выполненную"), "last")

    def test_done_fragment(self):
        self.assertEqual(_parse_done_command("отметь как выполненную купить молоко"), "купить молоко")

    def test_not_done(self):
        self.assertIsNone(_parse_done_command("просто текст"))
        self.assertIsNone(_parse_done_command("запиши в задачи: позвонить"))


class TestParseDeleteCommand(unittest.TestCase):
    def test_delete_last(self):
        self.assertEqual(_parse_delete_command("удали последнюю"), "last")
        self.assertEqual(_parse_delete_command("удали ее"), "last")
        self.assertEqual(_parse_delete_command("убери её"), "last")

    def test_delete_from_category(self):
        r = _parse_delete_command("из ссылки удали ее")
        self.assertIsInstance(r, tuple)
        self.assertEqual(r[0], "last_in_category")
        self.assertEqual(r[1], "Ссылки / Статьи")
        r2 = _parse_delete_command("удали из крипты")
        self.assertEqual(r2, ("last_in_category", "Крипта"))

    def test_delete_fragment(self):
        self.assertEqual(_parse_delete_command("удали заметку про молоко"), "молоко")

    def test_not_delete(self):
        self.assertIsNone(_parse_delete_command("запиши в задачи: позвонить"))


class TestParseSearchCommand(unittest.TestCase):
    def test_najdi_zametki_pro(self):
        self.assertEqual(_parse_search_command("найди заметки про молоко"), "молоко")
        self.assertEqual(_parse_search_command("найди про крипту"), "крипту")

    def test_poisk(self):
        self.assertEqual(_parse_search_command("поиск книги"), "книги")

    def test_not_search(self):
        self.assertIsNone(_parse_search_command("просто текст"))
        self.assertIsNone(_parse_search_command("запиши в задачи: молоко"))


class TestResolveDueDateFromIntent(unittest.TestCase):
    def test_not_tasks(self):
        self.assertIsNone(_resolve_due_date_from_intent({}, "Разное"))
        self.assertIsNone(_resolve_due_date_from_intent({"due_date_relative": "tomorrow"}, "Спорт"))

    def test_today_tomorrow(self):
        intent = {"due_date_relative": "today"}
        d = _resolve_due_date_from_intent(intent, "Задачи на сегодня/завтра")
        self.assertIsNotNone(d)
        self.assertEqual(len(d), 10)
        self.assertEqual(d.count("-"), 2)
        intent2 = {"due_date_relative": "tomorrow"}
        d2 = _resolve_due_date_from_intent(intent2, "Задачи на сегодня/завтра")
        self.assertIsNotNone(d2)

    def test_empty_or_null(self):
        self.assertIsNone(_resolve_due_date_from_intent({"due_date_relative": ""}, "Задачи на сегодня/завтра"))
        self.assertIsNone(_resolve_due_date_from_intent({}, "Задачи на сегодня/завтра"))


class TestHeuristicRoute(unittest.TestCase):
    def test_github(self):
        r = _heuristic_route("https://github.com/user/repo")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "Гитхаб репы")

    def test_youtube(self):
        r = _heuristic_route("https://youtu.be/abcd")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "YouTube / Видео")

    def test_task_keywords(self):
        r = _heuristic_route("купить молоко")
        self.assertIsNotNone(r)
        self.assertEqual(r[0], "Задачи на сегодня/завтра")

    def test_intent_hints(self):
        self.assertTrue(_should_use_intent_llm("удали последнюю"))
        self.assertTrue(_should_use_intent_llm("завтра купить молоко"))
        self.assertFalse(_should_use_intent_llm("купить молоко"))


if __name__ == "__main__":
    unittest.main()
