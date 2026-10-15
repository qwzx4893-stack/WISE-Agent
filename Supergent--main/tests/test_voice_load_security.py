"""Synthetic voice trust tests: no Torch, models, media, workers, or network."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import threading
from unittest.mock import Mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_module(monkeypatch, relative_path, name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _checkpoint_module(monkeypatch, load):
    torch = ModuleType("torch")
    torch.nn = SimpleNamespace(Module=object)
    torch.load = load
    monkeypatch.setitem(sys.modules, "torch", torch)
    return _source_module(
        monkeypatch, "tools/index-tts/indextts/utils/checkpoint.py",
        "synthetic_voice_checkpoint_security",
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_checkpoint_restricted_load_preserves_state_dict_semantics(monkeypatch, tmp_path, capsys, wrapped):
    weights = {"synthetic_weight": object()}
    load = Mock(return_value={"model": weights} if wrapped else weights)
    checkpoint = _checkpoint_module(monkeypatch, load)
    model = SimpleNamespace(load_state_dict=Mock(return_value=(["missing"], ["extra"])))
    path = str(tmp_path / "synthetic-checkpoint.pth")

    assert checkpoint.load_checkpoint(model, path) == {}
    load.assert_called_once_with(path, map_location="cpu", weights_only=True)
    model.load_state_dict.assert_called_once_with(weights, strict=False)
    diagnostics = capsys.readouterr().out
    assert "missing keys (1)" in diagnostics
    assert "unexpected keys (1)" in diagnostics


def test_checkpoint_preserves_optional_yaml_metadata(monkeypatch, tmp_path):
    load = Mock(return_value={"synthetic_weight": object()})
    checkpoint = _checkpoint_module(monkeypatch, load)
    model = SimpleNamespace(load_state_dict=Mock(return_value=([], [])))
    path = tmp_path / "synthetic-checkpoint.pth"
    path.with_suffix(".yaml").write_text("synthetic_fixture: true\n", encoding="utf-8")

    assert checkpoint.load_checkpoint(model, str(path)) == {"synthetic_fixture": True}
    load.assert_called_once_with(str(path), map_location="cpu", weights_only=True)


def test_checkpoint_rejection_propagates_without_unrestricted_retry(monkeypatch, tmp_path):
    rejection = RuntimeError("synthetic restricted-loader rejection")
    load = Mock(side_effect=rejection)
    checkpoint = _checkpoint_module(monkeypatch, load)
    model = SimpleNamespace(load_state_dict=Mock())
    path = str(tmp_path / "synthetic-rejected-checkpoint.pth")

    with pytest.raises(RuntimeError, match="synthetic restricted-loader rejection"):
        checkpoint.load_checkpoint(model, path)
    load.assert_called_once_with(path, map_location="cpu", weights_only=True)
    model.load_state_dict.assert_not_called()


@pytest.mark.parametrize("unsafe_override", [None, "1", "true", "0"])
def test_worker_restricts_all_loaders_without_mutating_parent_or_download_settings(monkeypatch, tmp_path, unsafe_override):
    tts = _source_module(monkeypatch, "core/voice/tts.py", "synthetic_voice_tts_security")
    inherited = {
        "TORCH_FORCE_WEIGHTS_ONLY_LOAD": "0",
        "HF_HUB_CACHE": str(tmp_path / "synthetic-hf-cache"),
        "HF_HUB_OFFLINE": "1",
        "USE_MODELSCOPE": "false",
        "PYTORCH_CUDA_ALLOC_CONF": "synthetic-allocator-setting",
    }
    if unsafe_override is not None:
        inherited["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = unsafe_override
    original = inherited.copy()
    monkeypatch.setattr(tts, "os", SimpleNamespace(environ=inherited))
    process = SimpleNamespace(poll=lambda: None)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(tts, "subprocess", SimpleNamespace(Popen=spawn, PIPE=-1, DEVNULL=-3))
    provider = object.__new__(tts.IndexTTSProvider)
    provider._proc_lock = threading.RLock()
    provider._idle_timer = None
    provider._proc = None
    provider._choose_device = lambda: "cpu"
    provider._python = tmp_path / "synthetic-python.exe"
    provider._sidecar = tmp_path / "synthetic-sidecar.py"
    provider._model_dir = tmp_path / "synthetic-models"
    provider.gpt_checkpoint = tmp_path / "synthetic-candidate.pth"
    provider.quality_profile = "balanced"

    assert provider._ensure_process() is process
    spawn.assert_called_once()
    args, kwargs = spawn.call_args
    assert args == ([str(provider._python), "-u", str(provider._sidecar)],)
    environment = kwargs["env"]
    assert environment["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] == "1"
    assert "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD" not in environment
    assert inherited == original
    for key in ("HF_HUB_CACHE", "HF_HUB_OFFLINE", "USE_MODELSCOPE", "PYTORCH_CUDA_ALLOC_CONF"):
        assert environment[key] == original[key]
    assert environment["WISE_TTS_DEVICE"] == "cpu"
    assert environment["WISE_TTS_QUALITY_PROFILE"] == "balanced"
    assert environment["WISE_INDEXTTS_GPT_CHECKPOINT"] == str(provider.gpt_checkpoint)
