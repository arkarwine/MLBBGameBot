from __future__ import annotations

import html
import logging
import os
import random
import re
import secrets
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedVoice,
    Update,
)
from telegram.constants import ChatType, ParseMode, PollType
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    PollAnswerHandler,
)
from telegram.request import HTTPXRequest

from audio_files import AudioService, MAX_IMAGE_BYTES
from storage import ScoreStore
from wiki_dialogues import Dialogue, DialogueRepository, WIKI_BASE_URL


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


DIFFICULTIES = {
    "easy": {"label": "Easy", "multiplier": 1, "show_transcript": True},
    "normal": {"label": "Normal", "multiplier": 2, "show_transcript": True},
    "hard": {"label": "Hard", "multiplier": 3, "show_transcript": False},
    "expert": {"label": "Expert", "multiplier": 5, "show_transcript": False},
}
DEFAULT_DIFFICULTY = "normal"
RANDOM_DIFFICULTY = "random"


def _question_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})


def _state_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.chat_data if _is_group(update) else context.user_data


def _parse_quiz_mode(arguments: list[str]) -> str:
    if not arguments:
        return RANDOM_DIFFICULTY
    if len(arguments) != 1:
        raise ValueError("Use /quiz or /quiz <easy|normal|hard|expert>")
    mode = arguments[0].casefold()
    if mode not in DIFFICULTIES and mode != RANDOM_DIFFICULTY:
        raise ValueError("Difficulty must be random, easy, normal, hard, or expert")
    return mode


def _parse_play_args(arguments: list[str]) -> tuple[int, str]:
    count = 10
    mode = RANDOM_DIFFICULTY
    count_seen = False
    mode_seen = False

    for argument in arguments:
        value = argument.casefold()
        if value.isdigit() and not count_seen:
            count = int(value)
            count_seen = True
        elif (value in DIFFICULTIES or value == RANDOM_DIFFICULTY) and not mode_seen:
            mode = value
            mode_seen = True
        else:
            raise ValueError("Use /play [1-50] [random|easy|normal|hard|expert]")

    if not 1 <= count <= 50:
        raise ValueError("Session length must be between 1 and 50")
    return count, mode


def _resolve_quiz_difficulty(mode: str) -> str:
    if mode == RANDOM_DIFFICULTY:
        return random.choice(list(DIFFICULTIES))
    return mode if mode in DIFFICULTIES else random.choice(list(DIFFICULTIES))


