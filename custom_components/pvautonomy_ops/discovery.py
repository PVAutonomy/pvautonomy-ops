"""Self-contained Discovery & Selection for PVAutonomy Ops.

EPIC-005-A1: Registry-first discovery, integration-owned selection,
capability-based health. No dependency on packages/setup/*.yaml.

Legacy Contract Inputs A-F are still supported as fallback.

P3-8-001: Device Registry discovery for Factory + Production devices.
Directive: D-OPS-FACTORY-DISCOVERY-001
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_SELECTED_DEVICE,
    DOMAIN,
    ENTITY_DEVICE_SELECTOR,
    ENTITY_DISCOVERY_SENSOR,
    ENTITY_HARDWARE_FAMILY_PATTERN,
    ENTITY_HEALTH_PATTERN,
    HEALTH_REQUIRED_CAPABILITIES,
    MODEL_REGISTRY_MAP,
)
from .utils.ha_api import HomeAssistantStateReader

_LOGGER = logging.getLogger(__name__)

# Device Registry matching constants (D-OPS-FACTORY-DISCOVERY-001)
MANUFACTURER_PVAUTONOMY = "PVAutonomy"
MODEL_FACTORY = "Edge101Factory"
MODEL_PRODUCTION = "Edge101"
DEVICE_KIND_FACTORY = "factory"
DEVICE_KIND_PRODUCTION = "production"
DEVICE_KIND_UNKNOWN = "unknown"

# Connectivity states
CONNECTIVITY_ONLINE = "online"
CONNECTIVITY_OFFLINE = "offline"
CONNECTIVITY_UNKNOWN = "unknown"


def _device_slug(value: str) -> str:
    """Normalize display names and device ids to the entity-id slug."""
    slug = value.lower().replace(" ", "_").replace("-", "_")
    if slug.startswith("edge101_"):
        slug = slug[len("edge101_"):]
    return slug


def _display_name_from_device_slug(device_name: str) -> str:
    """Return a customer-friendly label for legacy slug-only devices."""
    slug = _device_slug(device_name)
    parts = slug.split("_")
    if len(parts) < 3:
        return device_name

    model_slug = parts[0]
    number = parts[-1]
    site = "_".join(parts[1:-1])
    if model_slug not in MODEL_REGISTRY_MAP or not number.isdigit():
        return device_name

    model_label = model_slug.title()

    return f"{model_label} {site.replace('_', ' ').title()} {number.zfill(2)}"


# ============================================================================
# DiscoveredDevice dataclass (EPIC-005-A1)
# ============================================================================

@dataclass
class DiscoveredDevice:
    """A discovered PVAutonomy device.

    Primary key is ``device_id`` (HA Device Registry UUID).
    ``canonical_device_key`` (MAC last6) is optional metadata (PN-3).
    """

    device_id: str                          # HA device registry ID (always set)
    name: str                               # Device name
    mac: str | None = None                  # WiFi MAC if known
    canonical_device_key: str | None = None # last6 MAC (optional)
    state: str = DEVICE_KIND_UNKNOWN        # factory / production / unknown
    connectivity: str = CONNECTIVITY_UNKNOWN # online / offline / unknown
    metadata: dict = field(default_factory=dict)  # sw_version, model, identifiers


class ContractInputReader:
    """Self-contained discovery, selection, and health for PVAutonomy Ops.

    Primary path: HA Device/Entity Registry (no packages YAML needed).
    Legacy path: Contract Inputs A-F (backward compat fallback).
    """

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.state_reader = HomeAssistantStateReader(hass)
        # Cache for Device Registry scan
        self._registry_cache: dict[str, list[dict[str, Any]]] | None = None
        self._registry_cache_time: float = 0.0
        self._registry_cache_ttl: float = 10.0  # seconds
        # Track previous counts for change-only logging
        self._prev_factory_count: int = -1
        self._prev_production_count: int = -1

    # ================================================================
    # EPIC-005-A1: Unified discovery (registry-first + legacy fallback)
    # ================================================================

    async def get_all_discovered_devices(self) -> list[DiscoveredDevice]:
        """Discover all PVAutonomy devices from Device Registry + legacy fallback.

        Primary: scans HA Device Registry for manufacturer=PVAutonomy.
        Fallback: merges legacy template sensor devices (dedup by HA device_id).

        Returns:
            Unified list of DiscoveredDevice instances.
        """
        from .mac_utils import InvalidMACError, canonical_mac_last6

        devices: list[DiscoveredDevice] = []
        seen_device_ids: set[str] = set()

        # --- Primary: Device Registry ---
        registry_devices = await self.get_registry_devices()
        ent_reg = er.async_get(self.hass)

        for kind_key in ("factory", "production"):
            for dev_info in registry_devices.get(kind_key, []):
                ha_device_id = dev_info["id"]
                if ha_device_id in seen_device_ids:
                    continue
                seen_device_ids.add(ha_device_id)

                mac = dev_info.get("mac")
                device_key: str | None = None
                if mac:
                    try:
                        device_key = canonical_mac_last6(mac)
                    except InvalidMACError:
                        device_key = None

                connectivity = self._check_device_connectivity(
                    ent_reg, ha_device_id
                )

                devices.append(DiscoveredDevice(
                    device_id=ha_device_id,
                    name=dev_info.get("name", ""),
                    mac=mac,
                    canonical_device_key=device_key,
                    state=kind_key,
                    connectivity=connectivity,
                    metadata={
                        "model": dev_info.get("model", ""),
                        "sw_version": dev_info.get("sw_version", ""),
                        "identifiers": dev_info.get("identifiers", []),
                    },
                ))

        # --- Fallback: legacy template sensor (Input A) ---
        # Build normalized name set for name-based dedup against registry devices
        seen_names_normalized: set[str] = set()
        for d in devices:
            seen_names_normalized.add(
                d.name.lower().replace(" ", "_").replace("-", "_")
            )

        legacy_devices = await self.get_discovered_devices()
        for dev_name in legacy_devices:
            # Name-based dedup: normalize legacy name and compare to registry
            norm_name = dev_name.lower().replace(" ", "_").replace("-", "_")
            if norm_name in seen_names_normalized:
                _LOGGER.debug(
                    "Legacy device %s deduped by normalized name match",
                    dev_name,
                )
                continue

            # Entity probe dedup: try multiple sensor patterns
            ha_device_id = None
            for suffix in ("battery_soc_device", "ac_power_device",
                           "ac_output_power_device", "pv1_power_device"):
                probe = f"sensor.{dev_name}_{suffix}"
                entry = ent_reg.async_get(probe)
                if entry and entry.device_id:
                    ha_device_id = entry.device_id
                    break

            if ha_device_id and ha_device_id in seen_device_ids:
                _LOGGER.debug(
                    "Legacy device %s deduped (device_id %s in registry)",
                    dev_name, ha_device_id,
                )
                continue

            if not ha_device_id:
                ha_device_id = f"legacy_{dev_name}"
            seen_device_ids.add(ha_device_id)

            devices.append(DiscoveredDevice(
                device_id=ha_device_id,
                name=dev_name,
                state=DEVICE_KIND_PRODUCTION,
                connectivity=CONNECTIVITY_UNKNOWN,
                metadata={"source": "legacy_template_sensor"},
            ))

        _LOGGER.debug(
            "All discovered devices: %d total (%s)",
            len(devices),
            [d.name for d in devices],
        )
        return devices

    def _check_device_connectivity(
        self, ent_reg: er.EntityRegistry, ha_device_id: str
    ) -> str:
        """Check if a device has at least one online entity.

        Args:
            ent_reg: HA Entity Registry instance.
            ha_device_id: Device ID to check.

        Returns:
            "online" if any entity is not unavailable/unknown,
            "offline" if all are unavailable/unknown,
            "unknown" if no entities found.
        """
        device_entities = er.async_entries_for_device(ent_reg, ha_device_id)
        if not device_entities:
            return CONNECTIVITY_UNKNOWN

        for ent_entry in device_entities:
            state = self.hass.states.get(ent_entry.entity_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                return CONNECTIVITY_ONLINE

        return CONNECTIVITY_OFFLINE

    # ================================================================
    # EPIC-005-A1: Integration-owned selection (PN-2: per entry_id)
    # ================================================================

    async def get_selected_device_from_storage(
        self, entry_id: str | None = None
    ) -> str | None:
        """Get selected device from integration storage.

        EPIC-015 P2-01 — fail-closed semantics:
        - If entry_id is provided: return from that entry's storage ONLY.
          No legacy input_select fallback (prevents cross-entry bleed).
        - If entry_id is None: legacy fallback to input_select (backward
          compat for callers without entry context).

        Args:
            entry_id: Config entry ID (PN-2: per-entry selection).

        Returns:
            Device name or None.
        """
        if entry_id:
            # P2-01: Entry-scoped — ONLY check this entry's storage.
            # Do NOT fall back to legacy input_select.
            entry_data = self.hass.data.get(DOMAIN, {}).get(entry_id, {})
            selected = entry_data.get("selected_device")
            if selected:
                _LOGGER.debug("Selected device (storage, entry=%s): %s", entry_id[:8], selected)
                return selected
            _LOGGER.debug(
                "No selected_device in storage for entry_id=%s "
                "(fail closed — not falling back to legacy input_select)",
                entry_id[:8],
            )
            return None

        # No entry_id: legacy fallback (backward compat only)
        return await self._get_selected_device_legacy()

    async def set_selected_device(
        self, entry_id: str, device_name: str | None
    ) -> None:
        """Set selected device in integration storage + persist to config entry.

        Args:
            entry_id: Config entry ID.
            device_name: Device name to select, or None to deselect.
        """
        # Write to runtime data
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        entry_data = domain_data.setdefault(entry_id, {})
        entry_data["selected_device"] = device_name

        # Persist to config entry options
        entry: ConfigEntry | None = None
        for ce in self.hass.config_entries.async_entries(DOMAIN):
            if ce.entry_id == entry_id:
                entry = ce
                break

        if entry:
            new_options = dict(entry.options)
            new_options[CONF_SELECTED_DEVICE] = device_name
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info("Selected device persisted: %s (entry=%s)", device_name, entry_id)

        # Backward compat: sync legacy input_select if it exists
        selector_state = self.hass.states.get(ENTITY_DEVICE_SELECTOR)
        if selector_state is not None:
            try:
                await self.hass.services.async_call(
                    "input_select",
                    "select_option",
                    {
                        "entity_id": ENTITY_DEVICE_SELECTOR,
                        "option": device_name or "none",
                    },
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.debug("Failed to sync legacy input_select: %s", err)

    # ================================================================
    # EPIC-005-A1: Capability-based health (PN-1)
    # ================================================================

    async def compute_device_health(
        self, ha_device_id: str, model_hint: str | None = None
    ) -> dict[str, Any]:
        """Compute device health using Entity Registry capabilities.

        Uses device_class + state_class from Entity Registry entries — never
        matches against hardcoded entity_id strings (PN-1).

        Args:
            ha_device_id: HA Device Registry ID.
            model_hint: Optional model hint (e.g., "mic600") to select
                        required capabilities. Falls back to "default".

        Returns:
            Dict compatible with legacy get_device_health() shape:
            {available, state, device_name, entity_count, missing_sensors, last_check}
        """
        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, ha_device_id)

        if not device_entities:
            return {
                "available": False,
                "state": None,
                "device_name": ha_device_id,
                "entity_count": 0,
                "missing_sensors": [],
                "last_check": datetime.now(timezone.utc).isoformat(),
            }

        # Determine required capabilities for this device model
        cap_key = "default"
        if model_hint:
            hint_lower = model_hint.lower()
            for cap_name in HEALTH_REQUIRED_CAPABILITIES:
                if cap_name in hint_lower:
                    cap_key = cap_name
                    break
        required_classes = set(HEALTH_REQUIRED_CAPABILITIES.get(cap_key, []))

        # Scan entities for capability coverage
        found_classes: set[str] = set()
        unavailable_entities: list[str] = []
        total_entities = 0

        for ent_entry in device_entities:
            # Only check sensor entities for health (not buttons/switches/etc.)
            if not ent_entry.entity_id.startswith("sensor."):
                continue

            total_entities += 1
            state = self.hass.states.get(ent_entry.entity_id)

            if state is None or state.state in ("unavailable", "unknown"):
                unavailable_entities.append(ent_entry.entity_id)
                continue

            # Check device_class from entity registry or state attributes
            dc = ent_entry.device_class or (
                state.attributes.get("device_class") if state else None
            )
            if dc:
                found_classes.add(dc)

        # Determine health status
        missing_capabilities = required_classes - found_classes
        has_problem = len(missing_capabilities) > 0 or len(unavailable_entities) > 0

        _LOGGER.debug(
            "Health check for %s: cap_key=%s, required=%s, found=%s, "
            "missing_cap=%s, unavailable=%d/%d, problem=%s",
            ha_device_id, cap_key, required_classes, found_classes,
            missing_capabilities, len(unavailable_entities), total_entities,
            has_problem,
        )

        # Get device name for the result
        dev_reg = dr.async_get(self.hass)
        dev_entry = dev_reg.async_get(ha_device_id)
        device_name = dev_entry.name if dev_entry else ha_device_id

        return {
            "available": True,
            "state": has_problem,
            "device_name": device_name,
            "entity_count": total_entities,
            "missing_sensors": (
                [f"capability:{c}" for c in sorted(missing_capabilities)]
                + unavailable_entities
            ),
            "last_check": datetime.now(timezone.utc).isoformat(),
        }

    # ================================================================
    # Legacy Contract Input methods (backward compat)
    # ================================================================

    async def get_discovered_devices(self) -> list[str]:
        """Read legacy Input A: sensor.edge101_production_devices.devices[]

        Returns:
            List of device names (empty if not found).
        """
        devices = await self.state_reader.get_attribute(
            ENTITY_DISCOVERY_SENSOR, "devices", default=[]
        )

        if not devices:
            _LOGGER.debug(
                "No devices in legacy sensor %s.devices (expected if HACS-only)",
                ENTITY_DISCOVERY_SENSOR,
            )
        else:
            _LOGGER.debug("Legacy discovered %d devices: %s", len(devices), devices)

        return devices

    async def _get_selected_device_legacy(self) -> str | None:
        """Read legacy Input B: input_select.edge101_selected_production_device."""
        selected = await self.state_reader.get_state_value(
            ENTITY_DEVICE_SELECTOR, default="none"
        )

        if selected == "none" or selected is None:
            _LOGGER.debug("No device selected (legacy state=%s)", selected)
            return None

        _LOGGER.debug("Selected device (legacy): %s", selected)
        return selected

    async def get_selected_device(
        self, entry_id: str | None = None
    ) -> str | None:
        """Get selected device (storage-first, legacy fallback).

        This is the main API used by buttons, gates, etc.

        Args:
            entry_id: Config entry ID for entry-scoped lookup.
                      If provided, ONLY this entry's storage is checked
                      (no cross-entry bleed, no legacy fallback).
                      If None, iterates all entries + legacy fallback
                      (backward compat for callers without entry context).
        """
        domain_data = self.hass.data.get(DOMAIN, {})

        if entry_id:
            # Entry-scoped: ONLY check this entry's storage.
            # NO legacy fallback — prevents cross-entry bleed (P0-2 fix).
            entry_data = domain_data.get(entry_id, {})
            if isinstance(entry_data, dict):
                selected = entry_data.get("selected_device")
                if selected:
                    return selected
            _LOGGER.debug(
                "No selected_device in entry storage (entry_id=%s). "
                "Not falling back to legacy input_select (entry-scoped mode).",
                entry_id,
            )
            return None

        # No entry_id: iterate all entries (legacy compat, non-scoped)
        _LOGGER.warning(
            "get_selected_device() called without entry_id — "
            "using non-scoped fallback. Callers should pass entry_id."
        )
        for key, value in domain_data.items():
            if isinstance(value, dict) and "selected_device" in value:
                selected = value["selected_device"]
                if selected:
                    return selected

        # Fallback: legacy input_select
        return await self._get_selected_device_legacy()

    async def get_device_health(self, device: str) -> dict[str, Any]:
        """Read legacy Input C: binary_sensor.{device}_health + attributes.

        For HACS-only installs (no template sensor), falls back to
        compute_device_health() using capability-based matching.

        Args:
            device: Device name (e.g., 'sph10k_haus_03').

        Returns:
            Health info dict.
        """
        # Try legacy template sensor first
        device_slug = _device_slug(device)
        entity_id = ENTITY_HEALTH_PATTERN.format(device=device_slug)
        state = await self.state_reader.get_state(entity_id)

        if state is not None:
            has_problem = state.state == "on"
            return {
                "available": True,
                "state": has_problem,
                "device_name": state.attributes.get("device_name", device),
                "entity_count": state.attributes.get("entity_count", 0),
                "missing_sensors": state.attributes.get("missing_sensors", []),
                "last_check": state.attributes.get("last_check"),
            }

        # No legacy sensor → try capability-based health via Device Registry
        _LOGGER.debug(
            "Legacy health sensor %s not found, trying capability-based health",
            entity_id,
        )
        ha_device_id = await self._resolve_ha_device_id(device)
        if ha_device_id:
            return await self.compute_device_health(ha_device_id, model_hint=device)

        # Neither available
        _LOGGER.debug("No health source for device %s", device)
        return {
            "available": False,
            "state": None,
            "device_name": device,
            "entity_count": 0,
            "missing_sensors": [],
            "last_check": None,
        }

    async def _resolve_ha_device_id(self, device_name: str) -> str | None:
        """Resolve a device name to its HA Device Registry ID.

        Tries Device Registry name match first, then entity probe.
        """
        # Try Device Registry by name
        device_slug = _device_slug(device_name)
        registry_devices = await self.get_registry_devices()
        for kind_key in ("factory", "production"):
            for dev in registry_devices.get(kind_key, []):
                if (
                    dev["name"] == device_name
                    or _device_slug(dev["name"]) == device_slug
                ):
                    return dev["id"]

        # Try entity probe
        ent_reg = er.async_get(self.hass)
        probe = f"sensor.{device_slug}_battery_soc_device"
        entry = ent_reg.async_get(probe)
        if entry and entry.device_id:
            return entry.device_id

        return None

    # ================================================================
    # Device Registry Discovery (P3-8-001 — preserved)
    # ================================================================

    async def get_registry_devices(self) -> dict[str, list[dict[str, Any]]]:
        """Scan HA Device Registry for PVAutonomy Edge101 devices.

        Uses a short-lived cache (10s TTL) to avoid duplicate scans.

        Returns:
            Dict with 'factory' and 'production' device lists.
        """
        now = time.monotonic()
        if (
            self._registry_cache is not None
            and (now - self._registry_cache_time) < self._registry_cache_ttl
        ):
            return self._registry_cache

        registry = dr.async_get(self.hass)
        factory_devices: list[dict[str, Any]] = []
        production_devices: list[dict[str, Any]] = []

        for device_entry in registry.devices.values():
            if device_entry.manufacturer != MANUFACTURER_PVAUTONOMY:
                continue

            model = device_entry.model or ""
            kind = None

            if model == MODEL_FACTORY:
                kind = DEVICE_KIND_FACTORY
            elif model == MODEL_PRODUCTION:
                kind = DEVICE_KIND_PRODUCTION
            else:
                _LOGGER.debug(
                    "Skipping PVAutonomy device with unknown model: %s (name=%s)",
                    model, device_entry.name,
                )
                continue

            mac = None
            for conn_type, conn_id in device_entry.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac = conn_id
                    break

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

        fc = len(factory_devices)
        pc = len(production_devices)
        if fc != self._prev_factory_count or pc != self._prev_production_count:
            _LOGGER.info(
                "Device Registry scan: %d factory, %d production", fc, pc,
            )
            self._prev_factory_count = fc
            self._prev_production_count = pc
        else:
            _LOGGER.debug(
                "Device Registry scan (cached counts): %d factory, %d production",
                fc, pc,
            )

        result = {"factory": factory_devices, "production": production_devices}
        self._registry_cache = result
        self._registry_cache_time = now
        return result

    async def get_all_devices_for_dropdown(self) -> list[dict[str, str]]:
        """Get unified device list for dropdown population.

        Merges Device Registry + legacy, deduplicates by HA device_id.
        """
        registry_devices = await self.get_registry_devices()
        dropdown_items: list[dict[str, str]] = []

        for dev in registry_devices["factory"]:
            dropdown_items.append({
                "value": dev["name"],
                "label": f"{dev['name']} (factory)",
                "kind": DEVICE_KIND_FACTORY,
                "ha_device_id": dev.get("id", ""),
            })

        for dev in registry_devices["production"]:
            dropdown_items.append({
                "value": dev["name"],
                "label": f"{dev['name']} (production)",
                "kind": DEVICE_KIND_PRODUCTION,
                "ha_device_id": dev.get("id", ""),
            })

        # Legacy fallback (dedup by HA device_id)
        ent_reg = er.async_get(self.hass)
        registry_device_ids = {
            d.get("id", "") for d in (
                registry_devices["factory"] + registry_devices["production"]
            )
        }

        legacy_devices = await self.get_discovered_devices()
        for dev_name in legacy_devices:
            probe = f"sensor.{dev_name}_battery_soc_device"
            entry = ent_reg.async_get(probe)
            if entry and entry.device_id in registry_device_ids:
                continue
            display_name = _display_name_from_device_slug(dev_name)
            dropdown_items.append({
                "value": display_name,
                "label": f"{display_name} (production)",
                "kind": DEVICE_KIND_PRODUCTION,
            })

        # Deduplicate by normalised slug
        seen_slugs: set[str] = set()
        unique_items: list[dict[str, str]] = []
        for item in dropdown_items:
            slug = _device_slug(item["value"])
            if slug not in seen_slugs:
                seen_slugs.add(slug)
                unique_items.append(item)

        _LOGGER.debug(
            "Dropdown items: %d total (%s)",
            len(unique_items), [i["value"] for i in unique_items],
        )
        return unique_items

    async def get_selected_device_kind(
        self,
        device_name: str | None = None,
        entry_id: str | None = None,
    ) -> str | None:
        """Determine if selected device is factory or production.

        Checks Device Registry first, then legacy template sensor.

        Args:
            device_name: Explicit device name to classify. If None,
                         resolves via get_selected_device(entry_id).
            entry_id: Config entry ID for entry-scoped lookup when
                      device_name is not provided.
        """
        selected = device_name or await self.get_selected_device(entry_id)
        if selected is None:
            return None

        registry_devices = await self.get_registry_devices()

        # Normalize for comparison (Device Registry names may differ in
        # casing/separators from stored selection)
        selected_norm = _device_slug(selected)

        for dev in registry_devices["factory"]:
            if dev["name"] == selected or _device_slug(dev["name"]) == selected_norm:
                return DEVICE_KIND_FACTORY

        for dev in registry_devices["production"]:
            if dev["name"] == selected or _device_slug(dev["name"]) == selected_norm:
                return DEVICE_KIND_PRODUCTION

        # Fallback: legacy template sensor
        legacy_production = await self.get_discovered_devices()
        if any(_device_slug(dev) == selected_norm for dev in legacy_production):
            return DEVICE_KIND_PRODUCTION

        # P0-2 diagnostics: log why classification failed
        all_names = (
            [d["name"] for d in registry_devices["factory"]]
            + [d["name"] for d in registry_devices["production"]]
        )
        _LOGGER.warning(
            "Device kind unknown: selected='%s' (normalized='%s'), "
            "registry_names=%s, legacy_names=%s, entry_id=%s",
            selected,
            selected_norm,
            all_names[:10],
            legacy_production[:5],
            entry_id,
        )
        return None

    async def get_device_metrics(self, device: str) -> dict[str, Any]:
        """Read Inputs D+E: sensor/number.{device}_{metric}_device."""
        metrics = {}
        device_slug = _device_slug(device)

        sensor_pattern = f"sensor.{device_slug}_"
        number_pattern = f"number.{device_slug}_"
        switch_pattern = f"switch.{device_slug}_"

        for entity_id, state in self.hass.states.async_all():
            if not state:
                continue

            if entity_id.startswith(sensor_pattern) and entity_id.endswith("_device"):
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
        """Read Input F: sensor.{device}_hardware_family."""
        device_slug = _device_slug(device)
        entity_id = ENTITY_HARDWARE_FAMILY_PATTERN.format(device=device_slug)
        family = await self.state_reader.get_state_value(entity_id, default=None)

        if family is None or family == "unknown":
            _LOGGER.debug(
                "Hardware family not found for %s, inferring from entity pattern",
                device_slug,
            )
            if "edge101" in device.lower():
                return "edge101"
            return "unknown"

        _LOGGER.debug("Hardware family for %s: %s", device, family)
        return family

    async def validate_inputs(self, entry_id: str | None = None) -> dict[str, Any]:
        """Validate inputs are available (registry-first, legacy-tolerant).

        Missing legacy entities are NOT critical if Device Registry has devices.

        Args:
            entry_id: Config entry ID for entry-scoped device lookup.
        """
        missing = []
        warnings = []

        # Check Device Registry first (primary path)
        registry_devices = await self.get_registry_devices()
        registry_total = (
            len(registry_devices.get("factory", []))
            + len(registry_devices.get("production", []))
        )

        if registry_total > 0:
            # Device Registry has devices — legacy inputs not required
            if not await self.state_reader.entity_exists(ENTITY_DISCOVERY_SENSOR):
                warnings.append(
                    f"Legacy Input A missing: {ENTITY_DISCOVERY_SENSOR} "
                    "(not required — Device Registry active)"
                )
            if not await self.state_reader.entity_exists(ENTITY_DEVICE_SELECTOR):
                warnings.append(
                    f"Legacy Input B missing: {ENTITY_DEVICE_SELECTOR} "
                    "(not required — integration-owned selection active)"
                )
        else:
            # No Device Registry devices — legacy inputs are critical
            if not await self.state_reader.entity_exists(ENTITY_DISCOVERY_SENSOR):
                missing.append(f"Input A: {ENTITY_DISCOVERY_SENSOR}")
            if not await self.state_reader.entity_exists(ENTITY_DEVICE_SELECTOR):
                missing.append(f"Input B: {ENTITY_DEVICE_SELECTOR}")

        # Check discovered devices
        all_devices = await self.get_all_discovered_devices()
        if not all_devices:
            warnings.append("No devices discovered (registry + legacy both empty)")

        # Check selected device (entry-scoped to prevent cross-entry bleed)
        selected = await self.get_selected_device(entry_id=entry_id)
        if selected is None:
            warnings.append("No device selected")

        return {
            "valid": len(missing) == 0,
            "missing_inputs": missing,
            "warnings": warnings,
        }
