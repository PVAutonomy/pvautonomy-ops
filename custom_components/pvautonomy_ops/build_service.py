"""Build-only firmware service helper (EPIC-004).

SPEC: project-docs/PLANNING/active/specs/features/epic004-build-firmware-service/spec.md

This module backs the ``pvautonomy_ops.build_firmware`` service. It resolves
the target device metadata read-only from a single config entry, replays the
persisted build intent, runs the normal production build pipeline
(``run_build_pipeline`` — including proxy compile-secret injection), stores the
firmware artifact, and returns safe build metadata.

HARD INVARIANT — build only, never flash
-----------------------------------------
This module deliberately does NOT import or call any OTA / install /
reconfigure code path: no ``ota_upload_with_retry``, no
``install_production_from_factory``, no ``reconfigure_device``, no
``flash_uploader``, no ``factory_installer``. The ``build_firmware`` service
stops after artifact storage. A later, separate flash/install action installs
the prepared artifact after explicit user approval.

The only sibling module imported here is ``pipeline`` (lazily, inside the
function) which runs the shared Auto Configure → Compile → store flow.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_MAP_CONFIRMED,
    CONF_MODBUS_VERSION,
    CONF_SELECTED_DEVICE,
    CONF_SELECTED_TIER,
    DEFAULT_SELECTED_TIER,
)

_LOGGER = logging.getLogger(__name__)

_SERVICE = "pvautonomy_ops.build_firmware"


async def async_build_firmware_for_device(
    hass: HomeAssistant,
    *,
    entry_id: str,
    entry_data: dict[str, Any],
    device_name: str | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Build production firmware for one metadata-backed device, build-only.

    Args:
        hass: Home Assistant instance.
        entry_id: The already-resolved target config entry id (the caller is
            responsible for entry-scoped resolution / fail-closed ambiguity).
        entry_data: The resolved entry's runtime data (``hass.data[DOMAIN][entry_id]``).
        device_name: Optional device identifier. If omitted, the entry's
            selected device is used; if exactly one device is registered, that
            device is used; otherwise the call fails closed.
        force_rebuild: When True, bypass the proxy artifact cache for a cold
            build (registry-only update scenario).

    Returns:
        A dict of safe build metadata (no secret values):
        ``build_id``, ``cache_hit``, ``artifact_path``, ``firmware_size``,
        ``target_device``, ``registry_file``, ``build_backend``,
        ``force_rebuild``, ``duration_s``, ``is_simulated``.

    Raises:
        HomeAssistantError (fail-closed) on: missing metadata store, ambiguous
        or unknown device, build failure, or a cached artifact returned while
        ``force_rebuild`` was requested.
    """
    # Lazy import keeps this module free of the build/OTA stack at import time
    # and keeps the no-OTA static guard trivially true.
    from .pipeline import run_build_pipeline

    metadata_store = entry_data.get("metadata_store")
    if metadata_store is None:
        raise HomeAssistantError(
            f"{_SERVICE}: metadata store not initialized for entry_id "
            f"{entry_id!r}"
        )

    config: dict[str, Any] = entry_data.get("config", {}) or {}
    entry = entry_data.get("entry")
    entry_options = dict(getattr(entry, "options", {}) or {})

    # ── Resolve target device (read-only; fail closed on ambiguity) ──────
    requested = (device_name or "").strip()
    if not requested:
        requested = (
            (config.get(CONF_SELECTED_DEVICE) or "").strip()
            or (entry_options.get(CONF_SELECTED_DEVICE) or "").strip()
            or (str(entry_data.get("selected_device") or "")).strip()
        )

    metadata = None
    if requested:
        metadata = await metadata_store.lookup(requested)
        if metadata is None:
            raise HomeAssistantError(
                f"{_SERVICE}: no device metadata for identifier {requested!r} "
                f"in entry {entry_id!r} — provide a valid device_name"
            )
    else:
        all_devices = await metadata_store.get_all()
        if len(all_devices) == 1:
            metadata = all_devices[0]
        elif len(all_devices) == 0:
            raise HomeAssistantError(
                f"{_SERVICE}: no devices registered — provide device_name "
                "(or run device setup first)"
            )
        else:
            raise HomeAssistantError(
                f"{_SERVICE}: {len(all_devices)} devices registered in entry "
                f"{entry_id!r} — provide device_name (and entry_id) to "
                "disambiguate"
            )

    # ── Ownership assertion (defense-in-depth) ──────────────────────────
    # Fail closed when this entry is bound to a different physical device
    # than the resolved metadata (e.g. an SPH entry_id passed explicitly for
    # a MIC device). Only asserted when both ha_device_ids are known; legacy
    # entries with empty bindings are not blocked.
    from .runtime_identity import (
        async_resolve_metadata_mac_suffix,
        entry_owner_ha_device_id,
    )

    owner_ha = entry_owner_ha_device_id(entry)
    meta_ha = str(getattr(metadata, "ha_device_id", "") or "").strip()
    if owner_ha and meta_ha and owner_ha != meta_ha:
        raise HomeAssistantError(
            f"{_SERVICE}: entry {entry_id!r} does not own device "
            f"{metadata.device_id!r} (ha_device_id mismatch) — refusing to "
            "build firmware for a device bound to a different entry"
        )

    # ── Replay persisted build intent (same inputs as the Flash path) ────

    selected_tier = config.get(CONF_SELECTED_TIER, DEFAULT_SELECTED_TIER)
    modbus_version = config.get(CONF_MODBUS_VERSION, None)
    map_confirmed = config.get(CONF_MAP_CONFIRMED, False)
    channel = config.get("artifact_channel", "stable")
    target_device = metadata.device_id
    mac_suffix = await async_resolve_metadata_mac_suffix(hass, metadata)

    _LOGGER.info(
        "%s: starting build-only pipeline (entry=%s, device=%s, model=%s, "
        "registry=%s, backend=%s, tier=%s, modbus_version=%s, "
        "map_confirmed=%s, force_rebuild=%s)",
        _SERVICE,
        entry_id[:8] if entry_id else "",
        target_device,
        metadata.model_slug,
        metadata.registry_file,
        config.get("build_backend"),
        selected_tier,
        modbus_version,
        map_confirmed,
        force_rebuild,
    )

    result = await run_build_pipeline(
        hass,
        model=metadata.model_slug,
        site=metadata.site,
        number=metadata.number,
        registry_file=metadata.registry_file,
        mac_suffix=mac_suffix or None,
        channel=channel,
        build_backend=config.get("build_backend"),
        simulated_failure_mode=config.get("simulated_failure_mode", "none"),
        selected_tier=selected_tier,
        modbus_version=modbus_version,
        map_confirmed=map_confirmed,
        force_rebuild=force_rebuild,
        entry_id=entry_id,
    )

    if not result.success or not result.artifact_path:
        raise HomeAssistantError(
            f"{_SERVICE}: build failed for {target_device}: "
            f"{result.error or 'no artifact produced'}"
        )

    # Fail closed: a force-rebuild that came back from cache did NOT cold-build.
    if force_rebuild and result.cache_hit:
        raise HomeAssistantError(
            f"{_SERVICE}: force_rebuild was requested but the proxy returned a "
            f"cached artifact for {target_device} "
            f"(Build-ID: {result.build_job_id}) — cache bypass failed, "
            "refusing to report success"
        )

    metadata_out: dict[str, Any] = {
        "build_id": result.build_job_id,
        "cache_hit": result.cache_hit,
        "artifact_path": str(result.artifact_path),
        "firmware_size": result.firmware_size,
        "target_device": target_device,
        "registry_file": metadata.registry_file,
        "build_backend": result.build_backend,
        "force_rebuild": force_rebuild,
        "duration_s": round(result.duration_s, 1),
        "is_simulated": result.is_simulated,
    }

    _LOGGER.info(
        "%s: build complete for %s — build_id=%s, size=%d bytes, "
        "cache_hit=%s, backend=%s, artifact=%s",
        _SERVICE,
        target_device,
        result.build_job_id,
        result.firmware_size,
        result.cache_hit,
        result.build_backend,
        result.artifact_path,
    )
    return metadata_out
