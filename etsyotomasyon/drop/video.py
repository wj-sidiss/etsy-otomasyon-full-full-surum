"""A listing video made from a draft's own mockups.

Etsy lets a listing carry one video (5 to 15 seconds, MP4 or MOV, up to 100 MB). Most
sellers have none, and a listing with one is judged on more than its photos. This makes
a short, silent, square one from the pictures a draft already has: each picture slowly
zooms in and fades into the next.

The encoder is ffmpeg, a free program the seller installs once; etsyotomasyon does not ship
one. `find_ffmpeg` looks for it on the PATH and in the places it is usually installed.
Without it a run simply makes no video and says why.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ..errors import ValidationError

SIZE = 1080  # Etsy asks for at least 1080 px on the short side
FPS = 30
MIN_SECONDS, MAX_SECONDS = 5.0, 15.0
DEFAULT_SECONDS = 12.0
FADE = 0.6
MAX_PICTURES = 6
MAX_BYTES = 100 * 1024 * 1024
SUFFIX = ".mp4"
_ENV = "ETSYOTOMASYON_FFMPEG"
_PLACES = (
    "C:/Program Files/ffmpeg/bin/ffmpeg.exe",
    "C:/ffmpeg/bin/ffmpeg.exe",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)


def find_ffmpeg() -> str | None:
    """Where ffmpeg is: the ETSYOTOMASYON_FFMPEG variable, the PATH, then the usual places."""
    wanted = os.environ.get(_ENV, "").strip()
    if wanted and Path(wanted).is_file():
        return wanted
    found = shutil.which("ffmpeg")
    if found:
        return found
    for place in _PLACES:
        if Path(place).is_file():
            return place
    return None


def available() -> bool:
    return find_ffmpeg() is not None


def plan(count: int, seconds: float = DEFAULT_SECONDS) -> list[float]:
    """How long each of `count` pictures is on screen so that the video, with its fades,
    lasts `seconds` (kept inside what Etsy takes)."""
    count = max(1, min(MAX_PICTURES, int(count)))
    total = max(MIN_SECONDS, min(MAX_SECONDS, float(seconds)))
    if count == 1:
        return [total]
    # n clips overlap by FADE each: total = sum(durations) - (n - 1) * FADE
    each = (total + (count - 1) * FADE) / count
    return [round(each, 3)] * count


def command(ffmpeg: str, pictures: Sequence[Path], out: Path, seconds: float) -> list[str]:
    """The ffmpeg command that makes the video."""
    durations = plan(len(pictures), seconds)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostdin"]
    for picture in pictures:
        # One frame in: zoompan makes the clip's frames itself (`d` of them).
        cmd += ["-i", str(picture)]
    parts = []
    for i, length in enumerate(durations):
        frames = max(2, round(length * FPS))
        # Cut to a square, then zoom in a little over the clip (a slow push).
        parts.append(
            f"[{i}:v]scale={SIZE * 2}:{SIZE * 2}:force_original_aspect_ratio=increase,"
            f"crop={SIZE * 2}:{SIZE * 2},setsar=1,"
            f"zoompan=z='min(1+0.10*on/{frames},1.10)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={SIZE}x{SIZE}:fps={FPS}[c{i}]"
        )
    last, offset = "[c0]", 0.0
    for i in range(1, len(durations)):
        offset += durations[i - 1] - FADE
        parts.append(f"{last}[c{i}]xfade=transition=fade:duration={FADE}:offset={offset:.3f}[x{i}]")
        last = f"[x{i}]"
    cmd += ["-filter_complex", ";".join(parts), "-map", last, "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-an", "-movflags", "+faststart", str(out)]
    return cmd


def make(pictures: Sequence[Path], out: Path, *, seconds: float = DEFAULT_SECONDS,
         ffmpeg: str | None = None, timeout: float = 240.0) -> Path:
    """Make the video at `out` from the first MAX_PICTURES pictures; returns `out`.

    Raises ValidationError with a plain message when it cannot (no ffmpeg, no pictures,
    ffmpeg failed, the file is over Etsy's limit). Nothing is left half-written.
    """
    exe = ffmpeg or find_ffmpeg()
    if exe is None:
        raise ValidationError("ffmpeg is not installed, so no video was made (it is free: ffmpeg.org).")
    chosen = [Path(p) for p in pictures if Path(p).is_file()][:MAX_PICTURES]
    if not chosen:
        raise ValidationError("There is no picture to make a video from.")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".part-{out.name}")
    try:
        done = subprocess.run(command(exe, chosen, temporary, seconds), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _drop(temporary)
        raise ValidationError(f"The video could not be made: {exc}") from exc
    if done.returncode != 0 or not temporary.is_file():
        _drop(temporary)
        detail = (done.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise ValidationError(f"ffmpeg could not make the video: {detail[0][:200]}")
    if temporary.stat().st_size > MAX_BYTES:
        _drop(temporary)
        raise ValidationError("The video is over Etsy's 100 MB limit.")
    temporary.replace(out)
    return out


def _drop(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