class QuizEngine:
    def __init__(self, dialogues: list[Dialogue]) -> None:
        heroes_by_line: dict[str, set[str]] = {}
        for dialogue in dialogues:
            heroes_by_line.setdefault(_question_key(dialogue.text), set()).add(dialogue.hero)

        # Exclude lines shared by multiple heroes because they have ambiguous answers.
        self.dialogues = [
            dialogue
            for dialogue in dialogues
            if len(heroes_by_line[_question_key(dialogue.text)]) == 1
        ]
        self.base_dialogues = [
            dialogue for dialogue in self.dialogues if dialogue.skin.casefold() == "default"
        ]
        self.heroes = sorted({dialogue.hero for dialogue in self.dialogues})
        self.metadata = {dialogue.hero: dialogue for dialogue in self.dialogues}
        self.appearances = sorted({(dialogue.hero, dialogue.skin) for dialogue in self.dialogues})

        if len(self.heroes) < 4 or not self.dialogues or not self.base_dialogues:
            raise RuntimeError("At least four heroes with unique transcripts are required")

    def _similarity_score(self, correct: Dialogue, candidate_hero: str) -> int:
        candidate = self.metadata[candidate_hero]
        score = 0

        if correct.voice_actors and set(correct.voice_actors) & set(candidate.voice_actors):
            score += 100
        if correct.gender and correct.gender.casefold() == candidate.gender.casefold():
            score += 40

        role_overlap = set(correct.roles) & set(candidate.roles)
        score += 15 * len(role_overlap)
        if correct.roles and candidate.roles and correct.roles[0] == candidate.roles[0]:
            score += 10
        if correct.damage_type and correct.damage_type == candidate.damage_type:
            score += 4
        if correct.attack_type and correct.attack_type == candidate.attack_type:
            score += 2
        if correct.lane and correct.lane == candidate.lane:
            score += 2
        return score

    @staticmethod
    def answer_label(dialogue: Dialogue, difficulty: str) -> str:
        if difficulty == "expert":
            return f"{dialogue.hero} — {dialogue.skin}"
        return dialogue.hero

    def _similar_hero_distractors(self, dialogue: Dialogue) -> list[str]:
        candidates = [hero for hero in self.heroes if hero != dialogue.hero]
        random.shuffle(candidates)
        candidates.sort(
            key=lambda hero: self._similarity_score(dialogue, hero),
            reverse=True,
        )
        return candidates[:3]

    def _expert_distractors(self, dialogue: Dialogue) -> list[str]:
        correct_appearance = (dialogue.hero, dialogue.skin)
        candidates = [appearance for appearance in self.appearances if appearance != correct_appearance]
        random.shuffle(candidates)
        candidates.sort(
            key=lambda appearance: (
                200 if appearance[0] == dialogue.hero else 0
            ) + self._similarity_score(dialogue, appearance[0]),
            reverse=True,
        )
        return [f"{hero} — {skin}" for hero, skin in candidates[:3]]

    def new_question(
        self,
        difficulty: str = DEFAULT_DIFFICULTY,
        exclude_audio_files: set[str] | None = None,
    ) -> tuple[Dialogue, list[str]]:
        if difficulty not in DIFFICULTIES:
            difficulty = DEFAULT_DIFFICULTY
        question_pool = (
            self.base_dialogues if difficulty in {"easy", "normal"} else self.dialogues
        )
        if exclude_audio_files is not None:
            available = [
                dialogue
                for dialogue in question_pool
                if dialogue.audio_file not in exclude_audio_files
            ]
            if not available:
                exclude_audio_files.clear()
                available = question_pool
        else:
            available = question_pool
        dialogue = random.choice(available)

        if difficulty == "easy":
            distractors = random.sample(
                [hero for hero in self.heroes if hero != dialogue.hero],
                k=3,
            )
        elif difficulty == "expert":
            distractors = self._expert_distractors(dialogue)
        else:
            distractors = self._similar_hero_distractors(dialogue)

        choices = [self.answer_label(dialogue, difficulty), *distractors]
        random.shuffle(choices)
        return dialogue, choices


def _question_text(dialogue: Dialogue, difficulty: str) -> str:
    settings = DIFFICULTIES[difficulty]
    heading = "Identify the hero and skin" if difficulty == "expert" else "Who says this line?"
    text = (
        f"<b>{heading}</b>\n"
        f"Difficulty: <b>{settings['label']} ×{settings['multiplier']}</b>"
    )
    if settings["show_transcript"]:
        text += f"\n\n<i>\u201c{html.escape(dialogue.text)}\u201d</i>"
    else:
        text += "\n\n<i>Transcript hidden—listen carefully.</i>"
    return text


def _keyboard(
    question_id: str,
    choices: list[str],
    callback_prefix: str = "answer",
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            hero,
            callback_data=f"{callback_prefix}:{question_id}:{index}",
        )
        for index, hero in enumerate(choices)
    ]
    return InlineKeyboardMarkup([[button] for button in buttons])


def _inline_dialogue_pool(engine: QuizEngine, query: str) -> list[Dialogue]:
    normalized = query.strip().casefold()
    if not normalized:
        return engine.base_dialogues
    matches = [
        dialogue
        for dialogue in engine.base_dialogues
        if normalized in dialogue.hero.casefold()
    ]
    return matches or engine.base_dialogues


