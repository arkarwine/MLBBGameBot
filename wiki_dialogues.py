from __future__ import annotations

import html
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import httpx
import mwparserfromhell


API_URL = "https://mobile-legends.fandom.com/api.php"
CATEGORY = "Category:Hero audio"
WIKI_BASE_URL = "https://mobile-legends.fandom.com/wiki/"
USER_AGENT = "MLBBTranscriptQuizBot/1.0"
CACHE_VERSION = 4


@dataclass(frozen=True)
class HeroMetadata:
    gender: str = ""
    roles: tuple[str, ...] = ()
    damage_type: str = ""
    attack_type: str = ""
    lane: str = ""
    voice_actors: tuple[str, ...] = ()
    portrait_file: str = ""


@dataclass(frozen=True)
class Dialogue:
    hero: str
    text: str
    audio_file: str
    source_page: str
    gender: str = ""
    roles: tuple[str, ...] = ()
    damage_type: str = ""
    attack_type: str = ""
    lane: str = ""
    voice_actors: tuple[str, ...] = ()
    skin: str = "Default"
    portrait_file: str = ""
    splash_file: str = ""


def _plain_text(value: str) -> str:
    text = mwparserfromhell.parse(value).strip_code(normalize=True, collapse=True)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _skin_sections(wikitext: str) -> list[tuple[str, str]]:
    """Split an audio page's tabber source into (skin/variant, wikitext) pairs."""
    sections: list[tuple[str, list[str]]] = []
    current_skin = "Default"
    current_lines: list[str] = []

    for line in wikitext.splitlines():
        marker = re.fullmatch(r"\s*\|-\|(.+?)=\s*", line)
        if marker:
            if current_lines:
                sections.append((current_skin, current_lines))
            current_skin = _plain_text(marker.group(1)) or "Default"
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_skin, current_lines))
    return [(skin, "\n".join(lines)) for skin, lines in sections]


