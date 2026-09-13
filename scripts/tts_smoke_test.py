"""Смок-тест окружения: Chatterbox Multilingual синтезирует фразу на устройстве профиля,
faster-whisper распознаёт её обратно. Печатает время загрузки, скорость синтеза (RTF).

    venv\\Scripts\\python scripts/tts_smoke_test.py [--no-whisper] [--preset neutral]

Первый запуск скачивает веса с HuggingFace: Chatterbox ~3 ГБ, whisper small ~0.5 ГБ.
"""
import argparse
import sys
import time
import warnings
from pathlib import Path

from core.config import ROOT, load_config

TEXT = (
    "Старинный замок стоял на холме, и каждую ночь в его окнах горел свет. "
    "Никто в деревне не знал, кто там живёт, но все боялись спросить."
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="neutral")
    parser.add_argument("--no-whisper", action="store_true")
    args = parser.parse_args()

    cfg = load_config()  # выставляет env профиля до импорта torch
    import torch
    import torchaudio
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    from stressonnx import stress

    device = cfg["tts"]["device"]
    preset = cfg["tts"]["presets"][args.preset]
    out_dir = ROOT / cfg["paths"]["output"] / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"tts_{cfg['profile']['name']}_{args.preset}.wav"
    print(f"Профиль {cfg['profile']['name']}, устройство {device}, пресет {args.preset}: {preset}")

    stressed = stress(TEXT, "ru")
    print(f"Текст с ударениями: {stressed}")

    t0 = time.perf_counter()
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)
    t_load = time.perf_counter() - t0
    print(f"Модель загружена за {t_load:.1f} с")

    t0 = time.perf_counter()
    wav = model.generate(stressed, language_id=cfg["tts"]["language_id"], **preset)
    t_gen = time.perf_counter() - t0
    audio_s = wav.shape[-1] / model.sr
    torchaudio.save(str(wav_path), wav, model.sr)
    print(f"Синтез: {audio_s:.1f} с аудио за {t_gen:.1f} с (RTF {t_gen / audio_s:.2f}) → {wav_path}")
    if device == "cuda":
        print(f"Пик VRAM: {torch.cuda.max_memory_allocated() / 2**30:.2f} ГБ")

    del model
    if not args.no_whisper:
        from faster_whisper import WhisperModel

        s = cfg["subtitles"]
        t0 = time.perf_counter()
        whisper = WhisperModel(s["model"], device=s["device"], compute_type=s["compute_type"],
                               cpu_threads=cfg["runtime"]["cpu_threads"])
        segments, _ = whisper.transcribe(str(wav_path), language=s["language"], word_timestamps=True)
        words = [w for seg in segments for w in seg.words]
        t_asr = time.perf_counter() - t0
        print(f"Whisper {s['model']} ({s['device']}/{s['compute_type']}), {t_asr:.1f} с вкл. загрузку:")
        print("  " + "".join(w.word for w in words).strip())
        print(f"  слов с таймкодами: {len(words)}, последнее слово кончается на {words[-1].end:.2f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
