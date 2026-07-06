import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from storage import ScoreStore


class ScoreStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "quiz.db"
        self.store = ScoreStore(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def record(self, correct: bool, points: int) -> None:
        self.store.record_answer(
            user_id=123,
            username="tester",
            first_name="Test",
            quiz_mode="hard",
            audio_file="line.ogg",
            hero="Miya",
            skin="Default",
            difficulty="hard",
            correct=correct,
            points=points,
        )

    def test_scores_and_streaks_persist(self) -> None:
        self.record(True, 3)
        self.record(True, 3)
        self.record(False, 0)

        score = ScoreStore(self.database).get_score(123)

        self.assertEqual(score.answered, 3)
        self.assertEqual(score.correct, 2)
        self.assertEqual(score.points, 6)
        self.assertEqual(score.current_streak, 0)
        self.assertEqual(score.best_streak, 2)
        self.assertEqual(score.quiz_mode, "hard")

    def test_answer_history_is_written(self) -> None:
        self.record(True, 3)

        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM answer_history").fetchone()[0]

        self.assertEqual(count, 1)

    def test_mode_is_persisted_without_an_answer(self) -> None:
        self.store.set_mode(123, "expert", "tester", "Test")

        self.assertEqual(self.store.get_score(123).quiz_mode, "expert")

    def test_voice_file_id_cache_persists(self) -> None:
        self.store.set_voice_file_id("line.ogg", "telegram-file-id")

        reopened = ScoreStore(self.database)
        self.assertEqual(reopened.get_voice_file_id("line.ogg"), "telegram-file-id")


if __name__ == "__main__":
    unittest.main()
