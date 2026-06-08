"""PVAutonomy Ops Buttons (Contract Outputs I, J, K, L).

Phase 3: EXECUTE Actions via button press.
MVP: I (Discover), J (Flash) implemented first.
"""
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_MAP_CONFIRMED,
    CONF_MODBUS_VERSION,
    CONF_SELECTED_TIER,
    DEFAULT_SELECTED_TIER,
    DOMAIN,
)
from .flash_uploader import (
    ota_upload_with_retry,
    resolve_device_ip,
    get_ota_password,
    OTAError,
    OTA_DEFAULT_PORT,
)

# Button entity IDs (Contract v1.0.0)
ENTITY_BUTTON_DISCOVER = "pvautonomy_ops_discover"
ENTITY_BUTTON_FLASH = "pvautonomy_ops_flash_production"
ENTITY_BUTTON_GATES = "pvautonomy_ops_run_gates"
ENTITY_BUTTON_MIGRATE = "pvautonomy_ops_migrate_entities"

# Phase 3 Extensions (not in Contract v1.0.0)
ENTITY_BUTTON_RESTART = "pvautonomy_ops_restart_device"
ENTITY_BUTTON_BUILD_PIPELINE = "pvautonomy_ops_build_pipeline"

_LOGGER = logging.getLogger(__name__)


async def check_gates_passed(hass: HomeAssistant, entry_id: str | None = None) -> tuple[bool, str]:
    """Check if quality gates have passed (hard rule for critical actions).

    Args:
        hass: Home Assistant instance
        entry_id: Config entry ID for entry-scoped entity lookup.
            If None, falls back to hardcoded entity_id (single-entry compat).

    Returns:
        Tuple of (gates_ok: bool, error_message: str)

    Usage:
        Before executing critical actions (flash, migrate, cleanup):
        gates_ok, error = await check_gates_passed(hass, entry_id)
        if not gates_ok:
            raise ValueError(error)
    """
    # EPIC-006-STAB Phase 1: entry-scoped status entity lookup
    status_entity_id = None
    if entry_id:
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
        if isinstance(entry_data, dict):
            status_entity_id = entry_data.get("status_entity_id")
    if not status_entity_id:
        status_entity_id = "sensor.pvautonomy_ops_status"
    status_sensor = hass.states.get(status_entity_id)

    if not status_sensor:
        return False, "Status sensor not available (run gates first)"

    gates_overall = status_sensor.attributes.get("gates_overall")
    gates_last_run = status_sensor.attributes.get("gates_last_run")

    if not gates_overall:
        return False, "Quality gates have not been run (press 'Run Gates' button first)"

    if gates_overall == "fail":
        failed_gates = status_sensor.attributes.get("gates_fail", [])
        return False, f"Quality gates FAILED: {', '.join(failed_gates)} (fix issues or override)"

    # warn is allowed (user decision), only fail blocks
    _LOGGER.info("Quality gates check: %s (last run: %s)", gates_overall, gates_last_run)
    return True, ""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy Ops buttons from a ConfigEntry."""
    _LOGGER.info("Setting up PVAutonomy Ops buttons (ConfigEntry)")

    # PN-2: per entry_id keying
    entry_data = hass.data[DOMAIN][entry.entry_id]
    operation_runner = entry_data["operation_runner"]
    input_reader = entry_data["input_reader"]
    runtime_config = entry_data["config"]
    is_legacy = entry_data.get("is_legacy", False)  # EPIC-006-STAB Phase 2

    entry_id = entry.entry_id

    async_add_entities(
        [
            PVAutonomyOpsDiscoverButton(hass, operation_runner, input_reader, entry_id=entry_id, is_legacy=is_legacy),
            PVAutonomyOpsRestartButton(hass, operation_runner, input_reader, entry_id=entry_id, is_legacy=is_legacy),
            PVAutonomyOpsRunGatesButton(hass, operation_runner, input_reader, entry_id=entry_id, is_legacy=is_legacy),
            PVAutonomyOpsFlashButton(hass, operation_runner, input_reader, runtime_config, entry_id=entry_id, is_legacy=is_legacy),
        ],
        True,
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up PVAutonomy Ops buttons (legacy YAML, kept for backward compat)."""
    _LOGGER.info("Setting up PVAutonomy Ops buttons (YAML platform)")

    # PN-2: find first entry's data (legacy path has no entry object)
    entry_data = next(iter(hass.data.get(DOMAIN, {}).values()), {})
    operation_runner = entry_data["operation_runner"]
    input_reader = entry_data["input_reader"]
    runtime_config = entry_data.get("config", {})
    entry_id = entry_data.get("entry", None)
    entry_id = entry_id.entry_id if entry_id else None

    async_add_entities(
        [
            PVAutonomyOpsDiscoverButton(hass, operation_runner, input_reader, entry_id=entry_id),
            PVAutonomyOpsRestartButton(hass, operation_runner, input_reader, entry_id=entry_id),
            PVAutonomyOpsRunGatesButton(hass, operation_runner, input_reader, entry_id=entry_id),
            PVAutonomyOpsFlashButton(hass, operation_runner, input_reader, runtime_config, entry_id=entry_id),
        ],
        True,
    )


