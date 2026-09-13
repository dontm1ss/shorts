"""CLI TTS-модуля. Запускается отдельным процессом (tts-stack-spec, раздел 1).

    venv\\Scripts\\python -m tts scene tests/fixtures/scene.reddit_story.json [--preset calm]
    venv\\Scripts\\python -m tts say "Текст для озвучки" [--preset energetic] [--voice voices/a.wav]
    venv\\Scripts\\python -m tts presets "Текст для сравнения пресетов на слух"
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import warnings
from pathlib import Path

import soundfile as sf

from core.config import ROOT, load_config
from core.scene import load_scene, rel_path, save_scene, scene_path
from tts.engine import TTSEngine
from tts.scene import default_audio_path, resolve_voice, synthesize_scene

log = logging.getLogger("tts")


def _cmd_scene(args, cfg, engine: TTSEngine) -> int:
    scene = load_scene(args.scene)
    voices = cfg["tts"]["voices"]
    speakers = {line["speaker"] for line in scene.get("script", [])}
    genders = scene.get("meta", {}).get("genders", {})
    chosen = {voices.get(s) or voices.get(genders.get(s) or "", "") for s in speakers}
    if len(speakers) > 1 and len(chosen) == 1:
        log.warning("Все роли (%s) озвучены одним голосом — задайте tts.voices в config/base.toml",
                    ", ".join(sorted(speakers)))

    out_wav = Path(args.out) if args.out else default_audio_path(cfg, scene)
    t0 = time.perf_counter()
    result = synthesize_scene(scene, engine, preset=args.preset, out_path=out_wav, seed=args.seed)
    elapsed = time.perf_counter() - t0

    out_scene = Path(args.scene_out) if args.scene_out else scene_path(cfg, scene["scene_id"])
    save_scene(result, out_scene)
    audio_s = result["audio"]["duration_ms"] / 1000
    for line in result["script"]:
        log.info("  %6.2f с  %-8s %s", line["timestamp_ms"] / 1000, line["speaker"], line["stressed_text"])
    log.info("Аудио %.1f с за %.1f с (RTF %.2f): %s", audio_s, elapsed, elapsed / audio_s, rel_path(out_wav))
    log.info("Сцена: %s", rel_path(out_scene))
    if scene["type"] == "reddit_story":
        lo, hi = cfg["story"]["min_duration_s"], cfg["story"]["max_duration_s"]
        if not lo <= audio_s <= hi:
            log.warning("Длительность истории %.1f с вне диапазона %d–%d с", audio_s, lo, hi)
    return 0


def _say(engine: TTSEngine, text: str, preset: str, voice: Path | None, out: Path, seed: int | None) -> None:
    params = engine.tts_cfg["presets"][preset]
    t0 = time.perf_counter()
    audio, stressed = engine.synthesize(text, params, voice, seed=seed)
    elapsed = time.perf_counter() - t0
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, audio, engine.sr, subtype="PCM_16")
    audio_s = len(audio) / engine.sr
    log.info("[%s] %.1f с за %.1f с (RTF %.2f) → %s", preset, audio_s, elapsed, elapsed / audio_s, rel_path(out))
    log.info("  %s", stressed)


def _voice_arg(value: str | None) -> Path | None:
    return resolve_voice("cli", {"cli": value}) if value else None


def _cmd_say(args, cfg, engine: TTSEngine) -> int:
    if args.preset not in cfg["tts"]["presets"]:
        log.error("Неизвестный пресет %r. Доступны: %s", args.preset, ", ".join(cfg["tts"]["presets"]))
        return 2
    out =Path(args.out) if args.out else ROOT / cfg["paths"]["output"] / "tts" / f"say_{args.preset}.wav"
    _say(engine, args.text, args.preset, _voice_arg(args.voice), out, args.seed)
    return 0


def _cmd_presets(args, cfg, engine: TTSEngine) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / cfg["paths"]["output"] / "tts" / "presets"
    for preset in cfg["tts"]["presets"]:
        _say(engine, args.text, preset, _voice_arg(args.voice), out_dir / f"{preset}.wav", args.seed)
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(prog="python -m tts", description="TTS-модуль (Chatterbox)")
    parser.add_argument("--profile", help="профиль железа вместо автовыбора")
    parser.add_argument("--seed", type=int, help="seed вместо tts.seed из конфига")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scene", help="озвучить сцену JSON")
    p.add_argument("scene", type=Path)
    p.add_argument("--preset", help="по умолчанию — tts.scene_presets для типа сцены")
    p.add_argument("--out", help="путь wav (по умолчанию output/work/<scene_id>/voice.wav)")
    p.add_argument("--scene-out", help="путь итоговой сцены (по умолчанию output/scenes/<scene_id>.json)")
    p.set_defaults(fn=_cmd_scene)

    p = sub.add_parser("say", help="озвучить одну фразу")
    p.add_argument("text")
    p.add_argument("--preset", default="neutral")
    p.add_argument("--voice", help="референс-голос wav")
    p.add_argument("--out")
    p.set_defaults(fn=_cmd_say)

    p = sub.add_parser("presets", help="одна фраза всеми пресетами — сравнить на слух")
    p.add_argument("text")
    p.add_argument("--voice", help="референс-голос wav")
    p.add_argument("--out-dir")
    p.set_defaults(fn=_cmd_presets)

    args = parser.parse_args(argv)
    cfg = load_config(args.profile)   # выставляет env профиля до импорта torch
    log.info("Профиль %s, устройство %s", cfg["profile"]["name"], cfg["tts"]["device"])
    engine = TTSEngine(cfg)
    try:
        return args.fn(args, cfg, engine)
    finally:
        engine.unload()


if __name__ == "__main__":
    sys.exit(main())
