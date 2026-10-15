"""Reference bounds use fake decoders/tensors/models and disposable non-audio files."""

import ast
import contextlib
import importlib.util
import io
import math
import os
from pathlib import Path
import sys
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def source_function(relative, name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
    candidates = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            candidates.extend(method for method in node.body
                              if isinstance(method, ast.FunctionDef) and method.name == name)
    assert len(candidates) == 1
    module = ast.Module(body=[ast.ImportFrom(module="__future__",
                            names=[ast.alias(name="annotations")], level=0), candidates[0]], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / relative), "exec"), namespace)
    return namespace[name]


class FakeArray:
    def __init__(self, length):
        self.shape = (length,)


class FakeTensor:
    def __init__(self, length):
        self.shape = (length,)

    def unsqueeze(self, dimension):
        assert dimension == 0
        self.shape = (1, self.shape[-1])
        return self

    def __getitem__(self, index):
        _, samples = index
        return FakeTensor(min(self.shape[-1], samples.stop)).unsqueeze(0)


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    reference = tmp_path / "synthetic-reference.fixture"
    reference.write_bytes(b"synthetic fixture, not audio")
    metadata = SimpleNamespace(samplerate=44100, channels=2, frames=44100 * 60, duration=60.0)
    loads, headers, events = [], [], []
    sf = ModuleType("soundfile")

    def info(handle):
        assert hasattr(handle, "fileno") and not handle.closed
        headers.append(handle)
        events.append("metadata")
        return metadata

    sf.info = info
    monkeypatch.setitem(sys.modules, "soundfile", sf)
    librosa = ModuleType("librosa")

    def load(source, **kwargs):
        loads.append((source, kwargs.copy()))
        events.append("decode")
        rate = kwargs.get("sr", 22050)
        seconds = kwargs.get("duration", 60)
        return FakeArray(int(seconds * rate)), rate

    librosa.load = load
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    torch = ModuleType("torch")
    torch.tensor = lambda values: FakeTensor(values.shape[0])
    monkeypatch.setitem(sys.modules, "torch", torch)
    helper_path = ROOT / "core/voice/reference_audio.py"
    helper = None
    if helper_path.is_file():
        spec = importlib.util.spec_from_file_location("reference_audio", helper_path)
        helper = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "reference_audio", helper)
        spec.loader.exec_module(helper)
    return SimpleNamespace(reference=reference, metadata=metadata, loads=loads, headers=headers,
                           events=events, sf=sf, librosa=librosa, torch=torch, helper=helper)


def worker_loader(monkeypatch, fixture):
    original_namespace = {"librosa": fixture.librosa, "torch": fixture.torch}
    original = source_function("tools/index-tts/indextts/infer_v2_5.py", "_load_and_cut_audio",
                               original_namespace)
    model = SimpleNamespace(cache_spk_audio_prompt="existing-cache-key", cache_emo_audio_prompt="existing-key")
    model._load_and_cut_audio = MethodType(original, model)
    constructors = []

    def constructor(**kwargs):
        constructors.append(kwargs)
        fixture.events.append("model")
        return model

    parent = ModuleType("indextts")
    upstream = ModuleType("indextts.infer_v2_5")
    upstream.IndexTTS2 = constructor
    monkeypatch.setitem(sys.modules, "indextts", parent)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2_5", upstream)

    class ExistingPath:
        def __truediv__(self, name):
            return self

        def is_dir(self):
            return True

        def is_file(self):
            return True

        def __str__(self):
            return "synthetic-model-path"

    namespace = {"__name__": "synthetic_worker", "__package__": None, "_MODEL": None,
                 "_runtime_root": ExistingPath, "_model_dir": ExistingPath,
                 "_candidate_gpt_checkpoint": lambda: None, "contextlib": contextlib,
                 "sys": SimpleNamespace(path=[], stderr=io.StringIO()),
                 "os": SimpleNamespace(environ={"WISE_TTS_DEVICE": "cpu"})}
    return source_function("core/voice/indextts_sidecar.py", "_load_model", namespace), model, constructors


def test_actual_worker_model_installs_finite_reference_decode(monkeypatch, fixture):
    load_model, model, constructors = worker_loader(monkeypatch, fixture)
    assert load_model() is model
    result, rate = model._load_and_cut_audio(str(fixture.reference), 15)
    assert rate == 22050 and result.shape == (1, 15 * 22050)
    assert len(fixture.loads) == 1
    assert 15 <= fixture.loads[0][1].get("duration", math.inf) <= 15.25
    assert constructors[0]["use_qwen_emo"] is False
    assert model.cache_spk_audio_prompt == "existing-cache-key"
    assert model.cache_emo_audio_prompt == "existing-key"


