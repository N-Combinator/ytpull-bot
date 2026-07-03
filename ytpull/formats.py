"""Turn a yt-dlp info dict into a small set of user-facing quality options.

We deliberately do NOT expose raw yt-dlp format ids as choices: modern YouTube
serves most resolutions as video-only DASH streams that must be merged with an
audio track (ffmpeg) to produce a playable file. Choosing a raw video-only id
would yield a silent video. Instead we present resolution tiers and download
each with a merge selector.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityOption:
    key: str          # "v720", "v1080", "audio" — goes into callback_data
    label: str        # human label for the button
    est_size: int | None   # estimated bytes, or None if unknown
    kind: str         # "video" | "audio"


def _size(f: dict) -> int | None:
    return f.get("filesize") or f.get("filesize_approx")


def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def _has_video(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none")


def parse_options(info: dict, ffmpeg_available: bool) -> list[QualityOption]:
    """Build the quality menu from an extracted-info dict."""
    formats = info.get("formats") or []

    audio_only = [f for f in formats if _has_audio(f) and not _has_video(f)]
    best_audio = max(audio_only, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = _size(best_audio) if best_audio else None

    by_height: dict[int, list[dict]] = {}
    for f in formats:
        if not _has_video(f):
            continue
        h = f.get("height")
        if not h:
            continue
        by_height.setdefault(int(h), []).append(f)

    options: list[QualityOption] = []
    for h in sorted(by_height, reverse=True):
        bucket = by_height[h]
        progressive = [f for f in bucket if _has_audio(f)]
        if progressive:
            best = max(progressive, key=lambda f: _size(f) or 0)
            est = _size(best)
        else:
            # Video-only at this height needs an ffmpeg merge with the audio track.
            if not ffmpeg_available:
                continue
            best = max(bucket, key=lambda f: _size(f) or 0)
            vsize = _size(best)
            est = (vsize + audio_size) if (vsize and audio_size) else vsize
        options.append(QualityOption(key=f"v{h}", label=f"{h}p", est_size=est, kind="video"))

    if best_audio:
        options.append(
            QualityOption(key="audio", label="Аудио (mp3)", est_size=audio_size, kind="audio")
        )
    return options


def selector_for(key: str) -> str:
    """yt-dlp format selector for a given option key."""
    if key == "audio":
        return "bestaudio/best"
    if key.startswith("v"):
        h = key[1:]
        # Prefer a merged video+audio at/under the height; fall back to the best
        # progressive stream that already carries audio.
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    return "best"


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    units = ["B", "KB", "MB", "GB"]
    val = float(n)
    for u in units:
        if val < 1024 or u == "GB":
            return f"{val:.0f} {u}" if u in ("B", "KB") else f"{val:.1f} {u}"
        val /= 1024
    return f"{val:.1f} GB"
