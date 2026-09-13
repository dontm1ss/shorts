"""Конфигурация: config/base.toml + профиль железа + секреты из .env.

Профиль выбирается автоматически по ОС / архитектуре / GPU (секция [match]
в config/profiles/*.toml). Принудительный выбор — переменная окружения или
ключ в .env: SHORTS_PROFILE=<имя профиля>.

CLI:
    py -3.11 src/core/config.py                  # какой профиль выбран на этой машине
    py -3.11 src/core/config.py --profile NAME   # проверить конкретный профиль
    py -3.11 src/core/config.py --json           # итоговый конфиг целиком
"""
from __future__ import annotations

import argparse
import copy
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROFILES_DIR = CONFIG_DIR / "profiles"
ENV_FILE = ROOT / ".env"

GPU_VENDORS = {
    "nvidia": ("nvidia", "geforce", "quadro"),
    "amd": ("amd", "radeon"),
    "intel": ("intel",),
    "apple": ("apple",),
}


class ProfileError(RuntimeError):
    pass


def read_env_file(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_FILE
    if not path.exists():
        return {}
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("\"'")
    return env


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_apple_silicon() -> bool:
    # platform.machine() под Rosetta возвращает x86_64 — спрашиваем ядро напрямую
    return _run(["sysctl", "-n", "hw.optional.arm64"]).strip() == "1"


def _gpu_names(os_name: str) -> list[str]:
    names = []
    if os_name == "Darwin":
        if _is_apple_silicon():
            names.append("Apple Silicon GPU")
    elif os_name == "Windows":
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-CimInstance Win32_VideoController).Name"])
        names += [n.strip() for n in out.splitlines() if n.strip()]
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        names += [n.strip() for n in out.splitlines() if n.strip()]
    return list(dict.fromkeys(names))


def _os_label(os_name: str) -> str:
    if os_name == "Windows":
        build = sys.getwindowsversion().build
        return f"Windows {'11' if build >= 22000 else '10'} (build {build})"
    if os_name == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{os_name} {platform.release()}"


@functools.lru_cache(maxsize=1)
def detect_hardware() -> dict:
    os_name = platform.system()
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    if os_name == "Darwin" and _is_apple_silicon():
        arch = "arm64"
    gpus = _gpu_names(os_name)
    vendors = sorted({
        vendor for name in gpus for vendor, keys in GPU_VENDORS.items()
        if any(k in name.lower() for k in keys)
    })
    return {
        "os": os_name,
        "os_label": _os_label(os_name),
        "arch": arch,
        "cpu": platform.processor() or arch,
        "cpu_count": os.cpu_count(),
        "gpus": gpus,
        "gpu_vendors": vendors,
    }


