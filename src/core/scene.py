"""Работа со сценой — внутренним контрактом между модулями (schema/scene.schema.json)."""
from __future__ import annotations

import functools
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from core.config import ROOT

SCHEMA_PATH = ROOT / "schema" / "scene.schema.json"


class SceneError(ValueError):
    pass


@functools.lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def scene_errors(scene: dict) -> list[str]:
    return [
        f"{'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"
        for err in sorted(_validator().iter_errors(scene), key=lambda e: list(e.absolute_path))
    ]


def validate_scene(scene: dict) -> dict:
    if errors := scene_errors(scene):
        raise SceneError("Сцена не соответствует схеме:\n  " + "\n  ".join(errors))
    return scene


def load_scene(path: Path) -> dict:
    return validate_scene(json.loads(Path(path).read_text(encoding="utf-8")))


def save_scene(scene: dict, path: Path) -> None:
    validate_scene(scene)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rel_path(path: Path) -> str:
    """Путь для записи в сцену: относительно корня репозитория, с прямыми слэшами."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def abs_path(path: str | Path) -> Path:
    """Обратное к rel_path: путь из сцены → абсолютный."""
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


# Раскладка output/:
#   scenes/<scene_id>.json     — актуальное состояние сцены (передаётся между этапами)
#   work/<scene_id>/           — промежуточные файлы: voice.wav, subs.ass, subs.srt, ui.mov
#   videos/<type>_<id8>.mp4    — готовые ролики
def output_dir(cfg: dict, *parts: str) -> Path:
    return ROOT / cfg["paths"]["output"] / Path(*parts)


def scene_path(cfg: dict, scene_id: str) -> Path:
    return output_dir(cfg, "scenes", f"{scene_id}.json")


def work_dir(cfg: dict, scene: dict) -> Path:
    return output_dir(cfg, "work", scene["scene_id"])


def video_path(cfg: dict, scene_type: str, scene_id: str) -> Path:
    return output_dir(cfg, "videos", f"{scene_type}_{scene_id[:8]}.mp4")
