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
    h264: bool = True  # True if an H.264 (avc1) stream exists at this tier —
                       # tiers without it (usually >1080p) may not play on iOS


def _size(f: dict, duration: float | None = None) -> int | None:
    """Format size in bytes: exact if yt-dlp gives it, else estimated from bitrate.

    Many DASH formats carry no filesize/filesize_approx, only a bitrate (tbr/vbr/
    abr in kbit/s). With the clip duration we can approximate bytes so the menu
    never shows a bare '?'.
    """
    exact = f.get("filesize") or f.get("filesize_approx")
    if exact:
        return exact
    bitrate = f.get("tbr") or f.get("vbr") or f.get("abr")  # kbit/s
    if bitrate and duration:
        return int(bitrate * 1000 / 8 * duration)
    return None


def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in (None, "none")


def _has_video(f: dict) -> bool:
    return f.get("vcodec") not in (None, "none")


def _pick_video(bucket: list[dict]) -> dict:
    """The format `selector_for` will actually download at this height.

    Mirror the selector's preference (avc1 first) and — crucially for the size
    estimate — prefer formats that carry a REAL filesize, so a premium/unavailable
    stream with only a bitrate doesn't blow the estimate up (yt-dlp won't pick it
    either). Among the candidates, highest bitrate wins, like yt-dlp's `bestvideo`.
    """
    avc1 = [f for f in bucket if (f.get("vcodec") or "").startswith("avc1")]
    pool = avc1 or bucket
    real = [f for f in pool if (f.get("filesize") or f.get("filesize_approx"))]
    chooser = real or pool
    return max(chooser, key=lambda f: f.get("tbr") or 0)


def parse_options(info: dict, ffmpeg_available: bool) -> list[QualityOption]:
    """Build the quality menu from an extracted-info dict."""
    formats = info.get("formats") or []
    duration = info.get("duration")

    audio_only = [f for f in formats if _has_audio(f) and not _has_video(f)]
    best_audio = max(audio_only, key=lambda f: f.get("abr") or 0, default=None)
    audio_size = _size(best_audio, duration) if best_audio else None

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
        # We only offer up to 1080p: above that YouTube has no H.264 (only av01/vp9)
        # and the files are huge — 1080p is the sweet spot for the document-based
        # delivery that plays natively on phones.
        if h > 1080:
            continue
        bucket = by_height[h]
        has_h264 = any((f.get("vcodec") or "").startswith("avc1") for f in bucket)
        video_only = [f for f in bucket if not _has_audio(f)]
        if video_only and ffmpeg_available:
            # We merge the best video-only stream with a separate audio track, so
            # the estimate is that video's size plus the audio's.
            best = _pick_video(video_only)
            vsize = _size(best, duration)
            est = (vsize + audio_size) if (vsize and audio_size) else vsize
        else:
            progressive = [f for f in bucket if _has_audio(f)]
            if not progressive:
                continue
            est = _size(_pick_video(progressive), duration)
        options.append(
            QualityOption(key=f"v{h}", label=f"{h}p", est_size=est, kind="video", h264=has_h264)
        )

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
        # Download the EXACT height the user picked. avc1 is preferred only as a
        # tie-break AT that height (best compatibility); we no longer let the avc1
        # preference silently drop to a lower resolution — since files are sent as
        # documents, av01/vp9 play fine too. Only fall below if the height is absent.
        return (
            f"bestvideo[height={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[height={h}]+bestaudio/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]/best"
        )
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
