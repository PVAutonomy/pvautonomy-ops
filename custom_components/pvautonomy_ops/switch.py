"""PVAutonomy Ops virtual switch entities.

Expose customer-safe boolean controls that proxy richer underlying entities.
Currently used for:

- Export Limit ON/OFF:
  - ON  -> select.export_limit_enable = "RS485 Limit"
  - OFF -> select.export_limit_enable = "Disabled"
- Priority mode activation:
  - Load First toggle    -> select.priority_control = "Load First"
  - Battery First toggle -> select.priority_control = "Battery First"
- Grid First Schedule Enabled draft (HR1082, slot 1)
- Grid First commit helper (atomic activate of full draft bundle)

This keeps the customer dashboard simple while preserving the richer
underlying selects/numbers for diagnostics and advanced workflows.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time as dt_time
from time import monotonic
from weakref import WeakValueDictionary

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .const import CONF_SELECTED_DEVICE, DOMAIN
from .time import (
    _encode_time_to_register,
    get_grid_first_time_entity,
)

_LOGGER = logging.getLogger(__name__)

_EXPORT_LIMIT_ON_OPTION = "RS485 Limit"
_EXPORT_LIMIT_OFF_OPTION = "Disabled"
_EXPORT_LIMIT_ACTIVE_OPTIONS = frozenset({
    "RS485 Limit",
    "RS232 Limit",
    "CT Meter",
})
_EXPORT_LIMIT_EXPECTED_OPTION = "RS485 Limit"
_EXPORT_LIMIT_UNEXPECTED_OPTIONS = frozenset({
    "RS232 Limit",
    "CT Meter",
})
# HR122 (export_limit_enable) is unlock-gated: the registry write_policy is
# requires_unlock=true via an unsafe-tier unlock sequence, and the
# inverter silently ignores the write when the unlock has not run. The
# unlock is a firmware-side primitive exposed as a button; its absence means
# there is no path to honor the write, so it is treated as blocking.
_EXPORT_LIMIT_UNLOCK_SLUG = "growatt_official_unlock"
# Settle window waited AFTER pressing the unlock button and BEFORE writing
# HR122. The on-device unlock script (the generator's verified Core2 sequence)
# runs asynchronously with delays totalling ~7s, and ``button.press`` returns
# once that script is *started*, not finished. 8s gives the sequence a small
# margin so HR122 is written only after the inverter is actually unlocked;
# otherwise the still-locked inverter silently rejects the write and the toggle
# reverts (the original symptom). This models firmware-script completion by a
# time window — real completion is confirmed by on-hardware live validation.
# (Deliberately no unlock register numbers here: this module never touches the
# unlock password registers; the firmware owns the sequence.)
_EXPORT_LIMIT_UNLOCK_SETTLE_SECONDS = 8.0
# Bounded window during which a just-requested HR122 value is held before the
# source readback becomes the sole source of truth again (no permanent
# optimistic state).
_EXPORT_LIMIT_PENDING_SECONDS = 15.0
_PRIORITY_CONTROL_LOAD_FIRST = "Load First"
_PRIORITY_CONTROL_BATTERY_FIRST = "Battery First"
_GRID_FIRST_ACTIVATE = "Grid First"
_COMMIT_SETTLE_SECONDS = 15.0
_GRID_FIRST_SCHEDULE_DRAFT_INSTANCES: "WeakValueDictionary[str, PVAutonomyGridFirstScheduleEnabledDraft]" = (
    WeakValueDictionary()
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy switch entities from a ConfigEntry."""
    if _is_legacy_yaml_import_entry(entry):
        _LOGGER.info(
            "Skipping switch entity setup for legacy import entry %s",
            entry.entry_id[:8],
        )
        return

    device_name = await _resolve_selected_device_name(hass, entry)
    if not device_name:
        _LOGGER.warning(
            "Skipping switch entity setup for entry %s: selected device unavailable",
            entry.entry_id[:8],
        )
        return

    async_add_entities(
        [
            PVAutonomyExportLimitToggleSwitch(hass, entry, device_name),
            PVAutonomyPriorityModeToggleSwitch(
                hass,
                entry,
                device_name,
                mode_label=_PRIORITY_CONTROL_LOAD_FIRST,
                slug="load_first_activate",
                icon="mdi:home-lightning-bolt",
            ),
            PVAutonomyPriorityModeToggleSwitch(
                hass,
                entry,
                device_name,
                mode_label=_PRIORITY_CONTROL_BATTERY_FIRST,
                slug="battery_first_activate",
                icon="mdi:battery-arrow-down",
            ),
            PVAutonomyGridFirstScheduleEnabledDraft(hass, entry, device_name),
        ],
        True,
    )


