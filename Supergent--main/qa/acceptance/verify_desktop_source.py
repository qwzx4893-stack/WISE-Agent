"""Bounded, read-only verification of LOCAL source ZIPs; never extract or run.

Internal checksums are not authentication. Supply an independently retained
archive hash to check a sealed artifact, rather than a replaceable manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

MANIFEST = "DESKTOP_SOURCE_MANIFEST.json"
SCHEMA = "wise.desktop-source.v2"
SCOPE = "SOURCE_ONLY_NOT_STANDALONE_INSTALLER"
MAX_FILES = 50_000
MAX_FILE_BYTES = 16_000_000
MAX_MANIFEST_BYTES = 16_000_000
MAX_TOTAL_BYTES = 512_000_000
MAX_CENTRAL_BYTES = 32_000_000
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:[ .]|$)", re.I)


def safe_path(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name or ":" in name:
        raise ValueError("Unsafe archive path")
    parts = name.split("/")
    if PurePosixPath(name).is_absolute() or any(
        p in {"", ".", ".."} or p != p.strip() or p.endswith(".") or DEVICE_NAME.match(p)
        or any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts
    ):
        raise ValueError("Unsafe archive path: " + name)
    return name


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def content_digest(records: list[dict]) -> str:
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def archive_digest(stream) -> str:
    position = stream.tell()
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
    stream.seek(position)
    return digest.hexdigest()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest JSON key: " + key)
        result[key] = value
    return result


def bounded_directory(stream) -> None:
    """Bound the central directory before ZipFile allocates member objects.

    Our size/count bounds never require ZIP64 or multipart archives. Reject
    those formats, prefix wrappers and trailing data rather than guessing.
    """
    stream.seek(0, 2)
    size = stream.tell()
    if size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
        raise ValueError("Invalid or oversized source archive")
    stream.seek(max(0, size - 65_557))
    tail = stream.read(65_557)
    footer = tail.rfind(b"PK\x05\x06")
    if footer < 0 or len(tail) - footer < 22:
        raise ValueError("Missing bounded ZIP directory")
    signature, disk, cd_disk, disk_count, count, cd_bytes, cd_offset, comment_bytes = struct.unpack_from("<4s4H2LH", tail, footer)
    footer_offset = size - len(tail) + footer
    if disk or cd_disk or disk_count != count or count == 65_535 or cd_bytes == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise ValueError("ZIP64 or multipart source archive is unsupported")
    if count > MAX_FILES + 1:
        raise ValueError("Archive member bound exceeded")
    if cd_bytes > MAX_CENTRAL_BYTES or cd_offset + cd_bytes != footer_offset or footer + 22 + comment_bytes != len(tail):
        raise ValueError("Invalid or oversized ZIP directory")
    stream.seek(0)
    if stream.read(4) != b"PK\x03\x04":
        raise ValueError("Source ZIP wrappers are unsupported")
    stream.seek(cd_offset)
    actual_count = 0
    while stream.tell() < footer_offset:
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise ValueError("Invalid ZIP directory entry")
        name_bytes, extra_bytes, member_comment_bytes = struct.unpack_from("<3H", header, 28)
        stream.seek(name_bytes + extra_bytes + member_comment_bytes, 1)
        actual_count += 1
        if actual_count > MAX_FILES + 1:
            raise ValueError("Archive member bound exceeded")
    if stream.tell() != footer_offset or actual_count != count:
        raise ValueError("ZIP directory count/size mismatch")
    stream.seek(0)


def verify(output: Path, expected_sha256: str | None = None, *, required=None, allow=None) -> dict:
    # Import lazily so the packager can share this verifier without a cycle.
    if required is None or allow is None:
        from package_desktop_source import REQUIRED, allowed
        required, allow = REQUIRED, allowed

    if linked(output) or not output.is_file() or output.stat().st_size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
        raise ValueError("Invalid or oversized source archive")
    # Hash and inspect the same open file, so replacing the pathname cannot
    # separate provenance validation from the archive we actually inspect.
    with output.open("rb") as sealed:
        bounded_directory(sealed)
        return _verify_open(output, sealed, expected_sha256, required, allow)


def _verify_open(output, sealed, expected_sha256, required, allow):
    with zipfile.ZipFile(sealed) as archive:
        archive_hash = archive_digest(sealed)
        if expected_sha256 is not None and (not SHA256.fullmatch(expected_sha256) or archive_hash != expected_sha256):
            raise ValueError("Sealed archive checksum mismatch")
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 1:
            raise ValueError("Archive member bound exceeded")
        seen = set()
        total = 0
        for info in infos:
            # ZipInfo silently normalizes Windows backslashes and truncates NUL;
            # inspect the originally decoded name before trusting its alias.
            name = safe_path(info.orig_filename)
            if name != info.filename:
                raise ValueError("Unsafe archive path alias: " + name)
            if name.casefold() in seen:
                raise ValueError("Duplicate archive path: " + name)
            seen.add(name.casefold())
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or info.flag_bits & 1:
                raise ValueError("Non-regular or encrypted archive member: " + name)
            if name != MANIFEST and not allow(name):
                raise ValueError("Excluded runtime or unsupported archive path: " + name)
            bound = MAX_MANIFEST_BYTES if name == MANIFEST else MAX_FILE_BYTES
            if info.file_size > bound:
                raise ValueError("Archive member size bound exceeded: " + name)
            if name != MANIFEST:
                total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Archive expanded size bound exceeded")
        if MANIFEST not in archive.namelist():
            raise ValueError("Missing source manifest")
        manifest = json.loads(archive.read(MANIFEST), object_pairs_hook=_json_object)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or manifest.get("scope") != SCOPE or manifest.get("runtime_state_included") is not False or manifest.get("redistribution_ready") is not False:
            raise ValueError("Invalid source-only manifest contract")
        records = manifest.get("files")
        if not isinstance(records, list) or len(records) > MAX_FILES:
            raise ValueError("Invalid manifest file records")
        names = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
                raise ValueError("Invalid manifest record")
            name = safe_path(record["path"])
            if name == MANIFEST or name.casefold() in names or not allow(name):
                raise ValueError("Duplicate or excluded manifest path: " + name)
            names.add(name.casefold())
            if not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"]) or type(record["bytes"]) is not int or not 0 <= record["bytes"] <= MAX_FILE_BYTES:
                raise ValueError("Invalid manifest checksum or size")
        if {r["path"] for r in records} != set(archive.namelist()) - {MANIFEST}:
            raise ValueError("Manifest and archive membership differ")
        if records != sorted(records, key=lambda r: r["path"]) or manifest.get("content_sha256") != content_digest(records):
            raise ValueError("Manifest content checksum mismatch")
        missing = set(required) - {r["path"] for r in records}
        if missing:
            raise ValueError("Incomplete desktop bundle: " + ", ".join(sorted(missing)))
        for record in records:
            digest = hashlib.sha256()
            count = 0
            with archive.open(record["path"]) as member:
                while chunk := member.read(64 * 1024):
                    count += len(chunk)
                    if count > MAX_FILE_BYTES:
                        raise ValueError("Expanded archive member exceeds bound")
                    digest.update(chunk)
            if count != record["bytes"] or digest.hexdigest() != record["sha256"]:
                raise ValueError("Source file checksum mismatch: " + record["path"])
        stamp = manifest.get("source_stamp")
        if stamp is not None and (not isinstance(stamp, str) or not SHA256.fullmatch(stamp)):
            raise ValueError("Invalid source stamp")
        warnings = manifest.get("warnings")
        if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
            raise ValueError("Invalid manifest warnings")
        if archive_digest(sealed) != archive_hash:
            raise ValueError("Archive changed during verification")
    return {"archive": str(output.resolve()), "archive_sha256": archive_hash, "files": len(records), "source_stamp": stamp, "scope": SCOPE,
            "provenance": "external-sealed-hash-matched" if expected_sha256 else "internal-consistency-only",
            "redistribution_ready": False, "warnings": warnings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", help="Independently retained checksum of the sealed archive")
    args = parser.parse_args()
    print(json.dumps(verify(args.archive, args.sha256), ensure_ascii=False))
