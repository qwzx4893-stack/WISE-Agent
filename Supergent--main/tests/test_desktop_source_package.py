"""A source release must be complete, exclude runtime data and remain sealed."""
import importlib.util
import contextlib
import json
from pathlib import Path
import stat
import struct
import sys
import zipfile
import pytest

QA = Path(__file__).resolve().parents[1] / "qa" / "acceptance"
spec = importlib.util.spec_from_file_location("package_desktop_source", QA / "package_desktop_source.py")
packager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = packager
sys.path.insert(0, str(QA))
try:
    spec.loader.exec_module(packager)
finally:
    sys.path.remove(str(QA))


def source(root):
    for name in packager.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic source fixture", encoding="utf-8")
    return root


def test_package_has_entrypoint_assets_manifest_not_runtime(tmp_path):
    root = source(tmp_path / "src")
    for name in ("core/models/private.pth", "config/server.json", "core/.venv/lib/secrets.py", "core/logs/customer.json", "sessions/customer.json",
                 "skills/.lifecycle.json", "skills/.index_cache.json", "core/.tooling/private.py", "core/recordings/private.json",
                 "core/weights/private.json", "core/vendor/.venv/private.py", "core/.env.example", "browser_storage_state.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must-not-ship", encoding="utf-8")
    output = tmp_path / "desktop.zip"
    result = packager.package(root, output)
    assert result["scope"] == "SOURCE_ONLY_NOT_STANDALONE_INSTALLER"
    assert result["redistribution_ready"] is False
    assert result["provenance"] == "internal-consistency-only"
    assert any("No project LICENSE" in w for w in result["warnings"])
    with zipfile.ZipFile(output) as archive:
        assert set(packager.REQUIRED).issubset(archive.namelist())
        assert "DESKTOP_SOURCE_MANIFEST.json" in archive.namelist()
        assert all("must-not-ship" not in archive.read(name).decode("utf-8") for name in archive.namelist())
    with pytest.raises(FileExistsError):
        packager.package(root, output)
    assert packager.verify(output, result["archive_sha256"])["provenance"] == "external-sealed-hash-matched"


def test_incomplete_bundle_fails_without_artifact(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    output = tmp_path / "desktop.zip"
    with pytest.raises(ValueError, match="Incomplete"):
        packager.package(root, output)
    assert not output.exists()


def test_suspected_secret_requires_review_without_exposing_literal(tmp_path):
    root = source(tmp_path / "src")
    (root / "agent_core.py").write_text("ghp_" + "synthetic" * 5, encoding="utf-8")
    with pytest.raises(ValueError, match="Sensitive literal review") as exc:
        packager.package(root, tmp_path / "desktop.zip")
    assert "synthetic" not in str(exc.value)


def rewrite(output, transform):
    """Tamper only with a disposable owned fixture, never the real release."""
    with zipfile.ZipFile(output) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    entries = transform(entries)
    with zipfile.ZipFile(output, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)


def manifest_change(entries, mutation):
    changed = []
    for info, data in entries:
        if info.filename == packager.MANIFEST:
            manifest = json.loads(data)
            mutation(manifest)
            data = json.dumps(manifest).encode()
        changed.append((info, data))
    return changed


def test_safe_support_assets_not_silently_omitted(tmp_path):
    root = source(tmp_path / "src")
    additions = ("skills/example/scripts/task.mjs", "skills/example/assets/people.xml", "skills/example/assets/schema.sql",
                 "skills/example/assets/values.yaml.template", "skills/example/NOTICE", "skills/example/LICENSE",
                 "skills/.bundles.json", "skills/.catalog-sources.json", "ui/wise_web/assets/photo.webp")
    for name in additions:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe-static-fixture", encoding="utf-8")
    output = tmp_path / "desktop.zip"
    packager.package(root, output)
    with zipfile.ZipFile(output) as archive:
        assert set(additions).issubset(archive.namelist())
        manifest = json.loads(archive.read(packager.MANIFEST))
        assert manifest["files"] == sorted(manifest["files"], key=lambda r: r["path"])
        assert all(set(record) == {"path", "sha256", "bytes"} for record in manifest["files"])


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "C:/drive.py", "core\\backslash.py", "core/./x.py",
                                 "core//x.py", "core/../x.py", "core/NUL.py", "core/end./x.py", "core/space /x.py",
                                 "core/stream.py:private", "core/\x01name.py", "core/wildcard?.py", "core/COM¹.py", "core/NUL .txt"])
def test_verifier_rejects_unsafe_paths(tmp_path, name):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    info = zipfile.ZipInfo(name)
    info.filename = name  # Windows ZipInfo otherwise normalizes backslashes.
    rewrite(output, lambda entries: entries + [(info, b"payload")])
    with pytest.raises(ValueError, match="Unsafe archive path"):
        packager.verify(output)


