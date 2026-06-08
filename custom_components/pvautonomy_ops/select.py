"""PVAutonomy Ops Target Device Select Entity.

Provides a first-class HA select entity for choosing the target device
for all operational actions (flash, gates, restart, etc.).

Persists selection to ConfigEntry options via the existing
ContractInputReader.set_selected_device() API — all buttons already
read from the same storage via get_selected_device().

Contract: ops-contract-v1.md (v1.0.0) — Action D (set_selected_device)
"""

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ENTITY_TARGET_DEVICE_SELECT

_LOGGER = logging.getLogger(__name__)

NONE_OPTION = "none"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy Ops select entities from a ConfigEntry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    input_reader = entry_data["input_reader"]
    is_legacy = entry_data.get("is_legacy", False)  # EPIC-006-STAB Phase 2

    async_add_entities(
        [PVAutonomyOpsTargetDeviceSelect(hass, input_reader, entry.entry_id, is_legacy=is_legacy)],
        True,
    )


class PVAutonomyOpsTargetDeviceSelect(SelectEntity):
    """Select entity for choosing the target Edge101 device.

    Options are populated from discovery (Device Registry + legacy).
    Selection is persisted to ConfigEntry options and used by all
    operational buttons (Flash, Gates, Restart).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_translation_key = "target_device"

    def __init__(
        self,
        hass: HomeAssistant,
        input_reader,
        entry_id: str,
        *,
        is_legacy: bool = False,
    ) -> None:
        """Initialize target device select."""
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_TARGET_DEVICE_SELECT}" if entry_id else ENTITY_TARGET_DEVICE_SELECT
        self.hass = hass
        self.input_reader = input_reader
        self._entry_id = entry_id
        # UX Pack: hide target device select by default (debug-only).
        # Config Flow wizard binds devices — customers don't need this entity.
        self._attr_entity_registry_enabled_default = False
        self._attr_options = [NONE_OPTION]
        self._attr_current_option = NONE_OPTION

    async def async_added_to_hass(self) -> None:
        """Load current selection and register update listener."""
        # Load persisted selection
        stored = await self.input_reader.get_selected_device_from_storage(
            self._entry_id
        )
        if stored:
            # Ensure stored value is in options before setting
            await self._async_refresh_options()
            if stored in self._attr_options:
                self._attr_current_option = stored
            else:
                _LOGGER.warning(
                    "Stored device '%s' not in discovered devices, resetting",
                    stored,
                )
                self._attr_current_option = NONE_OPTION
                # Persist reset to Config Entry Options (prevent stale ghost)
                await self.input_reader.set_selected_device(
                    self._entry_id, None
                )

        else:
            await self._async_refresh_options()

        # Listen for discovery updates to refresh options
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_update", self._handle_update_event
            )
        )

    @callback
    def _handle_update_event(self, _event) -> None:
        """Schedule options refresh on domain update event."""
        self.hass.async_create_task(self._async_refresh_and_write())

    async def _async_refresh_and_write(self) -> None:
        """Refresh options and write state."""
        await self._async_refresh_options()
        self.async_write_ha_state()

    async def _async_refresh_options(self) -> None:
        """Rebuild options list from discovery."""
        try:
            dropdown_items = await self.input_reader.get_all_devices_for_dropdown()
            new_options = [NONE_OPTION] + [
                item["value"] for item in dropdown_items
            ]
        except Exception:
            _LOGGER.warning(
                "Failed to refresh device list for select entity", exc_info=True
            )
            new_options = [NONE_OPTION]

        self._attr_options = new_options

        # If current selection was removed from device list, reset
        if self._attr_current_option not in new_options:
            _LOGGER.info(
                "Selected device '%s' no longer in device list, resetting",
                self._attr_current_option,
            )
            self._attr_current_option = NONE_OPTION

    async def async_select_option(self, option: str) -> None:
        """Handle user selecting a device."""
        device_name = None if option == NONE_OPTION else option

        await self.input_reader.set_selected_device(
            self._entry_id, device_name
        )

        self._attr_current_option = option
        self.async_write_ha_state()

        # Trigger status sensor refresh
        self.hass.bus.async_fire(f"{DOMAIN}_update")

        _LOGGER.info("Target device set to: %s", option)
