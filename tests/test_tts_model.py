"""Тесты на настоящей модели Chatterbox (~3 ГБ весов, на CPU — минуты).
Запуск: SHORTS_MODEL_TESTS=1 venv\\Scripts\\python -m pytest tests/test_tts_model.py"""
import os

import pytest

from core.config import load_config
from tts import TTSEngine, plausible_duration

pytestmark = pytest.mark.skipif(
    os.environ.get("SHORTS_MODEL_TESTS") != "1",
    reason="нужна модель Chatterbox: SHORTS_MODEL_TESTS=1",
)


@pytest.fixture(scope="module")
def engine():
    eng = TTSEngine(load_config())
    yield eng
    eng.unload()


def test_real_stress_and_synthesis(engine):
    text = "Старинный замок стоял на холме, и каждую ночь в его окнах горел свет."
    audio, stressed = engine.synthesize(text, engine.tts_cfg["presets"]["neutral"])
    assert "\u0301" in stressed
    assert plausible_duration(stressed, len(audio) / engine.sr)


def test_presets_change_tempo(engine):
    """exaggeration ↑ — речь быстрее (tts-stack-spec, раздел 9)."""
    text = "Никто в деревне не знал, кто живёт в замке, но все боялись спросить об этом вслух."
    presets = engine.tts_cfg["presets"]
    calm, _ = engine.synthesize(text, presets["calm"])
    energetic, _ = engine.synthesize(text, presets["energetic"])
    assert len(energetic) < len(calm)
