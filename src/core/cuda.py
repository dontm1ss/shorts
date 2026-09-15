"""CUDA-библиотеки для faster-whisper (ctranslate2) на Windows.

ctranslate2 подгружает cuBLAS (cublas64_12.dll) по имени в момент первого распознавания,
а в venv эта библиотека есть только внутри torch (cu124). Если torch в процессе ещё не
импортирован (нарезка клипов, субтитры без озвучки), whisper на cuda падает с
«cublas64_12.dll is not found». Здесь каталог библиотек torch добавляется в поиск DLL.
"""
from __future__ import annotations

import functools
import importlib.util
import os
import sys
from pathlib import Path


@functools.lru_cache(maxsize=1)
def ensure_cuda_dlls() -> Path | None:
    """Сделать CUDA-библиотеки torch видимыми для ctranslate2. Возвращает каталог или None."""
    if sys.platform != "win32":
        return None
    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.submodule_search_locations:
        return None
    lib = Path(next(iter(spec.submodule_search_locations))) / "lib"
    if not (lib / "cublas64_12.dll").exists():
        return None
    os.add_dll_directory(str(lib))
    # ctranslate2 грузит cuBLAS через LoadLibrary по имени — ищется по PATH
    os.environ["PATH"] = f"{lib}{os.pathsep}{os.environ.get('PATH', '')}"
    return lib
