"""Artifact cache management for PVAutonomy Ops (EPIC-006-A5).

Provides bounded per-device firmware caching with atomic writes and
automatic cleanup of partial downloads.

Cache layout:
    {base}/{device_id}/{build_id}/firmware.ota.bin
    {base}/{device_id}/{build_id}/firmware.ota.bin.part  (temp)
    {base}/{device_id}/{build_id}/manifest.json          (optional)

Ref: WORK-ITEM-EPIC006-A5-OTA-ROBUSTNESS-CACHE-CLEANUP_UPDATED.md
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def ensure_cache_dir(base: Path, device_id: str, build_id: str) -> Path:
    """Create and return the cache directory for a specific build.

    Layout: {base}/{device_id}/{build_id}/
    """
    cache_dir = base / device_id / build_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_cached_firmware(
    base: Path, device_id: str, build_id: str
) -> Path | None:
    """Return path to cached firmware if it exists and is non-empty."""
    fw_path = base / device_id / build_id / "firmware.ota.bin"
    if fw_path.is_file() and fw_path.stat().st_size > 0:
        return fw_path
    return None


def write_atomic(
    dest: Path,
    data: bytes,
    expected_hash: str,
    expected_size: int,
    hash_alg: str = "sha256",
) -> None:
    """Write data atomically with hash/size verification.

    Writes to ``{dest}.part``, verifies hash and size, then renames to
    ``dest``.  On any verification failure the ``.part`` file is removed
    and a ``ValueError`` is raised.
    """
    part_path = dest.parent / (dest.name + ".part")
    part_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        part_path.write_bytes(data)

        # Verify size
        if expected_size and len(data) != expected_size:
            raise ValueError(
                f"Size mismatch: got {len(data)} bytes, "
                f"expected {expected_size} bytes"
            )

        # Verify hash
        if expected_hash:
            if hash_alg == "sha256":
                h = hashlib.sha256(data).hexdigest()
            elif hash_alg == "md5":
                h = hashlib.md5(data).hexdigest()
            else:
                h = hashlib.sha256(data).hexdigest()

            if h != expected_hash:
                raise ValueError(
                    f"{hash_alg} mismatch: computed {h[:16]}…, "
                    f"expected {expected_hash[:16]}…"
                )

        # Atomic rename
        os.replace(str(part_path), str(dest))
        _LOGGER.debug("Atomic write OK: %s (%d bytes)", dest, len(data))

    except Exception:
        # Clean up partial on any failure
        if part_path.is_file():
            part_path.unlink(missing_ok=True)
        raise


def prune_cache(base: Path, device_id: str, keep_n: int = 10) -> int:
    """Keep newest N build directories for a device, remove the rest.

    Directories are sorted by modification time (newest first).
    Returns the number of directories removed.
    """
    device_dir = base / device_id
    if not device_dir.is_dir():
        return 0

    build_dirs = sorted(
        [d for d in device_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    removed = 0
    for old_dir in build_dirs[keep_n:]:
        try:
            shutil.rmtree(old_dir)
            removed += 1
            _LOGGER.debug("Pruned cache dir: %s", old_dir)
        except OSError as exc:
            _LOGGER.warning("Failed to prune %s: %s", old_dir, exc)

    if removed:
        _LOGGER.info(
            "Cache pruned for %s: removed %d old builds (kept %d)",
            device_id, removed, keep_n,
        )

    return removed


def cleanup_partials(base: Path, max_age_minutes: int = 30) -> int:
    """Delete stale ``.part`` files and empty directories under base.

    Returns the number of partial files removed.
    """
    if not base.is_dir():
        return 0

    now = time.time()
    threshold = now - (max_age_minutes * 60)
    removed = 0

    for part_file in base.rglob("*.part"):
        try:
            if part_file.stat().st_mtime < threshold:
                part_file.unlink(missing_ok=True)
                removed += 1
                _LOGGER.debug("Removed stale partial: %s", part_file)
        except OSError as exc:
            _LOGGER.warning("Failed to remove %s: %s", part_file, exc)

    # Remove empty directories (bottom-up)
    for dirpath in sorted(base.rglob("*"), reverse=True):
        if dirpath.is_dir():
            # rmdir only succeeds if empty; non-empty is expected and ignored.
            with contextlib.suppress(OSError):
                dirpath.rmdir()

    if removed:
        _LOGGER.info(
            "Cleaned up %d stale partial file(s) from %s", removed, base
        )

    return removed
