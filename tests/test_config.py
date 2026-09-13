from pathlib import Path

import pytest

import core.config
from conftest import make_hw as hw
from core.config import ProfileError, load_config, load_profiles, select_profile


MAC_M2 = hw("Darwin", "arm64", ["Apple Silicon GPU"], ["apple"])
WIN10_GTX1060 = hw("Windows", gpus=["NVIDIA GeForce GTX 1060 6GB"], vendors=["nvidia"])
WIN11_RX6600 = hw("Windows", gpus=["AMD Radeon RX 6600"], vendors=["amd"])
# Ноутбук: встройка Intel + дискретная NVIDIA — должен выбраться NVIDIA-профиль
WIN_IGPU_NVIDIA = hw("Windows", gpus=["Intel UHD 630", "NVIDIA GeForce GTX 1060"],
                     vendors=["intel", "nvidia"])


@pytest.fixture(scope="module")
def profiles():
    return load_profiles()


def test_three_profiles(profiles):
    assert set(profiles) == {"mac_apple_silicon", "win11_amd", "win10_nvidia_gtx1060"}


def test_only_gtx1060_is_deploy_target(profiles):
    targets = [n for n, p in profiles.items() if p["profile"]["deploy_target"]]
    assert targets == ["win10_nvidia_gtx1060"]


@pytest.mark.parametrize("machine, expected", [
    (MAC_M2, "mac_apple_silicon"),
    (WIN10_GTX1060, "win10_nvidia_gtx1060"),
    (WIN11_RX6600, "win11_amd"),
    (WIN_IGPU_NVIDIA, "win10_nvidia_gtx1060"),
])
def test_auto_selection(profiles, machine, expected):
    assert select_profile(machine, profiles) == expected


@pytest.mark.parametrize("machine", [
    hw("Linux"),
    hw("Darwin", "x86_64", ["Intel Iris"], ["intel"]),   # Intel Mac
    hw("Windows", gpus=["Intel UHD 630"], vendors=["intel"]),
])
def test_unknown_hardware_raises(profiles, machine):
    with pytest.raises(ProfileError, match="SHORTS_PROFILE"):
        select_profile(machine, profiles)


def test_override(profiles):
    assert select_profile(WIN11_RX6600, profiles, "win10_nvidia_gtx1060") == "win10_nvidia_gtx1060"
    with pytest.raises(ProfileError, match="Неизвестный профиль"):
        select_profile(WIN11_RX6600, profiles, "linux_rtx4090")


@pytest.mark.parametrize("machine, device, encoder", [
    (MAC_M2, "mps", "h264_videotoolbox"),
    (WIN10_GTX1060, "cuda", "h264_nvenc"),
    (WIN11_RX6600, "cpu", "h264_amf"),
])
def test_merged_config(machine, device, encoder):
    cfg = load_config(hw=machine)
    assert cfg["tts"]["device"] == device
    assert cfg["render"]["encoder"] == encoder
    assert cfg["profile"]["matches_hardware"]
    # Общие настройки из base.toml доходят до каждого профиля
    assert (cfg["video"]["width"], cfg["video"]["height"]) == (1080, 1920)
    assert cfg["video"]["width"] * 16 == cfg["video"]["height"] * 9
    assert cfg["tts"]["presets"]["neutral"]["cfg_weight"] == 0.5
    assert cfg["queue"]["max_parallel_jobs"] == 1


def test_gtx1060_specifics():
    cfg = load_config(hw=WIN10_GTX1060)
    assert cfg["subtitles"]["compute_type"] == "int8"   # CC 6.1 — без float16
    assert cfg["tts"]["max_chars_per_chunk"] == 250     # перекрывает base (300)
    assert cfg["runtime"]["unload_models_between_jobs"] is True


def test_override_on_foreign_hardware_is_flagged():
    cfg = load_config(profile="win10_nvidia_gtx1060", hw=WIN11_RX6600)
    assert cfg["profile"]["selected_by"] == "override"
    assert cfg["profile"]["matches_hardware"] is False


@pytest.mark.parametrize("profile, expected", [
    ("win10_nvidia_gtx1060", "https://download.pytorch.org/whl/cu124"),
    ("win11_amd", "https://download.pytorch.org/whl/cpu"),
    ("mac_apple_silicon", ""),
])
def test_cli_get_torch_index(capsys, profile, expected):
    # setup.ps1 / setup.sh берут индекс torch именно так
    assert core.config.main(["--profile", profile, "--get", "install.torch_index_url"]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_cli_get_unknown_key(capsys):
    assert core.config.main(["--profile", "win11_amd", "--get", "nope.key"]) == 1


def test_no_secrets_in_config_files():
    root = Path(__file__).resolve().parents[1] / "config"
    for path in root.rglob("*.toml"):
        assert "sk-" not in path.read_text(encoding="utf-8"), path
