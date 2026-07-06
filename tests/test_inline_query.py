import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import QuizEngine, inline_answer, inline_query
from storage import ScoreStore
from wiki_dialogues import Dialogue


class InlineQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_answer_edits_message_and_persists_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreStore(Path(directory) / "quiz.db")
            dialogue = Dialogue("Miya", "A line", "miya.ogg", "Miya/Audio")
            application = SimpleNamespace(
                bot_data={
                    "inline_questions": {
                        "abc": {
                            "dialogue": dialogue,
                            "choices": ["Layla", "Miya", "Clint", "Bruno"],
                            "correct_index": 1,
                        }
                    },
                    "score_store": store,
                }
            )
            callback = SimpleNamespace(
                data="inline_answer:abc:1",
                answer=AsyncMock(),
                edit_message_caption=AsyncMock(),
            )
            user = SimpleNamespace(id=123, username="tester", first_name="Test")
            update = SimpleNamespace(callback_query=callback, effective_user=user)
            context = SimpleNamespace(application=application)

            await inline_answer(update, context)

            callback.answer.assert_awaited_once_with("Correct!")
            callback.edit_message_caption.assert_awaited_once()
            score = store.get_score(123)
            self.assertEqual(score.correct, 1)
            self.assertEqual(score.points, 2)

    async def test_inline_query_returns_one_audio_result_without_transcript(self) -> None:
        dialogues = [
            Dialogue("Miya", "Hidden Miya line", "m.ogg", "Miya/Audio"),
            Dialogue("Layla", "Layla line", "l.ogg", "Layla/Audio"),
            Dialogue("Clint", "Clint line", "c.ogg", "Clint/Audio"),
            Dialogue("Bruno", "Bruno line", "b.ogg", "Bruno/Audio"),
        ]
        engine = QuizEngine(dialogues)
        inline = SimpleNamespace(
            query="Miya",
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(inline_query=inline)
        application = SimpleNamespace(
            bot_data={
                "quiz_engine": engine,
                "inline_questions": {},
                "inline_order": [],
            }
        )
        context = SimpleNamespace(application=application)

        with patch("bot._inline_voice_file_id", new=AsyncMock(return_value="voice-id")):
            await inline_query(update, context)

        results = inline.answer.await_args.args[0]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].voice_file_id, "voice-id")
        self.assertNotIn("Hidden Miya line", results[0].caption)


if __name__ == "__main__":
    unittest.main()
