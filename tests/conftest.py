import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import core.config  # noqa: E402


def make_hw(os_name, arch="x86_64", gpus=(), vendors=()):
    return {
        "os": os_name, "os_label": os_name, "arch": arch, "cpu": arch, "cpu_count": 4,
        "gpus": list(gpus), "gpu_vendors": sorted(vendors),
    }


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """SHORTS_PROFILE из окружения или .env разработчика не должен влиять на тесты."""
    monkeypatch.delenv("SHORTS_PROFILE", raising=False)
    monkeypatch.setattr(core.config, "ENV_FILE", tmp_path / ".env")


@pytest.fixture
def dev_cfg(tmp_path):
    """Конфиг dev-профиля без обращения к реальному железу. scripting_history — во
    временной папке, чтобы тесты (adapt_story/generate_drama) не писали в реальный
    data/scripting-history.json репозитория. Голоса не заданы — записи владельца
    в voices/ не должны влиять на тесты (кому нужны — задают свои)."""
    cfg = core.config.load_config(
        hw=make_hw("Windows", gpus=["AMD Radeon RX 6600"], vendors=["amd"])
    )
    cfg["paths"]["scripting_history"] = str(tmp_path / "scripting-history.json")
    cfg["tts"]["voices"].update(female="", male="")
    return cfg