async def _send_answer_image(
    application: Application,
    bot: Bot,
    chat_id: int,
    dialogue: Dialogue,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    image_files = list(
        dict.fromkeys(
            file_name
            for file_name in (dialogue.splash_file, dialogue.portrait_file)
            if file_name
        )
    )
    if not image_files:
        return False

    audio_service: AudioService = application.bot_data["audio_service"]
    image_cache: dict[str, str] = application.bot_data["image_cache"]
    for image_file in image_files:
        try:
            cached_file_id = image_cache.get(image_file)
            if cached_file_id:
                photo: str | bytes = cached_file_id
            else:
                photo = await audio_service.resolve_url(
                    image_file,
                    max_bytes=MAX_IMAGE_BYTES,
                )

            try:
                message = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            except TelegramError:
                if cached_file_id:
                    raise
                image = await audio_service.download(
                    image_file,
                    MAX_IMAGE_BYTES,
                )
                message = await bot.send_photo(
                    chat_id=chat_id,
                    photo=image,
                    filename=image_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )

            if message.photo:
                image_cache[image_file] = message.photo[-1].file_id
            return True
        except Exception:
            LOGGER.warning("Could not send answer image %s", image_file, exc_info=True)
    return False


def _persist_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    user = update.effective_user
    if not user:
        return
    store: ScoreStore = context.application.bot_data["score_store"]
    store.set_mode(
        user.id,
        mode,
        username=user.username or "",
        first_name=user.first_name or "",
    )


def _session_summary(session: dict[str, object]) -> str:
    answered = int(session["answered"])
    correct = int(session["correct"])
    points = int(session["points"])
    percent = round(correct * 100 / answered) if answered else 0
    mode = str(session["mode"])
    mode_label = "Random" if mode == RANDOM_DIFFICULTY else DIFFICULTIES[mode]["label"]
    return (
        "<b>Session complete</b>\n\n"
        f"Score: <b>{correct}/{answered}</b> ({percent}%)\n"
        f"Points: <b>{points}</b>\n"
        f"Mode: <b>{mode_label}</b>"
    )


def _group_poll_explanation(dialogue: Dialogue) -> str:
    explanation = (
        f"Answer: {dialogue.hero}\n"
        f"Skin/voice set: {dialogue.skin}\n"
        f"Transcript: {dialogue.text}"
    )
    return explanation[:200]


async def _start_group_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    dialogue: Dialogue,
    choices: list[str],
    difficulty: str,
    quiz_mode: str,
    question_id: str,
) -> None:
    chat_id = update.effective_chat.id
    correct_answer = QuizEngine.answer_label(dialogue, difficulty)
    correct_index = choices.index(correct_answer)
    chat_state = _state_data(update, context)
    session = chat_state.get("session")
    if session:
        session["answered"] += 1
        session_complete = session["answered"] >= session["total"]
    else:
        session_complete = False
    next_markup = None if session_complete else InlineKeyboardMarkup(
        [[InlineKeyboardButton("Next question", callback_data="next")]]
    )
    heading = "Identify the hero and skin" if difficulty == "expert" else "Who is this hero?"
    poll_message = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"{heading} • {DIFFICULTIES[difficulty]['label']}",
        options=choices,
        is_anonymous=False,
        type=PollType.QUIZ,
        allows_multiple_answers=False,
        correct_option_id=correct_index,
        explanation=_group_poll_explanation(dialogue),
        reply_markup=next_markup,
    )
    poll_key = question_id
    poll_state = {
        "chat_id": chat_id,
        "message_id": poll_message.message_id,
        "poll_id": poll_message.poll.id,
        "dialogue": dialogue,
        "choices": choices,
        "correct_index": correct_index,
        "difficulty": difficulty,
        "quiz_mode": quiz_mode,
        "scored_users": set(),
    }
    context.application.bot_data["group_polls"][poll_key] = poll_state
    context.application.bot_data["poll_index"][poll_message.poll.id] = poll_key
    poll_order: list[str] = context.application.bot_data["poll_order"]
    poll_order.append(poll_key)
    if len(poll_order) > 500:
        oldest = poll_order.pop(0)
        old_state = context.application.bot_data["group_polls"].pop(oldest, None)
        if old_state:
            context.application.bot_data["poll_index"].pop(old_state["poll_id"], None)