class PVAutonomyOpsDiscoverButton(ButtonEntity):
    """Output I: button.pvautonomy_ops_discover.

    Manually trigger device discovery (re-scan inverter registry + packages).
    Contract: ops-contract-v1.md Section 2.3.I
    """

    _attr_name = "PVAutonomy Discover"
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader, entry_id: str | None = None, *, is_legacy: bool = False) -> None:
        """Initialize discover button."""
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_BUTTON_DISCOVER}" if entry_id else ENTITY_BUTTON_DISCOVER
        self._attr_suggested_object_id = ENTITY_BUTTON_DISCOVER
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        if is_legacy:
            self._attr_entity_registry_enabled_default = False

        self._attr_extra_state_attributes = {
            "last_result": None,
            "devices_found": None,
            "duration_ms": None,
            "error_message": None,
        }

    async def async_press(self) -> None:
        """Handle button press (Action A: Discover Devices).

        Contract: ops-contract-v1.md Section 3.1 Action A
        """
        _LOGGER.info("Discover button pressed")

        # Execute via operation runner (handles lock + lifecycle)
        result = await self.operation_runner.run(
            "discover",
            self._execute_discover
        )

        # Update button attributes with result
        discover_result = result.get("result", {})
        self._attr_extra_state_attributes = {
            "last_result": "success" if result["success"] else "error",
            "devices_found": discover_result.get("devices_found"),
            "factory_count": discover_result.get("factory_count", 0),
            "production_count": discover_result.get("production_count", 0),
            "duration_ms": result["duration_ms"],
            "error_message": result.get("error"),
        }

        # Trigger update
        self.async_write_ha_state()

        # Trigger status sensor update to reflect new last_discovery timestamp
        self.hass.bus.async_fire(f"{DOMAIN}_update")

        _LOGGER.info(
            "Discover completed: %s devices, duration=%dms",
            result.get("result", {}).get("devices_found", 0),
            result["duration_ms"]
        )

    async def _execute_discover(self) -> dict[str, Any]:
        """Execute discovery logic via HA Device Registry.

        P3-8-001: Active scan of Device Registry (Factory + Production).
        D-OPS-FACTORY-DISCOVERY-001

        Returns:
            Dict with discovery results:
                - devices_found: int
                - factory_count: int
                - production_count: int
                - factory_devices: list[dict]
                - production_devices: list[dict]
        """
        # Primary: Device Registry scan (Factory + Production)
        registry_devices = await self.input_reader.get_registry_devices()

        factory = registry_devices.get("factory", [])
        production = registry_devices.get("production", [])
        total = len(factory) + len(production)

        # Also read legacy template sensor for backward compatibility
        legacy_devices = await self.input_reader.get_discovered_devices()

        # Populate legacy dropdown (only if input_select exists)
        dropdown_items = await self.input_reader.get_all_devices_for_dropdown()
        if dropdown_items:
            selector_state = self.hass.states.get(
                "input_select.edge101_selected_production_device"
            )
            if selector_state is not None:
                options = ["none"] + [item["value"] for item in dropdown_items]
                try:
                    await self.hass.services.async_call(
                        "input_select",
                        "set_options",
                        {
                            "entity_id": "input_select.edge101_selected_production_device",
                            "options": options,
                        },
                        blocking=True,
                    )
                    _LOGGER.info(
                        "Dropdown updated with %d devices: %s",
                        len(dropdown_items),
                        options,
                    )
                except Exception as err:
                    _LOGGER.warning("Failed to update dropdown: %s", err)

        return {
            "devices_found": total,
            "factory_count": len(factory),
            "production_count": len(production),
            "factory_devices": factory,
            "production_devices": production,
            "legacy_devices": legacy_devices,
        }


