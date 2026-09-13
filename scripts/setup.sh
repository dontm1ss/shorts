#!/usr/bin/env bash
# Установка окружения под профиль текущей машины (macOS).
#   bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY311="${PYTHON:-python3.11}"
command -v "$PY311" >/dev/null || { echo "Нужен Python 3.11: brew install python@3.11"; exit 1; }
[ -x venv/bin/python ] || "$PY311" -m venv venv
PY=venv/bin/python

"$PY" -m pip install --upgrade pip "setuptools<81" wheel

PROFILE="$("$PY" src/core/config.py --get profile.name)"
INDEX="$("$PY" src/core/config.py --get install.torch_index_url)"
echo "Профиль железа: $PROFILE"

if [ -n "$INDEX" ]; then
    "$PY" -m pip install torch==2.6.0 torchaudio==2.6.0 --index-url "$INDEX"
else
    "$PY" -m pip install torch==2.6.0 torchaudio==2.6.0
fi
"$PY" -m pip install -r requirements.txt
"$PY" -m pip install -e .

command -v ffmpeg >/dev/null || echo "ffmpeg не найден: brew install ffmpeg"
"$PY" scripts/check_env.py
