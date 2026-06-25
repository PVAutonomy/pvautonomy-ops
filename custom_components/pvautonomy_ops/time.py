"""PVAutonomy Ops time draft entities for Grid First.

Expose Grid First slot times as native Home Assistant time entities.
These are draft-only — editing a time does NOT immediately write to
the inverter. The draft is committed to the inverter atomically by the
``activate_grid_first_draft`` service via
``custom_components.pvautonomy_ops.switch.async_commit_grid_first_draft``.
"""

from __future__ import annotations

import logging
from datetime import time as dt_time
from time import monotonic
from weakref import WeakValueDictionary

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_SELECTED_DEVICE, DOMAIN
from .dashboard_builder import load_registry
from .entity_cleanup import cleanup_unsupported_wrapper_entities, heal_grid_first_time_entities

_LOGGER = logging.getLogger(__name__)

_TIME_ENTITY_SUFFIX = "_time"
_SOURCE_ENTITY_SUFFIX = "_device"
_GRID_FIRST_SLOT_TIME_METRICS: tuple[str, ...] = (
    "grid_first_timeslot_1_start",
    "grid_first_timeslot_1_stop",
)
_TIME_ENTITY_NAMES: dict[str, str] = {
    "grid_first_timeslot_1_start": "Grid First Slot 1 Start",
    "grid_first_timeslot_1_stop": "Grid First Slot 1 Stop",
}
_TIME_ENTITY_ICONS: dict[str, str] = {
    "grid_first_timeslot_1_start": "mdi:clock-start",
    "grid_first_timeslot_1_stop": "mdi:clock-end",
}
_COMMIT_SETTLE_SECONDS = 15.0
_TIME_ENTITY_INSTANCES: "WeakValueDictionary[str, PVAutonomyGridFirstSlotTimeEntity]" = (
    WeakValueDictionary()
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy time entities from a ConfigEntry."""
    if _is_legacy_yaml_import_entry(entry):
        _LOGGER.info(
            "Skipping time entity setup for legacy import entry %s",
            entry.entry_id[:8],
        )
        return

    device_name = await _resolve_selected_device_name(hass, entry)
    if not device_name:
        _LOGGER.warning(
            "Skipping time entity setup for entry %s: selected device unavailable",
            entry.entry_id[:8],
        )
        return

    features = await _load_device_features(hass, entry)
    has_battery = features.get("battery_storage", True)

    if not has_battery:
        _LOGGER.info(
            "Skipping Grid First time entities for entry %s "
            "(features.battery_storage=False, device=%s)",
            entry.entry_id[:8],
            device_name,
        )
        try:
            cleanup_unsupported_wrapper_entities(hass, device_name, entry.entry_id)
        except Exception:  # noqa: BLE001 — defensive: cleanup is best-effort
            pass
        return

    try:
        healing_result = heal_grid_first_time_entities(hass, device_name, entry.entry_id)
    except Exception:  # noqa: BLE001 — defensive: heal is best-effort
        healing_result = None
    if healing_result is not None and (healing_result.deleted_count or healing_result.errors):
        _LOGGER.info(
            "Grid First time self-heal for %s: deleted=%d errors=%d",
            device_name,
            healing_result.deleted_count,
            len(healing_result.errors),
        )

    async_add_entities(
        [
            PVAutonomyGridFirstSlotTimeEntity(hass, entry, device_name, metric)
            for metric in _GRID_FIRST_SLOT_TIME_METRICS
        ],
        True,
    )


def _is_legacy_yaml_import_entry(entry: ConfigEntry) -> bool:
    """Return True for the old YAML import entry that must not emit runtime entities."""
    return (entry.unique_id or "") == f"{DOMAIN}_yaml_import"


def _normalize_device_name(device_name: str | None) -> str | None:
    """Normalize a selected device name to the entity-id prefix form."""
    if not device_name:
        return None
    normalized = device_name.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


async def _resolve_selected_device_name(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> str | None:
    """Resolve the selected device slug for entity-id construction."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    selected = _normalize_device_name(entry_data.get("selected_device"))
    if selected:
        return selected

    input_reader = entry_data.get("input_reader")
    if input_reader is not None:
        try:
            stored = await input_reader.get_selected_device_from_storage(entry.entry_id)
        except Exception:  # noqa: BLE001 — defensive: storage read is best-effort
            stored = None
        selected = _normalize_device_name(stored)
        if selected:
            return selected

    selected = _normalize_device_name(entry.options.get(CONF_SELECTED_DEVICE))
    if selected:
        return selected

    metadata_store = entry_data.get("metadata_store")
    ha_device_id = entry.options.get("ha_device_id") or entry.data.get("ha_device_id")
    if metadata_store is not None and ha_device_id:
        try:
            for metadata in await metadata_store.get_all():
                if metadata.ha_device_id == ha_device_id:
                    return _normalize_device_name(metadata.ensure_slug())
        except Exception:  # noqa: BLE001 — defensive: store read is best-effort
            return None

    return None


async def _load_device_features(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict:
    """Load inverter registry features for this config entry.

    Resolves the registry_file from device metadata, then loads the JSON and
    returns the ``features`` section.  Fails open: on any error returns ``{}``
    so an unknown/unresolvable device behaves like a battery-capable device
    (avoids accidentally hiding entities from SPH10K during startup races).
    """
    try:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        metadata_store = entry_data.get("metadata_store")
        ha_device_id = entry.options.get("ha_device_id") or entry.data.get("ha_device_id")
        if metadata_store is None or not ha_device_id:
            return {}
        registry_file: str | None = None
        for metadata in await metadata_store.get_all():
            if metadata.ha_device_id == ha_device_id:
                registry_file = getattr(metadata, "registry_file", None)
                break
        if not registry_file:
            return {}
        registry = await hass.async_add_executor_job(load_registry, registry_file)
        return registry.get("features", {})
    except Exception:  # noqa: BLE001 — fail-open: unknown device → battery-like
        return {}


def _encode_time_to_register(value: dt_time) -> int:
    """Encode an HH:MM time into Growatt's U_WORD time format."""
    return (value.hour << 8) + value.minute


def _decode_register_to_time(raw_value: int) -> dt_time | None:
    """Decode Growatt's U_WORD time format into a Python time."""
    hour = (raw_value >> 8) & 0xFF
    minute = raw_value & 0xFF
    if hour > 23 or minute > 59:
        return None
    return dt_time(hour=hour, minute=minute)


def _merge_partial_time_write(
    previous: dt_time | None,
    incoming: dt_time,
    *,
    now_monotonic: float,
    pending_until: float,
) -> dt_time:
    """Merge split hour/minute edits from HA's time picker.

    Some clients emit two rapid writes when editing a time field, e.g.
    ``23:00`` followed by ``00:59``. During the short write window we merge
    the second partial value with the previous optimistic target so
    ``23:59`` is preserved instead of collapsing to ``00:59``.
    """
    if previous is None or now_monotonic > pending_until:
        return incoming

    if incoming.hour == 0 and previous.hour != 0 and incoming.minute != previous.minute:
        return dt_time(hour=previous.hour, minute=incoming.minute)

    if (
        incoming.minute == 0
        and previous.minute != 0
        and incoming.hour != previous.hour
    ):
        return dt_time(hour=incoming.hour, minute=previous.minute)

    return incoming


def _should_delay_partial_time_write(
    previous: dt_time | None,
    incoming: dt_time,
) -> bool:
    """Return True when a first hour-only edit likely shouldn't write yet."""
    del previous
    return incoming.minute == 0 and incoming.hour != 0


def get_grid_first_time_entity(entity_id: str) -> "PVAutonomyGridFirstSlotTimeEntity | None":
    """Return the live Grid First draft time entity instance if available."""
    return _TIME_ENTITY_INSTANCES.get(entity_id)


class PVAutonomyGridFirstSlotTimeEntity(TimeEntity):
    """Draft time entity for Grid First Slot 1.

    This entity stores a local draft time value. Editing the time in
    the dashboard does NOT immediately write to the inverter. The draft
    is committed atomically by the ``activate_grid_first_draft`` service.

    On first load, the entity reads the current inverter value from the
    backing ESPHome number entity so the user sees what is currently set.
    After the user edits the value, the draft diverges from the live
    register until the next ``activate_grid_first_draft`` call.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_name: str,
        metric: str,
    ) -> None:
        """Initialize the draft time entity."""
        self.hass = hass
        self._entry_id = entry.entry_id
        self._metric = metric
        self._device_name = device_name
        self._source_entity_id = f"number.{device_name}_{metric}{_SOURCE_ENTITY_SUFFIX}"
        self.entity_id = f"time.{device_name}_{metric}{_TIME_ENTITY_SUFFIX}"
        self._attr_unique_id = f"{device_name}_{metric}{_TIME_ENTITY_SUFFIX}"
        self._attr_suggested_object_id = f"{device_name}_{metric}{_TIME_ENTITY_SUFFIX}"
        self._attr_name = _TIME_ENTITY_NAMES[metric]
        self._attr_icon = _TIME_ENTITY_ICONS.get(metric)
        self._attr_native_value: dt_time | None = None
        self._attr_available = False
        self._draft_dirty = False
        self._pending_commit_value: dt_time | None = None
        self._pending_commit_until = 0.0

    async def async_added_to_hass(self) -> None:
        """Load initial value from the live inverter register."""
        await super().async_added_to_hass()
        _TIME_ENTITY_INSTANCES[self.entity_id] = self
        self._sync_from_source_if_clean()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_entity_id],
                self._handle_source_state_change,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Remove this entity from the runtime lookup table."""
        _TIME_ENTITY_INSTANCES.pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Track live register updates while no local draft edit is pending."""
        if self._sync_from_source_if_clean():
            self.async_write_ha_state()

    def _sync_from_source_if_clean(self) -> bool:
        """Sync from the live register when no local draft edit is pending."""
        if self._draft_dirty:
            return False
        state = self.hass.states.get(self._source_entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            changed = self._attr_available or self._attr_native_value is not None
            self._attr_available = False
            self._attr_native_value = None
            return changed
        try:
            raw_value = int(round(float(state.state)))
        except (TypeError, ValueError):
            changed = self._attr_available or self._attr_native_value is not None
            self._attr_available = False
            self._attr_native_value = None
            return changed
        decoded = _decode_register_to_time(raw_value)
        if decoded is None:
            changed = self._attr_available or self._attr_native_value is not None
            self._attr_available = False
            self._attr_native_value = None
            return changed

        now = monotonic()
        if (
            self._pending_commit_value is not None
            and now < self._pending_commit_until
            and decoded != self._pending_commit_value
        ):
            return False

        if decoded == self._pending_commit_value or now >= self._pending_commit_until:
            self._pending_commit_value = None

        changed = (self._attr_available != (decoded is not None)) or (
            self._attr_native_value != decoded
        )
        self._attr_available = decoded is not None
        self._attr_native_value = decoded
        return changed

    async def async_update(self) -> None:
        """Periodic update: re-sync from source only if no user edit pending."""
        self._sync_from_source_if_clean()

    async def async_set_value(self, value: dt_time) -> None:
        """Store the user's edited time as a local draft (no Modbus write).

        The draft stays local until ``activate_grid_first_draft`` commits it.
        """
        self._attr_available = True
        self._attr_native_value = dt_time(hour=value.hour, minute=value.minute)
        self._draft_dirty = True
        self._pending_commit_value = None
        self._pending_commit_until = 0.0
        self.async_write_ha_state()
        _LOGGER.debug(
            "Grid First draft time set: %s = %02d:%02d (not yet written to inverter)",
            self.entity_id,
            value.hour,
            value.minute,
        )

    def mark_committed(self, value: dt_time | None = None) -> None:
        """Called after activate_grid_first writes the draft to the inverter."""
        self._draft_dirty = False
        if value is not None:
            self._attr_native_value = value
            self._attr_available = True
            self._pending_commit_value = value
            self._pending_commit_until = monotonic() + _COMMIT_SETTLE_SECONDS
        self.async_write_ha_state()