class PVAutonomyOpsFlashButton(ButtonEntity):
    """Output J: button.pvautonomy_ops_flash_production.

    Flash production firmware to selected Edge101 device.
    Contract: ops-contract-v1.md Section 2.3.J

    NOTE: Deferred to Phase 3.1 (requires ESPHome OTA integration)
    """

    _attr_name = "PVAutonomy Flash"
    _attr_icon = "mdi:download-box"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader, runtime_config: dict | None = None, entry_id: str | None = None, *, is_legacy: bool = False) -> None:
        """Initialize flash button."""
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_BUTTON_FLASH}" if entry_id else ENTITY_BUTTON_FLASH
        self._attr_suggested_object_id = ENTITY_BUTTON_FLASH
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        self._runtime_config = runtime_config or {}
        self._entry_id = entry_id  # EPIC-006-STAB Phase 1: entry-scoped lookup
        if is_legacy:
            self._attr_entity_registry_enabled_default = False

        self._attr_extra_state_attributes = {
            "target_device": None,
            "last_result": None,
            "firmware_version": None,
            "flash_duration_sec": None,
            "error_message": None,
        }

    async def async_press(self) -> None:
        """Handle button press (Action B: Flash Firmware).

        Contract: ops-contract-v1.md Section 3.1 Action B
        Directive: D-ADDON-FLASH-GUARD-001 (mandatory gate checks)
        Directive: D-OPS-FLASH-MODE-SPLIT-001 (factory vs production routing)

        Mode Guard (P3-11-001 / C1):
          - Factory device → Managed Pull via factory_installer
          - Production device → Push OTA via espota2
          - Unknown device kind → block with guidance
        """
        from .flash_guards import check_flash_guards, format_guard_block_message
        from .factory_installer import install_production_from_factory

        _LOGGER.info("Flash button pressed (entry_id=%s)", self._entry_id)

        # Get selected device — entry-scoped to prevent cross-entry bleed
        selected_device = await self.input_reader.get_selected_device(
            entry_id=self._entry_id
        )

        if not selected_device or selected_device == "none":
            _LOGGER.warning(
                "Flash blocked: no device selected (entry_id=%s)",
                self._entry_id,
            )
            self._attr_extra_state_attributes = {
                "target_device": None,
                "last_result": "rejected",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": "No device selected",
                "block_reason": "DEVICE_NOT_SELECTED",
                "install_method": None,
            }
            self.async_write_ha_state()
            return

        # Revalidate: selected device must exist in current discovery
        all_devices = await self.input_reader.get_all_discovered_devices()
        discovered_names = [d.name for d in all_devices]
        if selected_device not in discovered_names:
            _LOGGER.warning(
                "Flash blocked: selected device '%s' not in discovered "
                "devices %s (entry_id=%s). Resetting selection.",
                selected_device,
                discovered_names[:5],
                self._entry_id,
            )
            # Reset stale selection
            if self._entry_id:
                await self.input_reader.set_selected_device(
                    self._entry_id, None
                )
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "rejected",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": (
                    f"Device '{selected_device}' is no longer available. "
                    "Selection has been reset. Please select a device and retry."
                ),
                "block_reason": "DEVICE_NOT_DISCOVERED",
                "install_method": None,
            }
            self.async_write_ha_state()
            return

        # ── P3-11-001 / C1: Mode guard — detect device kind ──────
        device_kind = await self.input_reader.get_selected_device_kind(
            device_name=selected_device, entry_id=self._entry_id
        )
        _LOGGER.info(
            "Flash mode guard: device='%s', kind='%s', entry_id=%s",
            selected_device, device_kind, self._entry_id,
        )

        if device_kind == "factory":
            # ── FACTORY PATH: Managed Pull ──────────────────────
            _LOGGER.info(
                "Factory device detected — routing to managed pull installer"
            )

            # Factory path: flash guards are relaxed (GATE-FACTORY-001 only)
            guards_passed, block_reason, guard_message = await check_flash_guards(self.hass, entry_id=self._entry_id)
            if not guards_passed:
                block_message = await format_guard_block_message(self.hass, block_reason, entry_id=self._entry_id)
                self._attr_extra_state_attributes = {
                    "target_device": selected_device,
                    "last_result": "blocked",
                    "firmware_version": None,
                    "flash_duration_sec": None,
                    "error_message": block_message,
                    "block_reason": block_reason,
                    "install_method": "managed_pull",
                }
                self.async_write_ha_state()
                return

            # Execute factory install via operation runner
            async def _factory_install(device_name: str) -> dict:
                """Wrapper for factory installer to match operation_runner pattern."""
                async def _progress_cb(stage: str, pct: int) -> None:
                    pass  # Progress is fired via bus from installer

                return await install_production_from_factory(
                    self.hass,
                    device_name=device_name,
                    progress_cb=_progress_cb,
                )

            result = await self.operation_runner.run(
                "flash_firmware",
                _factory_install,
                selected_device,
            )

            # Update button attributes
            if result is None:
                self._attr_extra_state_attributes = {
                    "target_device": selected_device,
                    "last_result": "failed",
                    "firmware_version": None,
                    "flash_duration_sec": None,
                    "error_message": "Internal error: operation_runner returned None",
                    "block_reason": None,
                    "install_method": "managed_pull",
                }
            else:
                self._attr_extra_state_attributes = {
                    "target_device": selected_device,
                    "last_result": "success" if result.get("success") else "failed",
                    "firmware_version": result.get("result", {}).get("firmware_version") if result.get("result") else None,
                    "flash_duration_sec": result["duration_ms"] // 1000 if result.get("duration_ms") else None,
                    "error_message": result.get("error"),
                    "block_reason": None,
                    "install_method": "managed_pull",
                }

            self.async_write_ha_state()
            return

        if device_kind is None:
            # ── UNKNOWN DEVICE: block with guidance (C4) ────────
            # D1: structured diagnostics for support
            _LOGGER.warning(
                "Flash blocked: DEVICE_KIND_UNKNOWN | entry_id=%s | "
                "selected_device='%s' | discovered_count=%d | "
                "discovered_names=%s",
                self._entry_id,
                selected_device,
                len(discovered_names),
                discovered_names[:5],
            )
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "rejected",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": (
                    f"Unknown device type for '{selected_device}'. "
                    "Press Discover first to detect whether this is a "
                    "Factory or Production device."
                ),
                "block_reason": "DEVICE_KIND_UNKNOWN",
                "install_method": None,
            }
            self.async_write_ha_state()
            return

        # ── PRODUCTION PATH: Push OTA via espota2 ───────────────
        _LOGGER.info(
            "Production device detected — routing to Push OTA (espota2)"
        )

        # D-ADDON-FLASH-GUARD-001: Check preflight gates (MUST)
        guards_passed, block_reason, guard_message = await check_flash_guards(self.hass, entry_id=self._entry_id)

        if not guards_passed:
            _LOGGER.warning(
                "Flash BLOCKED by guards: %s (%s)",
                block_reason,
                guard_message
            )

            # Format user-friendly block message
            block_message = await format_guard_block_message(self.hass, block_reason, entry_id=self._entry_id)

            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "blocked",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": block_message,
                "block_reason": block_reason,  # Machine-readable
                "install_method": "push_ota",
            }
            self.async_write_ha_state()

            # Also update status sensor to reflect block
            self.hass.bus.async_fire(
                f"{DOMAIN}_update",
                {
                    "op_state": "blocked",
                    "last_error": block_message,
                    "block_reason": block_reason,
                }
            )
            return

        # Guards passed - execute flash via operation runner (Push OTA)
        result = await self.operation_runner.run(
            "flash_firmware",
            self._execute_flash,
            selected_device
        )

        # Update button attributes (defensive None handling)
        if result is None:
            # Operation runner returned None (unexpected error)
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "failed",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": "Internal error: operation_runner returned None",
                "block_reason": None,
                "install_method": "push_ota",
            }
        else:
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "success" if result.get("success") else "failed",
                "firmware_version": result.get("result", {}).get("firmware_version") if result.get("result") else None,
                "flash_duration_sec": result["duration_ms"] // 1000 if result.get("duration_ms") else None,
                "error_message": result.get("error"),
                "block_reason": None,
                "install_method": "push_ota",
            }

        self.async_write_ha_state()

    async def _execute_flash(self, device_id: str) -> dict[str, Any]:
        """Execute flash via Build Pipeline + OTA (EPIC-006-WP3, Deliverable C).

        Replaces the old artifacts.py path with:
        1. Metadata Store lookup → device model/site/number/registry_file
        2. run_build_pipeline() → compile device-specific firmware
        3. ota_upload_with_retry() → push firmware to device
        4. Postcheck → detect reboot + verify online

        Args:
            device_id: Device name to flash (e.g. "sph10k_haus_03")

        Returns:
            Dict with flash results

        Stages:
            init → metadata → build → upload → postcheck → complete/failed
        """
        from . import get_integration_data
        from .flash_guards import check_flash_guards
        from .pipeline import run_build_pipeline

        # Flash stage machine state
        # EPIC-015 P3-04: version = firmware version (if known),
        #                  build_job_id = CI job identifier (separate)
        flash_state: dict[str, Any] = {
            "stage": "init",
            "version": None,
            "build_job_id": None,
            "target_device": device_id,
            "error": None,
        }

        def update_stage(stage: str, progress: int) -> None:
            """Update flash stage and fire event for sensor UX."""
            flash_state["stage"] = stage
            _LOGGER.info("Flash stage: %s (progress=%d%%)", stage, progress)
            self.hass.bus.async_fire(
                f"{DOMAIN}_flash_stage",
                {
                    "entry_id": self._entry_id,  # EPIC-006-STAB Phase 1
                    "stage": stage,
                    "progress": progress,
                    "version": flash_state.get("version"),
                    "target_device": device_id,
                    "error": flash_state.get("error"),
                    # EPIC-006-STAB Phase 3: IP + build observability
                    "device_ip": flash_state.get("device_ip"),
                    "ip_method": flash_state.get("ip_method"),
                    "ip_duration_ms": flash_state.get("ip_duration_ms"),
                    "build_job_id": flash_state.get("build_job_id"),
                },
            )

        try:
            # STAGE 1: init (0%)
            update_stage("init", 0)

            # STAGE 2: preflight (5%)
            update_stage("preflight", 5)
            guards_passed, _block_reason, guard_message = await check_flash_guards(
                self.hass, entry_id=self._entry_id
            )
            if not guards_passed:
                flash_state["error"] = f"Preflight failed: {guard_message}"
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])
            _LOGGER.info("Flash preflight PASSED")

            # STAGE 3: metadata lookup (10%)
            update_stage("metadata", 10)

            # Resolve device metadata from store
            # EPIC-015 P1-04: entry-scoped lookup (no first-entry fallback)
            entry_data = get_integration_data(self.hass, self._entry_id)
            metadata_store = entry_data.get("metadata_store")
            if not metadata_store:
                flash_state["error"] = "Metadata store not initialized"
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])

            # EPIC-015 P2-05: Use read-only lookup (no auto-persist side effect)
            metadata = await metadata_store.lookup(device_id)
            if not metadata:
                flash_state["error"] = (
                    f"Device '{device_id}' not found in metadata store. "
                    "Use the Config Flow wizard to set up this device first."
                )
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])

            _LOGGER.info(
                "Metadata resolved: model=%s, site=%s, number=%d, registry=%s",
                metadata.model_slug,
                metadata.site,
                metadata.number,
                metadata.registry_file,
            )

            # STAGE 4: build pipeline (15→60%) — compile device-specific firmware
            update_stage("build", 15)
            config = self._runtime_config
            channel = config.get("artifact_channel", "stable")
            min_size_kb = config.get("flash_min_firmware_size_kb", 300)
            selected_tier = config.get(CONF_SELECTED_TIER, DEFAULT_SELECTED_TIER)
            # EPIC-015 P1-03: Replay persisted build intent for re-flash.
            # Safe defaults for historical entries without stored values.
            modbus_version = config.get(CONF_MODBUS_VERSION, None)
            map_confirmed = config.get(CONF_MAP_CONFIRMED, False)

            pipeline_result = await run_build_pipeline(
                self.hass,
                model=metadata.model_slug,
                site=metadata.site,
                number=metadata.number,
                registry_file=metadata.registry_file,
                mac_suffix=metadata.mac_suffix or None,
                channel=channel,
                build_backend=config.get("build_backend"),
                simulated_failure_mode=config.get("simulated_failure_mode", "none"),
                selected_tier=selected_tier,
                modbus_version=modbus_version,
                map_confirmed=map_confirmed,
                entry_id=self._entry_id,  # EPIC-015 P1-04: entry-scoped pipeline
            )

            if not pipeline_result.success or not pipeline_result.artifact_path:
                flash_state["error"] = (
                    f"Build pipeline failed: {pipeline_result.error or 'no artifact'}"
                )
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])

            # EPIC-015 P3-04: build_job_id is a CI identifier, not a firmware version
            flash_state["build_job_id"] = pipeline_result.build_job_id
            artifact_path = pipeline_result.artifact_path
            firmware_size = pipeline_result.firmware_size

            _LOGGER.info(
                "Build pipeline OK: artifact=%s, size=%d bytes, backend=%s",
                artifact_path,
                firmware_size,
                pipeline_result.build_backend,
            )

            # MIN_FIRMWARE_SIZE gate (P3-6-001)
            min_size_bytes = min_size_kb * 1024
            if firmware_size < min_size_bytes:
                flash_state["error"] = (
                    f"Firmware too small: {firmware_size} bytes "
                    f"(minimum {min_size_bytes} bytes / {min_size_kb} KB)"
                )
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])

            # STAGE 5: upload (60→85%) — OTA via espota2 SHA256
            update_stage("upload", 60)
            _LOGGER.info("Starting OTA upload to device: %s", device_id)

            device_ip, ip_method, ip_duration = resolve_device_ip(
                self.hass, device_id, ha_device_id=metadata.ha_device_id
            )
            flash_state["device_ip"] = device_ip
            flash_state["ip_method"] = ip_method
            flash_state["ip_duration_ms"] = ip_duration
            if not device_ip:
                flash_state["error"] = f"Cannot resolve IP for device: {device_id}"
                update_stage("failed", 0)
                raise OTAError(flash_state["error"])

            ota_result = await self.hass.async_add_executor_job(
                get_ota_password, self.hass, device_id
            )
            ota_password = ota_result.password if ota_result else None
            if not ota_password:
                _LOGGER.warning("No OTA password found — attempting upload without auth")

            # Progress callback: map OTA 0-100% → flash 60-85%
            async def _upload_progress(pct: int) -> None:
                flash_pct = 60 + int(pct * 0.25)
                update_stage("upload", flash_pct)

            # EPIC-006-A5: retry callback
            async def _retry_cb(
                attempt: int, max_attempts: int, _exc: Exception
            ) -> None:
                _LOGGER.info(
                    "OTA flash retry %d/%d for %s",
                    attempt + 1,
                    max_attempts,
                    device_id,
                )
                update_stage("upload", 60)

            # EPIC-015 P2-01: Read OTA config from entry-scoped data, not domain root
            await ota_upload_with_retry(
                self.hass,
                host=device_ip,
                port=OTA_DEFAULT_PORT,
                password=ota_password,
                firmware_path=artifact_path,
                progress_cb=_upload_progress,
                timeout_s=120.0,
                retries=entry_data.get("ota_retries", 3),
                delays=entry_data.get("ota_retry_delays", (0, 10, 30)),
                retry_cb=_retry_cb,
            )

            _LOGGER.info("OTA upload completed successfully")

            # Reload ESPHome entry after device reboot so it reconnects
            # with the new firmware (fire-and-forget, same delay as config_flow).
            mac = getattr(metadata, "mac_suffix", "") or ""
            if mac:
                from .keyring import schedule_post_flash_reload
                self.hass.async_create_task(
                    schedule_post_flash_reload(self.hass, mac)
                )

            # STAGE 6: postcheck (85→95%) — detect reboot + verify online
            update_stage("postcheck", 85)
            _LOGGER.info("Postcheck: waiting for device reboot...")
            await self._postcheck_reboot(device_id, update_stage)

            # STAGE 7: complete (100%)
            update_stage("complete", 100)
            _LOGGER.info(
                "Flash operation complete: device=%s, backend=%s",
                device_id,
                pipeline_result.build_backend,
            )

            return {
                "result": "success",
                "device_id": device_id,
                "firmware_version": flash_state["version"],
                "flash_duration_sec": 0,
                "stage": "complete",
            }

        except OTAError as e:
            flash_state["error"] = f"OTA upload error: {e}"
            update_stage("failed", 0)
            _LOGGER.error("OTA upload failed: %s", e)
            raise

        except Exception as e:
            if flash_state.get("stage") != "failed":
                flash_state["error"] = str(e)
                update_stage("failed", 0)
            _LOGGER.error(
                "Flash operation failed at stage %s: %s", flash_state["stage"], e
            )
            raise

    async def _postcheck_reboot(
        self,
        device_id: str,
        update_stage,
    ) -> None:
        """Postcheck: detect device reboot after OTA upload.

        Monitors uptime sensor, health sensor, or wifi signal for reboot
        detection (offline → online transition or uptime reset).
        """
        import asyncio as _asyncio

        await _asyncio.sleep(3)

        # Find monitoring sensor (uptime preferred, then health, then wifi)
        monitor_entity = None
        monitor_type = None
        initial_uptime = None

        for eid in self.hass.states.async_entity_ids("sensor"):
            if device_id in eid and "uptime" in eid.lower():
                monitor_entity = eid
                monitor_type = "uptime"
                break

        if not monitor_entity:
            for eid in self.hass.states.async_entity_ids("binary_sensor"):
                if device_id in eid and "health" in eid.lower():
                    monitor_entity = eid
                    monitor_type = "health"
                    break

        if not monitor_entity:
            for eid in self.hass.states.async_entity_ids("sensor"):
                if device_id in eid and "wifi" in eid.lower():
                    monitor_entity = eid
                    monitor_type = "wifi"
                    break

        if not monitor_entity:
            _LOGGER.warning(
                "Postcheck: no monitoring sensor found, waiting 15s as fallback"
            )
            await _asyncio.sleep(15)
            return

        init_state = self.hass.states.get(monitor_entity)
        if (
            monitor_type == "uptime"
            and init_state
            and init_state.state not in ("unavailable", "unknown")
        ):
            try:
                initial_uptime = float(init_state.state)
            except (ValueError, TypeError):
                initial_uptime = None

        _LOGGER.info(
            "Postcheck monitoring via %s (%s)", monitor_entity, monitor_type
        )

        # Poll for reboot detection (max 90s, every 2s)
        offline_detected = False
        max_wait = 90
        elapsed = 0

        while elapsed < max_wait:
            await _asyncio.sleep(2)
            elapsed += 2

            poll_pct = 85 + int((elapsed / max_wait) * 10)
            update_stage("postcheck", min(poll_pct, 95))

            cur_state = self.hass.states.get(monitor_entity)
            if not cur_state:
                continue

            cur_available = cur_state.state not in ("unavailable", "unknown")

            if not offline_detected and not cur_available:
                offline_detected = True
                _LOGGER.info("Postcheck: device offline (elapsed=%ds)", elapsed)

            if (
                monitor_type == "uptime"
                and cur_available
                and initial_uptime is not None
            ):
                with contextlib.suppress(ValueError, TypeError):
                    cur_uptime = float(cur_state.state)
                    if cur_uptime < initial_uptime:
                        _LOGGER.info(
                            "Postcheck: uptime reset %.1fs→%.1fs (reboot confirmed)",
                            initial_uptime,
                            cur_uptime,
                        )
                        return

            if offline_detected and cur_available:
                _LOGGER.info(
                    "Postcheck: device back online (elapsed=%ds)", elapsed
                )
                return
        if offline_detected:
            _LOGGER.warning(
                "Postcheck: device offline but not back within %ds", max_wait
            )
        else:
            _LOGGER.warning(
                "Postcheck: no reboot detected within %ds", max_wait
            )


