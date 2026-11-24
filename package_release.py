#!/usr/bin/env python3
"""
package_release.py — build a portable FOXAI Time Machine ZIP.

Works on Windows, macOS, and Linux. Explicitly encodes UNIX executable
permissions (0755) in ZIP metadata for the Mac/Linux one-click starters so
double-click works after unzip on those platforms — even when this script
runs on Windows (where the filesystem has no executable bit).

Does not change application behavior; packaging only.
"""
from __future__ import annotations

import fnmatch
import os
import stat
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT_NAME = "foxai-timemachine"
DEFAULT_ZIP_NAME = "FOXAI-Time-Machine.zip"

# Always mark these as UNIX 0755 in the ZIP (path relative to project root).
EXECUTABLE_FILES = {
    "Start Time Machine.command",
    "start-timemachine.sh",
}

# Directory names to skip entirely (any depth).
EXCLUDE_DIR_NAMES = {
    "state",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "cache_data",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
}

# Exact relative paths (posix) to skip.
EXCLUDE_REL_PATHS = {
    "config.local.json",
}

# Filename / glob patterns to skip (basename match).
EXCLUDE_NAME_GLOBS = (
    "*.db",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.log",
    ".DS_Store",
    "Thumbs.db",
    "*.zip",
)


def _posix_rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def should_exclude(path: Path) -> bool:
    """True if this file or directory must not enter the release ZIP."""
    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return True

    parts = rel.parts
    if any(part in EXCLUDE_DIR_NAMES for part in parts):
        return True

    rel_posix = rel.as_posix()
    if rel_posix in EXCLUDE_REL_PATHS:
        return True

    name = path.name
    for pat in EXCLUDE_NAME_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True

    return False


def iter_release_files():
    """Yield project files that belong in the portable ZIP."""
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        root = Path(dirpath)
        # Prune excluded directories in-place so os.walk does not descend.
        dirnames[:] = sorted(
            d for d in dirnames if not should_exclude(root / d)
        )
        for name in sorted(filenames):
            path = root / name
            if should_exclude(path):
                continue
            if not path.is_file():
                continue
            yield path


def zip_external_attr(mode: int) -> int:
    """Info-ZIP external_attr for a regular file with the given UNIX mode."""
    return (stat.S_IFREG | (mode & 0o7777)) << 16


def add_file(zf: zipfile.ZipFile, path: Path, arcname: str, mode: int) -> None:
    data = path.read_bytes()
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # UNIX — required for external_attr mode bits
    info.external_attr = zip_external_attr(mode)
    # Preserve a reasonable timestamp from the source file.
    mtime = path.stat().st_mtime
    info.date_time = datetime.fromtimestamp(mtime, tz=timezone.utc).timetuple()[:6]
    zf.writestr(info, data)


def build_zip(dest: Path) -> tuple[Path, list[str]]:
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    written: list[str] = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in iter_release_files():
            rel = _posix_rel(path)
            arcname = f"{ARCHIVE_ROOT_NAME}/{rel}"
            mode = 0o755 if rel in EXECUTABLE_FILES else 0o644
            add_file(zf, path, arcname, mode)
            written.append(rel)

    return dest, written


def verify_zip(zip_path: Path) -> None:
    """Fail loudly if executable bits or junk exclusions are wrong."""
    problems: list[str] = []
    junk_dir_names = {"__pycache__", ".pytest_cache", "cache_data"}
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        for name in names:
            parts = name.split("/")
            if junk_dir_names.intersection(parts):
                problems.append(f"junk path included: {name}")
            if name.endswith((".pyc", ".pyo", ".log")):
                problems.append(f"junk file included: {name}")

        for rel in EXECUTABLE_FILES:
            arc = f"{ARCHIVE_ROOT_NAME}/{rel}"
            if arc not in names:
                problems.append(f"missing launcher: {arc}")
                continue
            info = zf.getinfo(arc)
            mode = (info.external_attr >> 16) & 0o7777
            perms = mode & 0o777
            if info.create_system != 3:
                problems.append(f"{rel}: create_system={info.create_system} (want 3=UNIX)")
            if perms != 0o755:
                problems.append(f"{rel}: mode={oct(perms)} (want 0o755)")
            if not (mode & 0o111):
                problems.append(f"{rel}: execute bits not set")

    if problems:
        raise SystemExit("ZIP verification failed:\n  - " + "\n  - ".join(problems))


def main() -> int:
    out = PROJECT_ROOT.parent / DEFAULT_ZIP_NAME
    if len(sys.argv) > 1:
        out = Path(sys.argv[1]).expanduser().resolve()

    print("FOXAI Time Machine - release packager")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Output:  {out}")
    print()

    dest, written = build_zip(out)
    verify_zip(dest)

    exec_note = ", ".join(sorted(EXECUTABLE_FILES))
    print(f"Wrote {len(written)} files -> {dest}")
    print(f"Size:   {dest.stat().st_size:,} bytes")
    print(f"UNIX 0755 encoded for: {exec_note}")
    print("Excluded: __pycache__, *.pyc, .pytest_cache, cache_data, *.log, config.local.json, ...")
    print("Verified OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