async def _show_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    engine: QuizEngine = context.application.bot_data["quiz_engine"]
    state_data = _state_data(update, context)
    session = state_data.get("session")
    quiz_mode = session["mode"] if session else state_data.get("quiz_mode")
    if quiz_mode is None and update.effective_user:
        store: ScoreStore = context.application.bot_data["score_store"]
        quiz_mode = store.get_score(update.effective_user.id).quiz_mode
        state_data["quiz_mode"] = quiz_mode
    quiz_mode = quiz_mode or RANDOM_DIFFICULTY
    difficulty = _resolve_quiz_difficulty(quiz_mode)
    if session:
        seen_audio = session["seen_audio"]
    else:
        seen_audio = state_data.setdefault("seen_audio", set())
    dialogue, choices = engine.new_question(difficulty, seen_audio)
    seen_audio.add(dialogue.audio_file)
    question_id = secrets.token_hex(4)
    state_data["question"] = {
        "id": question_id,
        "dialogue": dialogue,
        "choices": choices,
        "media": False,
        "difficulty": difficulty,
    }

    text = _question_text(dialogue, difficulty)
    group_chat = _is_group(update)
    markup = None if group_chat else _keyboard(question_id, choices)

    audio_service: AudioService = context.application.bot_data["audio_service"]
    media_cache: dict[str, tuple[str, str]] = context.application.bot_data["media_cache"]
    cached_media = media_cache.get(dialogue.audio_file)
    chat_id = update.effective_chat.id

    try:
        if cached_media:
            media_kind, file_id = cached_media
            if media_kind == "voice":
                message = await context.bot.send_voice(
                    chat_id=chat_id,
                    voice=file_id,
                    caption=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
            else:
                message = await context.bot.send_document(
                    chat_id=chat_id,
                    document=file_id,
                    caption=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
        else:
            prepared = await audio_service.prepare(dialogue.audio_file)
            if prepared.opus:
                try:
                    message = await context.bot.send_voice(
                        chat_id=chat_id,
                        voice=prepared.opus,
                        filename=f"{dialogue.hero}-voice.ogg",
                        caption=text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                    )
                    media_cache[dialogue.audio_file] = ("voice", message.voice.file_id)
                    try:
                        context.application.bot_data["score_store"].set_voice_file_id(
                            dialogue.audio_file,
                            message.voice.file_id,
                        )
                    except Exception:
                        LOGGER.warning("Could not persist Telegram voice file ID", exc_info=True)
                except TelegramError:
                    LOGGER.warning("Telegram rejected voice format; sending original as a document")
                    message = await context.bot.send_document(
                        chat_id=chat_id,
                        document=prepared.original,
                        filename=dialogue.audio_file,
                        caption=text,
                        reply_markup=markup,
                        parse_mode=ParseMode.HTML,
                    )
                    media_cache[dialogue.audio_file] = ("document", message.document.file_id)
            else:
                message = await context.bot.send_document(
                    chat_id=chat_id,
                    document=prepared.original,
                    filename=dialogue.audio_file,
                    caption=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )
                media_cache[dialogue.audio_file] = ("document", message.document.file_id)

        state_data["question"]["media"] = True
    except Exception:
        LOGGER.exception("Could not attach %s; sending a text-only question", dialogue.audio_file)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
    if group_chat:
        await _start_group_poll(
            update,
            context,
            dialogue,
            choices,
            difficulty,
            str(quiz_mode),
            question_id,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "MLBB voice-line quiz. Listen to the audio and identify the hero.\n\n"
        "Use /quiz for random difficulty, or /quiz easy, /quiz normal, "
        "/quiz hard, or /quiz expert. Use /play 10 for a scored session and "
        "/stats to see your lifetime score. Group chats use native quiz polls. "
        "Inline mode supports @BotUsername and optional hero-name searches."
    )


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        mode = _parse_quiz_mode(context.args)
    except ValueError as error:
        await update.effective_message.reply_text(str(error))
        return
    state_data = _state_data(update, context)
    state_data["quiz_mode"] = mode
    state_data["seen_audio"] = set()
    state_data.pop("session", None)
    _persist_mode(update, context, mode)
    await _show_question(update, context)


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        count, mode = _parse_play_args(context.args)
    except ValueError as error:
        await update.effective_message.reply_text(str(error))
        return

    state_data = _state_data(update, context)
    state_data["quiz_mode"] = mode
    state_data["session"] = {
        "total": count,
        "answered": 0,
        "correct": 0,
        "points": 0,
        "mode": mode,
        "seen_audio": set(),
    }
    state_data.pop("seen_audio", None)
    _persist_mode(update, context, mode)
    await update.effective_message.reply_text(
        f"Starting a {count}-question session."
    )
    await _show_question(update, context)


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    state_data = _state_data(update, context)
    question = state_data.get("question")
    if not question or len(parts) != 3 or parts[1] != question["id"]:
        await query.answer("This question has expired. Use /quiz.", show_alert=True)
        return

    try:
        selected = question["choices"][int(parts[2])]
    except (ValueError, IndexError):
        await query.answer("Invalid answer.", show_alert=True)
        return

    await query.answer()
    dialogue: Dialogue = question["dialogue"]
    difficulty_name = question["difficulty"]
    settings = DIFFICULTIES[difficulty_name]
    correct_answer = QuizEngine.answer_label(dialogue, difficulty_name)
    correct = selected == correct_answer
    points_awarded = int(settings["multiplier"]) if correct else 0

    user = update.effective_user
    quiz_mode = state_data.get("quiz_mode", RANDOM_DIFFICULTY)
    if user:
        store: ScoreStore = context.application.bot_data["score_store"]
        try:
            store.record_answer(
                user_id=user.id,
                username=user.username or "",
                first_name=user.first_name or "",
                quiz_mode=quiz_mode,
                audio_file=dialogue.audio_file,
                hero=dialogue.hero,
                skin=dialogue.skin,
                difficulty=difficulty_name,
                correct=correct,
                points=points_awarded,
            )
        except Exception:
            LOGGER.exception("Could not persist score for Telegram user %s", user.id)

    session = state_data.get("session")
    session_complete = False
    session_progress = ""
    if session:
        session["answered"] += 1
        session["correct"] += int(correct)
        session["points"] += points_awarded
        session_complete = session["answered"] >= session["total"]
        session_progress = f"\nSession: <b>{session['answered']}/{session['total']}</b>"

    result = f"✅ <b>Correct! +{points_awarded} points</b>" if correct else (
        f"❌ <b>Incorrect.</b> You chose {html.escape(selected)}."
    )
    source_url = WIKI_BASE_URL + quote(dialogue.source_page.replace(" ", "_"), safe="/")
    text = (
        f"{result}\n\n"
        f"<i>\u201c{html.escape(dialogue.text)}\u201d</i>\n\n"
        f"Answer: <b>{html.escape(dialogue.hero)}</b>\n"
        f"Skin/voice set: <b>{html.escape(dialogue.skin)}</b>\n"
        f"Difficulty: <b>{settings['label']} ×{settings['multiplier']}</b>\n"
        f'<a href="{source_url}">Source: MLBB Wiki</a>'
        f"{session_progress}"
    )
    state_data.pop("question", None)
    next_label = "View results" if session_complete else "Next question"
    next_callback = "session_results" if session_complete else "next"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(next_label, callback_data=next_callback)]]
    )
    await query.edit_message_reply_markup(reply_markup=None)
    image_sent = await _send_answer_image(
        context.application,
        context.bot,
        update.effective_chat.id,
        dialogue,
        caption=text,
        reply_markup=markup,
    )
    if not image_sent:
        # Preserve a usable quiz flow if the wiki image cannot be downloaded.
        if question["media"]:
            await query.edit_message_caption(
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        else:
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
            )


