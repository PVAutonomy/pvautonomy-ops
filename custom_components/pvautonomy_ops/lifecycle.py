"""Device Lifecycle Management — Re-configure & Factory Reset (P3-12-001).

Handles all device lifecycle transitions beyond initial Factory→Production:
- Re-configure: Production → different Production (identity switch)
- Factory Reset: Production → Factory (full reset)

Both transitions use Push OTA (espota2) and include identity-switch handling:
old device goes offline, new device appears with different node/entity names.

Ref: WORKER-PROMPT-P3-12-001, Phase C (steps 6–7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .device_id import (
    compute_device_id,
    compute_node_name,
)

_LOGGER = logging.getLogger(__name__)

EVENT_LIFECYCLE_STAGE = f"{DOMAIN}_lifecycle_stage"

# Factory firmware URL for reset (generic, not device-specific)
FACTORY_FIRMWARE_CHANNEL = "factory"


@dataclass
class LifecycleResult:
    """Result of a lifecycle transition."""

    success: bool
    transition: str  # "reconfigure" | "factory_reset"
    old_device_id: str | None = None
    new_device_id: str | None = None
    old_node_name: str | None = None
    new_node_name: str | None = None
    stage: str = "init"
    error: str | None = None
    duration_s: float = 0.0
    orphaned_entities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "transition": self.transition,
            "old_device_id": self.old_device_id,
            "new_device_id": self.new_device_id,
            "old_node_name": self.old_node_name,
            "new_node_name": self.new_node_name,
            "stage": self.stage,
            "error": self.error,
            "duration_s": round(self.duration_s, 1),
            "orphaned_entities": self.orphaned_entities,
        }


async def reconfigure_device(
    hass: HomeAssistant,
    *,
    current_device_id: str,
    ha_device_id: str | None = None,
    new_model: str,
    new_site: str,
    new_number: str | int,
    registry_file: str,
    mac_suffix: str | None = None,
    version: str | None = None,
    channel: str = "stable",
    build_backend: str | None = None,
    entry_id: str | None = None,
) -> LifecycleResult:
    """Re-configure a Production device with a different config.

    Transition: Production (old config) → Production (new config)

    Flow:
    1. Compile new device-specific firmware (via build pipeline)
    2. Push OTA to running Production device (same site-wide OTA password)
    3. Detect identity switch (old node offline → new node online)
    4. Mark old device as replaced
    5. Provide re-adoption guidance

    The API encryption key is site-wide (D-OPS-OTA-SEC-001), so
    the identity switch does NOT cause authentication failures within
    one HA installation.

    Args:
        hass: Home Assistant instance.
        current_device_id: Current device display name (for logging/legacy).
        ha_device_id: HA Device Registry UUID (primary for entity resolution).
        new_model: New inverter model slug.
        new_site: New installation location.
        new_number: New device number.
        registry_file: Path to registry JSON for new model.
        mac_suffix: MAC suffix for device-specific secrets.
        version: Firmware version.
        channel: Release channel.
        build_backend: Override build backend (None → read from config).

    Returns:
        LifecycleResult with transition details.
    """
    from .flash_uploader import get_ota_password, ota_upload_with_retry, resolve_device_ip, OTAError, OTA_DEFAULT_PORT
    from .pipeline import run_build_pipeline

    import time

    new_device_id = compute_device_id(new_model, new_site, new_number)
    new_node_name = compute_node_name(new_model, new_site, new_number)

    # Parse old node name from current_device_id.
    # Handles both display names ("Sph10K Haus 03") and slugs ("edge101_sph10k_haus_03").
    # Display names: lowercase + replace spaces with hyphens → "sph10k-haus-03"
    # Slugs: strip "edge101_" prefix, replace _ with - → "sph10k-haus-03"
    slug = current_device_id.lower().replace(" ", "_").replace("-", "_")
    if slug.startswith("edge101_"):
        slug = slug[len("edge101_"):]
    old_node_name = slug.replace("_", "-") if slug else current_device_id

    result = LifecycleResult(
        success=False,
        transition="reconfigure",
        old_device_id=current_device_id,
        new_device_id=new_device_id,
        old_node_name=old_node_name,
        new_node_name=new_node_name,
    )
    start_time = time.monotonic()

    def _fire(stage: str, progress: int, **extra: Any) -> None:
        result.stage = stage
        hass.bus.async_fire(
            EVENT_LIFECYCLE_STAGE,
            {
                "entry_id": entry_id,  # EPIC-015 P2-02
                "transition": "reconfigure",
                "stage": stage,
                "progress": progress,
                "old_device_id": current_device_id,
                "new_device_id": new_device_id,
                **extra,
            },
        )

    _LOGGER.info(
        "Re-configure: %s → %s (node: %s → %s)",
        current_device_id, new_device_id, old_node_name, new_node_name,
    )
    _fire("init", 0)

    try:
        # ── Stage 1: Build new firmware ──
        _fire("building", 5, detail="Compiling new device-specific firmware")

        pipeline_result = await run_build_pipeline(
            hass,
            model=new_model,
            site=new_site,
            number=new_number,
            registry_file=registry_file,
            mac_suffix=mac_suffix,
            version=version,
            channel=channel,
            build_backend=build_backend,
            entry_id=entry_id,  # EPIC-015 P2-02
        )

        if not pipeline_result.success:
            result.error = f"Build failed: {pipeline_result.error}"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        _fire("build_done", 40, firmware_size=pipeline_result.firmware_size)

        # ── Stage 2: Push OTA to current device ──
        _fire("pushing_ota", 45, detail="Pushing new firmware to device")

        device_ip, _ip_method, _ip_dur = resolve_device_ip(hass, current_device_id, ha_device_id=ha_device_id)
        if not device_ip:
            result.error = f"Cannot resolve IP for {current_device_id}"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        ota_pw_result = await hass.async_add_executor_job(
            get_ota_password, hass, current_device_id
        )
        ota_password = ota_pw_result.password if ota_pw_result else None

        if pipeline_result.artifact_path is None:
            result.error = "No artifact path from build pipeline"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        async def _ota_progress(pct: int) -> None:
            mapped = 45 + int(pct * 0.3)
            _fire("pushing_ota", mapped, ota_progress=pct)

        async def _ota_retry_cb(attempt: int, max_attempts: int, _exc: Exception) -> None:
            _fire("pushing_ota", 45, detail=f"Flashing (attempt {attempt + 1}/{max_attempts})")

        # EPIC-015 P2-02: Read OTA config from entry-scoped data
        from . import get_integration_data as _get_int_data
        _entry_data = _get_int_data(hass, entry_id)

        await ota_upload_with_retry(
            hass,
            host=device_ip,
            port=OTA_DEFAULT_PORT,
            password=ota_password,
            firmware_path=pipeline_result.artifact_path,
            progress_cb=_ota_progress,
            timeout_s=120.0,
            retries=_entry_data.get("ota_retries", 3),
            delays=_entry_data.get("ota_retry_delays", (0, 10, 30)),
            retry_cb=_ota_retry_cb,
        )

        _fire("ota_done", 75, detail="OTA push completed")

        # ── Stage 3: Identity-switch detection ──
        _fire("identity_switch", 80, detail="Detecting identity switch")

        switch_ok = await _detect_identity_switch(
            hass,
            old_node=old_node_name,
            new_node=new_node_name,
            timeout_s=90,
            progress_cb=lambda pct: _fire("identity_switch", 80 + int(pct * 0.15)),
        )

        if switch_ok:
            _LOGGER.info(
                "Identity switch detected: %s offline, %s online",
                old_node_name, new_node_name,
            )
        else:
            _LOGGER.warning(
                "Identity switch not fully confirmed within timeout. "
                "Old: %s, New: %s", old_node_name, new_node_name,
            )

        # ── Stage 4: Mark old device as replaced ──
        _fire("cleanup", 95, detail="Marking old device as replaced")
        orphaned = await _mark_device_replaced(
            hass, old_node_name, new_device_id
        )
        result.orphaned_entities = orphaned

        # ── Complete ──
        result.success = True
        result.duration_s = time.monotonic() - start_time
        _fire("complete", 100, orphaned_count=len(orphaned))
        _LOGGER.info(
            "Re-configure COMPLETE: %s → %s (%.1fs, %d orphaned entities)",
            current_device_id, new_device_id, result.duration_s, len(orphaned),
        )

    except OTAError as exc:
        result.error = f"OTA push failed: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire("failed", 100, error=result.error)
        _LOGGER.error(result.error)
    except Exception as exc:
        result.error = f"Re-configure failed: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire("failed", 100, error=result.error)
        _LOGGER.exception(result.error)

    return result


async def factory_reset_device(
    hass: HomeAssistant,
    *,
    current_device_id: str,
    ha_device_id: str | None = None,
    hw_family: str = "edge101",
    entry_id: str | None = None,
) -> LifecycleResult:
    """Reset a Production device to Factory firmware.

    Transition: Production → Factory

    Flow:
    1. Download Factory firmware binary from GitHub Pages
    2. Push Factory binary via OTA to Production device
    3. Device reboots into Factory mode (no encryption, MAC suffix name)
    4. Old Production device → orphaned
    5. Factory device auto-discovered (no API encryption)

    Args:
        hass: Home Assistant instance.
        current_device_id: Device being reset.
        hw_family: Hardware family.

    Returns:
        LifecycleResult with transition details.
    """
    from .flash_uploader import get_ota_password, ota_upload_with_retry, resolve_device_ip, OTAError, OTA_DEFAULT_PORT
    from .artifacts import ArtifactError, download_artifact, verify_artifact

    import tempfile
    import time
    from pathlib import Path

    # Parse old node name from display name or slug
    slug = current_device_id.lower().replace(" ", "_").replace("-", "_")
    if slug.startswith("edge101_"):
        slug = slug[len("edge101_"):]
    old_node_name = slug.replace("_", "-") if slug else current_device_id

    result = LifecycleResult(
        success=False,
        transition="factory_reset",
        old_device_id=current_device_id,
        old_node_name=old_node_name,
        new_device_id=None,  # Factory device_id is MAC-based (unknown until discovered)
        new_node_name=None,
    )
    start_time = time.monotonic()

    def _fire(stage: str, progress: int, **extra: Any) -> None:
        result.stage = stage
        hass.bus.async_fire(
            EVENT_LIFECYCLE_STAGE,
            {
                "entry_id": entry_id,  # EPIC-015 P2-02
                "transition": "factory_reset",
                "stage": stage,
                "progress": progress,
                "old_device_id": current_device_id,
                "new_device_id": None,  # EPIC-015 P3-04: schema parity with reconfigure
                **extra,
            },
        )

    _LOGGER.info("Factory Reset: %s → Factory", current_device_id)
    _fire("init", 0)

    temp_dir = Path(tempfile.mkdtemp(prefix="pvautonomy_reset_"))

    try:
        # ── Stage 1: Download Factory firmware ──
        _fire("download_factory", 10, detail="Downloading Factory firmware")

        # EPIC-015 P2-02: entry-scoped config lookup
        from . import get_integration_data
        config = get_integration_data(hass, entry_id).get("config", {})
        channel = FACTORY_FIRMWARE_CHANNEL

        try:
            artifact = await download_artifact(
                version="factory",
                hw_family=hw_family,
                temp_dir=temp_dir,
                channel=channel,
                owner=config.get("artifact_owner"),
                repo=config.get("artifact_repo"),
            )
        except Exception as exc:
            result.error = f"Factory firmware download failed: {exc}"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        _fire("download_done", 30, firmware_size=artifact.firmware_path.stat().st_size)

        # ── Stage 1b: Verify artifact integrity (EPIC-015 P3-06) ──
        # Fail closed: corrupted or tampered firmware must not be OTA-pushed.
        try:
            await verify_artifact(artifact)
        except ArtifactError as exc:
            result.error = f"Factory firmware verification failed: {exc}"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        # ── Stage 2: Push OTA ──
        _fire("pushing_ota", 35, detail="Pushing Factory firmware to device")

        device_ip, _ip_method, _ip_dur = resolve_device_ip(hass, current_device_id, ha_device_id=ha_device_id)
        if not device_ip:
            result.error = f"Cannot resolve IP for {current_device_id}"
            result.duration_s = time.monotonic() - start_time
            _fire("failed", 100, error=result.error)
            return result

        ota_pw_result = await hass.async_add_executor_job(
            get_ota_password, hass, current_device_id
        )
        ota_password = ota_pw_result.password if ota_pw_result else None

        async def _ota_progress(pct: int) -> None:
            mapped = 35 + int(pct * 0.4)
            _fire("pushing_ota", mapped, ota_progress=pct)

        async def _ota_retry_cb(attempt: int, max_attempts: int, _exc: Exception) -> None:
            _fire("pushing_ota", 35, detail=f"Flashing Factory (attempt {attempt + 1}/{max_attempts})")

        # EPIC-015 P2-02: Read OTA config from entry-scoped data
        _entry_data = get_integration_data(hass, entry_id)

        await ota_upload_with_retry(
            hass,
            host=device_ip,
            port=OTA_DEFAULT_PORT,
            password=ota_password,
            firmware_path=artifact.firmware_path,
            progress_cb=_ota_progress,
            timeout_s=120.0,
            retries=_entry_data.get("ota_retries", 3),
            delays=_entry_data.get("ota_retry_delays", (0, 10, 30)),
            retry_cb=_ota_retry_cb,
        )

        _fire("ota_done", 75, detail="OTA push completed — device rebooting to Factory")

        # ── Stage 3: Wait for Factory device ──
        _fire("waiting_factory", 80, detail="Waiting for Factory device discovery")

        # Factory device uses name_add_mac_suffix: true, no API encryption
        # It will appear as a new device with a MAC-based name.
        # We can't predict the exact new name, so we wait for the old
        # device to go offline, which confirms the transition.
        await asyncio.sleep(5)

        # Detect old device going offline
        old_offline = await _wait_for_offline(
            hass, old_node_name, timeout_s=60
        )

        if old_offline:
            _LOGGER.info("Factory Reset: old device %s went offline", old_node_name)
        else:
            _LOGGER.warning(
                "Factory Reset: old device %s did not go fully offline "
                "within timeout (may still be rebooting)", old_node_name
            )

        # ── Stage 4: Update Device Registry to Factory ──
        _fire("cleanup", 82, detail="Updating device registry to Factory")
        await _update_device_registry_to_factory(
            hass,
            current_device_id=current_device_id,
            ha_device_id=ha_device_id,
        )

        # ── Stage 5: Re-adopt ESPHome for Factory firmware ──
        _fire("readopt_esphome", 85, detail="Removing old ESPHome config entry for Factory re-adoption")
        await _readopt_esphome_for_factory(
            hass,
            current_device_id=current_device_id,
            ha_device_id=ha_device_id,
            entry_id=entry_id,  # EPIC-015 P2-02
        )

        # ── Stage 6: Cleanup old device ──
        _fire("cleanup", 92, detail="Marking old device as reset")
        orphaned = await _mark_device_replaced(
            hass, old_node_name, "factory_reset"
        )
        result.orphaned_entities = orphaned

        # ── Complete ──
        result.success = True
        result.duration_s = time.monotonic() - start_time
        _fire(
            "complete", 100,
            detail=(
                "Factory Reset complete. Factory device will be "
                "auto-discovered via mDNS (no encryption)."
            ),
            orphaned_count=len(orphaned),
        )
        _LOGGER.info(
            "Factory Reset COMPLETE for %s (%.1fs)",
            current_device_id, result.duration_s,
        )

    except OTAError as exc:
        result.error = f"OTA push failed: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire("failed", 100, error=result.error)
        _LOGGER.error(result.error)
    except Exception as exc:
        result.error = f"Factory Reset failed: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire("failed", 100, error=result.error)
        _LOGGER.exception(result.error)
    finally:
        import shutil
        await hass.async_add_executor_job(shutil.rmtree, temp_dir, True)

    return result


# ---------------------------------------------------------------------------
# Identity-switch detection helpers
# ---------------------------------------------------------------------------


async def _detect_identity_switch(
    hass: HomeAssistant,
    *,
    old_node: str,
    new_node: str,
    timeout_s: int = 90,
    progress_cb: Any = None,
) -> bool:
    """Detect identity switch: old node goes offline, new node comes online.

    Args:
        old_node: Old ESPHome node name (e.g. "sph10k-haus-01")
        new_node: New ESPHome node name (e.g. "sph10k-garage-01")
        timeout_s: Max seconds to wait.
        progress_cb: Optional callback(percent: int).

    Returns:
        True if identity switch detected (old offline + new online).
    """
    old_slug = old_node.replace("-", "_")
    new_slug = new_node.replace("-", "_")

    old_offline = False
    new_online = False
    elapsed = 0

    while elapsed < timeout_s:
        await asyncio.sleep(2)
        elapsed += 2

        if progress_cb:
            pct = int((elapsed / timeout_s) * 100)
            progress_cb(pct)

        # Check old node — any sensor containing old slug
        if not old_offline:
            for eid in hass.states.async_entity_ids("sensor"):
                if old_slug in eid:
                    state = hass.states.get(eid)
                    if state and state.state in ("unavailable", "unknown"):
                        old_offline = True
                        _LOGGER.info("Identity switch: old node '%s' offline (at %ds)", old_node, elapsed)
                        break

        # Check new node
        if not new_online:
            for eid in hass.states.async_entity_ids("sensor"):
                if new_slug in eid:
                    state = hass.states.get(eid)
                    if state and state.state not in ("unavailable", "unknown"):
                        new_online = True
                        _LOGGER.info("Identity switch: new node '%s' online (at %ds)", new_node, elapsed)
                        break

        if old_offline and new_online:
            return True

        if old_offline and elapsed > 30:
            # Old is offline for >30s but new not yet online
            _LOGGER.info(
                "Identity switch: old offline, new not yet online (at %ds). "
                "May need manual re-adoption.", elapsed
            )

    return old_offline  # Partial success if at least old went offline


async def _wait_for_offline(
    hass: HomeAssistant,
    node_name: str,
    timeout_s: int = 60,
) -> bool:
    """Wait for a node to go offline (unavailable)."""
    slug = node_name.replace("-", "_")
    elapsed = 0

    while elapsed < timeout_s:
        await asyncio.sleep(2)
        elapsed += 2

        for eid in hass.states.async_entity_ids("sensor"):
            if slug in eid:
                state = hass.states.get(eid)
                if state and state.state in ("unavailable", "unknown"):
                    return True

    return False


async def _update_device_registry_to_factory(
    hass: HomeAssistant,
    *,
    current_device_id: str,
    ha_device_id: str | None = None,
) -> None:
    """Update HA Device Registry entry to reflect Factory firmware state.

    After a Factory Reset, the physical device reboots with Factory firmware
    but HA keeps the old Device Registry entry (matched by MAC address).
    This function updates model and sw_version so that discovery correctly
    identifies the device as a Factory device.

    Uses constants from discovery.py for model names (single source of truth).
    """
    from homeassistant.helpers import device_registry as dr
    from .discovery import MODEL_FACTORY

    dev_reg = dr.async_get(hass)
    device_entry = None

    # Prefer ha_device_id (Device Registry UUID) if available
    if ha_device_id:
        device_entry = dev_reg.async_get(ha_device_id)

    # Fallback: search by display name
    if device_entry is None:
        target = current_device_id.lower().replace(" ", "_").replace("-", "_")
        for entry in dev_reg.devices.values():
            dev_name = (entry.name or "").lower().replace(" ", "_").replace("-", "_")
            if dev_name == target:
                device_entry = entry
                break

    if device_entry is None:
        _LOGGER.warning(
            "Cannot update Device Registry: device '%s' not found",
            current_device_id,
        )
        return

    # Extract MAC suffix for factory device name (e.g., "2eb1e4")
    from .mac_utils import InvalidMACError, canonical_mac_last6
    mac_suffix = ""
    for conn_type, conn_id in device_entry.connections:
        if conn_type == dr.CONNECTION_NETWORK_MAC:
            with contextlib.suppress(InvalidMACError):
                mac_suffix = canonical_mac_last6(conn_id)
            break

    factory_name = f"PVAutonomy Edge101 {mac_suffix}" if mac_suffix else "PVAutonomy Edge101"

    dev_reg.async_update_device(
        device_entry.id,
        model=MODEL_FACTORY,
        sw_version="factory-2.0",
        name=factory_name,
    )

    _LOGGER.info(
        "Device Registry updated: '%s' → '%s' (model=%s, sw_version=factory-2.0)",
        current_device_id,
        factory_name,
        MODEL_FACTORY,
    )


async def _readopt_esphome_for_factory(
    hass: HomeAssistant,
    *,
    current_device_id: str,
    ha_device_id: str | None = None,
    entry_id: str | None = None,
) -> None:
    """Remove old ESPHome config entry so Factory device gets auto-discovered.

    After Factory Reset, the ESPHome config entry still has:
    - noise_psk (API encryption key) from Production firmware
    - device_name pointing to old Production node name

    Factory firmware has NO API encryption, so ESPHome can't connect with
    the old config entry. The cleanest fix: remove the old config entry
    entirely + any "ignored" discovery entries for the same MAC.
    HA will then auto-discover the Factory device via mDNS/zeroconf.

    Also removes stale entity registry entries (old entity_ids) that would
    otherwise persist and shadow the new Factory entities.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # ── Find the HA device entry ──
    device_entry = None
    if ha_device_id:
        device_entry = dev_reg.async_get(ha_device_id)
    if device_entry is None:
        target = current_device_id.lower().replace(" ", "_").replace("-", "_")
        for entry in dev_reg.devices.values():
            dev_name = (entry.name or "").lower().replace(" ", "_").replace("-", "_")
            if dev_name == target:
                device_entry = entry
                break

    if device_entry is None:
        _LOGGER.warning(
            "ESPHome re-adopt: device '%s' not found in registry, skipping",
            current_device_id,
        )
        return

    # ── Extract MAC for ignored-entry cleanup ──
    device_mac = ""
    for conn_type, conn_id in device_entry.connections:
        if conn_type == dr.CONNECTION_NETWORK_MAC:
            device_mac = conn_id.lower()
            break

    # ── Find ESPHome config entries for this device ──
    # EPIC-015 P3-02: use ce_id to avoid shadowing the Ops entry_id parameter.
    esphome_entry_ids: list[str] = []
    for ce_id in device_entry.config_entries:
        ce = hass.config_entries.async_get_entry(ce_id)
        if ce and ce.domain == "esphome":
            esphome_entry_ids.append(ce_id)

    # ── Remove entity registry entries for old config entries ──
    for esph_eid in esphome_entry_ids:
        stale_entities = [
            e for e in ent_reg.entities.values()
            if e.config_entry_id == esph_eid
        ]
        for ent in stale_entities:
            _LOGGER.debug(
                "ESPHome re-adopt: removing stale entity %s (unique_id=%s)",
                ent.entity_id, ent.unique_id,
            )
            ent_reg.async_remove(ent.entity_id)
        if stale_entities:
            _LOGGER.info(
                "ESPHome re-adopt: removed %d stale entity registry entries "
                "for config entry %s",
                len(stale_entities), esph_eid,
            )

    # ── Remove ESPHome config entries ──
    for esph_eid in esphome_entry_ids:
        _LOGGER.info(
            "ESPHome re-adopt: removing config entry %s (had noise_psk/old device_name)",
            esph_eid,
        )
        try:
            await hass.config_entries.async_remove(esph_eid)
        except Exception as exc:
            _LOGGER.warning(
                "ESPHome re-adopt: failed to remove config entry %s: %s",
                esph_eid, exc,
            )

    # ── Remove "ignored" discovery entries for the same MAC ──
    if device_mac:
        mac_no_colons = device_mac.replace(":", "")
        ignored_entries = [
            e for e in hass.config_entries.async_entries("esphome")
            if e.source == "ignore"
            and e.unique_id
            and e.unique_id.replace(":", "").lower() == mac_no_colons
        ]
        for ignored in ignored_entries:
            _LOGGER.info(
                "ESPHome re-adopt: removing ignored discovery entry '%s' (MAC match)",
                ignored.title,
            )
            try:
                await hass.config_entries.async_remove(ignored.entry_id)
            except Exception as exc:
                _LOGGER.warning(
                    "ESPHome re-adopt: failed to remove ignored entry %s: %s",
                    ignored.entry_id, exc,
                )

    # ── Clear keyring entry for this device (D-OPS-KEYRING-STRATEGY-001) ──
    # Config entry removal already handles the HA-side noise_psk.
    # Also clean the Ops keyring to avoid stale entries.
    try:
        from . import get_integration_data
        domain_data = get_integration_data(hass, entry_id)
        keyring = domain_data.get("keyring")
        if keyring and device_mac:
            from .mac_utils import canonical_mac_last6
            mac_suffix_clean = canonical_mac_last6(device_mac)
            await keyring.clear_production_noise_psk(mac_suffix_clean)
            _LOGGER.info(
                "ESPHome re-adopt: cleared keyring entry for mac_suffix=%s",
                mac_suffix_clean,
            )
    except Exception as exc:
        _LOGGER.debug(
            "ESPHome re-adopt: keyring cleanup skipped (%s)",
            type(exc).__name__,
        )

    _LOGGER.info(
        "ESPHome re-adopt: cleanup complete for '%s'. "
        "Factory device will be auto-discovered via mDNS.",
        current_device_id,
    )


async def _mark_device_replaced(
    hass: HomeAssistant,
    old_node_name: str,
    replaced_by: str,
) -> list[str]:
    """Mark old device entities as replaced/orphaned.

    Logs all entities that belong to the old node name and notes them
    as orphaned. Does NOT auto-delete (user should review and clean up
    manually or via the migrate_entities button).

    Returns list of orphaned entity IDs.
    """
    slug = old_node_name.replace("-", "_")
    orphaned: list[str] = []

    for domain in ("sensor", "binary_sensor", "switch", "number", "button", "update", "text"):
        for eid in hass.states.async_entity_ids(domain):
            if slug in eid:
                orphaned.append(eid)

    if orphaned:
        _LOGGER.info(
            "Marking %d entities as orphaned (old node: %s, replaced by: %s): %s",
            len(orphaned),
            old_node_name,
            replaced_by,
            orphaned[:10],  # Log first 10
        )

    return orphaned
