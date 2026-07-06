import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot import group_poll_answer
from storage import ScoreStore
from wiki_dialogues import Dialogue


class GroupPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiz_poll_vote_is_scored_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreStore(Path(directory) / "quiz.db")
            dialogue = Dialogue("Miya", "A line", "miya.ogg", "Miya/Audio")
            state = {
                "dialogue": dialogue,
                "correct_index": 1,
                "difficulty": "normal",
                "quiz_mode": "normal",
                "scored_users": set(),
            }
            application = SimpleNamespace(
                bot_data={
                    "poll_index": {"poll-1": "question-1"},
                    "group_polls": {"question-1": state},
                    "score_store": store,
                }
            )
            user = SimpleNamespace(id=123, username="tester", first_name="Test")
            update = SimpleNamespace(
                poll_answer=SimpleNamespace(
                    poll_id="poll-1",
                    option_ids=(1,),
                    user=user,
                )
            )
            context = SimpleNamespace(application=application)

            await group_poll_answer(update, context)
            await group_poll_answer(update, context)

            score = store.get_score(123)
            self.assertEqual(score.answered, 1)
            self.assertEqual(score.correct, 1)
            self.assertEqual(score.points, 2)


if __name__ == "__main__":
    unittest.main()
