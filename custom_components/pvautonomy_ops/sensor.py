"""PVAutonomy Ops Sensors (Contract Outputs G, H).

Publishes add-on status and device count to Home Assistant.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONTRACT_VERSION,
    DOMAIN,
    ENTITY_DEVICE_COUNT_SENSOR,
    ENTITY_STATUS_SENSOR,
    STATE_DEGRADED,
    STATE_ERROR,
    STATE_INITIALIZING,
    STATE_OK,
    STATE_WARN,
    VERSION,
)
from .discovery import ContractInputReader

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy Ops sensors from a ConfigEntry."""
    _LOGGER.info("Setting up PVAutonomy Ops sensors (ConfigEntry)")

    input_reader: ContractInputReader = hass.data[DOMAIN]["input_reader"]
    operation_tracker = hass.data[DOMAIN]["operation_tracker"]

    async_add_entities(
        [
            PVAutonomyOpsStatusSensor(input_reader, operation_tracker),
            PVAutonomyOpsDevicesCountSensor(input_reader),
        ],
        True,
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up PVAutonomy Ops sensors (legacy YAML, kept for backward compat)."""
    _LOGGER.info("Setting up PVAutonomy Ops sensors (YAML platform)")

    input_reader: ContractInputReader = hass.data[DOMAIN]["input_reader"]
    operation_tracker = hass.data[DOMAIN]["operation_tracker"]

    async_add_entities(
        [
            PVAutonomyOpsStatusSensor(input_reader, operation_tracker),
            PVAutonomyOpsDevicesCountSensor(input_reader),
        ],
        True,
    )


class PVAutonomyOpsStatusSensor(SensorEntity):
    """Output G: sensor.pvautonomy_ops_status."""

    _attr_name = "PVAutonomy Ops Status"
    _attr_unique_id = ENTITY_STATUS_SENSOR
    _attr_suggested_object_id = ENTITY_STATUS_SENSOR

    def __init__(self, input_reader: ContractInputReader, operation_tracker) -> None:
        self.input_reader = input_reader
        self.operation_tracker = operation_tracker  # Phase 3: lifecycle tracking
        self._attr_native_value = STATE_INITIALIZING
        self._attr_extra_state_attributes = {
            "version": VERSION,
            "contract_version": CONTRACT_VERSION,
            # Gates attributes (initialized as null)
            "gates_last_run": None,
            "gates_overall": None,
            "gates_passed_count": None,
            "gates_failed_count": None,
            "gates_warn_count": None,
            "gates_fail": None,
            "gates_warn": None,
            "gates_details": None,
            # Flash attributes (Phase 3.3)
            "flash_stage": None,
            "flash_version": None,
            "flash_target_device": None,
            "flash_last_success": None,
            "flash_last_error": None,
            "flash_last_error_time": None,
        }
        # Store latest gate results (updated via event)
        self._gate_results = None
        # Store latest flash state (updated via event)
        self._flash_state = None

    async def async_added_to_hass(self) -> None:
        """Register event listeners when sensor is added to HA."""
        await super().async_added_to_hass()
        
        # Listen for gate completion events
        self.hass.bus.async_listen(
            f"{DOMAIN}_gates_completed",
            self._handle_gates_completed
        )
        
        # Listen for flash stage events (Phase 3.3)
        self.hass.bus.async_listen(
            f"{DOMAIN}_flash_stage",
            self._handle_flash_stage
        )
        
        _LOGGER.debug("Registered gates_completed + flash_stage event listeners")

    async def _handle_gates_completed(self, event) -> None:
        """Handle gate completion event and update attributes.
        
        Args:
            event: Event containing gate results
        """
        data = event.data
        
        self._gate_results = {
            "gates_last_run": data.get("checked_at"),
            "gates_overall": data.get("overall"),
            "gates_passed_count": data.get("gates_passed"),
            "gates_failed_count": data.get("gates_failed"),
            "gates_warn_count": data.get("gates_warned"),
            "gates_fail": data.get("failed_gates"),
            "gates_warn": data.get("warned_gates"),
            "gates_details": data.get("details"),
        }
        
        _LOGGER.info(
            "Gates completed event received: %s (passed=%d, warned=%d, failed=%d)",
            data.get("overall"),
            data.get("gates_passed", 0),
            data.get("gates_warned", 0),
            data.get("gates_failed", 0),
        )
        
        # Trigger sensor update to refresh attributes
        self.async_schedule_update_ha_state(True)

    async def _handle_flash_stage(self, event) -> None:
        """Handle flash stage event and update attributes.
        
        Args:
            event: Event containing flash stage info
        """
        data = event.data
        stage = data.get("stage")
        
        # Initialize flash state if not exists (preserve existing values)
        if self._flash_state is None:
            self._flash_state = {
                "flash_stage": None,
                "flash_version": None,
                "flash_target_device": None,
                "flash_last_success": None,
                "flash_last_error": None,
                "flash_last_error_time": None,
            }
        
        # Update current stage info
        self._flash_state["flash_stage"] = stage
        self._flash_state["flash_version"] = data.get("version")
        self._flash_state["flash_target_device"] = data.get("target_device")
        
        # Update success/error tracking based on final stage
        if stage == "complete":
            self._flash_state["flash_last_success"] = datetime.now(timezone.utc).isoformat()
            # Clear error fields on success
            self._flash_state["flash_last_error"] = None
            self._flash_state["flash_last_error_time"] = None
        elif stage == "failed":
            error_msg = data.get("error", f"Flash failed at stage: {stage}")
            self._flash_state["flash_last_error"] = error_msg
            self._flash_state["flash_last_error_time"] = datetime.now(timezone.utc).isoformat()
        
        _LOGGER.info(
            "Flash stage event received: %s (version=%s, device=%s)",
            stage,
            data.get("version"),
            data.get("target_device"),
        )
        
        # Trigger sensor update to refresh attributes
        self.async_schedule_update_ha_state(True)

    async def async_update(self) -> None:
        try:
            validation = await self.input_reader.validate_inputs()
            devices = await self.input_reader.get_discovered_devices()
            selected = await self.input_reader.get_selected_device()

            # P3-8-001: Device Registry scan for Factory + Production
            registry_devices = await self.input_reader.get_registry_devices()
            factory_devices = registry_devices.get("factory", [])
            production_devices = registry_devices.get("production", [])
            selected_kind = await self.input_reader.get_selected_device_kind()

            devices_online = 0
            devices_offline = 0

            for device in devices:
                health = await self.input_reader.get_device_health(device)
                if not health.get("available", False):
                    devices_offline += 1
                    continue
                # Contract: True = problem/offline
                if health.get("state", False):
                    devices_offline += 1
                else:
                    devices_online += 1

            last_error = None
            last_error_time = None

            if not validation.get("valid", False):
                self._attr_native_value = STATE_DEGRADED
                missing = validation.get("missing_inputs", [])
                last_error = f"Missing inputs: {', '.join(missing)}"
                last_error_time = datetime.now(timezone.utc).isoformat()
            elif len(devices) == 0:
                self._attr_native_value = STATE_WARN
                last_error = "No devices discovered"
                last_error_time = datetime.now(timezone.utc).isoformat()
            elif devices_offline > 0:
                self._attr_native_value = STATE_WARN
            else:
                self._attr_native_value = STATE_OK

            # Merge Phase 3 operation tracking with Phase 2 attributes
            op_attrs = self.operation_tracker.to_dict()
            
            # Smart last_discovery: Only update when Discover action succeeds
            # Otherwise keep old value for accurate tracking
            if op_attrs.get("op_name") == "discover" and op_attrs.get("op_state") == "success":
                last_discovery = op_attrs.get("op_finished")
            else:
                # Preserve existing last_discovery from attributes (persistent across updates)
                last_discovery = self._attr_extra_state_attributes.get(
                    "last_discovery", 
                    datetime.now(timezone.utc).isoformat()  # Fallback for first run
                )
            
            # Merge gate results if available (from event)
            gate_attrs = {}
            if self._gate_results:
                gate_attrs = self._gate_results
            else:
                # Preserve existing gates attributes (persistent)
                gate_attrs = {
                    "gates_last_run": self._attr_extra_state_attributes.get("gates_last_run"),
                    "gates_overall": self._attr_extra_state_attributes.get("gates_overall"),
                    "gates_passed_count": self._attr_extra_state_attributes.get("gates_passed_count"),
                    "gates_failed_count": self._attr_extra_state_attributes.get("gates_failed_count"),
                    "gates_warn_count": self._attr_extra_state_attributes.get("gates_warn_count"),
                    "gates_fail": self._attr_extra_state_attributes.get("gates_fail"),
                    "gates_warn": self._attr_extra_state_attributes.get("gates_warn"),
                    "gates_details": self._attr_extra_state_attributes.get("gates_details"),
                }
            
            # Merge flash state if available (from event) - Phase 3.3
            flash_attrs = {}
            if self._flash_state:
                flash_attrs = self._flash_state
            else:
                # Preserve existing flash attributes (persistent)
                flash_attrs = {
                    "flash_stage": self._attr_extra_state_attributes.get("flash_stage"),
                    "flash_version": self._attr_extra_state_attributes.get("flash_version"),
                    "flash_target_device": self._attr_extra_state_attributes.get("flash_target_device"),
                    "flash_last_success": self._attr_extra_state_attributes.get("flash_last_success"),
                    "flash_last_error": self._attr_extra_state_attributes.get("flash_last_error"),
                    "flash_last_error_time": self._attr_extra_state_attributes.get("flash_last_error_time"),
                }
            
            self._attr_extra_state_attributes = {
                # Contract v1.0.0 attributes
                "version": VERSION,
                "contract_version": CONTRACT_VERSION,
                "last_discovery": last_discovery,
                "devices_total": len(devices),
                "devices_online": devices_online,
                "devices_offline": devices_offline,
                "active_device": selected,
                "active_device_kind": selected_kind,
                "last_error": last_error or op_attrs.get("last_error"),
                "last_error_time": last_error_time or op_attrs.get("last_error_time"),
                
                # P3-8-001: Factory + Production device lists (D-OPS-FACTORY-DISCOVERY-001 R4)
                "factory_devices": [d["name"] for d in factory_devices],
                "production_devices": [d["name"] for d in production_devices],
                "factory_count": len(factory_devices),
                "production_count": len(production_devices),
                
                # Phase 3 extensions (operation lifecycle)
                "op_state": op_attrs.get("op_state"),
                "op_name": op_attrs.get("op_name"),
                "op_started": op_attrs.get("op_started"),
                "op_finished": op_attrs.get("op_finished"),
                "op_progress": op_attrs.get("op_progress"),
                "op_duration_ms": op_attrs.get("op_duration_ms"),
                "last_action": op_attrs.get("last_action"),
                "last_action_time": op_attrs.get("last_action_time"),
                
                # Gates attributes (Phase 3 + User Directive)
                **gate_attrs,
                
                # Flash attributes (Phase 3.3)
                **flash_attrs,
            }

        except Exception as e:
            _LOGGER.error("Error updating status sensor: %s", e, exc_info=True)
            self._attr_native_value = STATE_ERROR
            self._attr_extra_state_attributes = {
                "version": VERSION,
                "contract_version": CONTRACT_VERSION,
                "last_error": str(e),
                "last_error_time": datetime.now(timezone.utc).isoformat(),
            }


class PVAutonomyOpsDevicesCountSensor(SensorEntity):
    """Output H: sensor.pvautonomy_ops_devices_count."""

    _attr_name = "PVAutonomy Ops Devices Count"
    _attr_unique_id = ENTITY_DEVICE_COUNT_SENSOR
    _attr_suggested_object_id = ENTITY_DEVICE_COUNT_SENSOR

    def __init__(self, input_reader: ContractInputReader) -> None:
        self.input_reader = input_reader
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {
            "online": 0,
            "offline": 0,
            "unknown": 0,
            "factory": 0,
            "production": 0,
        }

    async def async_update(self) -> None:
        try:
            devices = await self.input_reader.get_discovered_devices()
            registry_devices = await self.input_reader.get_registry_devices()

            online = 0
            offline = 0
            unknown = 0

            for device in devices:
                health = await self.input_reader.get_device_health(device)
                if not health.get("available", False):
                    unknown += 1
                elif health.get("state", False):
                    offline += 1
                else:
                    online += 1

            factory_count = len(registry_devices.get("factory", []))
            production_count = len(registry_devices.get("production", []))

            self._attr_native_value = len(devices) + factory_count
            self._attr_extra_state_attributes = {
                "online": online,
                "offline": offline,
                "unknown": unknown,
                "factory": factory_count,
                "production": production_count,
            }

        except Exception as e:
            _LOGGER.error("Error updating device count sensor: %s", e, exc_info=True)
            self._attr_native_value = 0
            self._attr_extra_state_attributes = {"online": 0, "offline": 0, "unknown": 0}