def parse_dialogues(
    page_title: str,
    wikitext: str,
    metadata: HeroMetadata | None = None,
    skin_portraits: dict[str, str] | None = None,
    default_skin_name: str = "",
) -> list[Dialogue]:
    """Extract {{Aq|file.ogg|transcript}} templates from one hero audio page."""
    hero = page_title.removesuffix("/Audio")
    metadata = metadata or HeroMetadata()
    skin_portraits = {key.casefold(): value for key, value in (skin_portraits or {}).items()}
    results: list[Dialogue] = []

    for skin, section in _skin_sections(wikitext):
        portrait_file = skin_portraits.get(skin.casefold(), metadata.portrait_file)
        splash_skin = default_skin_name if skin.casefold() == "default" else skin
        splash_file = f"{hero} ({splash_skin}).jpg" if splash_skin else ""
        for template in mwparserfromhell.parse(section).filter_templates(recursive=True):
            if str(template.name).strip().casefold() != "aq":
                continue
            if not template.has(1) or not template.has(2):
                continue

            audio_file = str(template.get(1).value).strip()
            transcript = _plain_text(str(template.get(2).value))
            inferred_portrait = re.match(r"(?i)^Audio(\d+)[._-]", audio_file)
            line_portrait = portrait_file
            if skin.casefold() not in skin_portraits and inferred_portrait:
                line_portrait = f"Hero{inferred_portrait.group(1)}-portrait.png"

            # Sound-only entries such as "[grunts]" do not make useful questions.
            if not transcript or re.fullmatch(r"\[[^]]+\]", transcript):
                continue

            results.append(
                Dialogue(
                    hero=hero,
                    text=transcript,
                    audio_file=audio_file,
                    source_page=page_title,
                    gender=metadata.gender,
                    roles=metadata.roles,
                    damage_type=metadata.damage_type,
                    attack_type=metadata.attack_type,
                    lane=metadata.lane,
                    voice_actors=metadata.voice_actors,
                    skin=skin,
                    portrait_file=line_portrait,
                    splash_file=splash_file,
                )
            )

    return results


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class MediaWikiClient:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=30,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.client.get(API_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2**attempt)
                continue

            if "error" in data:
                raise RuntimeError(f"MediaWiki API error: {data['error']}")
            return data
        raise RuntimeError("Could not read the MLBB Wiki API") from last_error

    def english_audio_pages(self) -> list[str]:
        pages: list[str] = []
        continuation: dict[str, Any] = {}

        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": CATEGORY,
                "cmnamespace": 0,
                "cmtype": "page",
                "cmlimit": "max",
                "format": "json",
                "formatversion": 2,
                **continuation,
            }
            data = self._get(params)
            pages.extend(item["title"] for item in data["query"]["categorymembers"])

            if "continue" not in data:
                break
            continuation = data["continue"]

        # English pages end exactly in /Audio. Translations add /id, /ja, etc.
        return sorted(title for title in pages if title.endswith("/Audio") and title.count("/") == 1)

    def page_sources(self, titles: list[str]) -> Iterable[tuple[str, str]]:
        # Small batches avoid oversized responses for heroes with many skin lines.
        for batch in _chunks(titles, 10):
            data = self._get(
                {
                    "action": "query",
                    "prop": "revisions",
                    "titles": "|".join(batch),
                    "rvprop": "content",
                    "rvslots": "main",
                    "format": "json",
                    "formatversion": 2,
                }
            )
            for page in data["query"]["pages"]:
                revisions = page.get("revisions", [])
                if not revisions:
                    continue
                source = revisions[0]["slots"]["main"].get("content", "")
                yield page["title"], source

    def _gameplay_metadata(self, heroes: list[str]) -> dict[str, HeroMetadata]:
        result: dict[str, HeroMetadata] = {}
        fields = ("id", "role1", "role2", "dmg_type", "atk_type", "lane1")

        for batch in _chunks(heroes, 20):
            lines = []
            for hero in batch:
                values = "\t".join(
                    f"{{{{#invoke:Hero|hero|{hero}|{field}|}}}}" for field in fields
                )
                lines.append(f"{hero}\t{values}")

            data = self._get(
                {
                    "action": "expandtemplates",
                    "text": "\n".join(lines),
                    "prop": "wikitext",
                    "title": "MLBB Wiki",
                    "format": "json",
                    "formatversion": 2,
                }
            )
            for line in data["expandtemplates"]["wikitext"].splitlines():
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                hero, hero_id, role1, role2, damage_type, attack_type, lane = (
                    part.strip() for part in parts
                )
                roles = tuple(role for role in (role1, role2) if role)
                result[hero] = HeroMetadata(
                    roles=roles,
                    damage_type=damage_type,
                    attack_type=attack_type,
                    lane=lane,
                    portrait_file=f"Hero{hero_id}-portrait.png" if hero_id else "",
                )
        return result

    @staticmethod
    def _story_metadata(wikitext: str) -> tuple[str, tuple[str, ...]]:
        for template in mwparserfromhell.parse(wikitext).filter_templates(recursive=True):
            if not template.name.matches("Infobox hero story"):
                continue

            gender = _plain_text(str(template.get("gender").value)) if template.has("gender") else ""
            if not template.has("en_va"):
                return gender, ()

            raw_actors = str(template.get("en_va").value)
            raw_actors = re.sub(
                r"<ref\b[^>]*>.*?</ref>|<ref\b[^>]*/>",
                "",
                raw_actors,
                flags=re.IGNORECASE | re.DOTALL,
            )
            actors = tuple(
                actor
                for part in re.split(r"<br\s*/?>|\n", raw_actors, flags=re.IGNORECASE)
                if (actor := _plain_text(part).strip(" ,;"))
            )
            return gender, actors
        return "", ()

    def hero_metadata(self, heroes: list[str]) -> dict[str, HeroMetadata]:
        metadata = self._gameplay_metadata(heroes)
        for hero, source in self.page_sources(heroes):
            gender, voice_actors = self._story_metadata(source)
            gameplay = metadata.get(hero, HeroMetadata())
            metadata[hero] = HeroMetadata(
                gender=gender,
                roles=gameplay.roles,
                damage_type=gameplay.damage_type,
                attack_type=gameplay.attack_type,
                lane=gameplay.lane,
                voice_actors=voice_actors,
                portrait_file=gameplay.portrait_file,
            )
        return metadata

    def skin_portraits(self, heroes: list[str]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {hero: {} for hero in heroes}
        marker_pattern = re.compile(r"(?m)^QUIZ_HERO_BEGIN:(.+?)$")

        for batch in _chunks(heroes, 5):
            text = "\n".join(
                f"QUIZ_HERO_BEGIN:{hero}\n{{{{#invoke:Skin|skinPage|{hero}}}}}"
                for hero in batch
            )
            data = self._get(
                {
                    "action": "expandtemplates",
                    "text": text,
                    "prop": "wikitext",
                    "title": "MLBB Wiki",
                    "format": "json",
                    "formatversion": 2,
                }
            )
            expanded = data["expandtemplates"]["wikitext"]
            matches = list(marker_pattern.finditer(expanded))
            for index, match in enumerate(matches):
                hero = match.group(1).strip()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(expanded)
                section = expanded[match.end() : end]
                for block in section.split('<div class="skin-box"')[1:]:
                    file_match = re.search(
                        r"\[\[File:([^|\]]+-portrait\.png)\|",
                        block,
                        flags=re.IGNORECASE,
                    )
                    name_match = re.search(
                        r'class="skin-box-name".*?<span[^>]*>(.*?)</span>',
                        block,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if not file_match or not name_match:
                        continue
                    name = html.unescape(re.sub(r"<[^>]+>", "", name_match.group(1))).strip()
                    if name:
                        result.setdefault(hero, {})[name] = file_match.group(1)
        return result

    def existing_files(self, file_names: set[str]) -> set[str]:
        existing: set[str] = set()
        for batch in _chunks(sorted(name for name in file_names if name), 50):
            data = self._get(
                {
                    "action": "query",
                    "prop": "imageinfo",
                    "titles": "|".join(f"File:{name}" for name in batch),
                    "iiprop": "size",
                    "format": "json",
                    "formatversion": 2,
                }
            )
            for page in data["query"]["pages"]:
                if page.get("imageinfo"):
                    existing.add(page["title"].removeprefix("File:"))
        return existing

    def fetch_all(self) -> list[Dialogue]:
        pages = self.english_audio_pages()
        heroes = [page.removesuffix("/Audio") for page in pages]
        metadata = self.hero_metadata(heroes)
        portraits = self.skin_portraits(heroes)
        dialogues: list[Dialogue] = []
        for title, source in self.page_sources(pages):
            hero = title.removesuffix("/Audio")
            default_skin_name = next(iter(portraits.get(hero, {})), "")
            dialogues.extend(
                parse_dialogues(
                    title,
                    source,
                    metadata.get(hero),
                    portraits.get(hero),
                    default_skin_name,
                )
            )

        portrait_files = {item.portrait_file for item in dialogues}
        splash_files = {item.splash_file for item in dialogues if item.splash_file}
        splash_png_files = {
            f"{Path(file_name).stem}.png" for file_name in splash_files
        }
        existing_media = self.existing_files(portrait_files | splash_files | splash_png_files)
        dialogues = [
            replace(
                item,
                portrait_file=(
                    item.portrait_file
                    if item.portrait_file in existing_media
                    else metadata[item.hero].portrait_file
                ),
                splash_file=(
                    item.splash_file
                    if item.splash_file in existing_media
                    else (
                        f"{Path(item.splash_file).stem}.png"
                        if item.splash_file
                        and f"{Path(item.splash_file).stem}.png" in existing_media
                        else ""
                    )
                ),
            )
            for item in dialogues
        ]

        # Remove exact duplicates while keeping deterministic cache output.
        unique = {(item.hero, item.text, item.audio_file): item for item in dialogues}
        return sorted(unique.values(), key=lambda item: (item.hero, item.text, item.audio_file))


class DialogueRepository:
    def __init__(self, cache_path: Path, max_age_hours: int = 24) -> None:
        self.cache_path = cache_path
        self.max_age_seconds = max_age_hours * 60 * 60

    def _read_cache(self) -> list[Dialogue]:
        payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        dialogues = []
        for item in payload["dialogues"]:
            item["roles"] = tuple(item.get("roles", ()))
            item["voice_actors"] = tuple(item.get("voice_actors", ()))
            dialogues.append(Dialogue(**item))
        return dialogues

    def _write_cache(self, dialogues: list[Dialogue]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        payload = {
            "cache_version": CACHE_VERSION,
            "source": API_URL,
            "fetched_at": int(time.time()),
            "dialogues": [asdict(item) for item in dialogues],
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.cache_path)

    def load(self, force_refresh: bool = False) -> list[Dialogue]:
        cache_is_fresh = (
            self.cache_path.exists()
            and time.time() - self.cache_path.stat().st_mtime < self.max_age_seconds
        )
        if cache_is_fresh and not force_refresh:
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if payload.get("cache_version") == CACHE_VERSION:
                    return self._read_cache()
            except (OSError, ValueError, KeyError, TypeError):
                pass

        client = MediaWikiClient()
        try:
            dialogues = client.fetch_all()
            if not dialogues:
                raise RuntimeError("The wiki returned no usable dialogue transcripts")
            self._write_cache(dialogues)
            return dialogues
        except Exception:
            # A stale cache is better than taking the quiz bot offline.
            if self.cache_path.exists():
                return self._read_cache()
            raise
        finally:
            client.close()
