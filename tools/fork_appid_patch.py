#!/usr/bin/env python3
"""Rewrite the applicationId baked into GameNative's prebuilt native libraries.

Some prebuilt binaries shipped in this repository hardcode ``app.gamenative`` - the
official applicationId, and with it the private data directory the app runs from - and
offer no way to override it at runtime. A build with a different applicationId, e.g. a
fork meant to be installed next to the official app, would keep pointing at the official
package's directory.

This script rewrites those strings in place. The replacement applicationId must not be
longer than ``app.gamenative``, because the strings live in read-only ELF sections that
cannot grow. An id of exactly the same length is replaced byte for byte; a shorter one
shifts the rest of the affected C string left and pads the tail with NUL bytes, which C
string handling ignores.

Usage:
    tools/fork_appid_patch.py app.gnfork.dev
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from pathlib import Path

OFFICIAL_ID = "app.gamenative"

# Plain ELF files, patched in place.
RAW_TARGETS = [
    "app/src/main/jniLibs/arm64-v8a/libevshim.so",
    "app/src/main/jniLibs/arm64-v8a/libkgslshim.so",
    "app/src/modern/assets/libredirect-bionic-wx.so",
]

# zstd-compressed tarballs whose members are patched and the archive repacked.
TAR_TARGETS = [
    "app/src/main/assets/redirect.tzst",
]


def patch_bytes(data: bytes, new_id: str) -> tuple[bytes, int]:
    """Replace the applicationId inside every C string that mentions it."""
    old_pkg = OFFICIAL_ID.encode()
    new_pkg = new_id.encode()
    buf = bytearray(data)
    count = 0
    start = 0
    while True:
        hit = buf.find(old_pkg, start)
        if hit < 0:
            break
        # Rewrite the whole NUL-terminated string the match belongs to, so that a
        # shorter id can shift its tail left without moving neighbouring strings.
        begin = buf.rfind(b"\x00", 0, hit) + 1
        end = buf.find(b"\x00", hit)
        if end < 0:
            raise ValueError(f"unterminated string at offset {hit}")
        original = bytes(buf[begin:end])
        replacement = original.replace(old_pkg, new_pkg)
        padding = len(original) - len(replacement)
        buf[begin:end] = replacement + b"\x00" * padding
        count += original.count(old_pkg)
        start = begin + len(replacement)
    return bytes(buf), count


def patch_raw(path: Path, new_id: str) -> int:
    data = path.read_bytes()
    patched, count = patch_bytes(data, new_id)
    if count:
        assert len(patched) == len(data), "patched file changed size"
        path.write_bytes(patched)
    return count


def zstd_decompress(path: Path) -> bytes:
    try:
        import zstandard
    except ImportError:
        return subprocess.run(
            ["zstd", "-dc", str(path)], check=True, stdout=subprocess.PIPE
        ).stdout
    with path.open("rb") as fh:
        return zstandard.ZstdDecompressor().stream_reader(fh).read()


def zstd_compress(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError:
        return subprocess.run(
            ["zstd", "-19", "-c"], check=True, input=data, stdout=subprocess.PIPE
        ).stdout
    return zstandard.ZstdCompressor(level=19).compress(data)


def patch_tar(path: Path, new_id: str) -> int:
    raw = zstd_decompress(path)

    src = tarfile.open(fileobj=io.BytesIO(raw))
    out = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=out, mode="w", format=src.format) as dst:
        for member in src.getmembers():
            if not member.isfile():
                dst.addfile(member)
                continue
            payload = src.extractfile(member).read()
            payload, hits = patch_bytes(payload, new_id)
            count += hits
            member.size = len(payload)
            dst.addfile(member, io.BytesIO(payload))
    src.close()

    if count:
        path.write_bytes(zstd_compress(out.getvalue()))
    return count


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    new_id = argv[1].strip()
    if not new_id or " " in new_id or "." not in new_id:
        print(f"error: '{new_id}' is not a usable applicationId", file=sys.stderr)
        return 2
    if len(new_id) > len(OFFICIAL_ID):
        print(
            f"error: '{new_id}' is longer than '{OFFICIAL_ID}'; the prebuilt libraries "
            "cannot be patched for it",
            file=sys.stderr,
        )
        return 2

    root = Path(__file__).resolve().parent.parent
    total = 0
    for rel in RAW_TARGETS:
        path = root / rel
        if not path.exists():
            print(f"skip {rel} (not present)")
            continue
        hits = patch_raw(path, new_id)
        total += hits
        print(f"{rel}: {hits} reference(s) rewritten")

    for rel in TAR_TARGETS:
        path = root / rel
        if not path.exists():
            print(f"skip {rel} (not present)")
            continue
        hits = patch_tar(path, new_id)
        total += hits
        print(f"{rel}: {hits} reference(s) rewritten")

    print(f"total: {total} reference(s) now point at {new_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
