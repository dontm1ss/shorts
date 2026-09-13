"""Чек-лист готовности окружения (docs/tts-stack-spec.md, раздел 11) с учётом профиля.

На целевой машине (cuda) проверяется всё из спеки; на dev-машинах вместо CUDA
проверяется устройство профиля (cpu / mps).

    venv\\Scripts\\python scripts/check_env.py
"""
import importlib.metadata as md
import shutil
import subprocess
import sys
import warnings

from core.config import ROOT, load_config, probe_encoder

results: list[tuple[bool, str, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            ok, detail = fn()
        except Exception as e:  # чек-лист должен дойти до конца, даже если что-то упало
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((ok, name, detail))
        return fn
    return wrap


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    cfg = load_config()
    device = cfg["tts"]["device"]
    print(f"Профиль: {cfg['profile']['name']} ({cfg['hardware']['os_label']})\n")

    @check("Python 3.11.x")
    def _():
        v = sys.version_info
        return v[:2] == (3, 11), sys.version.split()[0]

    @check("Изолированный venv")
    def _():
        return sys.prefix != sys.base_prefix, sys.prefix

    @check("torch 2.6.0")
    def _():
        import torch
        return torch.__version__.split("+")[0] == "2.6.0", torch.__version__

    @check(f"Устройство TTS из профиля: {device}")
    def _():
        import torch
        if device == "cuda":
            if not torch.cuda.is_available():
                return False, "torch.cuda.is_available() = False — CPU-сборка torch или драйвер NVIDIA"
            props = torch.cuda.get_device_properties(0)
            vram = props.total_memory / 2**30
            detail = f"{props.name}, {vram:.1f} ГБ VRAM, CC {props.major}.{props.minor}"
            if vram < 5:
                detail += " — мало VRAM: синтезировать строго по предложениям"
            return True, detail
        if device == "mps":
            return torch.backends.mps.is_available(), f"mps available={torch.backends.mps.is_available()}"
        return True, f"cpu, потоков torch: {torch.get_num_threads()}"

    if device == "cuda":
        @check("Драйвер NVIDIA")
        def _():
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            return bool(out), out or "nvidia-smi не отвечает"

    @check("setuptools < 81")
    def _():
        v = md.version("setuptools")
        return int(v.split(".")[0]) < 81, v

    @check("Watermarker Perth — реальный класс, не None")
    def _():
        import perth
        cls = perth.PerthImplicitWatermarker
        return cls is not None, repr(cls)

    @check("chatterbox-tts 0.1.7")
    def _():
        v = md.version("chatterbox-tts")
        return v == "0.1.7", v

    @check("stressonnx расставляет ударения")
    def _():
        from stressonnx import stress
        out = stress("старинный замок", "ru")
        return "\u0301" in out, out

    @check("Нет файлов chatterbox.py в проекте")
    def _():
        found = [p for p in ROOT.rglob("chatterbox.py") if "venv" not in p.parts]
        return not found, ", ".join(map(str, found)) or "нет"

    @check(f"faster-whisper: {cfg['subtitles']['device']}/{cfg['subtitles']['compute_type']}")
    def _():
        import ctranslate2
        wdev = cfg["subtitles"]["device"]
        supported = ctranslate2.get_supported_compute_types(wdev)
        ok = cfg["subtitles"]["compute_type"] in supported
        return ok, f"ctranslate2 {ctranslate2.__version__}, поддерживается: {', '.join(sorted(supported))}"

    @check("ffmpeg в PATH")
    def _():
        path = shutil.which("ffmpeg")
        return bool(path), path or "не найден"

    @check(f"Видеоэнкодер профиля: {cfg['render']['encoder']}")
    def _():
        r = cfg["render"]
        if probe_encoder(r["encoder"], r["encoder_args"]):
            return True, "кодирует"
        if probe_encoder(r["fallback_encoder"]):
            return False, f"не работает, но запасной {r['fallback_encoder']} кодирует"
        return False, "не работает, запасной тоже"

    @check(f"Браузер для записи iMessage: {cfg['imessage']['browser_channel']}")
    def _():
        from playwright.sync_api import sync_playwright

        from imessage.recorder import _launch
        with sync_playwright() as p:
            browser = _launch(p, cfg["imessage"]["browser_channel"])
            version = browser.version
            browser.close()
        return True, version

    @check("DeepSeek API-ключ в .env")
    def _():
        return bool(cfg["deepseek"]["api_key"]), "задан" if cfg["deepseek"]["api_key"] else "не задан"

    width = max(len(n) for _, n, _ in results)
    for ok, name, detail in results:
        print(f"{'✓' if ok else '✗'} {name:<{width}}  {detail}")
    failed = sum(not ok for ok, _, _ in results)
    print(f"\n{'Окружение готово' if not failed else f'Не пройдено пунктов: {failed}'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