BAD_METADATA = [("channels", 0), ("channels", 9), ("channels", True),
                ("samplerate", 0), ("samplerate", 192001), ("samplerate", math.inf),
                ("frames", 0), ("frames", math.nan), ("frames", math.inf),
                ("frames", 44100 * 601), ("duration", 0), ("duration", math.nan),
                ("duration", math.inf), ("duration", 601), ("duration", True),
                ("duration", 1.0)]


@pytest.mark.parametrize("field,value", BAD_METADATA)
def test_actual_job_rejects_bad_reference_before_model_or_output_work(fixture, field, value):
    setattr(fixture.metadata, field, value)
    load_model = Mock(side_effect=AssertionError("model must not initialize"))
    prepare = Mock(side_effect=AssertionError("text processing must not run"))
    namespace = {"__name__": "synthetic_worker", "__package__": None, "Path": Path,
                 "_load_model": load_model, "prepare_spoken_text": prepare}
    synthesize = source_function("core/voice/indextts_sidecar.py", "_synthesize", namespace)
    output = fixture.reference.parent / "never-created" / "output.fixture"
    with pytest.raises(ValueError):
        synthesize({"text": "fixture", "reference_audio": str(fixture.reference), "output": str(output)})
    load_model.assert_not_called()
    prepare.assert_not_called()
    assert not output.parent.exists()
    assert fixture.loads == []


@pytest.mark.parametrize("size,regular", [(0, True), (64 * 1024 * 1024 + 1, True), (1, False)])
def test_byte_and_regular_file_budget_precedes_metadata(fixture, monkeypatch, size, regular):
    import stat

    attributes = SimpleNamespace(st_size=size, st_mode=stat.S_IFREG if regular else stat.S_IFDIR)
    blocked = SimpleNamespace(stat=lambda: attributes,
                              open=Mock(side_effect=AssertionError("file must not be opened")))
    monkeypatch.setattr(fixture.helper, "Path", lambda path: blocked)
    with pytest.raises(ValueError):
        fixture.helper.validate_reference_audio(fixture.reference)
    blocked.open.assert_not_called()
    assert fixture.headers == [] and fixture.loads == []


@pytest.mark.parametrize("rate", [None, 16000, 22050])
def test_instance_adapter_preserves_rate_shape_path_cache_and_library_functions(monkeypatch, fixture, rate):
    load_model, model, _ = worker_loader(monkeypatch, fixture)
    original_load = fixture.librosa.load
    model = load_model()
    info = fixture.helper.validate_reference_audio(fixture.reference)
    model._wise_reference_audio = info
    tensor, decoded_rate = model._load_and_cut_audio(str(fixture.reference), 15, sr=rate)
    assert decoded_rate == (rate or 22050)
    assert tensor.shape == (1, 15 * decoded_rate)
    source, options = fixture.loads[0]
    assert source.name == str(fixture.reference) and source.closed
    assert options["duration"] == 15.25
    if rate:
        assert options["sr"] == rate
    else:
        assert "sr" not in options
    assert fixture.librosa.load is original_load
    assert model.cache_spk_audio_prompt == "existing-cache-key"
    assert model.cache_emo_audio_prompt == "existing-key"
    before = model._load_and_cut_audio
    fixture.helper.install_reference_audio_guard(model)
    assert model._load_and_cut_audio is before


def test_short_reference_and_greater_requested_duration_remain_bounded(monkeypatch, fixture):
    load_model, model, _ = worker_loader(monkeypatch, fixture)
    model = load_model()
    fixture.metadata.frames = 44100 * 5
    fixture.metadata.duration = 5.0
    tensor, rate = model._load_and_cut_audio(str(fixture.reference), 999)
    assert fixture.loads[0][1]["duration"] == 5.0
    assert tensor.shape == (1, 5 * rate)


def test_metadata_failure_has_no_alternate_unbounded_decoder(fixture, monkeypatch):
    monkeypatch.setattr(fixture.sf, "info", Mock(side_effect=RuntimeError("unsupported fixture format")))
    with pytest.raises(RuntimeError, match="unsupported fixture format"):
        fixture.helper.load_bounded_reference(str(fixture.reference), 15)
    assert fixture.loads == []


def test_decoder_failure_is_propagated_without_retry_or_tensor_allocation(fixture, monkeypatch):
    decoder = Mock(side_effect=RuntimeError("fixture decode failure"))
    tensor = Mock(side_effect=AssertionError("no tensor allocation"))
    monkeypatch.setattr(fixture.librosa, "load", decoder)
    monkeypatch.setattr(fixture.torch, "tensor", tensor)
    with pytest.raises(RuntimeError, match="fixture decode failure"):
        fixture.helper.load_bounded_reference(str(fixture.reference), 15)
    assert decoder.call_count == 1 and decoder.call_args.kwargs["duration"] == 15.25
    tensor.assert_not_called()


