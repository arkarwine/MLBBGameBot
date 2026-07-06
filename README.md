# MLBB Transcript Quiz Bot

A minimal Telegram bot that plays a Mobile Legends voice line, shows its transcript, and asks the player to identify the hero from four choices.

The bot reads English dialogue pages from the [MLBB Wiki](https://mobile-legends.fandom.com/wiki/Category:Hero_audio), caches them locally for 24 hours, and excludes sound-only or ambiguous lines. To make choices harder, distractors are ranked using wiki metadata: shared English voice actor first, then gender, role, damage type, attack type, and lane. After an answer, it identifies the audio tab's skin/voice set and sends its splash art, falling back to the portrait only when splash art is unavailable.

## Setup

Requirements: Python 3.10 or newer, [FFmpeg](https://ffmpeg.org/download.html), and a Telegram bot token from [@BotFather](https://t.me/BotFather).

FFmpeg converts the wiki's OGG/Vorbis files to OGG/Opus voice messages supported by Telegram. If FFmpeg is unavailable, the bot sends the original audio as a document instead.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=your-real-token
INLINE_CACHE_CHAT_ID=-1001234567890
```

Run the bot:

```powershell
python bot.py
```

The polling client retries startup and transient Telegram network interruptions automatically. Temporary connection resets are logged as warnings rather than fatal application errors.

The first start downloads the English transcript pages and creates `data/dialogues.json`. Later starts reuse that cache for 24 hours. Audio is fetched only when selected for a question; Telegram file IDs are reused for repeated lines during the same process. Delete the transcript cache to force an immediate refresh.

## Commands

- `/start` — show instructions
- `/quiz` — start with random difficulty; each next question is randomized
- `/quiz easy|normal|hard|expert` — start and keep the selected difficulty
- `/play 10` — start a 10-question session with random difficulty
- `/play 10 hard` — start a fixed-length session at the selected difficulty
- `/stats` — show persistent lifetime and current-session scores

## Difficulty

- **Easy ×1** — base hero voices only; audio and transcript with random hero choices
- **Normal ×2** — base hero voices only; audio and transcript with metadata-similar heroes
- **Hard ×3** — base and skin voices; audio only with metadata-similar heroes
- **Expert ×5** — base and skin voices; audio only, identifying both hero and skin/voice set

Scores, streaks, the last quiz mode, and answer history are stored in `data/quiz.db`. Questions do not repeat within an active `/quiz` run or `/play` session until the eligible pool is exhausted. Transcript answers link back to the source wiki for attribution.

## Group chats

In groups and supergroups, questions use non-anonymous Telegram quiz polls instead of answer buttons. Telegram marks the correct option and shows an explanation containing the hero, skin/voice set, and transcript. Group polls have no timeout and do not send splash art or a separate result message. Individual poll votes are included in persistent scores.

## Inline mode

Enable inline mode for the bot through `@BotFather` using `/setinline`. Set `INLINE_CACHE_CHAT_ID` to a private channel where the bot is an administrator; the bot uploads converted OGG/Opus voice files there to obtain reusable Telegram `file_id` values, then deletes the temporary messages. Without this setting, it attempts to use the inline requester’s private chat, which requires that user to have started the bot.

Users can type `@YourBotUsername` in any chat to generate one shareable audio quiz. Adding a hero name, such as `@YourBotUsername Miya`, filters it to that hero. The initial voice caption does not reveal the transcript. After an answer, the bot edits the caption to show the result, transcript, hero, and skin/voice set. Choices use one button per row and count toward persistent scores.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The transcript text is community content from the MLBB Wiki and is generally available under CC BY-SA. Game audio assets may have different copyright terms; verify that your use complies with those terms.
