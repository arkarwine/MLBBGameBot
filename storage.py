from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class UserScore:
    user_id: int
    answered: int = 0
    correct: int = 0
    points: int = 0
    current_streak: int = 0
    best_streak: int = 0
    quiz_mode: str = "random"

    @property
    def accuracy(self) -> int:
        return round(self.correct * 100 / self.answered) if self.answered else 0


@dataclass(frozen=True)
class AutoQuizChat:
    chat_id: int
    chat_type: str
    title: str = ""


class ScoreStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS user_scores (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    answered INTEGER NOT NULL DEFAULT 0,
                    correct INTEGER NOT NULL DEFAULT 0,
                    points INTEGER NOT NULL DEFAULT 0,
                    current_streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    quiz_mode TEXT NOT NULL DEFAULT 'random',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS answer_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    audio_file TEXT NOT NULL,
                    hero TEXT NOT NULL,
                    skin TEXT NOT NULL,
                    difficulty TEXT NOT NULL,
                    correct INTEGER NOT NULL,
                    points INTEGER NOT NULL,
                    answered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS answer_history_user_time
                    ON answer_history(user_id, answered_at DESC);
                CREATE TABLE IF NOT EXISTS telegram_voice_cache (
                    audio_file TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS auto_quiz_chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_quiz_at TEXT
                );
                """
            )

    def set_mode(
        self,
        user_id: int,
        quiz_mode: str,
        username: str = "",
        first_name: str = "",
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_scores (user_id, username, first_name, quiz_mode)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    quiz_mode = excluded.quiz_mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name, quiz_mode),
            )

    def record_answer(
        self,
        *,
        user_id: int,
        username: str,
        first_name: str,
        quiz_mode: str,
        audio_file: str,
        hero: str,
        skin: str,
        difficulty: str,
        correct: bool,
        points: int,
    ) -> UserScore:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT current_streak, best_streak FROM user_scores WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            current_streak = (int(row["current_streak"]) + 1) if row and correct else (1 if correct else 0)
            previous_best = int(row["best_streak"]) if row else 0
            best_streak = max(previous_best, current_streak)

            connection.execute(
                """
                INSERT INTO user_scores (
                    user_id, username, first_name, answered, correct, points,
                    current_streak, best_streak, quiz_mode
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    answered = user_scores.answered + 1,
                    correct = user_scores.correct + excluded.correct,
                    points = user_scores.points + excluded.points,
                    current_streak = excluded.current_streak,
                    best_streak = excluded.best_streak,
                    quiz_mode = excluded.quiz_mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    username,
                    first_name,
                    int(correct),
                    points,
                    current_streak,
                    best_streak,
                    quiz_mode,
                ),
            )
            connection.execute(
                """
                INSERT INTO answer_history (
                    user_id, audio_file, hero, skin, difficulty, correct, points
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, audio_file, hero, skin, difficulty, int(correct), points),
            )
        return self.get_score(user_id)

    def get_score(self, user_id: int) -> UserScore:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, answered, correct, points, current_streak,
                       best_streak, quiz_mode
                FROM user_scores WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if not row:
            return UserScore(user_id=user_id)
        return UserScore(
            user_id=int(row["user_id"]),
            answered=int(row["answered"]),
            correct=int(row["correct"]),
            points=int(row["points"]),
            current_streak=int(row["current_streak"]),
            best_streak=int(row["best_streak"]),
            quiz_mode=str(row["quiz_mode"]),
        )

    def get_voice_file_id(self, audio_file: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT file_id FROM telegram_voice_cache WHERE audio_file = ?",
                (audio_file,),
            ).fetchone()
        return str(row["file_id"]) if row else None

    def set_voice_file_id(self, audio_file: str, file_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO telegram_voice_cache (audio_file, file_id)
                VALUES (?, ?)
                ON CONFLICT(audio_file) DO UPDATE SET
                    file_id = excluded.file_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (audio_file, file_id),
            )

    def upsert_auto_quiz_chat(
        self,
        *,
        chat_id: int,
        chat_type: str,
        title: str = "",
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO auto_quiz_chats (chat_id, chat_type, title, enabled)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type = excluded.chat_type,
                    title = excluded.title,
                    enabled = 1,
                    seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id, chat_type, title),
            )

    def disable_auto_quiz_chat(self, chat_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE auto_quiz_chats
                SET enabled = 0, seen_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

    def list_auto_quiz_chats(self) -> list[AutoQuizChat]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, chat_type, title
                FROM auto_quiz_chats
                WHERE enabled = 1
                ORDER BY seen_at ASC
                """
            ).fetchall()
        return [
            AutoQuizChat(
                chat_id=int(row["chat_id"]),
                chat_type=str(row["chat_type"]),
                title=str(row["title"]),
            )
            for row in rows
        ]

    def mark_auto_quiz_sent(self, chat_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE auto_quiz_chats
                SET last_quiz_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