def test_reference_change_after_admission_is_rejected_before_decode(fixture):
    info = fixture.helper.validate_reference_audio(fixture.reference)
    fixture.reference.write_bytes(b"different synthetic fixture contents")
    with pytest.raises(ValueError, match="changed after validation"):
        fixture.helper.load_bounded_reference(str(fixture.reference), 15, expected=info)
    assert fixture.loads == []


def test_reference_change_during_decode_is_rejected_before_tensor_copy(fixture, monkeypatch):
    tensor = Mock(side_effect=AssertionError("no tensor allocation"))

    def modified_load(source, **kwargs):
        fixture.reference.write_bytes(b"modified while fake decoder reads")
        return FakeArray(int(15.25 * 22050)), 22050

    monkeypatch.setattr(fixture.librosa, "load", modified_load)
    monkeypatch.setattr(fixture.torch, "tensor", tensor)
    with pytest.raises(ValueError, match="changed while being read"):
        fixture.helper.load_bounded_reference(str(fixture.reference), 15)
    tensor.assert_not_called()


@pytest.mark.parametrize("length,rate", [(0, 22050), (20 * 22050, 22050), (10, math.inf), (10, 44100)])
def test_decoder_contract_rejection_precedes_tensor_copy(fixture, monkeypatch, length, rate):
    monkeypatch.setattr(fixture.librosa, "load", lambda source, **kwargs: (FakeArray(length), rate))
    tensor = Mock(side_effect=AssertionError("no tensor allocation"))
    monkeypatch.setattr(fixture.torch, "tensor", tensor)
    with pytest.raises(ValueError):
        fixture.helper.load_bounded_reference(str(fixture.reference), 15)
    tensor.assert_not_called()


def test_full_actual_job_validates_then_uses_guard_without_changing_inference_options(monkeypatch, fixture):
    load_model, model, _ = worker_loader(monkeypatch, fixture)
    model.low_vram = True
    inference_options = []

    def infer(**kwargs):
        inference_options.append(kwargs.copy())
        for rate in (None, None, 16000):
            model._load_and_cut_audio(kwargs["spk_audio_prompt"], 15, sr=rate)
        Path(kwargs["output_path"]).write_bytes(b"synthetic output, not audio")

    model.infer = infer
    read = Mock(return_value=(FakeArray(2205), 22050))
    write = Mock()
    original_info = fixture.sf.info
    monkeypatch.setattr(fixture.sf, "read", read, raising=False)
    monkeypatch.setattr(fixture.sf, "write", write, raising=False)
    monkeypatch.setattr(fixture.sf, "info", lambda source: original_info(source) if hasattr(source, "fileno")
                        else SimpleNamespace(samplerate=22050, frames=2205))
    namespace = {"__name__": "synthetic_worker", "__package__": None, "Path": Path,
                 "_load_model": load_model, "contextlib": contextlib,
                 "sys": SimpleNamespace(stderr=io.StringIO()), "os": SimpleNamespace(environ={}),
                 "prepare_spoken_text": lambda text, language, **kw: (text, language),
                 "_sentence_chunks": lambda model, text, language: [text],
                 "_quality_profile": lambda: {"max_tokens": 56, "top_k": 24, "num_beams": 1},
                 "_token_count": lambda *args: 1, "polish": lambda data, rate: data}
    source_function("core/voice/indextts_sidecar.py", "_synthesize_single", namespace)
    synthesize = source_function("core/voice/indextts_sidecar.py", "_synthesize", namespace)
    output = fixture.reference.parent / "output.fixture"
    result = synthesize({"text": "fixture", "reference_audio": str(fixture.reference), "output": str(output)})
    assert result["ok"] is True and result["duration_seconds"] == .1
    assert fixture.events.index("metadata") < fixture.events.index("model")
    assert len(fixture.loads) == 3
    assert all(options["duration"] == 15.25 for _, options in fixture.loads)
    assert inference_options[0]["spk_audio_prompt"] == str(fixture.reference)
    assert inference_options[0]["text_normalization"] is False
    assert inference_options[0]["num_beams"] == 1
    assert model.low_vram is True
    assert model._wise_reference_audio.path == fixture.reference


def test_unsupported_model_contract_cannot_enter_cached_model_state(monkeypatch, fixture):
    load_model, model, _ = worker_loader(monkeypatch, fixture)
    model._load_and_cut_audio = None
    with pytest.raises(RuntimeError, match="contract is unavailable"):
        load_model()
    with pytest.raises(RuntimeError, match="contract is unavailable"):
        load_model()
