"""Композитор + рендерер на синтетических медиа (ffmpeg lavfi): все раскладки, фон, субтитры,
запасной энкодер. Без модели TTS и без сети; ~10–20 с."""
import random
import uuid

import pytest

from compositor import build_plan, pick_background
from core.media import media_info, run_ffmpeg
from renderer import render_scene
from subtitles import subtitle_scene

FAST_X264 = ["-preset", "ultrafast", "-crf", "30"]


def lavfi(dst, video=None, audio=None, duration=5.0):
    args = []
    if video:
        args += ["-f", "lavfi", "-i", f"{video}:d={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"{audio}:d={duration}"]
    if video:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac" if dst.suffix == ".mp4" else "pcm_s16le"]
    run_ffmpeg([*args, str(dst)])
    return dst


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "parkour": lavfi(d / "parkour.mp4", "testsrc2=s=640x360:r=30", "sine=f=200", 20),
        "source": lavfi(d / "source.mp4", "testsrc=s=1280x720:r=30", "sine=f=330", 8),
        "silent": lavfi(d / "silent.mp4", "testsrc=s=1280x720:r=30", None, 6),
        "voice": lavfi(d / "voice.wav", None, "sine=f=440:sample_rate=24000", 4),
        "ui": lavfi(d / "ui.mp4", "color=c=white:s=1080x1150:r=30", None, 4),
    }


@pytest.fixture
def cfg(dev_cfg, media, tmp_path):
    dev_cfg["paths"]["parkour"] = str(media["parkour"])
    dev_cfg["paths"]["output"] = str(tmp_path / "output")
    dev_cfg["render"]["encoder"] = "libx264"
    dev_cfg["render"]["encoder_args"] = FAST_X264
    return dev_cfg


def base_scene(cfg, scene_type, tmp_path):
    sid = str(uuid.uuid4())
    return {"scene_id": sid, "type": scene_type, "created_at": "2026-09-13T12:00:00Z",
            "output": {"resolution": "1080x1920", "path": str(tmp_path / "videos" / f"{sid}.mp4")}}


def voiced(scene, media):
    scene["script"] = [
        {"speaker": "narrator", "text": "Мой сосед три года парковался на моём месте.",
         "timestamp_ms": 0, "duration_ms": 2500},
        {"speaker": "narrator", "text": "Терпение кончилось.", "timestamp_ms": 2800, "duration_ms": 1200},
    ]
    scene["audio"] = {"path": str(media["voice"]), "duration_ms": 4000}
    return scene


def check_video(path, duration):
    info = media_info(path)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"]
    assert info["duration_ms"] / 1000 == pytest.approx(duration, abs=0.2)


def test_background_fits_inside_file(cfg):
    rng = random.Random(1)
    for _ in range(50):
        bg = pick_background(cfg, 15000, rng)
        assert 0 <= bg["start_ms"] <= 20000 - 15000
    assert pick_background(cfg, 60000, rng)["start_ms"] == 0     # фон короче ролика — с нуля + цикл


def test_story_full_bg_with_subtitles(cfg, media, tmp_path):
    scene = voiced(base_scene(cfg, "reddit_story", tmp_path), media)
    scene["background"] = pick_background(cfg, 5000, random.Random(2))
    scene = subtitle_scene(scene, cfg, use_whisper=False)
    plan = build_plan(scene, cfg, "libx264", FAST_X264)
    assert "subtitles=subs.ass" in plan.args[plan.args.index("-filter_complex") + 1]
    stats = render_scene(scene, cfg)
    check_video(plan.output, 4.0 + cfg["compositor"]["tail_ms"] / 1000)
    assert stats["encoder"] == "libx264"


@pytest.mark.parametrize("source_key, with_voice", [("source", False), ("silent", False), ("source", True)])
def test_clip_top_bottom(cfg, media, tmp_path, source_key, with_voice):
    scene = base_scene(cfg, "video_clip", tmp_path)
    scene["source_video"] = {"path": str(media[source_key]), "clip_start_ms": 1000, "clip_end_ms": 4500}
    if with_voice:
        scene["audio"] = {"path": str(media["voice"]), "duration_ms": 4000}
    scene["background"] = pick_background(cfg, 3500, random.Random(3))
    render_scene(scene, cfg)
    check_video(scene["output"]["path"], 3.5)


def test_drama_ui_over_bg(cfg, media, tmp_path):
    scene = voiced(base_scene(cfg, "imessage_drama", tmp_path), media)
    scene["script"][0]["speaker"] = "A"
    scene["script"][1]["speaker"] = "B"
    scene["background"] = pick_background(cfg, 5000, random.Random(4))
    render_scene(scene, cfg, ui_video=media["ui"])
    check_video(scene["output"]["path"], 4.0 + cfg["compositor"]["tail_ms"] / 1000)


def test_falls_back_to_libx264(cfg, media, tmp_path):
    cfg["render"]["encoder"] = "h264_does_not_exist"
    cfg["render"]["fallback_encoder_args"] = FAST_X264
    scene = voiced(base_scene(cfg, "reddit_story", tmp_path), media)
    scene["background"] = pick_background(cfg, 5000)
    assert render_scene(scene, cfg)["encoder"] == "libx264"


def test_missing_background_is_explicit(cfg, media, tmp_path):
    scene = voiced(base_scene(cfg, "reddit_story", tmp_path), media)
    with pytest.raises(ValueError, match="background"):
        build_plan(scene, cfg, "libx264", FAST_X264)
