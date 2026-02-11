"""PVAutonomy Ops Integration.

Phase 2: READ Inputs A-F + WRITE Outputs G-H (COMPLETE)
Phase 3: EXECUTE Actions A-G + Buttons I-L (IN PROGRESS)
Phase 3.6: Config/Options Flow MVP (P3-6-001)

Contract: ops-contract-v1.md (v1.0.0)
Directive: D-ADDON-002, D-ADDON-BASELINE-SEC-001, P3-6-001
"""
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import CONTRACT_VERSION, DOMAIN, UPDATE_INTERVAL, VERSION
from .config_flow import (
    CONF_ARTIFACT_CHANNEL,
    CONF_ARTIFACT_HW_FAMILY,
    CONF_ARTIFACT_OWNER,
    CONF_ARTIFACT_REPO,
    CONF_FLASH_MIN_SIZE_KB,
    CONF_GATES_FRESHNESS_MIN,
    CONF_POLL_INTERVAL,
    CONF_STRICT_GATES,
    DEFAULT_ARTIFACT_CHANNEL,
    DEFAULT_ARTIFACT_HW_FAMILY,
    DEFAULT_ARTIFACT_OWNER,
    DEFAULT_ARTIFACT_REPO,
    DEFAULT_FLASH_MIN_SIZE_KB,
    DEFAULT_GATES_FRESHNESS_MIN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_STRICT_GATES,
)
from .discovery import ContractInputReader
from .operations import OperationLock, OperationRunner, OperationTracker

_LOGGER = logging.getLogger(__name__)

# Platforms to forward via ConfigEntry
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


def get_runtime_config(entry: ConfigEntry) -> dict:
    """Build runtime config dict from ConfigEntry options with defaults.

    Args:
        entry: The config entry to read options from.

    Returns:
        Dict with all runtime config values (guaranteed complete with defaults).
    """
    opts = entry.options
    return {
        CONF_POLL_INTERVAL: opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        CONF_ARTIFACT_CHANNEL: opts.get(CONF_ARTIFACT_CHANNEL, DEFAULT_ARTIFACT_CHANNEL),
        CONF_ARTIFACT_HW_FAMILY: opts.get(CONF_ARTIFACT_HW_FAMILY, DEFAULT_ARTIFACT_HW_FAMILY),
        CONF_ARTIFACT_OWNER: opts.get(CONF_ARTIFACT_OWNER, DEFAULT_ARTIFACT_OWNER),
        CONF_ARTIFACT_REPO: opts.get(CONF_ARTIFACT_REPO, DEFAULT_ARTIFACT_REPO),
        CONF_FLASH_MIN_SIZE_KB: opts.get(CONF_FLASH_MIN_SIZE_KB, DEFAULT_FLASH_MIN_SIZE_KB),
        CONF_GATES_FRESHNESS_MIN: opts.get(CONF_GATES_FRESHNESS_MIN, DEFAULT_GATES_FRESHNESS_MIN),
        CONF_STRICT_GATES: opts.get(CONF_STRICT_GATES, DEFAULT_STRICT_GATES),
    }


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up PVAutonomy Ops from YAML (triggers import to ConfigEntry).

    If ``pvautonomy_ops:`` is in configuration.yaml, this creates a
    ConfigEntry via the import flow so the integration runs under
    the modern ConfigEntry lifecycle.
    """
    if DOMAIN in config:
        _LOGGER.info(
            "YAML config detected for %s — triggering ConfigEntry import", DOMAIN
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}, data=config.get(DOMAIN) or {}
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PVAutonomy Ops from a ConfigEntry.

    This is the modern lifecycle entry point — called both for UI-created
    entries and YAML-imported entries.
    """
    _LOGGER.info(
        "Setting up PVAutonomy Ops entry (version %s, contract %s)",
        VERSION,
        CONTRACT_VERSION,
    )

    # Build runtime config from options
    runtime_config = get_runtime_config(entry)
    poll_interval = runtime_config[CONF_POLL_INTERVAL]

    # Initialize core components
    input_reader = ContractInputReader(hass)
    operation_lock = OperationLock()
    operation_tracker = OperationTracker(hass)
    operation_runner = OperationRunner(hass, operation_tracker, operation_lock)

    # Store in hass.data for platforms to access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN] = {
        "input_reader": input_reader,
        "operation_lock": operation_lock,
        "operation_tracker": operation_tracker,
        "operation_runner": operation_runner,
        "config": runtime_config,
        "entry": entry,
    }

    # Listen for options updates (live reload without restart)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Defer platform setup until HA is fully started (templates ready)
    async def start_integration(_event):
        """Initialize platforms and periodic updates after HA fully started."""
        _LOGGER.info(
            "HA fully started, forwarding platforms (version %s, contract %s)",
            VERSION,
            CONTRACT_VERSION,
        )

        # Forward platform setup via ConfigEntry (modern pattern)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _LOGGER.info("Platform forwarding initiated")

        # Schedule periodic update
        async def periodic_update(_now=None):
            """Periodic update handler."""
            _LOGGER.debug("Periodic update triggered")
            hass.bus.async_fire(f"{DOMAIN}_update")

        _LOGGER.info(
            "Starting periodic updates (interval=%s seconds)", poll_interval
        )
        cancel_timer = async_track_time_interval(
            hass, periodic_update, timedelta(seconds=poll_interval)
        )
        # Store cancel handle for unload
        hass.data[DOMAIN]["cancel_timer"] = cancel_timer

        # Run initial update
        await periodic_update()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_integration)

    _LOGGER.info(
        "PVAutonomy Ops entry setup complete. Waiting for HOMEASSISTANT_STARTED. HA State: %s",
        hass.state,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PVAutonomy Ops config entry."""
    _LOGGER.info("Unloading PVAutonomy Ops entry")

    # Cancel periodic timer
    cancel_timer = hass.data[DOMAIN].get("cancel_timer")
    if cancel_timer:
        cancel_timer()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.pop(DOMAIN, None)

    return unload_ok


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update (live reload)."""
    _LOGGER.info("Options updated — reloading PVAutonomy Ops entry")
    await hass.config_entries.async_reload(entry.entry_id)
