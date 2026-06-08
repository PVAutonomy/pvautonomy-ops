"""Firmware Artifact Management — Factory Reset Path Only.

EPIC-015 P3-06: This module is the SOLE live caller for factory-reset firmware
downloads.  The Flash button (push OTA) uses pipeline.py + build_backend.py
instead.  Do NOT add new callers without reviewing the verification contract.

Live consumer: lifecycle.py:factory_reset_device → download_artifact → verify_artifact
Contract: caller MUST call verify_artifact() before OTA use (fail-closed).

Also provides ESP-Web-Tools compatible manifest generation for Factory Managed Pull
(factory_installer.py) and get_latest_version() for version resolution.
"""
import asyncio
import hashlib
import logging
import json
from typing import Any
from pathlib import Path

import aiohttp

from .const import ARTIFACTS_BASE_URL

_LOGGER = logging.getLogger(__name__)


class ArtifactError(Exception):
    """Artifact download or verification failed."""
    pass


class FirmwareArtifact:
    """Firmware artifact with manifest and binary."""
    
    def __init__(self, manifest: dict[str, Any], firmware_path: Path):
        """Initialize firmware artifact.
        
        Args:
            manifest: Parsed manifest.json
            firmware_path: Path to downloaded firmware.bin
        """
        self.manifest = manifest
        self.firmware_path = firmware_path
        
    @property
    def version(self) -> str:
        """Firmware version from manifest."""
        return self.manifest.get("version", "unknown")
    
    @property
    def channel(self) -> str:
        """Release channel (stable|beta|dev)."""
        return self.manifest.get("channel", "unknown")
    
    @property
    def hw_family(self) -> str:
        """Hardware family (edge101, etc.)."""
        return self.manifest.get("hw_family", "unknown")
    
    @property
    def sha256(self) -> str:
        """Expected SHA256 checksum."""
        return self.manifest.get("sha256", "")
    
    @property
    def esphome_min(self) -> str:
        """Minimum ESPHome version required."""
        return self.manifest.get("esphome_min", "0.0.0")


async def download_artifact(
    version: str,
    hw_family: str,
    temp_dir: Path,
    channel: str = "stable",
    owner: str | None = None,
    repo: str | None = None,
) -> FirmwareArtifact:
    """Download firmware artifact from GitHub Releases.
    
    Args:
        version: Firmware version (e.g., "1.0.3")
        hw_family: Hardware family (e.g., "edge101")
        temp_dir: Temporary directory for downloads
        channel: Release channel (stable|beta|dev)
        owner: GitHub owner (overrides const.py default)
        repo: GitHub repo (overrides const.py default)
        
    Returns:
        FirmwareArtifact with validated manifest and firmware
        
    Raises:
        ArtifactError: If download or verification fails
    """
    _LOGGER.info(
        "Downloading firmware artifact: version=%s, hw_family=%s, channel=%s",
        version,
        hw_family,
        channel
    )
    
    # Construct artifact URLs (MVP flat pattern)
    # Pattern: {base_url}/v{version}/{file}
    # GitHub Releases serve assets at root level (no hw_family subdirectory)
    # If owner/repo provided, construct dynamic base_url; else use const default
    if owner and repo:
        effective_base_url = f"https://github.com/{owner}/{repo}/releases/download"
    else:
        effective_base_url = ARTIFACTS_BASE_URL
    base_url = f"{effective_base_url}/v{version}"
    manifest_url = f"{base_url}/manifest.json"
    firmware_url = f"{base_url}/firmware.bin"
    
    _LOGGER.debug(
        "Artifact URLs (MVP flat): manifest=%s, firmware=%s",
        manifest_url,
        firmware_url
    )
    
    try:
        # aiohttp follows redirects by default (allow_redirects=True)
        # GitHub Releases return 302 → release-assets CDN URL
        async with aiohttp.ClientSession() as session:
            # Download manifest.json
            _LOGGER.debug("Downloading manifest: %s", manifest_url)
            async with session.get(
                manifest_url,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True  # Explicit: follow GitHub 302 redirects
            ) as resp:
                if resp.status != 200:
                    raise ArtifactError(
                        f"Manifest download failed: {resp.status} {resp.reason}"
                    )
                manifest_data = await resp.text()
                manifest = json.loads(manifest_data)
            
            # Validate manifest schema
            required_fields = ["version", "channel", "hw_family", "sha256"]
            missing = [f for f in required_fields if f not in manifest]
            if missing:
                raise ArtifactError(
                    f"Invalid manifest: missing fields {missing}"
                )
            
            # HARD REQUIREMENT: hw_family must match (safety check)
            manifest_hw_family = manifest.get("hw_family")
            if manifest_hw_family != hw_family:
                raise ArtifactError(
                    f"Hardware family mismatch: expected '{hw_family}', "
                    f"manifest declares '{manifest_hw_family}'"
                )
            
            # Download firmware.bin
            firmware_path = temp_dir / "firmware.bin"
            _LOGGER.debug("Downloading firmware: %s → %s", firmware_url, firmware_path)
            
            async with session.get(
                firmware_url,
                timeout=aiohttp.ClientTimeout(total=180),
                allow_redirects=True  # Explicit: follow GitHub 302 redirects
            ) as resp:
                if resp.status != 200:
                    raise ArtifactError(
                        f"Firmware download failed: {resp.status} {resp.reason}"
                    )
                
                firmware_data = await resp.read()
                await asyncio.get_event_loop().run_in_executor(
                    None, firmware_path.write_bytes, firmware_data
                )
            
            _LOGGER.info(
                "Downloaded firmware: %d bytes → %s",
                len(firmware_data),
                firmware_path
            )
            
            # Create artifact
            artifact = FirmwareArtifact(manifest, firmware_path)
            
            return artifact
    
    except aiohttp.ClientError as e:
        raise ArtifactError(f"Network error during download: {e}") from e
    except json.JSONDecodeError as e:
        raise ArtifactError(f"Invalid manifest JSON: {e}") from e
    except OSError as e:
        raise ArtifactError(f"File I/O error: {e}") from e


