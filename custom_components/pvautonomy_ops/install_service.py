"""Install-prepared-firmware service helper (EPIC-004).

SPEC:
``project-docs/PLANNING/active/specs/features/epic004-install-prepared-firmware-service/spec.md``

This module backs the ``pvautonomy_ops.install_prepared_firmware`` service.
It installs an *already prepared* firmware artifact (produced earlier by
the build-only service) onto the selected production Edge101 device via
the existing OTA path. The build pipeline is never invoked here.

HARD INVARIANT — install only, never build
------------------------------------------
This module is install-only by construction. It does NOT import the build
pipeline, the wizard/stepper, the factory installer, or any reconfigure
helper. The static test suite enforces this with both an import-graph
check and a name-reference check, so forbidden symbol names (build
pipeline entry points, wizard/reconfigure entry points) must not appear
anywhere in this file. Only ``flash_uploader`` (the existing OTA helper
surface), ``runtime_identity`` (legacy metadata MAC-suffix recovery), and
``keyring`` (post-flash reload scheduling) are touched.

The only sibling modules imported here are ``const``, ``flash_uploader``
(``ota_upload_with_retry``, ``resolve_device_ip``, ``get_ota_password``,
``OTAError``, ``OTA_DEFAULT_PORT``), ``runtime_identity`` (lazy import for
missing legacy metadata suffixes), and ``keyring`` (lazy import for the
practical post-upload reload).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_SELECTED_DEVICE,
    DOMAIN,
)
from .flash_uploader import (
    OTA_DEFAULT_PORT,
    OTAError,
    get_ota_password,
    ota_upload_with_retry,
    resolve_device_ip,
)

_LOGGER = logging.getLogger(__name__)

_SERVICE = "pvautonomy_ops.install_prepared_firmware"

# Canonical location where ``pvautonomy_ops.build_firmware`` stores the
# prepared artifact. Mirrors ``pipeline.ARTIFACT_DIR`` but kept local so
# this module does NOT import the build pipeline.
_BUILDS_DIR = Path("/config/pvautonomy/builds")
_ARTIFACT_FILENAME = "firmware.ota.bin"

# Plausibility floor — a real ESPHome production firmware is well above
# this. The proxy/GHA build path normally produces >900 KB artifacts. We
# refuse to flash anything smaller than this so a truncated/empty file
# never reaches the device.
_MIN_ARTIFACT_BYTES = 300 * 1024


def _artifact_path_for(device_id: str) -> Path:
    """Canonical prepared-artifact path for ``device_id``."""
    return _BUILDS_DIR / device_id / _ARTIFACT_FILENAME


async def async_install_prepared_firmware_for_device(
    hass: HomeAssistant,
    *,
    entry_id: str,
    entry_data: dict[str, Any],
    device_name: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Install the prepared firmware artifact for one device via OTA.

    Args:
        hass: Home Assistant instance.
        entry_id: The already-resolved target config entry id (the caller is
            responsible for entry-scoped resolution / fail-closed ambiguity).
        entry_data: The resolved entry's runtime data (``hass.data[DOMAIN][entry_id]``).
        device_name: Optional device identifier. If omitted, the entry's
            selected device is used; if exactly one device is registered, that
            device is used; otherwise the call fails closed.
        confirmed: Must be ``True``. The service refuses to flash without
            explicit confirmation.

    Returns:
        Safe install metadata (no secret values):
        ``target_device``, ``artifact_path``, ``firmware_size``,
        ``device_ip``, ``ip_method``, ``ota_password_scope``.

    Raises:
        HomeAssistantError (fail-closed) on: missing confirmation, missing
        metadata store, ambiguous/unknown device, missing or implausibly
        small artifact, unresolved IP, missing OTA password, or OTA failure.
    """
    if not confirmed:
        raise HomeAssistantError(
            f"{_SERVICE}: refusing to install without explicit confirmation "
            "(confirmed: true required)"
        )

    metadata_store = entry_data.get("metadata_store")
    if metadata_store is None:
        raise HomeAssistantError(
            f"{_SERVICE}: metadata store not initialized for entry_id "
            f"{entry_id!r}"
        )

    config: dict[str, Any] = entry_data.get("config", {}) or {}
    entry = entry_data.get("entry")
    entry_options = dict(getattr(entry, "options", {}) or {})

    # ── Resolve target device (read-only; fail closed on ambiguity) ─────
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

    target_device = metadata.device_id
    from .runtime_identity import (
        async_resolve_metadata_esphome_host,
        async_resolve_metadata_mac_suffix,
        entry_owner_ha_device_id,
    )

    # Ownership assertion (defense-in-depth): refuse to install onto a device
    # this entry does not own (e.g. an SPH entry_id passed for a MIC device).
    # Only asserted when both ha_device_ids are known.
    owner_ha = entry_owner_ha_device_id(entry)
    meta_ha = str(getattr(metadata, "ha_device_id", "") or "").strip()
    if owner_ha and meta_ha and owner_ha != meta_ha:
        raise HomeAssistantError(
            f"{_SERVICE}: entry {entry_id!r} does not own device "
            f"{target_device!r} (ha_device_id mismatch) — refusing to install "
            "firmware onto a device bound to a different entry"
        )

    mac_suffix = await async_resolve_metadata_mac_suffix(hass, metadata)

    _LOGGER.info(
        "%s: starting install (entry=%s, device=%s)",
        _SERVICE,
        entry_id[:8] if entry_id else "",
        target_device,
    )

    # ── Stage 1: locate + validate the prepared artifact ────────────────
    artifact_path = _artifact_path_for(target_device)

    def _stat_artifact() -> tuple[bool, int]:
        if not artifact_path.is_file():
            return False, 0
        return True, artifact_path.stat().st_size

    exists, firmware_size = await hass.async_add_executor_job(_stat_artifact)
    if not exists:
        raise HomeAssistantError(
            f"{_SERVICE}: no prepared firmware found for {target_device}. "
            'Bitte zuerst „Firmware vorbereiten" ausführen.'
        )

    if firmware_size < _MIN_ARTIFACT_BYTES:
        raise HomeAssistantError(
            f"{_SERVICE}: prepared firmware for {target_device} is "
            f"implausibly small ({firmware_size} bytes; minimum "
            f"{_MIN_ARTIFACT_BYTES} bytes). Bitte neu vorbereiten und "
            "erneut versuchen."
        )

    _emit_install_stage(
        hass,
        entry_id=entry_id,
        stage="validated",
        progress=10,
        target_device=target_device,
        artifact_path=artifact_path,
        firmware_size=firmware_size,
    )

    # ── Stage 2: resolve device IP ──────────────────────────────────────
    ha_device_id = getattr(metadata, "ha_device_id", "") or None
    device_ip, ip_method, ip_duration_ms = resolve_device_ip(
        hass, target_device, ha_device_id=ha_device_id
    )
    if not device_ip:
        device_ip = await async_resolve_metadata_esphome_host(hass, metadata)
        if device_ip:
            ip_method = "esphome_config_metadata"
    if not device_ip:
        raise HomeAssistantError(
            f"{_SERVICE}: cannot resolve IP for {target_device} "
            f"(ip_method={ip_method}). Bitte prüfen, ob das Gerät online "
            "ist und im Netzwerk erreichbar."
        )

    _emit_install_stage(
        hass,
        entry_id=entry_id,
        stage="ip_resolved",
        progress=20,
        target_device=target_device,
        artifact_path=artifact_path,
        firmware_size=firmware_size,
        device_ip=device_ip,
        ip_method=ip_method,
        ip_duration_ms=ip_duration_ms,
    )

    # ── Stage 3: resolve OTA password (per-device preferred) ────────────
    ota_lookup_key = mac_suffix or target_device
    ota_result = await hass.async_add_executor_job(
        get_ota_password, hass, ota_lookup_key
    )
    if ota_result is None or not getattr(ota_result, "password", None):
        raise HomeAssistantError(
            f"{_SERVICE}: no OTA password configured for {target_device}. "
            "Bitte die OTA-Konfiguration prüfen."
        )
    ota_password = ota_result.password
    ota_password_scope = getattr(ota_result, "scope", None)

    # ── Stage 4: OTA upload ─────────────────────────────────────────────
    operation_tracker = entry_data.get("operation_tracker")

    async def _upload_progress(pct: int) -> None:
        # Map OTA 0–100% to install 30–90% for the operation tracker.
        mapped = 30 + int(pct * 0.6)
        if operation_tracker is not None:
            try:
                operation_tracker.update_progress(mapped, "uploading firmware")
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "install_prepared_firmware: tracker progress raised",
                    exc_info=True,
                )
        _emit_install_stage(
            hass,
            entry_id=entry_id,
            stage="upload",
            progress=mapped,
            target_device=target_device,
            artifact_path=artifact_path,
            firmware_size=firmware_size,
            device_ip=device_ip,
        )

    async def _retry_cb(attempt: int, max_attempts: int, exc: Exception) -> None:
        _LOGGER.info(
            "%s: OTA retry %d/%d for %s: %s",
            _SERVICE, attempt + 1, max_attempts, target_device, exc,
        )
        _emit_install_stage(
            hass,
            entry_id=entry_id,
            stage="upload_retry",
            progress=30,
            target_device=target_device,
            artifact_path=artifact_path,
            firmware_size=firmware_size,
            device_ip=device_ip,
        )

    _emit_install_stage(
        hass,
        entry_id=entry_id,
        stage="upload",
        progress=30,
        target_device=target_device,
        artifact_path=artifact_path,
        firmware_size=firmware_size,
        device_ip=device_ip,
    )

    try:
        await ota_upload_with_retry(
            hass,
            host=device_ip,
            port=OTA_DEFAULT_PORT,
            password=ota_password,
            firmware_path=artifact_path,
            progress_cb=_upload_progress,
            timeout_s=180.0,
            retries=entry_data.get("ota_retries", 3),
            delays=entry_data.get("ota_retry_delays", (0, 10, 30)),
            retry_cb=_retry_cb,
        )
    except OTAError as exc:
        _emit_install_stage(
            hass,
            entry_id=entry_id,
            stage="failed",
            progress=0,
            target_device=target_device,
            artifact_path=artifact_path,
            firmware_size=firmware_size,
            device_ip=device_ip,
            error=str(exc),
        )
        raise HomeAssistantError(
            f"{_SERVICE}: OTA upload failed for {target_device}: {exc}. "
            "Bitte später erneut versuchen oder Support kontaktieren."
        ) from exc

    _emit_install_stage(
        hass,
        entry_id=entry_id,
        stage="reconnect",
        progress=95,
        target_device=target_device,
        artifact_path=artifact_path,
        firmware_size=firmware_size,
        device_ip=device_ip,
    )

    # ── Stage 5: practical reconnect (fire-and-forget reload) ───────────
    try:
        mac = mac_suffix
        if mac:
            from .keyring import schedule_post_flash_reload
            hass.async_create_task(schedule_post_flash_reload(hass, mac))
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug(
            "%s: post-flash reload scheduling raised — continuing",
            _SERVICE, exc_info=True,
        )

    _emit_install_stage(
        hass,
        entry_id=entry_id,
        stage="complete",
        progress=100,
        target_device=target_device,
        artifact_path=artifact_path,
        firmware_size=firmware_size,
        device_ip=device_ip,
    )

    _LOGGER.info(
        "%s: install complete for %s (size=%d bytes, ip=%s, method=%s)",
        _SERVICE,
        target_device,
        firmware_size,
        device_ip,
        ip_method,
    )

    return {
        "target_device": target_device,
        "artifact_path": str(artifact_path),
        "firmware_size": firmware_size,
        "device_ip": device_ip,
        "ip_method": ip_method,
        "ota_password_scope": ota_password_scope,
    }


def _emit_install_stage(
    hass: HomeAssistant,
    *,
    entry_id: str | None,
    stage: str,
    progress: int,
    target_device: str,
    artifact_path: Path,
    firmware_size: int,
    device_ip: str | None = None,
    ip_method: str | None = None,
    ip_duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Fire a ``pvautonomy_ops_install_stage`` event for sensor UX.

    Mirrors the ``..._flash_stage`` event shape (entry-scoped, stage +
    progress + target_device + error) but is distinct so the customer card
    can show the install path independently of legacy flash UX. Never
    carries the OTA password or any other secret value.
    """
    hass.bus.async_fire(
        f"{DOMAIN}_install_stage",
        {
            "entry_id": entry_id,
            "stage": stage,
            "progress": progress,
            "target_device": target_device,
            "artifact_path": str(artifact_path),
            "firmware_size": firmware_size,
            "device_ip": device_ip,
            "ip_method": ip_method,
            "ip_duration_ms": ip_duration_ms,
            "error": error,
        },
    )
