"""Композитор + рендерер на синтетических медиа (ffmpeg lavfi): все раскладки, фон, субтитры,
запасной энкодер. Без модели TTS и без сети; ~10–20 с."""
import random
import subprocess
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


def lavfi_alpha_split(dst, duration=4.0):
    """Пол-кадра непрозрачно-белое, пол-кадра прозрачное — как настоящая запись iMessage
    (recorder.py: qtrle/argb, прозрачный фон страницы). Проверяет, что overlay в
    layouts.py реально учитывает альфу, а не рисует поверх фона сплошной прямоугольник.
    color=white@0.0 альфу не даёт (в source-фильтре color она игнорируется, кадр всегда
    выходит непрозрачным yuv420p) — альфу через geq по координате X."""
    run_ffmpeg(["-f", "lavfi",
               "-i", f"color=c=white:s=200x150:d={duration},format=yuva420p,"
                     "geq=r=255:g=255:b=255:a='if(lt(X,100),255,0)'",
               "-c:v", "qtrle", str(dst)])
    return dst


def sample_pixel(path, x, y, t=0.1):
    """RGB верхнего левого угла блока 2×2 кадра — проверить, что дошло до итогового рендера,
    а не просто «файл не упал». crop=1:1 (ровно один пиксель) в этой версии ffmpeg падает
    с «Invalid too big or non positive size» — берём 2×2 и читаем первый пиксель."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(path),
         "-frames:v", "1", "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    return tuple(proc.stdout[:3])


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "parkour": lavfi(d / "parkour.mp4", "testsrc2=s=640x360:r=30", "sine=f=200", 20),
        "red_bg": lavfi(d / "red_bg.mp4", "color=c=red:s=640x360:r=30", "sine=f=200", 20),
        "source": lavfi(d / "source.mp4", "testsrc=s=1280x720:r=30", "sine=f=330", 8),
        "silent": lavfi(d / "silent.mp4", "testsrc=s=1280x720:r=30", None, 6),
        "voice": lavfi(d / "voice.wav", None, "sine=f=440:sample_rate=24000", 4),
        "ui": lavfi_alpha_split(d / "ui.mov", 4),
    }


@pytest.fixture
def cfg(dev_cfg, media, tmp_path):
    dev_cfg["backgrounds"] = [{"name": "test_bg", "path": str(media["parkour"])}]
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
    for _ in range(50):                                          # петля: запас фона перед стартом
        assert 800 <= pick_background(cfg, 15000, rng, lead_ms=800)["start_ms"] <= 20000 - 15000


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


def test_drama_ui_over_bg_is_transparent(cfg, media, tmp_path):
    """Фон iMessage-интерфейса прозрачный (style.css) — фоновое видео должно быть видно
    сквозь него, а не только в нижней части кадра (owner: убрать белый фон, оставить
    сообщения на фоне паркура/сабвея)."""
    cfg["backgrounds"] = [{"name": "red_bg", "path": str(media["red_bg"])}]
    scene = voiced(base_scene(cfg, "imessage_drama", tmp_path), media)
    scene["script"][0]["speaker"] = "A"
    scene["script"][1]["speaker"] = "B"
    scene["background"] = pick_background(cfg, 5000, random.Random(4))
    render_scene(scene, cfg, ui_video=media["ui"])
    out = scene["output"]["path"]
    # фикстура "ui" — левая половина непрозрачно-белая, правая полностью прозрачная;
    # если бы overlay игнорировал альфу, обе половины были бы белыми
    opaque = sample_pixel(out, 10, 10)
    transparent = sample_pixel(out, cfg["video"]["width"] - 10, 10)
    assert all(c > 240 for c in opaque)
    assert transparent[0] > 180 and transparent[1] < 80 and transparent[2] < 80   # красный фон просвечивает


def test_loop_drama_seamless_background(cfg, media, tmp_path):
    """Петля: ролик без хвоста (пауза как между репликами, целое число кадров), фон в конце плавно
    переходит в кадры перед своим началом — последний кадр продолжается первым."""
    from compositor import scene_duration_s
    ramp = tmp_path / "ramp.mp4"                                  # красный канал растёт: 12 единиц в секунду
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=20,geq=r='12*T':g=0:b=0",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p", str(ramp)])
    scene = voiced(base_scene(cfg, "imessage_drama", tmp_path), media)
    scene["script"][0]["speaker"], scene["script"][1]["speaker"] = "A", "B"
    scene["meta"] = {"loop": True}
    scene["background"] = {"type": "ramp", "source_path": str(ramp), "start_ms": 6000}

    duration = scene_duration_s(scene, cfg)
    assert duration * 30 == pytest.approx(round(duration * 30))                          # целое число кадров
    assert duration == pytest.approx(4.0 + cfg["tts"]["pause_between_lines_ms"] / 1000, abs=1 / 30)
    graph = build_plan(scene, cfg, "libx264", FAST_X264, media["ui"]).args
    assert f"xfade=transition=fade:duration=0.800:offset={duration - 0.8:.3f}" in " ".join(graph)

    render_scene(scene, cfg, ui_video=media["ui"])
    out = scene["output"]["path"]
    check_video(out, duration)
    first = sample_pixel(out, 540, 1800, 0)                          # ниже интерфейса — только фон
    last = sample_pixel(out, 540, 1800, duration - 1.5 / 30)
    assert abs(first[0] - 12 * 6) <= 6                                # фон с 6-й секунды
    assert abs(last[0] - first[0]) <= 6                               # без перехода было бы 12 × 4.3 ≈ 52

    scene["background"]["start_ms"] = 300                             # фона перед стартом нет — без перехода
    assert "xfade" not in " ".join(build_plan(scene, cfg, "libx264", FAST_X264, media["ui"]).args)
    del scene["meta"]["loop"]                                         # не петля — хвост как обычно
    assert scene_duration_s(scene, cfg) == pytest.approx(4.0 + cfg["compositor"]["tail_ms"] / 1000)


def test_background_start_beyond_replaced_file(cfg, media, tmp_path):
    """Фон заменили файлом короче, а у сцены старт выбран по старому — отрезок берётся внутри файла."""
    scene = voiced(base_scene(cfg, "reddit_story", tmp_path), media)
    scene["background"] = {"type": "test_bg", "source_path": str(media["parkour"]), "start_ms": 1_638_480}
    args = build_plan(scene, cfg, "libx264", FAST_X264).args
    start = float(args[args.index("-ss") + 1])
    assert 0 <= start < 20                                          # файл фона — 20 с
    render_scene(scene, cfg)
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
