"""Contract Input Reader for PVAutonomy Ops.

Reads Contract Inputs A-F from Home Assistant.
Implements graceful degradation (no crashes on missing entities).
"""
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    ENTITY_CONTROL_NUMBER_PATTERN,
    ENTITY_CONTROL_SWITCH_PATTERN,
    ENTITY_DEVICE_SELECTOR,
    ENTITY_DISCOVERY_SENSOR,
    ENTITY_HARDWARE_FAMILY_PATTERN,
    ENTITY_HEALTH_PATTERN,
    ENTITY_RUNTIME_SENSOR_PATTERN,
)
from .utils.ha_api import HomeAssistantStateReader

_LOGGER = logging.getLogger(__name__)


class ContractInputReader:
    """Read Contract Inputs from Home Assistant."""

    def __init__(self, hass: HomeAssistant):
        """Initialize input reader.
        
        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self.state_reader = HomeAssistantStateReader(hass)

    async def get_discovered_devices(self) -> list[str]:
        """Read Input A: sensor.edge101_production_devices.devices[]
        
        Returns:
            List of device names (empty if not found)
        """
        devices = await self.state_reader.get_attribute(
            ENTITY_DISCOVERY_SENSOR, "devices", default=[]
        )
        
        if not devices:
            _LOGGER.warning(
                "No devices found in %s.devices", ENTITY_DISCOVERY_SENSOR
            )
        else:
            _LOGGER.debug("Discovered %d devices: %s", len(devices), devices)
        
        return devices

    async def get_selected_device(self) -> str | None:
        """Read Input B: input_select.edge101_selected_production_device
        
        Returns:
            Selected device name or None if not selected or 'none'
        """
        selected = await self.state_reader.get_state_value(
            ENTITY_DEVICE_SELECTOR, default="none"
        )
        
        if selected == "none" or selected is None:
            _LOGGER.debug("No device selected (state=%s)", selected)
            return None
        
        _LOGGER.debug("Selected device: %s", selected)
        return selected

    async def get_device_health(self, device: str) -> dict[str, Any]:
        """Read Input C: binary_sensor.{device}_health + attributes
        
        Args:
            device: Device name (e.g., 'sph10k_haus_03')
            
        Returns:
            Dict with health info: {
                'state': bool,  # True = problem, False = ok
                'device_name': str,
                'entity_count': int,
                'missing_sensors': list[str],
                'last_check': str | None,
                'available': bool  # False if entity missing
            }
        """
        entity_id = ENTITY_HEALTH_PATTERN.format(device=device)
        state = await self.state_reader.get_state(entity_id)
        
        if state is None:
            _LOGGER.warning("Health sensor not found: %s", entity_id)
            return {
                "available": False,
                "state": None,
                "device_name": device,
                "entity_count": 0,
                "missing_sensors": [],
                "last_check": None,
            }
        
        # Binary sensor: on = problem, off = ok
        has_problem = state.state == "on"
        
        return {
            "available": True,
            "state": has_problem,
            "device_name": state.attributes.get("device_name", device),
            "entity_count": state.attributes.get("entity_count", 0),
            "missing_sensors": state.attributes.get("missing_sensors", []),
            "last_check": state.attributes.get("last_check"),
        }

    async def get_device_metrics(self, device: str) -> dict[str, Any]:
        """Read Inputs D+E: sensor/number.{device}_{metric}_device
        
        Args:
            device: Device name
            
        Returns:
            Dict of all runtime sensors and control entities for this device
        """
        metrics = {}
        
        # Get all entities for this device
        # Pattern: sensor.{device}_*_device
        sensor_pattern = f"sensor.{device}_"
        number_pattern = f"number.{device}_"
        switch_pattern = f"switch.{device}_"
        
        # Search through all states
        for entity_id, state in self.hass.states.async_all():
            if not state:
                continue
            
            # Check if entity matches device patterns
            if entity_id.startswith(sensor_pattern) and entity_id.endswith("_device"):
                # Extract metric name
                metric = entity_id.replace(sensor_pattern, "").replace("_device", "")
                metrics[f"sensor_{metric}"] = {
                    "entity_id": entity_id,
                    "state": state.state,
                    "unit": state.attributes.get("unit_of_measurement"),
                    "device_class": state.attributes.get("device_class"),
                }
            
            elif entity_id.startswith(number_pattern) and entity_id.endswith("_device"):
                metric = entity_id.replace(number_pattern, "").replace("_device", "")
                metrics[f"number_{metric}"] = {
                    "entity_id": entity_id,
                    "state": state.state,
                    "min": state.attributes.get("min"),
                    "max": state.attributes.get("max"),
                    "step": state.attributes.get("step"),
                }
            
            elif entity_id.startswith(switch_pattern) and entity_id.endswith("_device"):
                metric = entity_id.replace(switch_pattern, "").replace("_device", "")
                metrics[f"switch_{metric}"] = {
                    "entity_id": entity_id,
                    "state": state.state,
                }
        
        _LOGGER.debug("Found %d metrics for device %s", len(metrics), device)
        return metrics

    async def get_hardware_family(self, device: str) -> str | None:
        """Read Input F: sensor.{device}_hardware_family
        
        Args:
            device: Device name
            
        Returns:
            Hardware family string or None if not found
        """
        entity_id = ENTITY_HARDWARE_FAMILY_PATTERN.format(device=device)
        
        family = await self.state_reader.get_state_value(entity_id, default=None)
        
        if family is None or family == "unknown":
            _LOGGER.debug(
                "Hardware family not found for %s, inferring from entity pattern",
                device,
            )
            # Fallback: infer from device name pattern
            # If entity contains 'edge101' or similar, assume edge101
            if "edge101" in device.lower():
                return "edge101"
            # Default fallback
            return "unknown"
        
        _LOGGER.debug("Hardware family for %s: %s", device, family)
        return family

    async def validate_inputs(self) -> dict[str, Any]:
        """Validate all required inputs are available.
        
        Returns:
            Dict with validation results: {
                'valid': bool,
                'missing_inputs': list[str],
                'warnings': list[str],
            }
        """
        missing = []
        warnings = []
        
        # Check Input A (Discovery) - CRITICAL
        if not await self.state_reader.entity_exists(ENTITY_DISCOVERY_SENSOR):
            missing.append(f"Input A: {ENTITY_DISCOVERY_SENSOR}")
        
        # Check Input B (Selector) - CRITICAL
        if not await self.state_reader.entity_exists(ENTITY_DEVICE_SELECTOR):
            missing.append(f"Input B: {ENTITY_DEVICE_SELECTOR}")
        
        # Check discovered devices
        devices = await self.get_discovered_devices()
        if not devices:
            warnings.append("No devices discovered (Input A returns empty list)")
        
        # Check selected device
        selected = await self.get_selected_device()
        if selected is None:
            warnings.append("No device selected (Input B is 'none')")
        
        return {
            "valid": len(missing) == 0,
            "missing_inputs": missing,
            "warnings": warnings,
        }