async def group_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    poll_answer = update.poll_answer
    poll_key = context.application.bot_data["poll_index"].get(poll_answer.poll_id)
    if not poll_key:
        return
    state = context.application.bot_data["group_polls"].get(poll_key)
    user = poll_answer.user
    if not state or not user or not poll_answer.option_ids:
        return
    scored_users: set[int] = state["scored_users"]
    if user.id in scored_users:
        return
    scored_users.add(user.id)

    dialogue: Dialogue = state["dialogue"]
    difficulty = str(state["difficulty"])
    settings = DIFFICULTIES[difficulty]
    correct = poll_answer.option_ids[0] == int(state["correct_index"])
    store: ScoreStore = context.application.bot_data["score_store"]
    try:
        store.record_answer(
            user_id=user.id,
            username=user.username or "",
            first_name=user.first_name or "",
            quiz_mode=str(state["quiz_mode"]),
            audio_file=dialogue.audio_file,
            hero=dialogue.hero,
            skin=dialogue.skin,
            difficulty=difficulty,
            correct=correct,
            points=int(settings["multiplier"]) if correct else 0,
        )
    except Exception:
        scored_users.discard(user.id)
        LOGGER.exception("Could not persist group poll score for %s", user.id)


async def _inline_voice_file_id(
    context: ContextTypes.DEFAULT_TYPE,
    dialogue: Dialogue,
    requester_id: int,
) -> str | None:
    store: ScoreStore = context.application.bot_data["score_store"]
    cached = store.get_voice_file_id(dialogue.audio_file)
    if cached:
        return cached

    memory_cache: dict[str, tuple[str, str]] = context.application.bot_data["media_cache"]
    cached_media = memory_cache.get(dialogue.audio_file)
    if cached_media and cached_media[0] == "voice":
        store.set_voice_file_id(dialogue.audio_file, cached_media[1])
        return cached_media[1]

    cache_target: int | str = requester_id
    configured_target = os.getenv("INLINE_CACHE_CHAT_ID", "").strip()
    if configured_target:
        cache_target = (
            int(configured_target)
            if configured_target.lstrip("-").isdigit()
            else configured_target
        )

    audio_service: AudioService = context.application.bot_data["audio_service"]
    try:
        prepared = await audio_service.prepare(dialogue.audio_file)
        if not prepared.opus:
            raise RuntimeError("FFmpeg is required to prepare inline voice audio")
        message = await context.bot.send_voice(
            chat_id=cache_target,
            voice=prepared.opus,
            filename=f"{dialogue.hero}-voice.ogg",
        )
        file_id = message.voice.file_id
        try:
            await context.bot.delete_message(cache_target, message.message_id)
        except TelegramError:
            LOGGER.debug("Could not delete temporary inline voice cache message")
        memory_cache[dialogue.audio_file] = ("voice", file_id)
        store.set_voice_file_id(dialogue.audio_file, file_id)
        return file_id
    except Exception:
        LOGGER.exception("Could not prepare inline audio %s", dialogue.audio_file)
        return None


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    engine: QuizEngine = context.application.bot_data["quiz_engine"]
    pool = _inline_dialogue_pool(engine, query.query)
    dialogue = random.choice(pool)
    voice_file_id = await _inline_voice_file_id(context, dialogue, query.from_user.id)
    if not voice_file_id:
        await query.answer([], cache_time=0, is_personal=True)
        return

    distractors = engine._similar_hero_distractors(dialogue)
    choices = [dialogue.hero, *distractors]
    random.shuffle(choices)
    question_id = secrets.token_hex(8)
    inline_questions: dict[str, dict[str, object]] = context.application.bot_data[
        "inline_questions"
    ]
    inline_questions[question_id] = {
        "dialogue": dialogue,
        "choices": choices,
        "correct_index": choices.index(dialogue.hero),
    }
    inline_order: list[str] = context.application.bot_data["inline_order"]
    inline_order.append(question_id)
    while len(inline_order) > 1000:
        oldest = inline_order.pop(0)
        inline_questions.pop(oldest, None)

    result = InlineQueryResultCachedVoice(
        id=question_id,
        voice_file_id=voice_file_id,
        title="MLBB Audio Quiz",
        caption="<b>Who says this voice line?</b>\n\nChoose an answer below.",
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(
            question_id,
            choices,
            callback_prefix="inline_answer",
        ),
    )
    await query.answer([result], cache_time=0, is_personal=True)


