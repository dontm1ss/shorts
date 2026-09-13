import json

import pytest

from core.media import media_info, run_ffmpeg
from core.scene import scene_errors
from video_clipper import cut_video, plan_segments


def test_spec_example_120s():
    """ТЗ 1.2: 2-минутное видео → 60 файлов, последние ~15 короче 30 с, без паддинга."""
    segments = plan_segments(120_000, 30_000, 2_000)
    assert len(segments) == 60
    assert segments[0] == (0, 30_000)
    assert segments[-1] == (118_000, 120_000)
    short = [s for s in segments if s[1] - s[0] < 30_000]
    assert len(short) == 14        # в ТЗ «~15»: ровно 14 — старты 92…118 с (старт 90 даёт полные 30 с)
    assert all(end <= 120_000 for _, end in segments)
    assert all(b[0] - a[0] == 2_000 for a, b in zip(segments, segments[1:]))


@pytest.mark.parametrize("duration, count", [(1, 1), (2_000, 1), (2_001, 2), (31_000, 16)])
def test_edge_durations(duration, count):
    assert len(plan_segments(duration, 30_000, 2_000)) == count


def test_bad_params():
    with pytest.raises(ValueError):
        plan_segments(10_000, 30_000, 0)


def test_cut_video_files_and_metadata(dev_cfg, tmp_path):
    src = tmp_path / "film.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=640x360:r=25:d=7", "-f", "lavfi", "-i", "sine=d=7",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)])
    dev_cfg["clipper"].update(window_s=4, step_s=2)
    dev_cfg["render"].update(encoder="libx264", encoder_args=["-preset", "ultrafast"])
    dev_cfg["paths"]["output"] = str(tmp_path / "out")

    scenes = cut_video(src, dev_cfg, write_files=True, out_dir=tmp_path / "clips")
    assert len(scenes) == 4                                        # старты 0, 2, 4, 6
    assert all(scene_errors(s) == [] for s in scenes)
    meta = json.loads((tmp_path / "clips" / "segments.json").read_text(encoding="utf-8"))
    durations = [media_info(tmp_path / "clips" / (m["file"].rsplit("/", 1)[-1]))["duration_ms"]
                 for m in meta["segments"]]
    assert durations == pytest.approx([4000, 4000, 3000, 1000], abs=120)   # точный рез, хвосты короче
    assert [s["source_video"]["clip_start_ms"] for s in scenes] == [0, 2000, 4000, 6000]