async def verify_artifact(artifact: FirmwareArtifact) -> bool:
    """Verify firmware artifact integrity (SHA256).
    
    Args:
        artifact: Firmware artifact to verify
        
    Returns:
        bool: True if checksum matches
        
    Raises:
        ArtifactError: If verification fails
    """
    _LOGGER.info("Verifying firmware integrity: %s", artifact.firmware_path)
    
    try:
        # Compute SHA256 of downloaded firmware (in executor to avoid blocking)
        def _compute_sha256(path: Path) -> str:
            sha256 = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            return sha256.hexdigest()
        
        calculated = await asyncio.get_event_loop().run_in_executor(
            None, _compute_sha256, artifact.firmware_path
        )
        expected = artifact.sha256
        
        if calculated != expected:
            _LOGGER.error(
                "SHA256 mismatch! Expected: %s, Got: %s",
                expected,
                calculated
            )
            raise ArtifactError(
                "Firmware integrity check failed: SHA256 mismatch"
            )
        
        _LOGGER.info("Firmware integrity verified: SHA256 OK")
        return True
    
    except OSError as e:
        raise ArtifactError(f"Cannot read firmware file: {e}") from e


def get_latest_version(hw_family: str, channel: str = "stable") -> str:
    """Get latest firmware version for hardware family.
    
    Args:
        hw_family: Hardware family (e.g., "edge101")
        channel: Release channel (stable|beta|dev)
        
    Returns:
        str: Latest version (e.g., "1.0.3")
        
    Note:
        MVP implementation returns hardcoded version.
        TODO: Query GitHub API for latest release.
    """
    # MVP: Hardcoded version (will be replaced with GitHub API query)
    _LOGGER.warning(
        "Using hardcoded firmware version (MVP) - TODO: implement GitHub API query"
    )
    return "1.0.4"  # TODO: Query GitHub Releases API


def generate_esphome_manifest(
    name: str,
    version: str,
    firmware_md5: str,
    firmware_filename: str = "firmware.ota.bin",
    chip_family: str = "ESP32",
) -> dict[str, Any]:
    """Generate ESP-Web-Tools compatible manifest for ESPHome managed updates.

    ESPHome's `update: platform: http_request` requires this specific manifest
    format (ESP-Web-Tools / Improv schema). It is different from PVAutonomy's
    own push-OTA manifest (sha256/channel/hw_family).

    Format:
        {
          "name": "PVAutonomy Edge101",
          "version": "1.0.4",
          "builds": [{
            "chipFamily": "ESP32",
            "ota": {
              "md5": "<md5hex>",
              "path": "firmware.ota.bin"
            }
          }]
        }

    Args:
        name: Firmware display name
        version: Firmware version string
        firmware_md5: MD5 hex digest of the firmware.ota.bin file
        firmware_filename: Filename of the OTA binary (relative to manifest URL)
        chip_family: ESP chip family ("ESP32", "ESP32-S3", etc.)

    Returns:
        Dict ready for json.dumps()
    """
    manifest = {
        "name": name,
        "version": version,
        "builds": [
            {
                "chipFamily": chip_family,
                "ota": {
                    "md5": firmware_md5,
                    "path": firmware_filename,
                },
            }
        ],
    }
    _LOGGER.debug("Generated ESP-Web-Tools manifest: %s", json.dumps(manifest))
    return manifest
