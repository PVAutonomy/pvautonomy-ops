"""Config-entry diagnostics for pvautonomy_ops (M3A / #169 WP5).

For the **installation anchor** this returns the source-neutral, redacted Grid
Power capability diagnostics (`GridPowerManager.diagnostics()`) — enough to
distinguish healthy / stale / unavailable / source-missing / invalid-metadata /
invalid-combination / unconfigured without leaking any customer-specific
identifier (source identity is an opaque hash; no raw unique_id, entity_id,
device_id, config_entry_id, MQTT topic, or device token).

For **device entries** it returns a minimal, value-redacted summary — key names
only, never values — so no secret (proxy/build key, noise PSK, compile secret,
customer id, …) can enter a diagnostics download.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, ENTRY_KIND
from .grid_power import GRID_POWER_MANAGER_KEY
from .installation_anchor import is_installation_anchor


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a pvautonomy_ops config entry."""
    if is_installation_anchor(entry):
        manager = (
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get(GRID_POWER_MANAGER_KEY)
        )
        grid_power = (
            manager.diagnostics()
            if manager is not None
            else {"configured": False, "capability_state": "unknown"}
        )
        return {
            "entry_kind": "installation_anchor",
            "grid_power": grid_power,
        }

    # Device (or legacy/yaml-import) entry: NEVER emit config values — only the
    # set of keys present, so a diagnostics download cannot leak a secret.
    return {
        "entry_kind": entry.data.get(ENTRY_KIND, "device"),
        "data_keys": sorted(entry.data.keys()),
        "option_keys": sorted((entry.options or {}).keys()),
    }