class PVAutonomyOpsRestartButton(ButtonEntity):
    """Phase 3 Extension: button.pvautonomy_ops_restart_device.

    Restart selected Edge101 device via ESPHome service.
    NOT in Contract v1.0.0 (Phase 3 experimental feature).
    """

    _attr_name = "PVAutonomy Restart"
    _attr_icon = "mdi:restart"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader, entry_id: str | None = None, *, is_legacy: bool = False) -> None:
        """Initialize restart button."""
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_BUTTON_RESTART}" if entry_id else ENTITY_BUTTON_RESTART
        self._attr_suggested_object_id = ENTITY_BUTTON_RESTART
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        if is_legacy:
            self._attr_entity_registry_enabled_default = False

        self._attr_extra_state_attributes = {
            "target_device": None,
            "last_result": None,
            "restart_duration_sec": None,
            "offline_detected": None,
            "online_detected": None,
            "error_message": None,
        }

    async def async_press(self) -> None:
        """Handle button press (Restart Device).

        Phase 3 Extension: Soft restart via ESPHome service.
        """
        _LOGGER.info("Restart button pressed (entry_id=%s)", self._entry_id)

        # Get selected device — entry-scoped
        selected_device = await self.input_reader.get_selected_device(
            entry_id=self._entry_id
        )

        if not selected_device or selected_device == "none":
            _LOGGER.warning("Restart blocked: no device selected")
            self._attr_extra_state_attributes = {
                "target_device": None,
                "last_result": "rejected",
                "restart_duration_sec": None,
                "offline_detected": None,
                "online_detected": None,
                "error_message": "No device selected",
            }
            self.async_write_ha_state()
            return

        # Execute via operation runner
        result = await self.operation_runner.run(
            "restart_device",
            self._execute_restart,
            selected_device
        )

        # Update button attributes
        self._attr_extra_state_attributes = {
            "target_device": selected_device,
            "last_result": result.get("result", {}).get("status", "error"),
            "restart_duration_sec": result["duration_ms"] // 1000 if result["duration_ms"] else None,
            "offline_detected": result.get("result", {}).get("offline_detected"),
            "online_detected": result.get("result", {}).get("online_detected"),
            "error_message": result.get("error"),
        }

        self.async_write_ha_state()

        # Trigger status sensor update
        self.hass.bus.async_fire(f"{DOMAIN}_update")

    async def _execute_restart(self, device_entity_id: str) -> dict[str, Any]:
        """Execute restart logic with ESPHome service call.

        Uses dynamic entity discovery to find restart button (robust against entity naming changes).

        Args:
            device_entity_id: Device to restart (e.g., "sph10k_haus_03")

        Returns:
            Dict with restart results:
                - status: 'success' | 'warn' | 'error'
                - offline_detected: ISO timestamp or None
                - online_detected: ISO timestamp or None
                - error: Error message if failed
        """
        import asyncio

        _LOGGER.info("Executing restart for device: %s", device_entity_id)

        # STEP 1: Find ESPHome restart entity (button or switch)
        restart_entity, restart_domain = await self._find_restart_entity(device_entity_id)

        if not restart_entity:
            return {
                "status": "error",
                "offline_detected": None,
                "online_detected": None,
                "error": f"Restart entity not found for device: {device_entity_id}",
            }

        _LOGGER.info("Found restart %s: %s", restart_domain, restart_entity)

        # STEP 2: Find monitoring sensor (fuzzy matching, same as restart entity)
        monitor_entity = None
        monitor_type = None

        # Try to find uptime sensor (preferred for reboot detection)
        for entity_id in self.hass.states.async_entity_ids("sensor"):
            if device_entity_id in entity_id and "uptime" in entity_id.lower():
                monitor_entity = entity_id
                monitor_type = "uptime"
                _LOGGER.info("Found uptime sensor: %s", monitor_entity)
                break

        # Fallback to health sensor
        if not monitor_entity:
            for entity_id in self.hass.states.async_entity_ids("binary_sensor"):
                if device_entity_id in entity_id and "health" in entity_id.lower():
                    monitor_entity = entity_id
                    monitor_type = "health"
                    _LOGGER.info("Found health sensor: %s", monitor_entity)
                    break

        # Last fallback to wifi sensor
        if not monitor_entity:
            for entity_id in self.hass.states.async_entity_ids("sensor"):
                if device_entity_id in entity_id and "wifi" in entity_id.lower():
                    monitor_entity = entity_id
                    monitor_type = "wifi"
                    _LOGGER.info("Found wifi sensor: %s", monitor_entity)
                    break

        if not monitor_entity:
            _LOGGER.warning("No monitoring sensor found (searched for uptime/health/wifi with '%s' in entity_id)", device_entity_id)

        # Record initial state for monitoring
        initial_state = None
        initial_available = False
        initial_uptime = None

        if monitor_entity:
            initial_state = self.hass.states.get(monitor_entity)
            initial_available = initial_state.state not in ["unavailable", "unknown"] if initial_state else False

            # If monitoring uptime, record initial value
            if monitor_type == "uptime" and initial_available:
                try:
                    initial_uptime = float(initial_state.state)
                    _LOGGER.debug("Initial uptime: %.2fs", initial_uptime)
                except (ValueError, TypeError):
                    initial_uptime = None

            _LOGGER.debug("Initial state: %s (available=%s)", initial_state.state if initial_state else "None", initial_available)

        # STEP 3: Call ESPHome restart (robust handling for momentary switches)
        try:
            if restart_domain == "button":
                # Buttons: Simple press
                await self.hass.services.async_call(
                    "button",
                    "press",
                    {"entity_id": restart_entity},
                    blocking=True,
                )
                _LOGGER.info("Restart button pressed: %s", restart_entity)
            elif restart_domain == "switch":
                # Switches: Often momentary (like buttons)
                # Robust pattern: turn_on → wait → optional turn_off
                await self.hass.services.async_call(
                    "switch",
                    "turn_on",
                    {"entity_id": restart_entity},
                    blocking=True,
                )
                _LOGGER.info("Restart switch activated (turn_on): %s", restart_entity)

                # Brief pause (allow restart trigger to register)
                await asyncio.sleep(0.5)

                # Check if switch is still ON (may auto-reset to OFF)
                switch_state = self.hass.states.get(restart_entity)
                if switch_state and switch_state.state == "on":
                    # Switch did not auto-reset → manually turn off
                    await self.hass.services.async_call(
                        "switch",
                        "turn_off",
                        {"entity_id": restart_entity},
                        blocking=True,
                    )
                    _LOGGER.debug("Restart switch reset (turn_off): %s", restart_entity)
                else:
                    _LOGGER.debug("Restart switch auto-reset detected (momentary)")
        except Exception as e:
            _LOGGER.error("Restart activation failed: %s", e, exc_info=True)
            return {
                "status": "error",
                "offline_detected": None,
                "online_detected": None,
                "error": f"Restart activation failed: {str(e)}",
            }

        # STEP 4: Best-effort reboot detection (if monitoring available)
        if not monitor_entity:
            # No monitoring available - assume success after button press
            _LOGGER.info("No monitoring sensor, assuming restart successful")
            return {
                "status": "success",
                "offline_detected": "N/A (no monitoring)",
                "online_detected": "N/A (no monitoring)",
                "error": None,
            }

        # Poll for offline→online sequence OR uptime reset (max 90 seconds)
        offline_detected = None
        online_detected = None
        uptime_reset_detected = None
        max_wait = 90  # seconds
        poll_interval = 2  # seconds
        elapsed = 0

        _LOGGER.info("Polling for reboot detection (max %ds)...", max_wait)

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            current_state = self.hass.states.get(monitor_entity)
            if not current_state:
                continue

            current_available = current_state.state not in ["unavailable", "unknown"]

            # Method 1: Detect offline transition
            if not offline_detected and not current_available:
                offline_detected = datetime.now(timezone.utc).isoformat()
                _LOGGER.info("Device offline detected at %s (elapsed=%ds)", offline_detected, elapsed)

            # Method 2: Detect uptime reset (device rebooted without going offline)
            if monitor_type == "uptime" and current_available and initial_uptime is not None:
                with contextlib.suppress(ValueError, TypeError):
                    current_uptime = float(current_state.state)
                    if current_uptime < initial_uptime:
                        uptime_reset_detected = datetime.now(timezone.utc).isoformat()
                        _LOGGER.info("Uptime reset detected: %.2fs → %.2fs (elapsed=%ds)", initial_uptime, current_uptime, elapsed)
                        # SUCCESS: uptime reset indicates reboot
                        return {
                            "status": "success",
                            "offline_detected": offline_detected or "N/A",
                            "online_detected": uptime_reset_detected,
                            "error": None,
                        }

            # Method 3: Detect online transition (after offline)
            if offline_detected and not online_detected and current_available:
                online_detected = datetime.now(timezone.utc).isoformat()
                _LOGGER.info("Device online detected at %s (elapsed=%ds)", online_detected, elapsed)
                # SUCCESS: saw full offline→online sequence
                return {
                    "status": "success",
                    "offline_detected": offline_detected,
                    "online_detected": online_detected,
                    "error": None,
                }

        # Timeout reached - determine result
        if uptime_reset_detected:
            # Uptime reset seen but didn't return early
            return {
                "status": "success",
                "offline_detected": offline_detected or "N/A",
                "online_detected": uptime_reset_detected,
                "error": None,
            }
        elif offline_detected and not online_detected:
            _LOGGER.warning("Restart timeout: offline detected but device not back online after %ds", max_wait)
            return {
                "status": "warn",
                "offline_detected": offline_detected,
                "online_detected": None,
                "error": f"Device offline detected but did not come back online within {max_wait}s",
            }
        else:
            _LOGGER.warning("Restart uncertain: no offline/uptime reset detected within %ds", max_wait)
            return {
                "status": "warn",
                "offline_detected": None,
                "online_detected": None,
                "error": f"No reboot detected within {max_wait}s (device may not have restarted)",
            }

    async def _find_restart_entity(self, device_entity_id: str) -> tuple[str | None, str | None]:
        """Find ESPHome restart entity (button or switch) for device.

        Search strategy:
        1. Pattern match: Extended list of common restart entity patterns
        2. Friendly-Name Fallback: Search all switches/buttons matching device_id

        Args:
            device_entity_id: Device identifier (e.g., "sph10k_haus_03")

        Returns:
            Tuple of (entity_id, domain) or (None, None) if not found
        """
        # A) Pattern-based search (most common naming conventions)
        patterns = [
            # Switches (ESPHome often uses switches for restart)
            ("switch", f"switch.{device_entity_id}_restart"),
            ("switch", f"switch.{device_entity_id}_restart_device"),
            ("switch", f"switch.{device_entity_id}_reboot"),
            ("switch", f"switch.{device_entity_id}_neustart"),  # German ESPHome default
            ("switch", f"switch.{device_entity_id}_restart_esp"),
            # Buttons (newer ESPHome versions)
            ("button", f"button.{device_entity_id}_restart"),
            ("button", f"button.{device_entity_id}_restart_device"),
            ("button", f"button.{device_entity_id}_reboot"),
        ]

        for domain, entity_id in patterns:
            if self.hass.states.get(entity_id):
                _LOGGER.info("Found restart %s via pattern: %s", domain, entity_id)
                return entity_id, domain

        # B) Friendly-Name Fallback: Iterate over all switches/buttons
        _LOGGER.debug("Pattern match failed, searching via friendly_name...")

        restart_keywords = ["restart", "neustart", "reboot"]

        for domain in ["switch", "button"]:
            for entity_id in self.hass.states.async_entity_ids(domain):
                # Filter: entity_id must contain device identifier
                if f"{device_entity_id}_" not in entity_id:
                    continue

                state = self.hass.states.get(entity_id)
                if not state:
                    continue

                # Check entity_id for restart keywords
                entity_lower = entity_id.lower()
                if any(keyword in entity_lower for keyword in restart_keywords):
                    _LOGGER.info("Found restart %s via entity_id: %s", domain, entity_id)
                    return entity_id, domain

                # Check friendly_name for restart keywords
                friendly_name = state.attributes.get("friendly_name", "").lower()
                if any(keyword in friendly_name for keyword in restart_keywords):
                    _LOGGER.info("Found restart %s via friendly_name: %s (%s)", domain, entity_id, friendly_name)
                    return entity_id, domain

        _LOGGER.error("Could not find restart entity for device: %s (tried switches/buttons with entity_id containing '%s_')", device_entity_id, device_entity_id)
        return None, None


