"""Device ID scheme for per-device firmware pipeline (P3-12-001).

Deterministic device_id computation from setup inputs.
Ref: WORKER-PROMPT-P3-12-001, Phase A1.

Device ID scheme:
    device_id = edge101_{model}_{site}_{nn}
    Example:   edge101_sph10k_haus_02

Node name (ESPHome):
    node_name = {model}-{site}-{nn}
    Example:   sph10k-haus-02

Artifact paths (GitHub Pages):
    /firmware/edge101/{channel}/{device_id}/manifest.json
    /firmware/edge101/{channel}/{device_id}/firmware.ota.bin
"""

from __future__ import annotations

import logging
import re

from .const import ARTIFACTS_PAGES_BASE_URL, DEVICE_HW_PREFIX

_LOGGER = logging.getLogger(__name__)


def compute_device_id(
    model: str,
    site: str,
    number: str | int,
) -> str:
    """Compute deterministic device_id from setup inputs.

    Args:
        model: Inverter model slug (e.g. "sph10k", "mic600", "mid15k")
        site: Installation location (e.g. "haus", "garage", "werkstatt")
        number: Device number at site (e.g. "01", "02", 1, 2)

    Returns:
        device_id like "edge101_sph10k_haus_02"
    """
    model_clean = _slugify(model)
    site_clean = _slugify(site)
    nn = str(number).zfill(2)

    device_id = f"{DEVICE_HW_PREFIX}_{model_clean}_{site_clean}_{nn}"
    _LOGGER.debug("Computed device_id: %s (model=%s, site=%s, nn=%s)",
                  device_id, model, site, nn)
    return device_id


def compute_node_name(
    model: str,
    site: str,
    number: str | int,
) -> str:
    """Compute ESPHome node name from setup inputs.

    This is the value used for `esphome: name:` in the device YAML.
    Uses hyphens (ESPHome convention), not underscores.

    Returns:
        node_name like "sph10k-haus-02"
    """
    model_clean = _slugify(model).replace("_", "-")
    site_clean = _slugify(site).replace("_", "-")
    nn = str(number).zfill(2)

    return f"{model_clean}-{site_clean}-{nn}"


def friendly_name_from_slug(slug: str) -> str:
    """Derive deterministic friendly_name from device slug (ADR-003, EPIC-011).

    The friendly_name controls the HA entity ID prefix via ESPHome.
    It MUST be derived solely from the slug — no free-form user input.

    Example: "sph10k-haus-02" → "Sph10K Haus 02"
    """
    return slug.replace("-", " ").title()


def compute_esphome_device_yaml_name(
    model: str,
    site: str,
    number: str | int,
) -> str:
    """Compute the expected ESPHome device YAML filename.

    Returns:
        filename like "edge101-sph10k-haus-02.device.yaml"
    """
    node_name = compute_node_name(model, site, number)
    return f"{DEVICE_HW_PREFIX}-{node_name}.device.yaml"


def build_device_manifest_url(
    device_id: str,
    channel: str = "stable",
    hw_family: str = "edge101",
) -> str:
    """Build per-device manifest URL on GitHub Pages.

    Returns:
        URL like "https://pvautonomy.github.io/pvautonomy-firmware/firmware/edge101/stable/edge101_sph10k_haus_02/manifest.json"
    """
    url = (
        f"{ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}"
        f"/{channel}/{device_id}/manifest.json"
    )
    _LOGGER.info("Built per-device manifest URL: %s", url)
    return url


def build_factory_firmware_url(
    hw_family: str = "edge101",
) -> str:
    """Build URL for pre-built Factory firmware binary (for Factory Reset).

    Returns:
        URL like "https://pvautonomy.github.io/pvautonomy-firmware/firmware/edge101/factory/firmware.ota.bin"
    """
    return (
        f"{ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}"
        f"/factory/firmware.ota.bin"
    )


def parse_device_id(device_id: str) -> dict[str, str] | None:
    """Parse a device_id back into components.

    Args:
        device_id: e.g. "edge101_sph10k_haus_02"

    Returns:
        {"hw_prefix": "edge101", "model": "sph10k", "site": "haus", "number": "02"}
        or None if format doesn't match.
    """
    parts = device_id.split("_")
    if len(parts) < 4 or parts[0] != DEVICE_HW_PREFIX:
        _LOGGER.warning("Cannot parse device_id: %s", device_id)
        return None

    return {
        "hw_prefix": parts[0],
        "model": parts[1],
        "site": parts[2],
        "number": parts[3],
    }


def _slugify(text: str) -> str:
    """Slugify text for use in device IDs.

    Lowercases, replaces hyphens/spaces with underscores,
    strips non-alphanumeric characters.
    """
    text = text.lower().strip()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    return text