@pytest.mark.parametrize("name", ["agent_core.py", "AGENT_CORE.PY", packager.MANIFEST])
def test_verifier_rejects_duplicate_and_windows_case_collisions(tmp_path, name):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    with pytest.warns(UserWarning) if name != "AGENT_CORE.PY" else contextlib.nullcontext():
        rewrite(output, lambda entries: entries + [(zipfile.ZipInfo(name), b"payload")])
    with pytest.raises(ValueError, match="Duplicate archive path"):
        packager.verify(output)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR])
def test_verifier_rejects_nonregular_zip_entries(tmp_path, mode):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    info = zipfile.ZipInfo("core/link.py")
    info.create_system = 3
    info.external_attr = (mode | 0o777) << 16
    rewrite(output, lambda entries: entries + [(info, b"outside")])
    with pytest.raises(ValueError, match="Non-regular"):
        packager.verify(output)


@pytest.mark.parametrize("name", ["core/.env", "core/logs/customer.json", "config/server.json", "skills/.lifecycle.json",
                                 "core/.tooling/private.py", "core/recordings/private.json", "core/weights/manifest.json", "core/.voice-venv/private.py"])
def test_verifier_rejects_runtime_payload_even_with_forged_manifest(tmp_path, name):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    rewrite(output, lambda entries: entries + [(zipfile.ZipInfo(name), b"private")])
    with pytest.raises(ValueError, match="Excluded runtime"):
        packager.verify(output)


def test_verifier_rejects_modified_source_and_external_hash(tmp_path):
    output = tmp_path / "desktop.zip"
    result = packager.package(source(tmp_path / "src"), output)
    rewrite(output, lambda entries: [(info, b"altered" if info.filename == "agent_core.py" else data) for info, data in entries])
    with pytest.raises(ValueError, match="Source file checksum mismatch"):
        packager.verify(output)
    with pytest.raises(ValueError, match="Sealed archive checksum mismatch"):
        packager.verify(output, result["archive_sha256"])


@pytest.mark.parametrize("mutation,match", [
    (lambda m: m.update(scope="INSTALLER"), "source-only"),
    (lambda m: m.update(runtime_state_included=True), "source-only"),
    (lambda m: m.update(redistribution_ready=True), "source-only"),
    (lambda m: m.update(content_sha256="0" * 64), "content checksum"),
    (lambda m: m["files"].append(m["files"][0]), "Duplicate"),
    (lambda m: m["files"].pop(), "membership"),
    (lambda m: m["files"][0].update(bytes=True), "checksum or size"),
])
def test_verifier_rejects_invalid_manifest(tmp_path, mutation, match):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    rewrite(output, lambda entries: manifest_change(entries, mutation))
    with pytest.raises(ValueError, match=match):
        packager.verify(output)


def test_verifier_rejects_missing_manifest_and_extra_source(tmp_path):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    rewrite(output, lambda entries: entries + [(zipfile.ZipInfo("core/extra.py"), b"unlisted")])
    with pytest.raises(ValueError, match="membership"):
        packager.verify(output)
    rewrite(output, lambda entries: [(info, data) for info, data in entries if info.filename != packager.MANIFEST])
    with pytest.raises(ValueError, match="Missing source manifest"):
        packager.verify(output)


def test_verifier_requires_baseline_even_if_manifest_hashes_match(tmp_path):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    def remove(entries):
        entries = [(info, data) for info, data in entries if info.filename != "core/models/local_model_manager.py"]
        def mutate(manifest):
            manifest["files"] = [r for r in manifest["files"] if r["path"] != "core/models/local_model_manager.py"]
            manifest["content_sha256"] = packager.content_digest(manifest["files"])
        return manifest_change(entries, mutate)
    rewrite(output, remove)
    with pytest.raises(ValueError, match="Incomplete"):
        packager.verify(output)


def test_restricted_license_tree_is_excluded_but_safe_notices_remain(tmp_path):
    root = source(tmp_path / "src")
    folder = root / "skills" / "restricted"
    folder.mkdir(parents=True)
    (folder / "LICENSE").write_text("Distribute, sublicense, or transfer", encoding="utf-8")
    (folder / "SKILL.md").write_text("restricted source", encoding="utf-8")
    output = tmp_path / "desktop.zip"
    packager.package(root, output)
    with zipfile.ZipFile(output) as archive:
        assert not any(name.startswith("skills/restricted/") for name in archive.namelist())
        assert json.loads(archive.read(packager.MANIFEST))["redistribution_exclusions"] == ["skills/restricted"]


def test_packager_never_follows_source_symlink(tmp_path):
    root = source(tmp_path / "src")
    outside = tmp_path / "private.py"
    outside.write_text("private-data", encoding="utf-8")
    link = root / "core" / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows source symlink privilege unavailable")
    assert link not in packager.collect(root)


def test_exclusive_create_race_never_deletes_another_artifact(tmp_path, monkeypatch):
    root = source(tmp_path / "src")
    output = tmp_path / "desktop.zip"
    original = packager.zipfile.ZipFile
    def race(path, mode, **kwargs):
        if mode == "x":
            output.write_bytes(b"other-owner-sealed-file")
        return original(path, mode, **kwargs)
    monkeypatch.setattr(packager.zipfile, "ZipFile", race)
    with pytest.raises(FileExistsError):
        packager.package(root, output)
    assert output.read_bytes() == b"other-owner-sealed-file"


