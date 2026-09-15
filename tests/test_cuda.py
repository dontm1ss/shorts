import importlib.machinery
import os

import pytest

from core import cuda


@pytest.fixture(autouse=True)
def fresh_cache():
    cuda.ensure_cuda_dlls.cache_clear()
    yield
    cuda.ensure_cuda_dlls.cache_clear()


def _fake_torch(monkeypatch, tmp_path, with_cublas: bool):
    lib = tmp_path / "torch" / "lib"
    lib.mkdir(parents=True)
    if with_cublas:
        (lib / "cublas64_12.dll").write_bytes(b"")
    spec = importlib.machinery.ModuleSpec("torch", None, is_package=True)
    spec.submodule_search_locations = [str(tmp_path / "torch")]
    monkeypatch.setattr(cuda.importlib.util, "find_spec", lambda name: spec)
    monkeypatch.setattr(cuda.os, "add_dll_directory", lambda path: None, raising=False)
    monkeypatch.setenv("PATH", "C:\\other")
    return lib


def test_not_windows_does_nothing(monkeypatch):
    monkeypatch.setattr(cuda.sys, "platform", "darwin")
    assert cuda.ensure_cuda_dlls() is None


def test_torch_cuda_libs_put_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cuda.sys, "platform", "win32")
    lib = _fake_torch(monkeypatch, tmp_path, with_cublas=True)
    assert cuda.ensure_cuda_dlls() == lib
    assert os.environ["PATH"].split(os.pathsep)[0] == str(lib)


def test_cpu_torch_without_cublas_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(cuda.sys, "platform", "win32")
    _fake_torch(monkeypatch, tmp_path, with_cublas=False)
    assert cuda.ensure_cuda_dlls() is None
    assert os.environ["PATH"] == "C:\\other"
