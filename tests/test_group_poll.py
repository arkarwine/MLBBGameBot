import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatType

from bot import QuizEngine, _send_auto_quiz_to_chat, group_poll_answer, hourly_auto_quiz
from storage import ScoreStore
from wiki_dialogues import Dialogue


def sample_dialogues() -> list[Dialogue]:
    return [
        Dialogue("Miya", "Miya line", "miya.ogg", "Miya/Audio"),
        Dialogue("Layla", "Layla line", "layla.ogg", "Layla/Audio"),
        Dialogue("Clint", "Clint line", "clint.ogg", "Clint/Audio"),
        Dialogue("Bruno", "Bruno line", "bruno.ogg", "Bruno/Audio"),
    ]


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

    async def test_hourly_auto_quiz_sends_to_registered_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreStore(Path(directory) / "quiz.db")
            store.upsert_auto_quiz_chat(
                chat_id=-100123,
                chat_type=ChatType.SUPERGROUP,
                title="MLBB Fans",
            )
            dialogues = sample_dialogues()
            bot = SimpleNamespace(
                send_voice=AsyncMock(),
                send_document=AsyncMock(),
                send_message=AsyncMock(),
                send_poll=AsyncMock(
                    return_value=SimpleNamespace(
                        message_id=55,
                        poll=SimpleNamespace(id="poll-55"),
                    )
                ),
            )
            application = SimpleNamespace(
                bot_data={
                    "quiz_engine": QuizEngine(dialogues),
                    "score_store": store,
                    "media_cache": {dialogue.audio_file: ("voice", "voice-id") for dialogue in dialogues},
                    "group_polls": {},
                    "poll_index": {},
                    "poll_order": [],
                    "auto_quiz_seen_audio": {},
                }
            )
            context = SimpleNamespace(application=application, bot=bot)

            with patch("bot._resolve_quiz_difficulty", return_value="normal"):
                await hourly_auto_quiz(context)

            bot.send_voice.assert_awaited_once()
            bot.send_poll.assert_awaited_once()
            poll_kwargs = bot.send_poll.await_args.kwargs
            self.assertEqual(poll_kwargs["chat_id"], -100123)
            self.assertFalse(poll_kwargs["is_anonymous"])
            self.assertEqual(application.bot_data["poll_index"], {"poll-55": application.bot_data["poll_order"][0]})

            with closing(sqlite3.connect(Path(directory) / "quiz.db")) as connection:
                last_quiz_at = connection.execute(
                    "SELECT last_quiz_at FROM auto_quiz_chats WHERE chat_id = ?",
                    (-100123,),
                ).fetchone()[0]
            self.assertIsNotNone(last_quiz_at)

    async def test_auto_quiz_channel_poll_is_anonymous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dialogues = sample_dialogues()
            bot = SimpleNamespace(
                send_voice=AsyncMock(),
                send_document=AsyncMock(),
                send_message=AsyncMock(),
                send_poll=AsyncMock(
                    return_value=SimpleNamespace(
                        message_id=56,
                        poll=SimpleNamespace(id="poll-56"),
                    )
                ),
            )
            application = SimpleNamespace(
                bot_data={
                    "quiz_engine": QuizEngine(dialogues),
                    "score_store": ScoreStore(Path(directory) / "quiz.db"),
                    "media_cache": {dialogue.audio_file: ("voice", "voice-id") for dialogue in dialogues},
                    "group_polls": {},
                    "poll_index": {},
                    "poll_order": [],
                }
            )
            context = SimpleNamespace(application=application, bot=bot)

            with patch("bot._resolve_quiz_difficulty", return_value="normal"):
                await _send_auto_quiz_to_chat(context, -100456, ChatType.CHANNEL, set())

            self.assertTrue(bot.send_poll.await_args.kwargs["is_anonymous"])


if __name__ == "__main__":
    unittest.main()
