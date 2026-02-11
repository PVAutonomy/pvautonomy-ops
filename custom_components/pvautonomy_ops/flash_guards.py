"""Flash Action Guards (D-ADDON-FLASH-GUARD-001).

Enforces preflight requirements before flash/migrate/cleanup operations.
"""
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Guard configuration (default — overridden by Options Flow at runtime)
DEFAULT_GATES_FRESHNESS_MINUTES = 10


class FlashGuardError(Exception):
    """Flash guard validation failed."""

    def __init__(self, block_reason: str, message: str):
        """Initialize guard error.
        
        Args:
            block_reason: Machine-readable block reason
            message: Human-readable error message
        """
        self.block_reason = block_reason
        super().__init__(message)


async def check_flash_guards(hass: HomeAssistant) -> tuple[bool, str, str]:
    """Check all flash guard requirements (D-ADDON-FLASH-GUARD-001).
    
    Reads gates_freshness_minutes and strict_gates_required from runtime
    config (Options Flow).  Falls back to hardcoded defaults when config
    is unavailable.
    
    Args:
        hass: Home Assistant instance
        
    Returns:
        tuple[bool, str, str]: (passed, block_reason, message)
            - passed: True if all guards pass
            - block_reason: Machine-readable reason if blocked
            - message: Human-readable message
            
    Raises:
        FlashGuardError: If critical guards fail
    """
    # Read runtime config from Options Flow (P3-6-001)
    runtime_config = hass.data.get(DOMAIN, {}).get("config", {})
    freshness_minutes = runtime_config.get(
        "gates_freshness_minutes", DEFAULT_GATES_FRESHNESS_MINUTES
    )
    strict_gates = runtime_config.get("strict_gates_required", True)
    gates_freshness_timeout = timedelta(minutes=freshness_minutes)

    # Get status sensor
    status_entity_id = "sensor.pvautonomy_ops_status"
    status_state = hass.states.get(status_entity_id)
    
    if not status_state:
        raise FlashGuardError(
            "gates_missing",
            "Status sensor not found - cannot validate gates"
        )
    
    attrs = status_state.attributes
    
    # G-1: Gates must exist
    gates_overall = attrs.get("gates_overall")
    gates_last_run = attrs.get("gates_last_run")
    
    if gates_overall is None:
        _LOGGER.warning("Flash blocked: gates_overall is null (run gates first)")
        return False, "gates_missing", "Quality gates not run - press 'Run Gates' first"
    
    if gates_last_run is None:
        _LOGGER.warning("Flash blocked: gates_last_run is null")
        return False, "gates_missing", "Gates timestamp missing - press 'Run Gates' first"
    
    # G-2: Gates must be fresh (≤ 10 minutes)
    # Use HA datetime utilities for TZ-safe comparison
    try:
        parsed = dt_util.parse_datetime(gates_last_run)
        if not parsed:
            _LOGGER.error(
                "Flash blocked: cannot parse gates_last_run timestamp: %s",
                gates_last_run
            )
            return False, "invalid_timestamp", "Gates timestamp invalid format - run gates again"
        
        # Convert to UTC for comparison
        last_run_utc = dt_util.as_utc(parsed)
        now = dt_util.utcnow()
        age = now - last_run_utc
        
        if age > gates_freshness_timeout:
            _LOGGER.warning(
                "Flash blocked: gates too old (age=%s, limit=%s)",
                age,
                gates_freshness_timeout
            )
            return False, "gates_stale", f"Gates expired ({int(age.total_seconds() / 60)}min old, limit {freshness_minutes}min) - run gates again"
    
    except Exception as e:
        _LOGGER.error("Flash blocked: error processing gates_last_run: %s", e, exc_info=True)
        return False, "invalid_timestamp", "Gates timestamp processing failed - run gates again"
    
    # G-3: Gates must be PASS (not warn, not fail)
    if gates_overall == "fail":
        gates_fail = attrs.get("gates_fail", [])
        _LOGGER.warning(
            "Flash blocked: gates_overall=fail (failed gates: %s)",
            ", ".join(gates_fail)
        )
        return False, "gates_failed", f"Gates FAILED: {', '.join(gates_fail)} - fix issues first"
    
    if gates_overall == "warn":
        gates_warn = attrs.get("gates_warn", [])
        if strict_gates:
            _LOGGER.warning(
                "Flash blocked: gates_overall=warn, strict_gates=True (warned gates: %s)",
                ", ".join(gates_warn)
            )
            return False, "gates_warned", f"Gates WARNED: {', '.join(gates_warn)} - resolve warnings (strict mode)"
        else:
            _LOGGER.warning(
                "Flash allowed despite warnings (strict_gates=False): %s",
                ", ".join(gates_warn)
            )
    
    if gates_overall != "pass":
        _LOGGER.warning("Flash blocked: gates_overall=%s (expected 'pass')", gates_overall)
        return False, "gates_failed", f"Gates status '{gates_overall}' not acceptable"
    
    # All guards passed
    _LOGGER.info(
        "Flash guards PASSED: gates=%s, age=%s",
        gates_overall,
        age
    )
    return True, "", "Flash guards passed"


async def format_guard_block_message(
    hass: HomeAssistant,
    block_reason: str
) -> str:
    """Format user-friendly block message with actionable guidance.
    
    Args:
        hass: Home Assistant instance
        block_reason: Machine-readable block reason
        
    Returns:
        str: Formatted block message with next steps
    """
    status_entity_id = "sensor.pvautonomy_ops_status"
    status_state = hass.states.get(status_entity_id)
    
    if not status_state:
        return "Status sensor unavailable - cannot determine block reason"
    
    attrs = status_state.attributes
    
    if block_reason == "gates_missing":
        return (
            "⚠️ Quality gates not run yet.\n"
            "Next step: Press 'Run Gates' button first."
        )
    
    if block_reason == "gates_stale":
        last_run = attrs.get("gates_last_run")
        if last_run:
            try:
                last_run_dt = datetime.fromisoformat(last_run)
                age_min = int((datetime.now(timezone.utc) - last_run_dt).total_seconds() / 60)
                return (
                    f"⚠️ Quality gates expired (last run: {age_min} minutes ago).\n"
                    f"Next step: Press 'Run Gates' to refresh validation."
                )
            except (ValueError, TypeError):
                pass
        return "⚠️ Quality gates too old. Next step: Run gates again."
    
    if block_reason == "gates_failed":
        gates_fail = attrs.get("gates_fail", [])
        if gates_fail:
            return (
                f"⚠️ Quality gates FAILED: {', '.join(gates_fail)}\n"
                f"Next step: Fix issues, then run gates again."
            )
        return "⚠️ Quality gates failed. Next step: Check gate details and fix issues."
    
    if block_reason == "gates_warned":
        gates_warn = attrs.get("gates_warn", [])
        if gates_warn:
            return (
                f"⚠️ Quality gates WARNED: {', '.join(gates_warn)}\n"
                f"Next step: Resolve warnings or acknowledge risk."
            )
        return "⚠️ Quality gates have warnings. Next step: Review and resolve."
    
    if block_reason == "op_busy":
        op_name = attrs.get("op_name", "unknown")
        return (
            f"⚠️ Operation already running: {op_name}\n"
            f"Next step: Wait for operation to complete."
        )
    
    return f"⚠️ Flash blocked: {block_reason}"