async def inline_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid quiz.", show_alert=True)
        return
    question_id = parts[1]
    state = context.application.bot_data["inline_questions"].pop(question_id, None)
    if not state:
        await query.answer("This inline quiz has already been answered.", show_alert=True)
        return
    try:
        selected_index = int(parts[2])
        selected = state["choices"][selected_index]
    except (ValueError, IndexError):
        await query.answer("Invalid answer.", show_alert=True)
        return

    dialogue: Dialogue = state["dialogue"]
    correct = selected_index == int(state["correct_index"])
    await query.answer("Correct!" if correct else f"Answer: {dialogue.hero}")
    user = update.effective_user
    if user:
        store: ScoreStore = context.application.bot_data["score_store"]
        try:
            store.record_answer(
                user_id=user.id,
                username=user.username or "",
                first_name=user.first_name or "",
                quiz_mode="inline",
                audio_file=dialogue.audio_file,
                hero=dialogue.hero,
                skin=dialogue.skin,
                difficulty="normal",
                correct=correct,
                points=2 if correct else 0,
            )
        except Exception:
            LOGGER.exception("Could not persist inline quiz score for %s", user.id)

    result = "✅ <b>Correct!</b>" if correct else (
        f"❌ <b>Incorrect.</b> You chose {html.escape(selected)}."
    )
    source_url = WIKI_BASE_URL + quote(dialogue.source_page.replace(" ", "_"), safe="/")
    text = (
        f"{result}\n\n"
        f"<i>\u201c{html.escape(dialogue.text)}\u201d</i>\n\n"
        f"Answer: <b>{html.escape(dialogue.hero)}</b>\n"
        f"Skin/voice set: <b>{html.escape(dialogue.skin)}</b>\n"
        f'<a href="{source_url}">Source: MLBB Wiki</a>'
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Create another quiz", switch_inline_query_current_chat="")]]
    )
    await query.edit_message_caption(
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_reply_markup(reply_markup=None)
    state_data = _state_data(update, context)
    session = state_data.get("session")
    if session and session["answered"] >= session["total"]:
        await update.effective_message.reply_text(
            _session_summary(session),
            parse_mode=ParseMode.HTML,
        )
        state_data.pop("session", None)
        return
    await _show_question(update, context)


async def session_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    state_data = _state_data(update, context)
    session = state_data.get("session")
    if not session:
        await query.answer("This session has already ended.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        _session_summary(session),
        parse_mode=ParseMode.HTML,
    )
    state_data.pop("session", None)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    store: ScoreStore = context.application.bot_data["score_store"]
    score = store.get_score(user.id)
    mode_label = (
        "Random"
        if score.quiz_mode == RANDOM_DIFFICULTY
        else (
            "Inline"
            if score.quiz_mode == "inline"
            else DIFFICULTIES.get(
                score.quiz_mode,
                DIFFICULTIES[DEFAULT_DIFFICULTY],
            )["label"]
        )
    )
    text = (
        f"Lifetime score: {score.correct}/{score.answered} ({score.accuracy}%)\n"
        f"Points: {score.points}\n"
        f"Current streak: {score.current_streak}\n"
        f"Best streak: {score.best_streak}\n"
        f"Quiz mode: {mode_label}"
    )
    session = _state_data(update, context).get("session")
    if session:
        text += "\n\n" + _session_summary(session).replace("<b>Session complete</b>\n\n", "<b>Current session</b>\n\n")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update is None and isinstance(context.error, NetworkError):
        LOGGER.warning(
            "Telegram polling connection was interrupted; retrying automatically: %s",
            context.error,
        )
        return
    LOGGER.exception("Unhandled Telegram update error", exc_info=context.error)


async def shutdown(application: Application) -> None:
    audio_service: AudioService = application.bot_data["audio_service"]
    await audio_service.close()


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env or the environment")

    repository = DialogueRepository(Path("data/dialogues.json"))
    LOGGER.info("Loading MLBB dialogue transcripts...")
    dialogues = repository.load()
    engine = QuizEngine(dialogues)
    LOGGER.info("Loaded %d usable transcripts from %d heroes", len(engine.dialogues), len(engine.heroes))

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=15,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=10,
        media_write_timeout=60,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=2,
        connect_timeout=15,
        read_timeout=45,
        write_timeout=15,
        pool_timeout=10,
    )
    application = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(updates_request)
        .post_shutdown(shutdown)
        .build()
    )
    application.bot_data["quiz_engine"] = engine
    application.bot_data["audio_service"] = AudioService()
    application.bot_data["score_store"] = ScoreStore(Path("data/quiz.db"))
    application.bot_data["media_cache"] = {}
    application.bot_data["image_cache"] = {}
    application.bot_data["group_polls"] = {}
    application.bot_data["poll_index"] = {}
    application.bot_data["poll_order"] = []
    application.bot_data["inline_questions"] = {}
    application.bot_data["inline_order"] = []
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("play", play))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(answer, pattern=r"^answer:"))
    application.add_handler(CallbackQueryHandler(inline_answer, pattern=r"^inline_answer:"))
    application.add_handler(CallbackQueryHandler(next_question, pattern=r"^next$"))
    application.add_handler(CallbackQueryHandler(session_results, pattern=r"^session_results$"))
    application.add_handler(PollAnswerHandler(group_poll_answer))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_error_handler(error_handler)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        timeout=20,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