class PVAutonomyOpsRunGatesButton(ButtonEntity):
    """Output K: button.pvautonomy_ops_run_gates (Action K: Run Quality Gates)."""

    _attr_name = "PVAutonomy Readiness Check"
    _attr_icon = "mdi:gate"

    def __init__(self, hass, operation_runner, input_reader, entry_id: str | None = None, *, is_legacy: bool = False):
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_BUTTON_GATES}" if entry_id else ENTITY_BUTTON_GATES
        self._attr_suggested_object_id = ENTITY_BUTTON_GATES
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        self._entry_id = entry_id  # EPIC-006-STAB Phase 1: entry-scoped events
        if is_legacy:
            self._attr_entity_registry_enabled_default = False
        self._attr_extra_state_attributes = {
            "target_device": None,
            "last_result": None,
            "gates_total": None,
            "gates_passed": None,
            "gates_failed": None,
            "gates_warned": None,
            "failed_gates": None,
            "warned_gates": None,
            "duration_ms": None,
        }

    async def async_press(self) -> None:
        """Execute gate validation (Contract Action K)."""
        _LOGGER.info("Run Gates button pressed (entry_id=%s)", self._entry_id)
        start_time = datetime.now(timezone.utc)

        # Get selected device — entry-scoped
        selected_device = await self.input_reader.get_selected_device(
            entry_id=self._entry_id
        )

        try:
            # Execute gates via operation runner
            result = await self.operation_runner.run(
                "run_gates",
                self._execute_gates,
                {"target_device": selected_device, "entry_id": self._entry_id},
            )

            # Extract gate results from operation wrapper
            if result["success"] and result.get("result"):
                gate_results = result["result"]

                # Update attributes from gate results
                self._attr_extra_state_attributes.update(
                    {
                        "target_device": selected_device,
                        "last_result": gate_results.get("last_result"),
                        "gates_total": gate_results.get("gates_total"),
                        "gates_passed": gate_results.get("gates_passed"),
                        "gates_failed": gate_results.get("gates_failed"),
                        "gates_warned": gate_results.get("gates_warned"),
                        "failed_gates": gate_results.get("failed_gates"),
                        "warned_gates": gate_results.get("warned_gates"),
                        "duration_ms": result["duration_ms"],
                    }
                )

                # Trigger sensor update with gate results
                self.hass.bus.async_fire(
                    f"{DOMAIN}_gates_completed",
                    {
                        "entry_id": self._entry_id,  # EPIC-006-STAB Phase 1
                        "overall": gate_results.get("last_result"),
                        "gates_total": gate_results.get("gates_total"),
                        "gates_passed": gate_results.get("gates_passed"),
                        "gates_failed": gate_results.get("gates_failed"),
                        "gates_warned": gate_results.get("gates_warned"),
                        "failed_gates": gate_results.get("failed_gates"),
                        "warned_gates": gate_results.get("warned_gates"),
                        "details": gate_results.get("details"),
                        "checked_at": start_time.isoformat(),
                    },
                )

                _LOGGER.info(
                    "Quality gates completed: %s (total=%d, passed=%d, warned=%d, failed=%d)",
                    gate_results.get("last_result"),
                    gate_results.get("gates_total"),
                    gate_results.get("gates_passed"),
                    gate_results.get("gates_warned"),
                    gate_results.get("gates_failed"),
                )
            else:
                # Operation failed - log error
                _LOGGER.error(
                    "Gate execution failed: %s",
                    result.get("error", "Unknown error"),
                )
                self._attr_extra_state_attributes["last_result"] = "error"

            # Write state to HA (CRITICAL!)
            self.async_write_ha_state()

        except Exception as e:
            _LOGGER.error("Gate execution failed: %s", e, exc_info=True)
            self._attr_extra_state_attributes.update(
                {
                    "last_result": "error",
                    "gates_total": 0,
                    "gates_passed": 0,
                    "gates_failed": 0,
                    "gates_warned": 0,
                    "failed_gates": [],
                    "warned_gates": [],
                    "duration_ms": int(
                        (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                    ),
                }
            )

            # Write error state to HA
            self.async_write_ha_state()

    async def _execute_gates(self, operation_data: dict[str, Any]) -> dict[str, Any]:
        """Execute quality gates check.

        Args:
            operation_data: Dictionary with target_device (optional)

        Returns:
            Dictionary with gate results
        """
        from .gates import QualityGateChecker

        start_time = datetime.now(timezone.utc)
        target_device = operation_data.get("target_device")
        entry_id = operation_data.get("entry_id", self._entry_id)

        # Initialize gate checker
        gate_checker = QualityGateChecker(self.hass, self.input_reader)

        # Run all gates — entry-scoped to prevent cross-entry bleed (P0 fix)
        summary = await gate_checker.run_all_gates(target_device, entry_id=entry_id)

        # Calculate duration
        duration_ms = int(
            (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
        )

        # Map overall status to last_result (pass|warn|fail)
        overall = summary["overall"]
        last_result = overall  # pass, warn, fail directly map

        return {
            "last_result": last_result,
            "gates_total": summary["total"],
            "gates_passed": summary["passed"],
            "gates_failed": summary["failed"],
            "gates_warned": summary["warned"],
            "failed_gates": summary["failed_gates"],
            "warned_gates": summary["warned_gates"],
            "details": summary["details"],
            "duration_ms": duration_ms,
        }



# PVAutonomyOpsBuildPipelineButton removed in EPIC-006-WP3 (Deliverable C).
# Build pipeline is now integrated into the Flash button (_execute_flash).

