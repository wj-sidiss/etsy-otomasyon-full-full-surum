"""The listing video made from a draft's pictures (drop.video) and its place in a run."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from PIL import Image

from etsyotomasyon.drop import video
from etsyotomasyon.errors import ValidationError


def test_the_clips_add_up_to_the_video_length_and_stay_inside_etsys_limits():
    for count in (1, 2, 4, 6, 9):
        parts = video.plan(count, 12)
        total = sum(parts) - (len(parts) - 1) * video.FADE
        assert abs(total - 12) < 0.05 and len(parts) <= video.MAX_PICTURES
    assert abs(sum(video.plan(3, 99)) - 2 * video.FADE - 15) < 0.05  # never over 15 s
    assert abs(sum(video.plan(3, 1)) - 2 * video.FADE - 5) < 0.05  # never under 5 s


def test_the_command_has_one_input_per_picture_and_fades_between_clips(tmp_path):
    pictures = [tmp_path / f"p{i}.jpg" for i in range(3)]
    cmd = video.command("ffmpeg", pictures, tmp_path / "out.mp4", 12)
    assert cmd.count("-i") == 3 and cmd[-1].endswith("out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("zoompan") == 3 and graph.count("xfade") == 2
    assert "-an" in cmd and "+faststart" in cmd


def test_without_ffmpeg_or_pictures_it_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "find_ffmpeg", lambda: None)
    with pytest.raises(ValidationError, match="ffmpeg"):
        video.make([tmp_path / "a.jpg"], tmp_path / "o.mp4")
    with pytest.raises(ValidationError, match="picture"):
        video.make([tmp_path / "missing.jpg"], tmp_path / "o.mp4", ffmpeg="ffmpeg")


def test_a_failed_encode_leaves_nothing_behind(tmp_path):
    picture = tmp_path / "a.jpg"
    Image.new("RGB", (40, 40), "white").save(picture)
    with pytest.raises(ValidationError, match="could not"):
        video.make([picture], tmp_path / "o.mp4", ffmpeg=str(tmp_path / "no-such-ffmpeg"))
    assert list(tmp_path.glob("*.mp4")) == [] and list(tmp_path.glob(".part-*")) == []


@pytest.mark.skipif(video.find_ffmpeg() is None, reason="ffmpeg is not installed")
def test_a_real_video_is_square_h264_and_the_asked_length(tmp_path):
    pictures = []
    for i, colour in enumerate(("red", "green", "blue")):
        path = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (800, 600), colour).save(path)
        pictures.append(path)
    out = video.make(pictures, tmp_path / "v" / "out.mp4", seconds=6)
    probe = shutil.which("ffprobe")
    assert out.is_file() and out.stat().st_size > 0
    if probe:
        info = subprocess.run([probe, "-v", "error", "-show_entries",
                               "stream=width,height,codec_name:format=duration", "-of", "csv=p=0", str(out)],
                              capture_output=True, text=True).stdout
        assert "h264,1080,1080" in info and abs(float(info.split()[-1].split(",")[-1]) - 6) < 0.3
