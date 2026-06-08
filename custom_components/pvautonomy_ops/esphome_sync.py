"""ESPHome Builder YAML sync — write generated YAML to /config/esphome/.

Ensures that PVAutonomy-managed device configs appear in ESPHome Builder
after wizard setup. Supports archive (never delete) for safety.

Safety invariant: Only operates on files matching MANAGED_FILE_PATTERN.
Never deletes user-created ESPHome files.

Ref: WORKER-PROMPT-ESPHome-BUILDER-YAML-SYNC.md
"""

from __future__ import annotations

import fnmatch
import logging
import shutil
import time
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import ESPHOME_CONFIG_DIR

_LOGGER = logging.getLogger(__name__)

# Safety: only PVAutonomy-managed files match this glob
MANAGED_FILE_PATTERN = "edge101-*.device.yaml"
ARCHIVE_SUBDIR = "archive"
# Header comment injected into managed YAML files (machine-detectable)
MANAGED_HEADER = "# PVAutonomy Managed — do not edit manually\n"


def is_pvautonomy_managed(filepath: Path) -> bool:
    """Check if a YAML file was created by PVAutonomy (contains MANAGED_HEADER).

    Safe to call on any file; returns False if file doesn't exist or can't be read.
    """
    try:
        if not filepath.is_file():
            return False
        with filepath.open("r", encoding="utf-8") as f:
            first_line = f.readline()
        return first_line.strip() == MANAGED_HEADER.strip()
    except OSError:
        return False


async def async_write_esphome_yaml(
    hass: HomeAssistant,
    *,
    filename: str,
    yaml_text: str,
) -> str:
    """Write device YAML to /config/esphome/ (appears in ESPHome Builder).

    Args:
        hass: Home Assistant instance (for executor scheduling).
        filename: Target filename (e.g., "edge101-mic600-garage-06.device.yaml").
        yaml_text: Complete ESPHome YAML content.

    Returns:
        Absolute path of the written file.

    Raises:
        ValueError: If filename does not match the managed file pattern.
        OSError: If the write fails (permissions, disk full, etc.).
    """
    if not fnmatch.fnmatch(filename, MANAGED_FILE_PATTERN):
        raise ValueError(
            f"Filename {filename!r} does not match {MANAGED_FILE_PATTERN!r} — "
            f"refusing to write (safety gate)"
        )

    target = Path(ESPHOME_CONFIG_DIR) / filename

    # Prepend managed header if not already present
    if not yaml_text.startswith(MANAGED_HEADER.strip()):
        yaml_text = MANAGED_HEADER + yaml_text

    def _write_sync() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml_text, encoding="utf-8")
        _LOGGER.debug(
            "Wrote ESPHome YAML: %s (%d bytes)", target, len(yaml_text)
        )
        return str(target)

    path = await hass.async_add_executor_job(_write_sync)
    _LOGGER.info("ESPHome YAML synced: %s", path)
    return path


async def async_archive_esphome_yaml(
    hass: HomeAssistant,
    *,
    filename: str,
    reason: str = "relocate",
) -> str | None:
    """Move a PVAutonomy-managed YAML file to /config/esphome/archive/.

    Safety: refuses to archive files that do not match MANAGED_FILE_PATTERN
    or do not contain MANAGED_HEADER.

    Args:
        hass: Home Assistant instance.
        filename: Filename to archive (e.g., "edge101-mic600-garage-06.device.yaml").
        reason: Archive reason for logging (e.g., "relocate", "device_removed").

    Returns:
        Archive path if moved, None if file did not exist or was not PVA-managed.
    """
    source = Path(ESPHOME_CONFIG_DIR) / filename

    def _archive_sync() -> str | None:
        if not source.is_file():
            _LOGGER.debug("Archive skipped — file not found: %s", source)
            return None

        if not fnmatch.fnmatch(filename, MANAGED_FILE_PATTERN):
            _LOGGER.warning(
                "Archive refused — filename %r does not match managed pattern",
                filename,
            )
            return None

        if not is_pvautonomy_managed(source):
            _LOGGER.warning(
                "Archive refused — %s is not PVAutonomy-managed (missing header)",
                source,
            )
            return None

        archive_dir = Path(ESPHOME_CONFIG_DIR) / ARCHIVE_SUBDIR
        archive_dir.mkdir(parents=True, exist_ok=True)

        archive_target = archive_dir / filename
        if archive_target.exists():
            # Collision: append timestamp suffix
            stem = archive_target.stem
            suffix = archive_target.suffix
            archive_target = archive_dir / f"{stem}_{int(time.time())}{suffix}"

        shutil.move(str(source), str(archive_target))
        _LOGGER.info(
            "Archived ESPHome YAML: %s → %s (reason: %s)",
            source,
            archive_target,
            reason,
        )
        return str(archive_target)

    return await hass.async_add_executor_job(_archive_sync)