def test_failed_write_removes_only_owned_artifact(tmp_path, monkeypatch):
    root = source(tmp_path / "src")
    output = tmp_path / "desktop.zip"
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic packaging failure")
    monkeypatch.setattr(packager.zipfile.ZipFile, "writestr", fail)
    with pytest.raises(RuntimeError, match="synthetic packaging failure"):
        packager.package(root, output)
    assert not output.exists()


def test_size_bounds_refuse_oversized_source_without_artifact(tmp_path, monkeypatch):
    root = source(tmp_path / "src")
    monkeypatch.setattr(packager, "MAX_FILE_BYTES", 3)
    output = tmp_path / "desktop.zip"
    with pytest.raises(ValueError, match="per-file bound"):
        packager.package(root, output)
    assert not output.exists()


def test_verifier_rejects_duplicate_json_keys(tmp_path):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    rewrite(output, lambda entries: [(info, b'{"schema":"one","schema":"two"}' if info.filename == packager.MANIFEST else data) for info, data in entries])
    with pytest.raises(ValueError, match="Duplicate manifest JSON key"):
        packager.verify(output)


@pytest.mark.parametrize("bound,match", [("MAX_FILES", "member bound"), ("MAX_FILE_BYTES", "member size bound"),
                                      ("MAX_MANIFEST_BYTES", "member size bound"), ("MAX_TOTAL_BYTES", "oversized source archive")])
def test_verifier_bounds_compressed_input(tmp_path, monkeypatch, bound, match):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    verifier = sys.modules["verify_desktop_source"]
    monkeypatch.setattr(verifier, bound, 1)
    if bound == "MAX_TOTAL_BYTES":
        monkeypatch.setattr(verifier, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match=match):
        packager.verify(output)


def test_model_state_directories_are_excluded_except_application_modules(tmp_path):
    root = source(tmp_path / "src")
    state_names = ("skills/example/models/config.json", "core/voice/models/config.json", "core/models/models/config.json")
    for name in state_names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("model-weight-state", encoding="utf-8")
    names = {p.relative_to(root).as_posix() for p in packager.collect(root)}
    assert "core/models/local_model_manager.py" in names
    assert not set(state_names) & names


@pytest.mark.parametrize("name", ["skills/example/LICENSE.exe", "skills/example/NOTICE.dll", "core/COPYING.pth",
                                 "core/NOTICE.wav", "core/LICENSE.key", "core/NOTICE.sqlite3"])
def test_notice_filename_cannot_bypass_binary_runtime_exclusions(tmp_path, name):
    root = source(tmp_path / "src")
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private-runtime-fixture")
    assert path not in packager.collect(root)
    output = tmp_path / "desktop.zip"
    packager.package(root, output)
    rewrite(output, lambda entries: entries + [(zipfile.ZipInfo(name), b"private-runtime-fixture")])
    with pytest.raises(ValueError, match="Excluded runtime"):
        packager.verify(output)


def test_verifier_bounds_expanded_source_even_when_zip_is_small(tmp_path, monkeypatch):
    root = source(tmp_path / "src")
    (root / "agent_core.py").write_text("x" * 1_000_000, encoding="utf-8")
    output = tmp_path / "desktop.zip"
    packager.package(root, output)
    assert output.stat().st_size < 100_000
    monkeypatch.setattr(sys.modules["verify_desktop_source"], "MAX_TOTAL_BYTES", 100_000)
    with pytest.raises(ValueError, match="expanded size bound"):
        packager.verify(output)


def test_verifier_rejects_encrypted_flag_before_reading_payload(tmp_path):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    data = bytearray(output.read_bytes())
    # ZIP writers clear manually assigned encryption flags. Alter the owned
    # fixture's first local and central flags without encrypting any content.
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    data[local + 6] |= 1
    data[central + 8] |= 1
    output.write_bytes(data)
    with pytest.raises(ValueError, match="encrypted archive member"):
        packager.verify(output)


def test_verifier_bounds_real_member_count_before_zipfile_allocation(tmp_path, monkeypatch):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    data = bytearray(output.read_bytes())
    footer = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, footer + 8, 1, 1)
    output.write_bytes(data)
    monkeypatch.setattr(sys.modules["verify_desktop_source"], "MAX_FILES", 1)
    with pytest.raises(ValueError, match="member bound"):
        packager.verify(output)


def test_verifier_rejects_hidden_trailing_payload(tmp_path):
    output = tmp_path / "desktop.zip"
    packager.package(source(tmp_path / "src"), output)
    output.write_bytes(output.read_bytes() + b"hidden-runtime-data")
    with pytest.raises(ValueError, match="Invalid or oversized ZIP directory"):
        packager.verify(output)
