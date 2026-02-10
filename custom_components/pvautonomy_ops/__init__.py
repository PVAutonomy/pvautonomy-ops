"""PVAutonomy Ops Integration.

Phase 2: READ Inputs A-F + WRITE Outputs G-H (COMPLETE)
Phase 3: EXECUTE Actions A-G + Buttons I-L (IN PROGRESS)

Contract: ops-contract-v1.md (v1.0.0)
Directive: D-ADDON-002 (Phase 2), D-ADDON-BASELINE-SEC-001 (Phase 3 Preflight)
"""
import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import CONTRACT_VERSION, DOMAIN, UPDATE_INTERVAL, VERSION
from .discovery import ContractInputReader
from .operations import OperationLock, OperationRunner, OperationTracker

_LOGGER = logging.getLogger(__name__)

# Platforms to load (YAML-based setup)
PLATFORMS = ["sensor", "button"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up PVAutonomy Ops component.
    
    Args:
        hass: Home Assistant instance
        config: Configuration from configuration.yaml
        
    Returns:
        True if setup successful
    """
    _LOGGER.info(
        "Initializing PVAutonomy Ops (version %s, contract %s)",
        VERSION,
        CONTRACT_VERSION,
    )
    
    # Initialize input reader
    input_reader = ContractInputReader(hass)
    
    # Initialize Phase 3 operations manager
    operation_lock = OperationLock()
    operation_tracker = OperationTracker(hass)
    operation_runner = OperationRunner(hass, operation_tracker, operation_lock)
    
    # Store in hass.data for sensors/buttons/services to access
    hass.data[DOMAIN] = {
        "input_reader": input_reader,
        "operation_lock": operation_lock,
        "operation_tracker": operation_tracker,
        "operation_runner": operation_runner,
    }
    
    # Defer platform setup until HA is fully started (templates ready)
    async def start_integration(_event):
        """Initialize platforms and periodic updates after HA fully started."""
        _LOGGER.info(
            "HA fully started, loading platforms (version %s, contract %s)",
            VERSION,
            CONTRACT_VERSION,
        )
        
        # Load platforms via HA discovery (stable YAML pattern)
        # NOTE: async_load_platform marked deprecated but remains most stable
        # for YAML-based custom components in HA 2024-2026
        for platform in PLATFORMS:
            _LOGGER.info("Loading %s platform...", platform)
            hass.async_create_task(
                async_load_platform(hass, platform, DOMAIN, {}, config)
            )
        
        _LOGGER.info("Platform loading initiated")
        
        # Schedule periodic update
        async def periodic_update(_now=None):
            """Periodic update handler."""
            _LOGGER.debug("Periodic update triggered")
            # Trigger sensor updates via event
            hass.bus.async_fire(f"{DOMAIN}_update")
        
        _LOGGER.info(
            "Starting periodic updates (interval=%s seconds)", UPDATE_INTERVAL
        )
        async_track_time_interval(
            hass, periodic_update, timedelta(seconds=UPDATE_INTERVAL)
        )
        # Run initial update
        await periodic_update()
    
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_integration)
    
    _LOGGER.info(
        "PVAutonomy Ops initialized successfully. Event listener registered for HOMEASSISTANT_STARTED. HA State: %s",
        hass.state,
    )
    return True
