"""Firmware Publisher — upload artifacts to GitHub Pages (P3-12-001).

Publishes device-specific firmware (OTA binary + manifest.json) to
GitHub Pages under per-device paths for deterministic manifest URLs.

Path scheme:
  Generic (Factory):  /firmware/{hw}/{channel}/manifest.json
  Per-device (Prod):  /firmware/{hw}/{channel}/{device_id}/manifest.json

Two backends:
  1. Shell script (``publish_firmware_pages.sh``) — for CLI / developer use
  2. GitHub API (``GitHubPagesPublisher``) — for in-HA autonomous publishing

Ref: WORKER-PROMPT-P3-12-001, Phase B4.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .device_id import build_device_manifest_url

_LOGGER = logging.getLogger(__name__)

# GitHub Pages config
REPO_OWNER = "PVAutonomy"
REPO_NAME = "pvautonomy-firmware"
PAGES_BRANCH = "gh-pages"
CHIP_FAMILY = "ESP32"
FIRMWARE_NAME = "PVAutonomy Edge101"


@dataclass
class PublishResult:
    """Result of a firmware publish operation."""

    success: bool
    manifest_url: str | None = None
    firmware_url: str | None = None
    device_id: str | None = None
    version: str | None = None
    firmware_size: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "manifest_url": self.manifest_url,
            "firmware_url": self.firmware_url,
            "device_id": self.device_id,
            "version": self.version,
            "firmware_size": self.firmware_size,
            "error": self.error,
        }


def generate_manifest_json(
    *,
    version: str,
    firmware_md5: str,
    firmware_path: str = "firmware.ota.bin",
    chip_family: str = CHIP_FAMILY,
    name: str = FIRMWARE_NAME,
) -> str:
    """Generate ESP-Web-Tools compatible manifest.json content.

    Returns JSON string.
    """
    manifest = {
        "name": name,
        "version": version,
        "builds": [
            {
                "chipFamily": chip_family,
                "ota": {
                    "md5": firmware_md5,
                    "path": firmware_path,
                },
            }
        ],
    }
    return json.dumps(manifest, indent=2)


async def publish_device_firmware(
    hass: HomeAssistant,
    *,
    device_id: str,
    firmware_bytes: bytes,
    version: str,
    channel: str = "stable",
    hw_family: str = "edge101",
) -> PublishResult:
    """Publish device-specific firmware to GitHub Pages.

    This is the in-HA publish path. It writes the firmware binary and
    manifest.json to a local staging area and then calls the publish
    script (which handles git clone/push to gh-pages).

    For the initial MVP, this stages files locally and invokes the
    shell script. A future version may use the GitHub Contents API
    directly.

    Args:
        hass: Home Assistant instance.
        device_id: Device identifier (e.g. ``edge101_sph10k_haus_02``).
        firmware_bytes: Compiled OTA firmware binary.
        version: Firmware version string.
        channel: Release channel (stable/beta).
        hw_family: Hardware family (edge101).

    Returns:
        PublishResult with manifest URL on success.
    """
    _LOGGER.info(
        "Publishing firmware for %s v%s (%d bytes)",
        device_id, version, len(firmware_bytes),
    )

    result = PublishResult(
        success=False,
        device_id=device_id,
        version=version,
        firmware_size=len(firmware_bytes),
    )

    try:
        # Compute MD5 for manifest
        firmware_md5 = hashlib.md5(firmware_bytes).hexdigest()

        # Generate manifest
        manifest_content = generate_manifest_json(
            version=version,
            firmware_md5=firmware_md5,
        )

        # Stage files locally
        stage_dir = Path("/config/pvautonomy/publish_staging") / device_id
        stage_dir.mkdir(parents=True, exist_ok=True)

        firmware_path = stage_dir / "firmware.ota.bin"
        manifest_path = stage_dir / "manifest.json"

        await hass.async_add_executor_job(firmware_path.write_bytes, firmware_bytes)
        await hass.async_add_executor_job(manifest_path.write_text, manifest_content)

        _LOGGER.info(
            "Staged firmware for %s: %s (%d bytes, MD5: %s)",
            device_id, firmware_path, len(firmware_bytes), firmware_md5,
        )

        # Call publish script (runs in executor to avoid blocking)
        publish_ok = await hass.async_add_executor_job(
            _run_publish_script,
            str(firmware_path),
            version,
            hw_family,
            channel,
            device_id,
        )

        if publish_ok:
            result.success = True
            result.manifest_url = build_device_manifest_url(device_id, channel)
            result.firmware_url = result.manifest_url.replace(
                "manifest.json", "firmware.ota.bin"
            )
            _LOGGER.info(
                "Published successfully: %s", result.manifest_url
            )
        else:
            result.error = "Publish script failed (check logs)"
            _LOGGER.error("Publish script failed for %s", device_id)

    except Exception as exc:
        result.error = f"Publish failed: {exc}"
        _LOGGER.exception("Publish error for %s", device_id)

    return result


def _run_publish_script(
    firmware_path: str,
    version: str,
    hw_family: str,
    channel: str,
    device_id: str,
) -> bool:
    """Run the publish shell script (blocking, in executor)."""
    import subprocess

    script = Path("/config/scripts/publish_firmware_pages.sh")
    if not script.exists():
        # Try local dev path
        script = Path("scripts/publish_firmware_pages.sh")
    if not script.exists():
        _LOGGER.error("Publish script not found")
        return False

    cmd = [
        str(script),
        firmware_path,
        version,
        hw_family,
        channel,
        device_id,
    ]

    _LOGGER.info("Running publish script: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            _LOGGER.error(
                "Publish script failed (rc=%d): %s",
                proc.returncode,
                proc.stderr or proc.stdout,
            )
            return False
        _LOGGER.info("Publish script output: %s", proc.stdout[-500:])
        return True
    except subprocess.TimeoutExpired:
        _LOGGER.error("Publish script timed out")
        return False
    except Exception as exc:
        _LOGGER.exception("Publish script error: %s", exc)
        return False
