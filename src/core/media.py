"""ffprobe / ffmpeg: общие утилиты для модулей, работающих с медиа."""
from __future__ import annotations

import functools
import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


@functools.lru_cache(maxsize=128)
def _probe(path: str, mtime: float) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode:
        raise MediaError(f"ffprobe {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def media_info(path: Path | str) -> dict:
    """{duration_ms, width, height, fps, has_audio} — результат кэшируется по mtime файла."""
    path = Path(path)
    if not path.exists():
        raise MediaError(f"Файл не найден: {path}")
    data = _probe(str(path), path.stat().st_mtime)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    info = {
        "duration_ms": round(float(data["format"].get("duration", 0)) * 1000),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
        "width": None, "height": None, "fps": None,
    }
    if video:
        num, _, den = video.get("r_frame_rate", "0/1").partition("/")
        info.update(width=video["width"], height=video["height"],
                    fps=round(int(num) / int(den or 1), 3) if int(den or 1) else None)
    return info


def run_ffmpeg(args: list[str], cwd: Path | None = None, log_path: Path | None = None) -> float:
    """Запускает ffmpeg, возвращает время выполнения в секундах. При ошибке — MediaError
    с хвостом stderr. Полный stderr пишется в log_path, если задан."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    elapsed = time.perf_counter() - t0
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(" ".join(cmd) + "\n\n" + proc.stderr, encoding="utf-8")
    if proc.returncode:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"ffmpeg завершился с кодом {proc.returncode}:\n{tail}")
    return elapsed


def extract_audio(src: Path, dst: Path, start_ms: int = 0, duration_ms: int | None = None,
                  sample_rate: int = 16000) -> Path:
    """Моно wav из видео/аудио — для распознавания речи."""
    args = ["-ss", f"{start_ms / 1000:.3f}", "-i", str(src)]
    if duration_ms is not None:
        args += ["-t", f"{duration_ms / 1000:.3f}"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([*args, "-vn", "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)])
    return dst
