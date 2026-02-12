"""Contract Input Reader for PVAutonomy Ops.

Reads Contract Inputs A-F from Home Assistant.
Implements graceful degradation (no crashes on missing entities).

P3-8-001: Added Device Registry discovery for Factory + Production devices.
Directive: D-OPS-FACTORY-DISCOVERY-001
"""
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

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

# Device Registry matching constants (D-OPS-FACTORY-DISCOVERY-001)
MANUFACTURER_PVAUTONOMY = "PVAutonomy"
MODEL_FACTORY = "Edge101Factory"
MODEL_PRODUCTION = "Edge101"
DEVICE_KIND_FACTORY = "factory"
DEVICE_KIND_PRODUCTION = "production"


class ContractInputReader:
    """Read Contract Inputs from Home Assistant."""

    def __init__(self, hass: HomeAssistant):
        """Initialize input reader.
        
        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self.state_reader = HomeAssistantStateReader(hass)
        # Cache for Device Registry scan (avoids duplicate scans per update cycle)
        self._registry_cache: dict[str, list[dict[str, Any]]] | None = None
        self._registry_cache_time: float = 0.0
        self._registry_cache_ttl: float = 10.0  # seconds
        # Track previous counts for change-only logging
        self._prev_factory_count: int = -1
        self._prev_production_count: int = -1

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

    # ================================================================
    # Device Registry Discovery (P3-8-001)
    # D-OPS-FACTORY-DISCOVERY-001: Factory + Production via HA Device Registry
    # ================================================================

    async def get_registry_devices(self) -> dict[str, list[dict[str, Any]]]:
        """Scan HA Device Registry for PVAutonomy Edge101 devices.
        
        Uses a short-lived cache (10s TTL) to avoid duplicate scans
        when multiple sensors call this in the same update cycle.
        
        Identifies Factory vs Production devices by model field:
        - Edge101Factory → factory
        - Edge101 → production
        
        Returns:
            Dict with 'factory' and 'production' device lists.
            Each device is a dict: {id, name, kind, model, sw_version, mac, identifiers}
        """
        # Return cached result if fresh enough
        now = time.monotonic()
        if self._registry_cache is not None and (now - self._registry_cache_time) < self._registry_cache_ttl:
            return self._registry_cache

        registry = dr.async_get(self.hass)
        factory_devices: list[dict[str, Any]] = []
        production_devices: list[dict[str, Any]] = []

        for device_entry in registry.devices.values():
            # Check manufacturer
            if device_entry.manufacturer != MANUFACTURER_PVAUTONOMY:
                continue

            model = device_entry.model or ""
            kind = None

            if model == MODEL_FACTORY:
                kind = DEVICE_KIND_FACTORY
            elif model == MODEL_PRODUCTION:
                kind = DEVICE_KIND_PRODUCTION
            else:
                # Unknown PVAutonomy model — skip
                _LOGGER.debug(
                    "Skipping PVAutonomy device with unknown model: %s (name=%s)",
                    model,
                    device_entry.name,
                )
                continue

            # Extract MAC from connections if available
            mac = None
            for conn_type, conn_id in device_entry.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac = conn_id
                    break

            # Build device info dict
            device_info = {
                "id": device_entry.id,
                "name": device_entry.name or "",
                "kind": kind,
                "model": model,
                "sw_version": device_entry.sw_version or "",
                "mac": mac,
                "identifiers": [
                    list(ident) for ident in device_entry.identifiers
                ],
            }

            if kind == DEVICE_KIND_FACTORY:
                factory_devices.append(device_info)
            else:
                production_devices.append(device_info)

        # Log at INFO only when counts change, DEBUG otherwise
        fc = len(factory_devices)
        pc = len(production_devices)
        if fc != self._prev_factory_count or pc != self._prev_production_count:
            _LOGGER.info(
                "Device Registry scan: %d factory, %d production",
                fc, pc,
            )
            self._prev_factory_count = fc
            self._prev_production_count = pc
        else:
            _LOGGER.debug(
                "Device Registry scan (cached counts): %d factory, %d production",
                fc, pc,
            )

        result = {
            "factory": factory_devices,
            "production": production_devices,
        }
        # Store in cache
        self._registry_cache = result
        self._registry_cache_time = now
        return result

    async def get_all_devices_for_dropdown(self) -> list[dict[str, str]]:
        """Get unified device list for dropdown population.
        
        Merges Device Registry (Factory + Production) into a single list
        suitable for input_select options.
        
        Returns:
            List of dicts: {value, label, kind}
            - value: the option value for input_select
            - label: human-readable display label
            - kind: 'factory' or 'production'
        """
        registry_devices = await self.get_registry_devices()
        dropdown_items: list[dict[str, str]] = []

        # Add factory devices
        for dev in registry_devices["factory"]:
            name = dev["name"]
            dropdown_items.append({
                "value": name,
                "label": f"{name} (factory)",
                "kind": DEVICE_KIND_FACTORY,
            })

        # Add production devices (from legacy template sensor for compatibility)
        legacy_devices = await self.get_discovered_devices()
        for dev_name in legacy_devices:
            dropdown_items.append({
                "value": dev_name,
                "label": f"{dev_name} (production)",
                "kind": DEVICE_KIND_PRODUCTION,
            })

        # Deduplicate: if a device appears in both registry and legacy, keep registry
        seen_values = set()
        unique_items: list[dict[str, str]] = []
        for item in dropdown_items:
            if item["value"] not in seen_values:
                seen_values.add(item["value"])
                unique_items.append(item)

        _LOGGER.debug(
            "Dropdown items: %d total (%s)",
            len(unique_items),
            [i["value"] for i in unique_items],
        )
        return unique_items

    async def get_selected_device_kind(self) -> str | None:
        """Determine if the currently selected device is factory or production.
        
        Returns:
            'factory', 'production', or None if no device selected
        """
        selected = await self.get_selected_device()
        if selected is None:
            return None

        registry_devices = await self.get_registry_devices()

        # Check factory devices
        for dev in registry_devices["factory"]:
            if dev["name"] == selected:
                return DEVICE_KIND_FACTORY

        # Check production (legacy)
        production = await self.get_discovered_devices()
        if selected in production:
            return DEVICE_KIND_PRODUCTION

        return None

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
