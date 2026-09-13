"""Проверка JSON-сцен по schema/scene.schema.json.

Использование:
    venv\\Scripts\\python schema/validate_scene.py tests/fixtures/scene.reddit_story.json ...
"""
import json
import sys
from pathlib import Path

from core.scene import scene_errors


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        print(__doc__)
        return 2
    failed = 0
    for arg in argv:
        path = Path(arg)
        errors = scene_errors(json.loads(path.read_text(encoding="utf-8")))
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
