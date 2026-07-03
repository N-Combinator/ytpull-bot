"""Thin async wrapper around yt-dlp for extraction and download."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from typing import Any, Callable

from yt_dlp import YoutubeDL

# yt-dlp raises yt_dlp.utils.DownloadError for most user-facing failures.
try:  # pragma: no cover - import shape guard
    from yt_dlp.utils import DownloadError
except Exception:  # pragma: no cover
    class DownloadError(Exception):
        ...


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def extract_info(url: str) -> dict[str, Any]:
    """Fetch metadata + available formats without downloading."""
    def _run() -> dict[str, Any]:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.to_thread(_run)


async def download(
    url: str,
    selector: str,
    outdir: str,
    to_audio: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> str:
    """Download `url` with the given format selector; return the output path."""
    job_id = uuid.uuid4().hex[:8]
    outtmpl = os.path.join(outdir, f"{job_id}.%(ext)s")

    def _run() -> str:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": selector,
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "restrictfilenames": True,
        }
        if progress is not None:
            opts["progress_hooks"] = [progress]
        if to_audio:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if to_audio:
                path = os.path.splitext(path)[0] + ".mp3"

        if not os.path.exists(path):
            # merge_output_format / postprocessing may have changed the extension.
            base = os.path.join(outdir, job_id)
            for ext in (".mp4", ".mkv", ".webm", ".mp3", ".m4a"):
                if os.path.exists(base + ext):
                    return base + ext
            raise DownloadError("Downloaded file not found on disk")
        return path

    return await asyncio.to_thread(_run)
