"""Проверка JSON-сцен по schema/scene.schema.json.

Использование:
    python schema/validate_scene.py mock-data/*.json
"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name("scene.schema.json")


def load_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate_file(validator: Draft202012Validator, path: Path) -> list[str]:
    scene = json.loads(path.read_text(encoding="utf-8"))
    return [
        f"{'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(scene), key=lambda e: list(e.absolute_path))
    ]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        print(__doc__)
        return 2
    validator = load_validator()
    failed = 0
    for arg in argv:
        path = Path(arg)
        errors = validate_file(validator, path)
        if errors:
            failed += 1
            print(f"FAIL {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
