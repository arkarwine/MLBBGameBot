from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from typing import Any

import httpx

from wiki_dialogues import API_URL, USER_AGENT


MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class PreparedAudio:
    original: bytes
    opus: bytes | None


class AudioService:
    """Resolve Fandom audio files and convert OGG/Vorbis to Telegram voice OGG/Opus."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def resolve_url(self, file_name: str, max_bytes: int = MAX_AUDIO_BYTES) -> str:
        response = await self.client.get(
            API_URL,
            params={
                "action": "query",
                "prop": "imageinfo",
                "titles": f"File:{file_name}",
                "iiprop": "url|mime|size",
                "format": "json",
                "formatversion": 2,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        page = data["query"]["pages"][0]
        image_info = page.get("imageinfo", [])
        if not image_info:
            raise RuntimeError(f"File is missing from the wiki: {file_name}")

        info = image_info[0]
        if int(info.get("size", 0)) > max_bytes:
            raise RuntimeError(f"Wiki file is unexpectedly large: {file_name}")
        url = str(info["url"])
        if str(info.get("mime", "")).startswith("image/"):
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}format=original"
        return url

    async def download(self, file_name: str, max_bytes: int) -> bytes:
        url = await self.resolve_url(file_name, max_bytes=max_bytes)
        response = await self.client.get(url)
        response.raise_for_status()
        content = response.content
        if not content or len(content) > max_bytes:
            raise RuntimeError(f"Invalid wiki file payload: {file_name}")
        return content

    async def _convert_to_opus(self, source: bytes) -> bytes | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None

        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-application",
            "voip",
            "-f",
            "ogg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(source)
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg conversion failed: {error.decode(errors='replace')}")
        if b"OpusHead" not in output[:256]:
            raise RuntimeError("FFmpeg did not produce an OGG/Opus voice file")
        return output

    async def prepare(self, audio_file: str) -> PreparedAudio:
        original = await self.download(audio_file, MAX_AUDIO_BYTES)
        return PreparedAudio(original=original, opus=await self._convert_to_opus(original))
