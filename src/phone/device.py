"""Подключение к телефону: поиск adb, список устройств, uiautomator2, отправка файлов.

uiautomator2 импортируется лениво (только при реальном управлении устройством) — тесты
и команды вроде `python -m phone devices` работают и без установленного пакета.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.config import ROOT
from phone import PhoneError


def adb_exe(cfg: dict) -> str:
    """Путь к adb.exe: из конфига (phone.adb_dir, относительно корня репозитория),
    иначе — из PATH. Нет ни того, ни другого — подсказываем скрипт установки."""
    configured = str(cfg["phone"].get("adb_dir") or "").strip()
    if configured:
        candidate = ROOT / configured / "adb.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("adb")
    if found:
        return found
    raise PhoneError(
        "adb не найден. Запустите scripts\\setup_phone.ps1 на прод-ПК "
        "(скачает platform-tools) или добавьте adb в PATH."
    )


def run_adb(cfg: dict, *args: str, serial: str | None = None, timeout: int = 60) -> str:
    """Выполнить adb <args> и вернуть stdout. Ошибка adb — PhoneError с stderr."""
    cmd = [adb_exe(cfg)]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise PhoneError(f"adb не запустился: {e}") from e
    if proc.returncode != 0:
        raise PhoneError(f"adb {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def devices(cfg: dict) -> list[dict]:
    """Подключённые устройства: список {serial, state, ...} из `adb devices -l`."""
    out = run_adb(cfg, "devices", "-l")
    result = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entry = {"serial": parts[0], "state": parts[1]}
        for part in parts[2:]:
            key, _, value = part.partition(":")
            if key:
                entry[key] = value
        result.append(entry)
    return result


def pick_serial(cfg: dict, serial: str | None = None) -> str:
    """Какой серийник использовать. Явный serial или phone.serial из конфига — как есть;
    иначе — единственное устройство в состоянии device. Несколько/ни одного — ошибка."""
    serial = serial or str(cfg["phone"].get("serial") or "").strip() or None
    if serial:
        return serial
    ready = [d for d in devices(cfg) if d["state"] == "device"]
    if not ready:
        raise PhoneError("Телефон не подключён. Включите USB-отладку и подключите кабель "
                         "(docs/phone-automation.md).")
    if len(ready) > 1:
        names = ", ".join(d["serial"] for d in ready)
        raise PhoneError(f"Подключено несколько устройств ({names}) — задайте phone.serial.")
    return ready[0]["serial"]


def shell(cfg: dict, command: str, serial: str | None = None, timeout: int = 60) -> str:
    return run_adb(cfg, "shell", command, serial=serial, timeout=timeout)


def push(cfg: dict, local: Path, remote: str, serial: str | None = None) -> str:
    run_adb(cfg, "push", str(local), remote, serial=serial)
    return remote


def media_scan(cfg: dict, remote_path: str, serial: str | None = None) -> None:
    """Сказать галерее пересканировать файл, чтобы он появился при выборе ролика в приложении."""
    shell(cfg, f'am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote_path}',
          serial=serial)


def screenshot(cfg: dict, out: Path, serial: str | None = None) -> Path:
    """Снимок экрана: adb screencap в файл на телефоне → adb pull на ПК."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_remote = "/sdcard/shorts_farm_screen.png"
    shell(cfg, f"screencap -p {tmp_remote}", serial=serial)
    run_adb(cfg, "pull", tmp_remote, str(out), serial=serial)
    shell(cfg, f"rm {tmp_remote}", serial=serial)
    return out


def u2(cfg: dict, serial: str | None = None):
    """Подключиться к устройству через uiautomator2 (ленивый импорт). Возвращает объект d."""
    try:
        import uiautomator2 as u2lib  # noqa: PLC0415
    except ImportError as e:
        raise PhoneError("uiautomator2 не установлен. Запустите scripts\\setup_phone.ps1.") from e
    device = u2lib.connect(pick_serial(cfg, serial))
    if not device.info:
        raise PhoneError("uiautomator2 не достучался до устройства: включите USB-отладку "
                         "и разрешите отладку с этого ПК на экране телефона.")
    return device


def app_start(cfg: dict, package: str, serial: str | None = None) -> None:
    """Запустить приложение по пакету (monkey). Если пакета нет — читаемая ошибка."""
    shell(cfg, f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
          serial=serial, timeout=90)