def _is_legacy_yaml_import_entry(entry: ConfigEntry) -> bool:
    """Return True for the legacy YAML import entry."""
    return (entry.unique_id or "") == f"{DOMAIN}_yaml_import"


def _normalize_device_name(device_name: str | None) -> str | None:
    """Normalize a selected device name to entity-id slug form."""
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


class PVAutonomyExportLimitToggleSwitch(SwitchEntity):
    """Customer-safe ON/OFF proxy for export_limit_enable (HR122).

    HR122 is a protected, unlock-gated holding register. A bare
    ``select_option`` write is silently ignored by the inverter unless the
    unlock sequence (unsafe tier) has run first, so this proxy
    is fail-closed and honest:

    - ON/OFF is refused with a clear error when no unlock primitive is
      exposed on the device (e.g. customer firmware without the unlock
      button) — the write would otherwise appear to succeed while changing
      nothing.
    - When an unlock primitive IS present it is invoked before the write.
    - The reported state is never assumed from the requested action; it is
      derived from the source-select readback, with a bounded pending window
      for in-flight writes.

    No password/secret register value is read, logged, or surfaced here; the
    unlock sequence itself lives in firmware.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_name: str,
    ) -> None:
        """Initialize the proxy switch."""
        self.hass = hass
        self._entry_id = entry.entry_id
        self._device_name = device_name
        self._source_entity_id = f"select.{device_name}_export_limit_enable_device"
        self.entity_id = f"switch.{device_name}_export_limit_toggle_device"
        self._attr_unique_id = f"{device_name}_export_limit_toggle_device"
        self._attr_suggested_object_id = f"{device_name}_export_limit_toggle_device"
        self._attr_name = "Export Limit"
        self._attr_icon = "mdi:transmission-tower-export"
        self._attr_is_on = False
        self._attr_available = False
        self._current_mode: str | None = None
        self._unlock_entity_id = f"button.{device_name}_{_EXPORT_LIMIT_UNLOCK_SLUG}"
        self._pending_option: str | None = None
        self._pending_until = 0.0

    async def async_added_to_hass(self) -> None:
        """Sync initial state and subscribe to source-select changes."""
        await super().async_added_to_hass()
        await self._async_refresh_from_source()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_entity_id],
                self._handle_source_state_change,
            )
        )

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Mirror source-select updates into the switch state."""
        self._apply_source_state(event.data.get("new_state"))
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh from the source select entity."""
        await self._async_refresh_from_source()

    async def _async_refresh_from_source(self) -> None:
        """Load the latest state from the source select entity."""
        self._apply_source_state(self.hass.states.get(self._source_entity_id))

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Expose the current backend mode for diagnostic / support visibility."""
        return {
            "export_limit_mode": self._current_mode,
            "export_limit_status": self._export_limit_status,
            "unlock_available": self._unlock_available,
        }

    @property
    def _unlock_available(self) -> bool:
        """Return True only when the on-device unlock primitive is usable.

        HR122 writes are silently ignored unless the unlock sequence has run,
        so a missing/unavailable unlock primitive is blocking. The unlock is
        exposed as a button; a never-pressed button reports STATE_UNKNOWN,
        which is still usable — only a missing entity or STATE_UNAVAILABLE
        counts as unavailable. No password/secret register is read here.
        """
        state = self.hass.states.get(self._unlock_entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    @property
    def _export_limit_status(self) -> str:
        """Return a customer-safe status for the current export-limit path."""
        if self._pending_option is not None and monotonic() < self._pending_until:
            return "Pending"
        if self._current_mode is None:
            return "Unknown"
        if self._current_mode == _EXPORT_LIMIT_OFF_OPTION:
            return "Off"
        if self._current_mode == _EXPORT_LIMIT_EXPECTED_OPTION:
            return "RS485"
        if self._current_mode in _EXPORT_LIMIT_UNEXPECTED_OPTIONS:
            return f"Unexpected ({self._current_mode})"
        return "Unknown"

    def _apply_source_state(self, state: State | None) -> None:
        """Derive ON/OFF honestly from the source-select readback.

        State is never assumed from the requested action. A just-requested
        value is held only for a bounded pending window; once the device
        confirms it (or the window elapses) the readback becomes the sole
        source of truth, so an ignored/rejected write surfaces instead of a
        permanent optimistic state.
        """
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._attr_available = False
            self._attr_is_on = False
            self._current_mode: str | None = None
            return

        self._attr_available = True
        self._current_mode = state.state

        if self._pending_option is not None:
            confirmed = state.state == self._pending_option
            expired = monotonic() >= self._pending_until
            if confirmed or expired:
                self._pending_option = None
                self._pending_until = 0.0
            else:
                # Write still in flight: reflect the requested value for the
                # bounded pending window only, not the stale readback.
                self._attr_is_on = (
                    self._pending_option in _EXPORT_LIMIT_ACTIVE_OPTIONS
                )
                return

        self._attr_is_on = state.state in _EXPORT_LIMIT_ACTIVE_OPTIONS

    async def async_turn_on(self, **kwargs) -> None:
        """Enable export limit (fail-closed when no unlock path exists)."""
        await self._async_apply_option(_EXPORT_LIMIT_ON_OPTION)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable export limit (fail-closed when no unlock path exists)."""
        await self._async_apply_option(_EXPORT_LIMIT_OFF_OPTION)

    async def _async_apply_option(self, option: str) -> None:
        """Unlock, then write HR122, never assuming the write succeeded.

        Fail-closed: if no unlock primitive is available the change is
        refused with a clear error, because the register write would be
        silently ignored by the inverter. On success the state is re-derived
        from the source readback inside a bounded pending window — there is
        no permanent optimistic state.
        """
        if not self._unlock_available:
            raise HomeAssistantError(
                "Export Limit change blocked: the inverter unlock primitive "
                "is unavailable, so the HR122 write would be silently ignored. "
                "Run the device unlock first, then retry."
            )

        await self._async_run_unlock()
        # Wait out the on-device unlock settle window before writing HR122.
        # button.press returns when the ESPHome unlock script is *started*, not
        # finished; writing HR122 immediately would race the still-running
        # (~7s, delay-spaced) sequence and hit a locked inverter. Reached only
        # on the unlock path — the fail-closed guard above has already returned
        # for the no-unlock case, so we never sleep when no unlock was pressed.
        await asyncio.sleep(_EXPORT_LIMIT_UNLOCK_SETTLE_SECONDS)
        await self._async_write_option(option)

        self._pending_option = option
        self._pending_until = monotonic() + _EXPORT_LIMIT_PENDING_SECONDS
        await self._async_refresh_from_source()
        self.async_write_ha_state()

    async def _async_run_unlock(self) -> None:
        """Invoke the on-device unlock primitive.

        The unlock sequence and any password registers it writes live in
        firmware; this only presses the exposed button. No secret/password
        value is handled, logged, or surfaced by the integration.
        """
        await self.hass.services.async_call(
            "button",
            "press",
            {"entity_id": self._unlock_entity_id},
            blocking=True,
        )

    async def _async_write_option(self, option: str) -> None:
        """Write a target option through to the underlying select."""
        await self.hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": self._source_entity_id,
                "option": option,
            },
            blocking=True,
        )


class PVAutonomyPriorityModeToggleSwitch(SwitchEntity):
    """Activation-only toggle for a confirmed priority mode."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_name: str,
        *,
        mode_label: str,
        slug: str,
        icon: str,
    ) -> None:
        """Initialize the mode toggle."""
        self.hass = hass
        self._entry_id = entry.entry_id
        self._device_name = device_name
        self._mode_label = mode_label
        self._source_entity_id = f"select.{device_name}_priority_control_device"
        self.entity_id = f"switch.{device_name}_{slug}_device"
        self._attr_unique_id = f"{device_name}_{slug}_device"
        self._attr_suggested_object_id = f"{device_name}_{slug}_device"
        self._attr_name = "Activate"
        self._attr_icon = icon
        self._attr_is_on = False
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Sync initial state and subscribe to source-select changes."""
        await super().async_added_to_hass()
        await self._async_refresh_from_source()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_entity_id],
                self._handle_source_state_change,
            )
        )

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Mirror source-select updates into the switch state."""
        self._apply_source_state(event.data.get("new_state"))
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh from the source select entity."""
        await self._async_refresh_from_source()

    async def _async_refresh_from_source(self) -> None:
        """Load the latest state from the source select entity."""
        self._apply_source_state(self.hass.states.get(self._source_entity_id))

    def _apply_source_state(self, state: State | None) -> None:
        """Map the current select option to switch ON/OFF."""
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._attr_available = False
            self._attr_is_on = False
            return

        self._attr_available = True
        self._attr_is_on = state.state == self._mode_label

    async def async_turn_on(self, **kwargs) -> None:
        """Activate this priority mode."""
        await self.hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": self._source_entity_id,
                "option": self._mode_label,
            },
            blocking=True,
        )
        self._attr_available = True
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """No-op: priority modes are selected explicitly, not deselected.

        The user can activate another mode via its own toggle. Keeping this
        as a no-op avoids inventing an implicit fallback target.
        """
        await self._async_refresh_from_source()
        self.async_write_ha_state()


@dataclass
class _ScheduleDraftRestoreData(ExtraStoredData):
    """Persisted bookkeeping for the schedule-enabled draft switch.

    Restoring the on/off state alone is not enough: after a restart a *clean*
    draft must re-adopt the live HR1082 state, while a *dirty* draft (a pending
    manual edit) must survive untouched until the next Activate. The dirty
    marker therefore has to outlive a restart alongside the on/off value.
    """

    draft_dirty: bool

    def as_dict(self) -> dict[str, bool]:
        """Return a JSON-serialisable representation for restore storage."""
        return {"draft_dirty": self.draft_dirty}


class PVAutonomyGridFirstScheduleEnabledDraft(SwitchEntity, RestoreEntity):
    """Draft-only switch for Grid First Schedule Enabled.

    This is a local draft field — toggling it does NOT write to HR1082.
    The value is committed to the inverter atomically by the Grid First
    Activate switch.

    A pending (dirty) draft edit and its on/off value are restored across HA
    restarts via RestoreEntity, so a manual edit is not silently reverted to
    the live source state on the next startup.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_name: str,
    ) -> None:
        """Initialize the draft switch."""
        self.hass = hass
        self._entry_id = entry.entry_id
        self._device_name = device_name
        self._source_entity_id = f"switch.{device_name}_grid_first_timeslot_1_enabled_device"
        self.entity_id = f"switch.{device_name}_grid_first_schedule_enabled_draft"
        self._attr_unique_id = f"{device_name}_grid_first_schedule_enabled_draft"
        self._attr_suggested_object_id = f"{device_name}_grid_first_schedule_enabled_draft"
        self._attr_name = "Schedule Enabled"
        self._attr_icon = "mdi:calendar-check"
        self._attr_is_on = False
        self._attr_available = False
        self._draft_dirty = False
        self._pending_commit_value: bool | None = None
        self._pending_commit_until = 0.0

    @property
    def extra_restore_state_data(self) -> _ScheduleDraftRestoreData:
        """Persist the dirty marker so a pending edit survives a restart."""
        return _ScheduleDraftRestoreData(self._draft_dirty)

    async def async_added_to_hass(self) -> None:
        """Restore any pending draft edit, then track the live switch."""
        await super().async_added_to_hass()
        _GRID_FIRST_SCHEDULE_DRAFT_INSTANCES[self.entity_id] = self
        await self._async_restore_draft_state()
        # Adopt the live HR1082 state only when no dirty edit was restored.
        # _sync_from_source_if_clean() is a no-op while dirty, so a restored
        # pending edit is never overwritten by the live source.
        self._sync_from_source_if_clean()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_entity_id],
                self._handle_source_state_change,
            )
        )

    async def _async_restore_draft_state(self) -> None:
        """Restore a pending draft edit (dirty marker + on/off) across restarts.

        Fail-safe to a clean draft: when no dirty edit was persisted, or the
        last state is missing/unavailable, the draft is left clean so the live
        HR1082 state is re-adopted by ``_sync_from_source_if_clean()``.
        """
        last_extra = await self.async_get_last_extra_data()
        if last_extra is None:
            return
        if not bool(last_extra.as_dict().get("draft_dirty", False)):
            return

        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return

        self._draft_dirty = True
        self._attr_available = True
        self._attr_is_on = last_state.state == "on"
        self._pending_commit_value = None
        self._pending_commit_until = 0.0

    async def async_will_remove_from_hass(self) -> None:
        """Remove this entity from the runtime lookup table."""
        _GRID_FIRST_SCHEDULE_DRAFT_INSTANCES.pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_source_state_change(self, event) -> None:
        """Track live HR1082 while no local draft edit is pending."""
        if self._sync_from_source_if_clean():
            self.async_write_ha_state()

    def _sync_from_source_if_clean(self) -> bool:
        """Sync draft state from live HR1082 when no local edit is pending."""
        if self._draft_dirty:
            return False

        state = self.hass.states.get(self._source_entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            changed = self._attr_available or self._attr_is_on
            self._attr_available = False
            self._attr_is_on = False
            return changed

        is_on = state.state == "on"
        now = monotonic()
        if (
            self._pending_commit_value is not None
            and now < self._pending_commit_until
            and is_on != self._pending_commit_value
        ):
            return False

        if is_on == self._pending_commit_value or now >= self._pending_commit_until:
            self._pending_commit_value = None

        changed = (not self._attr_available) or (self._attr_is_on != is_on)
        self._attr_available = True
        self._attr_is_on = is_on
        return changed

    async def async_update(self) -> None:
        """Periodic refresh from the live schedule-enabled switch."""
        self._sync_from_source_if_clean()

    async def async_turn_on(self, **kwargs) -> None:
        """Set draft to enabled (no Modbus write)."""
        self._attr_available = True
        self._attr_is_on = True
        self._draft_dirty = True
        self._pending_commit_value = None
        self._pending_commit_until = 0.0
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Set draft to disabled (no Modbus write)."""
        self._attr_available = True
        self._attr_is_on = False
        self._draft_dirty = True
        self._pending_commit_value = None
        self._pending_commit_until = 0.0
        self.async_write_ha_state()

    def mark_committed(self, value: bool | None = None) -> None:
        """Reset draft mode after a successful Grid First commit."""
        self._draft_dirty = False
        if value is not None:
            self._attr_is_on = value
            self._attr_available = True
            self._pending_commit_value = value
            self._pending_commit_until = monotonic() + _COMMIT_SETTLE_SECONDS
        self.async_write_ha_state()


def get_grid_first_schedule_enabled_draft(
    entity_id: str,
) -> PVAutonomyGridFirstScheduleEnabledDraft | None:
    """Return the live Grid First schedule draft switch if available."""
    return _GRID_FIRST_SCHEDULE_DRAFT_INSTANCES.get(entity_id)


def _read_required_number_state(
    hass: HomeAssistant,
    entity_id: str,
    *,
    label: str,
) -> float:
    """Read a required numeric state from Home Assistant."""
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise HomeAssistantError(f"{label} unavailable")
    try:
        return float(state.state)
    except (TypeError, ValueError) as exc:
        raise HomeAssistantError(f"{label} invalid: {state.state}") from exc


def _read_required_draft_time(
    hass: HomeAssistant,
    entity_id: str,
    *,
    label: str,
) -> dt_time:
    """Read a required Grid First draft time from the live entity instance."""
    entity = get_grid_first_time_entity(entity_id)
    if entity is not None and entity.native_value is not None:
        return entity.native_value

    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise HomeAssistantError(f"{label} unavailable")
    try:
        return dt_time.fromisoformat(state.state)
    except (TypeError, ValueError) as exc:
        raise HomeAssistantError(f"{label} invalid: {state.state}") from exc


def _read_required_schedule_enabled(
    hass: HomeAssistant,
    entity_id: str,
) -> bool:
    """Read the current Grid First schedule draft state."""
    entity = get_grid_first_schedule_enabled_draft(entity_id)
    if entity is not None:
        if not entity.available:
            raise HomeAssistantError("Schedule Enabled unavailable")
        return entity.is_on

    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        raise HomeAssistantError("Schedule Enabled unavailable")
    return state.state == "on"


async def async_commit_grid_first_draft(
    hass: HomeAssistant,
    *,
    device_name: str,
) -> None:
    """Commit the full Grid First draft bundle to the inverter.

    Manual dashboard editing is draft-only. This helper writes the full bundle
    in Growatt-like order so the inverter sees a coherent Grid First setup.
    Fail-closed: missing/invalid required inputs raise HomeAssistantError
    BEFORE any service call is issued.
    """
    priority_entity_id = f"select.{device_name}_priority_control_device"
    discharge_rate_entity_id = f"number.{device_name}_grid_first_discharge_power_rate_device"
    stop_soc_entity_id = f"number.{device_name}_grid_first_stop_soc_device"
    start_number_entity_id = f"number.{device_name}_grid_first_timeslot_1_start_device"
    stop_number_entity_id = f"number.{device_name}_grid_first_timeslot_1_stop_device"
    enable_switch_entity_id = f"switch.{device_name}_grid_first_timeslot_1_enabled_device"
    draft_start_entity_id = f"time.{device_name}_grid_first_timeslot_1_start_time"
    draft_stop_entity_id = f"time.{device_name}_grid_first_timeslot_1_stop_time"
    draft_enabled_entity_id = f"switch.{device_name}_grid_first_schedule_enabled_draft"

    # Validate ALL inputs before any write.
    discharge_rate = _read_required_number_state(
        hass,
        discharge_rate_entity_id,
        label="Grid First Discharge Rate",
    )
    stop_soc = _read_required_number_state(
        hass,
        stop_soc_entity_id,
        label="Grid First Stop Discharge SoC",
    )
    start_time = _read_required_draft_time(
        hass,
        draft_start_entity_id,
        label="Grid First Slot 1 Start",
    )
    stop_time = _read_required_draft_time(
        hass,
        draft_stop_entity_id,
        label="Grid First Slot 1 Stop",
    )
    slot_enabled = _read_required_schedule_enabled(hass, draft_enabled_entity_id)

    start_encoded = _encode_time_to_register(start_time)
    stop_encoded = _encode_time_to_register(stop_time)

    _LOGGER.info(
        "Grid First commit: %s rate=%s stop_soc=%s start=%02d:%02d(%d) stop=%02d:%02d(%d) enabled=%s",
        device_name,
        discharge_rate,
        stop_soc,
        start_time.hour,
        start_time.minute,
        start_encoded,
        stop_time.hour,
        stop_time.minute,
        stop_encoded,
        slot_enabled,
    )

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": discharge_rate_entity_id, "value": discharge_rate},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": stop_soc_entity_id, "value": stop_soc},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": start_number_entity_id, "value": start_encoded},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": stop_number_entity_id, "value": stop_encoded},
        blocking=True,
    )
    await hass.services.async_call(
        "switch",
        "turn_on" if slot_enabled else "turn_off",
        {"entity_id": enable_switch_entity_id},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": priority_entity_id, "option": _GRID_FIRST_ACTIVATE},
        blocking=True,
    )

    start_entity = get_grid_first_time_entity(draft_start_entity_id)
    if start_entity is not None:
        start_entity.mark_committed(start_time)
    stop_entity = get_grid_first_time_entity(draft_stop_entity_id)
    if stop_entity is not None:
        stop_entity.mark_committed(stop_time)
    schedule_entity = get_grid_first_schedule_enabled_draft(draft_enabled_entity_id)
    if schedule_entity is not None:
        schedule_entity.mark_committed(slot_enabled)
