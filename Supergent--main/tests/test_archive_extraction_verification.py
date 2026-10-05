"""Benign, isolated negative PoCs for the scanner's archive-traversal claim."""
import io
import stat
import tarfile
import zipfile
from pathlib import Path
import pytest
from core.tool_installer import ToolInstaller


@pytest.mark.parametrize("name", ["../escaped.txt", "..\\escaped.txt", "/escaped.txt", "nested/../../escaped.txt"])
def test_zip_escape_rejected_before_any_member_is_extracted(tmp_path, name):
    archive = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr("safe.txt", "innocent member")
        file.writestr(name, "synthetic marker")
    into = tmp_path / "extraction"
    with pytest.raises(ValueError, match="escapes"):
        object.__new__(ToolInstaller)._extract_archive(archive, into, "safe.txt")
    assert list(into.iterdir()) == []
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_symlink_metadata_rejected(tmp_path):
    archive = tmp_path / "crafted.zip"
    member = zipfile.ZipInfo("shortcut")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr(member, "../outside")
    with pytest.raises(ValueError, match="symlinks"):
        object.__new__(ToolInstaller)._extract_archive(archive, tmp_path / "out", "shortcut")


def test_tar_data_filter_rejects_outside_path(tmp_path):
    archive = tmp_path / "crafted.tar"
    payload = b"synthetic marker"
    with tarfile.open(archive, "w") as file:
        member = tarfile.TarInfo("../escaped.txt")
        member.size = len(payload)
        file.addfile(member, io.BytesIO(payload))
    with pytest.raises(tarfile.FilterError):
        object.__new__(ToolInstaller)._extract_archive(archive, tmp_path / "out", "escaped.txt")
    assert not (tmp_path / "escaped.txt").exists()


def test_regular_zip_is_usable_after_validation(tmp_path):
    archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr("nested/tool.exe", "synthetic executable marker; never launched")
    result = object.__new__(ToolInstaller)._extract_archive(archive, tmp_path / "out", "tool.exe")
    assert result == tmp_path / "out/nested/tool.exe"
    assert result.read_text().startswith("synthetic")