def load_profiles() -> dict[str, dict]:
    profiles = {}
    for path in sorted(PROFILES_DIR.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        name = data.get("profile", {}).get("name")
        if name != path.stem:
            raise ProfileError(f"{path.name}: profile.name={name!r} не совпадает с именем файла")
        profiles[name] = data
    return profiles


def profile_matches(profile: dict, hw: dict) -> bool:
    rule = profile.get("match", {})
    if "os" in rule and rule["os"] != hw["os"]:
        return False
    if "arch" in rule and rule["arch"] != hw["arch"]:
        return False
    if "gpu_vendor" in rule and rule["gpu_vendor"] not in hw["gpu_vendors"]:
        return False
    return True


def select_profile(hw: dict, profiles: dict[str, dict], override: str | None = None) -> str:
    if override:
        if override not in profiles:
            raise ProfileError(
                f"Неизвестный профиль {override!r}. Доступны: {', '.join(profiles)}"
            )
        return override
    candidates = sorted(
        (p for p in profiles.values() if profile_matches(p, hw)),
        key=lambda p: -p["profile"].get("priority", 0),
    )
    if not candidates:
        raise ProfileError(
            f"Ни один профиль не подходит под железо: {hw['os_label']}, {hw['arch']}, "
            f"GPU: {', '.join(hw['gpus']) or 'не найдены'}. "
            f"Задайте профиль явно: SHORTS_PROFILE=<{'|'.join(profiles)}>"
        )
    return candidates[0]["profile"]["name"]


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(profile: str | None = None, hw: dict | None = None) -> dict:
    env_file = read_env_file()
    override = profile or os.environ.get("SHORTS_PROFILE") or env_file.get("SHORTS_PROFILE")
    hw = hw or detect_hardware()
    profiles = load_profiles()
    name = select_profile(hw, profiles, override)

    base = tomllib.loads((CONFIG_DIR / "base.toml").read_text(encoding="utf-8"))
    cfg = deep_merge(base, profiles[name])
    cfg.pop("match")
    cfg["profile"]["selected_by"] = "override" if override else "auto"
    cfg["profile"]["matches_hardware"] = profile_matches(profiles[name], hw)
    cfg["hardware"] = hw
    cfg["paths"]["root"] = str(ROOT)
    cfg["deepseek"]["api_key"] = (
        os.environ.get("DEEPSEEK_API_KEY") or env_file.get("DEEPSEEK_API_KEY") or None
    )
    # Переменные, которые должны быть выставлены до импорта torch
    for key, value in cfg["tts"].get("env", {}).items():
        os.environ.setdefault(key, value)
    return cfg


def probe_encoder(encoder: str, args: list[str] = ()) -> bool:
    """Реальная проверка энкодера: ffmpeg -encoders показывает и те, что на этом
    железе не заработают (например h264_nvenc на машине с AMD)."""
    if not shutil.which("ffmpeg"):
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=black:s=320x568:d=0.2",
           "-c:v", encoder, *args, "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _summary(cfg: dict) -> str:
    p, hw, r, s, v = cfg["profile"], cfg["hardware"], cfg["render"], cfg["subtitles"], cfg["video"]
    ok = lambda flag: "работает" if flag else "НЕ работает"
    lines = [
        f"Профиль:   {p['name']} ({'задан вручную' if p['selected_by'] == 'override' else 'автовыбор'})",
        f"           {p['description']}",
        f"Назначение: {'ЦЕЛЕВОЕ ЖЕЛЕЗО' if p['deploy_target'] else 'разработка/тесты'}",
        f"Железо:    {hw['os_label']}, {hw['arch']}, {hw['cpu_count']} потоков CPU",
        f"GPU:       {', '.join(hw['gpus']) or 'не найдены'}",
        f"Видео:     {v['width']}x{v['height']} @ {v['fps']} fps",
        f"TTS:       device={cfg['tts']['device']}, чанк {cfg['tts']['max_chars_per_chunk']} симв.",
        f"Субтитры:  {s['engine']} {s['model']}, {s['device']}/{s['compute_type']}",
        f"Рендер:    {r['encoder']} — {ok(probe_encoder(r['encoder'], r['encoder_args']))}; "
        f"запасной {r['fallback_encoder']} — {ok(probe_encoder(r['fallback_encoder']))}",
        f"DeepSeek:  ключ {'задан' if cfg['deepseek']['api_key'] else 'НЕ задан'}",
    ]
    if not p["matches_hardware"]:
        lines.append("ВНИМАНИЕ:  профиль не соответствует железу этой машины")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Показать выбранный профиль конфигурации")
    parser.add_argument("--profile", help="имя профиля вместо автовыбора")
    parser.add_argument("--json", action="store_true", help="вывести итоговый конфиг")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.profile)
    except ProfileError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    if args.json:
        shown = copy.deepcopy(cfg)
        if shown["deepseek"]["api_key"]:
            shown["deepseek"]["api_key"] = "***"
        print(json.dumps(shown, ensure_ascii=False, indent=2))
    else:
        print(_summary(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
