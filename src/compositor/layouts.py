"""Сборка команды ffmpeg для раскладок кадра 9:16.

    full_bg     паркур на весь кадр + озвучка + субтитры            (reddit_story)
    top_bottom  клип-источник сверху, паркур снизу + субтитры на стыке (video_clip)
    ui_over_bg  запись интерфейса iMessage сверху, паркур снизу      (imessage_drama)

Субтитры подключаются по имени файла относительно рабочей папки ffmpeg (cwd = папка .ass):
так не нужно экранировать путь Windows с двоеточием и кириллицей внутри filtergraph.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.media import media_info
from core.scene import abs_path


@dataclass
class RenderPlan:
    args: list[str]              # аргументы ffmpeg без входного "ffmpeg -y"
    cwd: Path | None
    duration_s: float
    output: Path
    inputs: list[Path] = field(default_factory=list)


def _fit(label_in: str, label_out: str, w: int, h: int, fps: int, mode: str = "cover") -> str:
    if mode == "contain":   # вписать целиком поверх размытой копии
        return (f"[{label_in}]split=2[{label_out}_a][{label_out}_b];"
                f"[{label_out}_a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[{label_out}_bg];"
                f"[{label_out}_b]scale={w}:{h}:force_original_aspect_ratio=decrease[{label_out}_fg];"
                f"[{label_out}_bg][{label_out}_fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}[{label_out}]")
    return (f"[{label_in}]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps}[{label_out}]")


def _bg_input(scene: dict, duration_s: float) -> list[str]:
    bg = scene["background"]
    path = abs_path(bg["source_path"])
    start_s = bg["start_ms"] / 1000
    loop = ["-stream_loop", "-1"] if media_info(path)["duration_ms"] / 1000 < start_s + duration_s else []
    return [*loop, "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}", "-i", str(path)]


def scene_duration_s(scene: dict, cfg: dict) -> float:
    if scene["type"] == "video_clip":
        sv = scene["source_video"]
        return (sv["clip_end_ms"] - sv["clip_start_ms"]) / 1000
    return (scene["audio"]["duration_ms"] + cfg["compositor"]["tail_ms"]) / 1000


def build_plan(scene: dict, cfg: dict, encoder: str, encoder_args: list[str],
               ui_video: Path | None = None) -> RenderPlan:
    v, comp = cfg["video"], cfg["compositor"]
    W, H, fps = v["width"], v["height"], v["fps"]
    layout = comp["layout"][scene["type"]]
    duration = scene_duration_s(scene, cfg)
    output = abs_path(scene["output"]["path"])
    if "background" not in scene:
        raise ValueError("В сцене нет background — сначала выберите фон (compositor.pick_background)")

    use_subs = scene["type"] in comp["subtitles_for"] and "subtitles" in scene
    subs = abs_path(scene["subtitles"]["path"]) if use_subs else None
    inputs: list[str] = []
    graph: list[str] = []

    if layout == "full_bg":
        inputs += _bg_input(scene, duration)                                   # 0: фон
        inputs += ["-i", str(abs_path(scene["audio"]["path"]))]               # 1: озвучка
        graph.append(_fit("0:v", "base", W, H, fps))
        audio_src = "1:a"
    elif layout == "top_bottom":
        sv = scene["source_video"]
        half = H // 2
        inputs += ["-ss", f"{sv['clip_start_ms'] / 1000:.3f}", "-t", f"{duration:.3f}",
                   "-i", str(abs_path(sv["path"]))]                            # 0: клип
        inputs += _bg_input(scene, duration)                                   # 1: фон
        graph.append(_fit("0:v", "top", W, half, fps, comp["clip_fit"]))
        graph.append(_fit("1:v", "bottom", W, H - half, fps))
        graph.append("[top][bottom]vstack=inputs=2[base]")
        if not media_info(abs_path(sv["path"]))["has_audio"]:                # клип без звука — тишина
            inputs += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            orig = "2:a"
        else:
            orig = "0:a"
        if "audio" in scene:                                                   # озвучка поверх клипа
            voice_idx = 3 if orig == "2:a" else 2
            inputs += ["-i", str(abs_path(scene["audio"]["path"]))]
            graph.append(f"[{orig}]volume=0.35[orig];[orig][{voice_idx}:a]"
                         "amix=inputs=2:duration=longest:normalize=0[mix]")
            audio_src = "mix"
        else:
            audio_src = orig
    elif layout == "ui_over_bg":
        if ui_video is None:
            raise ValueError("Для iMessage-сцены нужна запись интерфейса (ui_video)")
        ui_h = comp["ui_height"]
        inputs += ["-i", str(ui_video)]                                        # 0: интерфейс
        inputs += _bg_input(scene, duration)                                   # 1: фон
        inputs += ["-i", str(abs_path(scene["audio"]["path"]))]               # 2: озвучка
        graph.append(_fit("1:v", "bgfull", W, H, fps))
        graph.append(f"[0:v]scale={W}:{ui_h},setsar=1,fps={fps}[ui]")
        graph.append("[bgfull][ui]overlay=0:0:eof_action=repeat[base]")
        audio_src = "2:a"
    else:
        raise ValueError(f"Неизвестная раскладка {layout!r}")

    if subs:
        graph.append(f"[base]subtitles={subs.name}[vout]")
    else:
        graph.append("[base]null[vout]")
    # aformat явно: ffmpeg 6.0 не согласует раскладку каналов между aresample и apad для моно-озвучки
    graph.append(f"[{audio_src}]loudnorm={comp['loudnorm']},aresample=48000,"
                 "aformat=sample_rates=48000:channel_layouts=stereo,apad[aout]")

    args = [*inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
            "-c:v", encoder, *encoder_args, "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", cfg["video"]["audio_bitrate"], "-ar", "48000",
            "-t", f"{duration:.3f}", "-movflags", "+faststart", str(output)]
    return RenderPlan(args=args, cwd=subs.parent if subs else None, duration_s=duration, output=output)
