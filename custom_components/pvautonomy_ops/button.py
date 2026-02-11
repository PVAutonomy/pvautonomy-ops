"""PVAutonomy Ops Buttons (Contract Outputs I, J, K, L).

Phase 3: EXECUTE Actions via button press.
MVP: I (Discover), J (Flash) implemented first.
"""
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN
from .artifacts import download_artifact, verify_artifact, get_latest_version, ArtifactError
from .flash_uploader import (
    ota_upload,
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

_LOGGER = logging.getLogger(__name__)


async def check_gates_passed(hass: HomeAssistant) -> tuple[bool, str]:
    """Check if quality gates have passed (hard rule for critical actions).
    
    Args:
        hass: Home Assistant instance
        
    Returns:
        Tuple of (gates_ok: bool, error_message: str)
        
    Usage:
        Before executing critical actions (flash, migrate, cleanup):
        gates_ok, error = await check_gates_passed(hass)
        if not gates_ok:
            raise ValueError(error)
    """
    # Read sensor.pvautonomy_ops_status gates attributes
    status_sensor = hass.states.get("sensor.pvautonomy_ops_status")
    
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

    operation_runner = hass.data[DOMAIN]["operation_runner"]
    input_reader = hass.data[DOMAIN]["input_reader"]

    async_add_entities(
        [
            PVAutonomyOpsDiscoverButton(hass, operation_runner, input_reader),
            PVAutonomyOpsRestartButton(hass, operation_runner, input_reader),
            PVAutonomyOpsRunGatesButton(hass, operation_runner, input_reader),
            PVAutonomyOpsFlashButton(hass, operation_runner, input_reader),
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

    operation_runner = hass.data[DOMAIN]["operation_runner"]
    input_reader = hass.data[DOMAIN]["input_reader"]

    async_add_entities(
        [
            PVAutonomyOpsDiscoverButton(hass, operation_runner, input_reader),
            PVAutonomyOpsRestartButton(hass, operation_runner, input_reader),
            PVAutonomyOpsRunGatesButton(hass, operation_runner, input_reader),
            # Phase 3.3: Flash button with guard protection (D-ADDON-FLASH-GUARD-001)
            PVAutonomyOpsFlashButton(hass, operation_runner, input_reader),
        ],
        True,
    )


class PVAutonomyOpsDiscoverButton(ButtonEntity):
    """Output I: button.pvautonomy_ops_discover.
    
    Manually trigger device discovery (re-scan inverter registry + packages).
    Contract: ops-contract-v1.md Section 2.3.I
    """

    _attr_name = "PVAutonomy Ops Discover"
    _attr_unique_id = ENTITY_BUTTON_DISCOVER
    _attr_suggested_object_id = ENTITY_BUTTON_DISCOVER
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader) -> None:
        """Initialize discover button.
        
        Args:
            hass: Home Assistant instance
            operation_runner: Operation runner with lock and tracking
            input_reader: Contract input reader
        """
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        
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
        self._attr_extra_state_attributes = {
            "last_result": "success" if result["success"] else "error",
            "devices_found": result.get("result", {}).get("devices_found"),
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
        """Execute discovery logic.
        
        Returns:
            Dict with discovery results:
                - devices_found: int
                - devices_new: int (always 0 for re-scan)
                - devices_removed: int (always 0 for re-scan)
                - devices: list[dict]
        """
        # Get current discovered devices (re-reads Input A)
        devices = await self.input_reader.get_discovered_devices()
        
        # For MVP: Discovery is passive (just re-reads template sensor)
        # Future: Active scan of inverter registry + ESPHome devices
        
        return {
            "devices_found": len(devices),
            "devices_new": 0,  # MVP: no diff tracking yet
            "devices_removed": 0,
            "devices": [{"entity_id": d} for d in devices],
        }


class PVAutonomyOpsFlashButton(ButtonEntity):
    """Output J: button.pvautonomy_ops_flash_production.
    
    Flash production firmware to selected Edge101 device.
    Contract: ops-contract-v1.md Section 2.3.J
    
    NOTE: Deferred to Phase 3.1 (requires ESPHome OTA integration)
    """

    _attr_name = "PVAutonomy Ops Flash Production"
    _attr_unique_id = ENTITY_BUTTON_FLASH
    _attr_suggested_object_id = ENTITY_BUTTON_FLASH
    _attr_icon = "mdi:download-box"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader) -> None:
        """Initialize flash button.
        
        Args:        hass: Home Assistant instance
            operation_runner: Operation runner with lock and tracking
            input_reader: Contract input reader
        """
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        
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
        """
        from .flash_guards import check_flash_guards, format_guard_block_message
        
        _LOGGER.info("Flash button pressed")
        
        # Get selected device (legacy input_select)
        selected_device = await self.input_reader.get_selected_device()
        
        if not selected_device or selected_device == "none":
            _LOGGER.warning("Flash blocked: no device selected")
            self._attr_extra_state_attributes = {
                "target_device": None,
                "last_result": "rejected",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": "No device selected",
                "block_reason": None,
            }
            self.async_write_ha_state()
            return
        
        # D-ADDON-FLASH-GUARD-001: Check preflight gates (MUST)
        guards_passed, block_reason, guard_message = await check_flash_guards(self.hass)
        
        if not guards_passed:
            _LOGGER.warning(
                "Flash BLOCKED by guards: %s (%s)",
                block_reason,
                guard_message
            )
            
            # Format user-friendly block message
            block_message = await format_guard_block_message(self.hass, block_reason)
            
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "blocked",
                "firmware_version": None,
                "flash_duration_sec": None,
                "error_message": block_message,
                "block_reason": block_reason,  # Machine-readable
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
        
        # Guards passed - execute flash via operation runner
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
            }
        else:
            self._attr_extra_state_attributes = {
                "target_device": selected_device,
                "last_result": "success" if result.get("success") else "failed",
                "firmware_version": result.get("result", {}).get("firmware_version") if result.get("result") else None,
                "flash_duration_sec": result["duration_ms"] // 1000 if result.get("duration_ms") else None,
                "error_message": result.get("error"),
                "block_reason": None,
            }
        
        self.async_write_ha_state()

    async def _execute_flash(self, device_id: str) -> dict[str, Any]:
        """Execute flash operation with stage machine (Phase 3.3 MVP).
        
        Args:
            device_id: Device entity ID to flash
            
        Returns:
            Dict with flash results
            
        Stages:
            init → preflight → download → verify → upload → postcheck → complete/failed
        """
        from .flash_guards import check_flash_guards
        
        # Flash stage machine state (MVP)
        flash_state = {
            "stage": "init",
            "version": None,
            "target_device": device_id,
            "error": None,
        }
        
        def update_stage(stage: str, progress: int):
            """Update flash stage and trigger sensor refresh."""
            flash_state["stage"] = stage
            _LOGGER.info("Flash stage: %s (progress=%d%%)", stage, progress)
            
            # Trigger sensor update with flash stage info
            self.hass.bus.async_fire(
                f"{DOMAIN}_flash_stage",
                {
                    "stage": stage,
                    "progress": progress,
                    "version": flash_state.get("version"),
                    "target_device": device_id,
                    "error": flash_state.get("error"),  # Include error message for failed stage
                }
            )
        
        try:
            # STAGE 1: init (0%)
            update_stage("init", 0)
            
            # STAGE 2: preflight (10%)
            update_stage("preflight", 10)
            guards_passed, block_reason, guard_message = await check_flash_guards(self.hass)
            
            if not guards_passed:
                flash_state["error"] = f"Preflight failed: {guard_message}"
                update_stage("failed", 0)
                raise ValueError(flash_state["error"])
            
            _LOGGER.info("Flash preflight PASSED")
            
            # Prepare temp directory for artifacts
            temp_dir = Path(tempfile.mkdtemp(prefix="pvautonomy_flash_"))
            _LOGGER.debug("Created temp dir: %s", temp_dir)
            
            try:
                # Read runtime config (Options Flow values)
                config = self.hass.data[DOMAIN].get("config", {})
                hw_family = config.get("artifact_hw_family_default", "edge101")
                channel = config.get("artifact_channel", "stable")
                min_size_kb = config.get("flash_min_firmware_size_kb", 300)

                # STAGE 3: download (30%) - REAL IMPLEMENTATION
                update_stage("download", 30)
                _LOGGER.info("Downloading firmware artifact...")
                
                # Download firmware from GitHub Releases
                # MVP: get version from artifacts module
                firmware_version = get_latest_version(hw_family, channel)
                artifact = await download_artifact(
                    version=firmware_version,
                    hw_family=hw_family,
                    temp_dir=temp_dir,
                    channel=channel,
                    owner=config.get("artifact_owner"),
                    repo=config.get("artifact_repo"),
                )
                
                flash_state["version"] = artifact.version
                flash_state["artifact_path"] = str(artifact.firmware_path)
                firmware_size = artifact.firmware_path.stat().st_size
                _LOGGER.info(
                    "Downloaded firmware: version=%s, size=%d bytes",
                    artifact.version,
                    firmware_size
                )

                # MIN_FIRMWARE_SIZE gate (P3-6-001): reject stub/corrupt binaries
                min_size_bytes = min_size_kb * 1024
                if firmware_size < min_size_bytes:
                    flash_state["error"] = (
                        f"Firmware too small: {firmware_size} bytes "
                        f"(minimum {min_size_bytes} bytes / {min_size_kb} KB)"
                    )
                    update_stage("failed", 0)
                    raise ValueError(flash_state["error"])
                
                # STAGE 4: verify (50%) - REAL IMPLEMENTATION
                update_stage("verify", 50)
                _LOGGER.info("Verifying firmware integrity (SHA256)...")
                
                # Verify SHA256 checksum
                await verify_artifact(artifact)
                
                _LOGGER.info("Firmware integrity verified: SHA256 OK")
                
                # STAGE 5: upload (30→80%) — REAL OTA via espota2 SHA256
                update_stage("upload", 30)
                _LOGGER.info("Starting OTA upload to device: %s", device_id)
                
                # Resolve device IP from HA states (no hardcoded IPs)
                device_ip = resolve_device_ip(self.hass, device_id)
                if not device_ip:
                    flash_state["error"] = f"Cannot resolve IP for device: {device_id}"
                    update_stage("failed", 0)
                    raise OTAError(flash_state["error"])
                
                # Get OTA password from secrets (no hardcoded passwords)
                ota_password = await self.hass.async_add_executor_job(
                    get_ota_password, self.hass, device_id
                )
                if not ota_password:
                    _LOGGER.warning("No OTA password found — attempting upload without auth")
                
                # Progress callback: map OTA 0-100% → flash 30-80%
                async def _upload_progress(pct: int) -> None:
                    flash_pct = 30 + int(pct * 0.5)
                    update_stage("upload", flash_pct)
                
                # Execute OTA upload
                await ota_upload(
                    self.hass,
                    host=device_ip,
                    port=OTA_DEFAULT_PORT,
                    password=ota_password,
                    firmware_path=artifact.firmware_path,
                    progress_cb=_upload_progress,
                    timeout_s=120.0,
                )
                
                _LOGGER.info("OTA upload completed successfully")
                
                # STAGE 6: postcheck (85→95%) — detect reboot + verify online
                update_stage("postcheck", 85)
                _LOGGER.info("Postcheck: waiting for device reboot...")
                
                import asyncio as _asyncio
                
                # Brief pause — device is rebooting after OTA
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
                
                if monitor_entity:
                    init_state = self.hass.states.get(monitor_entity)
                    if monitor_type == "uptime" and init_state and init_state.state not in ("unavailable", "unknown"):
                        try:
                            initial_uptime = float(init_state.state)
                        except (ValueError, TypeError):
                            initial_uptime = None
                    
                    _LOGGER.info("Postcheck monitoring via %s (%s)", monitor_entity, monitor_type)
                    
                    # Poll for reboot detection (max 90s, every 2s)
                    offline_detected = False
                    max_wait = 90
                    elapsed = 0
                    
                    while elapsed < max_wait:
                        await _asyncio.sleep(2)
                        elapsed += 2
                        
                        # Update progress: 85→95% over polling period
                        poll_pct = 85 + int((elapsed / max_wait) * 10)
                        update_stage("postcheck", min(poll_pct, 95))
                        
                        cur_state = self.hass.states.get(monitor_entity)
                        if not cur_state:
                            continue
                        
                        cur_available = cur_state.state not in ("unavailable", "unknown")
                        
                        # Method 1: Detect offline transition
                        if not offline_detected and not cur_available:
                            offline_detected = True
                            _LOGGER.info("Postcheck: device offline (elapsed=%ds)", elapsed)
                        
                        # Method 2: Uptime reset (device rebooted)
                        if monitor_type == "uptime" and cur_available and initial_uptime is not None:
                            try:
                                cur_uptime = float(cur_state.state)
                                if cur_uptime < initial_uptime:
                                    _LOGGER.info(
                                        "Postcheck: uptime reset %.1fs→%.1fs (reboot confirmed)",
                                        initial_uptime, cur_uptime
                                    )
                                    break
                            except (ValueError, TypeError):
                                pass
                        
                        # Method 3: Online after offline
                        if offline_detected and cur_available:
                            _LOGGER.info("Postcheck: device back online (elapsed=%ds)", elapsed)
                            break
                    else:
                        if offline_detected:
                            _LOGGER.warning("Postcheck: device offline but not back within %ds", max_wait)
                        else:
                            _LOGGER.warning("Postcheck: no reboot detected within %ds", max_wait)
                else:
                    _LOGGER.warning("Postcheck: no monitoring sensor found, waiting 15s as fallback")
                    await _asyncio.sleep(15)
                
                _LOGGER.info("Postcheck complete")
                
                # STAGE 7: complete (100%)
                update_stage("complete", 100)
                _LOGGER.info(
                    "Flash operation complete: version=%s, device=%s",
                    flash_state["version"],
                    device_id
                )
                
                # Micro-regression guard: Ensure stage is never unset on success path
                if not flash_state.get("stage") or flash_state["stage"] not in ["complete"]:
                    _LOGGER.warning(
                        "Flash stage missing or invalid after completion (stage=%s), forcing 'complete'",
                        flash_state.get("stage")
                    )
                    flash_state["stage"] = "complete"
                    update_stage("complete", 100)  # Re-fire event to ensure sensor gets it
                
                return {
                    "result": "success",
                    "device_id": device_id,
                    "firmware_version": flash_state["version"],
                    "flash_duration_sec": 0,  # Will be filled by operation_runner
                    "stage": "complete",
                }
                
            except ArtifactError as e:
                flash_state["error"] = f"Artifact error: {e}"
                update_stage("failed", 0)
                _LOGGER.error("Artifact download/verify failed: %s", e)
                raise
            
            except OTAError as e:
                flash_state["error"] = f"OTA upload error: {e}"
                update_stage("failed", 0)
                _LOGGER.error("OTA upload failed: %s", e)
                raise
                
            finally:
                # Cleanup temp directory (success or failure, in executor to avoid blocking)
                import shutil
                import asyncio
                await asyncio.get_event_loop().run_in_executor(
                    None, shutil.rmtree, temp_dir, True
                )
                _LOGGER.debug("Cleaned up temp dir: %s", temp_dir)
            
        except Exception as e:
            flash_state["error"] = str(e)
            update_stage("failed", 0)
            _LOGGER.error("Flash operation failed at stage %s: %s", flash_state["stage"], e)
            raise


class PVAutonomyOpsRestartButton(ButtonEntity):
    """Phase 3 Extension: button.pvautonomy_ops_restart_device.
    
    Restart selected Edge101 device via ESPHome service.
    NOT in Contract v1.0.0 (Phase 3 experimental feature).
    """

    _attr_name = "PVAutonomy Ops Restart Device"
    _attr_unique_id = ENTITY_BUTTON_RESTART
    _attr_suggested_object_id = ENTITY_BUTTON_RESTART
    _attr_icon = "mdi:restart"

    def __init__(self, hass: HomeAssistant, operation_runner, input_reader) -> None:
        """Initialize restart button.
        
        Args:
            hass: Home Assistant instance
            operation_runner: Operation runner with lock and tracking
            input_reader: Contract input reader
        """
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
        
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
        _LOGGER.info("Restart button pressed")
        
        # Get selected device (legacy input_select)
        selected_device = await self.input_reader.get_selected_device()
        
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
                try:
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
                except (ValueError, TypeError):
                    pass
            
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
        elif not offline_detected:
            _LOGGER.warning("Restart uncertain: no offline/uptime reset detected within %ds", max_wait)
            return {
                "status": "warn",
                "offline_detected": None,
                "online_detected": None,
                "error": f"No reboot detected within {max_wait}s (device may not have restarted)",
            }
        else:
            # Should not reach here
            return {
                "status": "error",
                "offline_detected": offline_detected,
                "online_detected": online_detected,
                "error": "Unexpected state in restart tracking",
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

    _attr_name = "PVAutonomy Ops Run Gates"
    _attr_unique_id = ENTITY_BUTTON_GATES
    _attr_suggested_object_id = ENTITY_BUTTON_GATES
    _attr_icon = "mdi:gate"

    def __init__(self, hass, operation_runner, input_reader):
        self.hass = hass
        self.operation_runner = operation_runner
        self.input_reader = input_reader
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
        _LOGGER.info("Run Gates button pressed")
        start_time = datetime.now(timezone.utc)

        # Get selected device (legacy input_select, optional filtering)
        selected_device = await self.input_reader.get_selected_device()

        try:
            # Execute gates via operation runner
            result = await self.operation_runner.run(
                "run_gates",
                self._execute_gates,
                {"target_device": selected_device},
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

        # Initialize gate checker
        gate_checker = QualityGateChecker(self.hass, self.input_reader)

        # Run all gates
        summary = await gate_checker.run_all_gates(target_device)

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

