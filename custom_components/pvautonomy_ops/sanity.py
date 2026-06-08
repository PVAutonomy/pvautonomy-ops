"""Post-install sanity checks (P3-13-001).

Verifies a freshly-installed (or re-configured) device is actually
operational by checking a small set of "heartbeat" signals.

Ref: WORKER-PROMPT-P3-13-001, Section 6.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Heartbeat sensors — at least one must update within the timeout
_HEARTBEAT_SENSORS = [
    "sensor.{node}_uptime_device",
    "sensor.{node}_wifi_signal_device",
]

# Core value sensors — should be non-null/unavailable
_CORE_SENSORS = [
    "sensor.{node}_battery_soc_device",
    "sensor.{node}_ac_output_power_device",
    "sensor.{node}_pv1_power_device",
]

# Maximum time (seconds) to wait for sensors to become available
SANITY_TIMEOUT = 90
SANITY_POLL_INTERVAL = 5
# Warm-up delay (seconds) before starting checks after OTA reboot
SANITY_WARMUP_DELAY = 15


async def run_sanity_checks(
    hass: HomeAssistant,
    *,
    node_name: str,
    timeout: int = SANITY_TIMEOUT,
) -> dict[str, Any]:
    """Run post-install sanity checks for a device.

    Args:
        hass: Home Assistant instance.
        node_name: ESPHome node name (e.g. "sph10k-haus-02").
                   Underscores are used for entity IDs.
        timeout: Max seconds to wait for sensors.

    Returns:
        Dict with keys: passed, checks, failures, heartbeat_ok,
        core_values, duration_s.
    """
    # Normalize node name: dashes → underscores for entity IDs
    node_slug = node_name.replace("-", "_")

    # Warm-up: give the device time to boot, connect WiFi/API, start sensors
    _LOGGER.info("Sanity warm-up: waiting %ds for %s to stabilize…", SANITY_WARMUP_DELAY, node_name)
    await asyncio.sleep(SANITY_WARMUP_DELAY)

    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    heartbeat_ok = False
    core_values: dict[str, Any] = {}

    elapsed = 0
    # Wait for at least one heartbeat sensor
    while elapsed < timeout:
        for pattern in _HEARTBEAT_SENSORS:
            entity_id = pattern.format(node=node_slug)
            state = hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable", ""):
                heartbeat_ok = True
                checks.append({
                    "name": f"Heartbeat ({entity_id})",
                    "result": "pass",
                    "value": state.state,
                })
                break
        if heartbeat_ok:
            break
        await asyncio.sleep(SANITY_POLL_INTERVAL)
        elapsed += SANITY_POLL_INTERVAL

    if not heartbeat_ok:
        failures.append("No heartbeat sensor responded within timeout")
        for pattern in _HEARTBEAT_SENSORS:
            entity_id = pattern.format(node=node_slug)
            checks.append({
                "name": f"Heartbeat ({entity_id})",
                "result": "fail",
                "value": None,
            })

    # Check core sensors (best-effort, not blocking)
    for pattern in _CORE_SENSORS:
        entity_id = pattern.format(node=node_slug)
        state = hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable", ""):
            core_values[entity_id] = state.state
            checks.append({
                "name": f"Core value ({entity_id})",
                "result": "pass",
                "value": state.state,
            })
        else:
            # Not a hard failure — sensor may not exist for this model
            value = state.state if state else "not_found"
            checks.append({
                "name": f"Core value ({entity_id})",
                "result": "warn" if state else "skip",
                "value": value,
            })
            if state and state.state in ("unknown", "unavailable"):
                failures.append(f"{entity_id} is {state.state}")

    passed = heartbeat_ok  # Heartbeat is the hard requirement
    _LOGGER.info(
        "Sanity checks for %s: passed=%s, heartbeat=%s, core_values=%d, failures=%d",
        node_name,
        passed,
        heartbeat_ok,
        len(core_values),
        len(failures),
    )

    return {
        "passed": passed,
        "checks": checks,
        "failures": failures,
        "heartbeat_ok": heartbeat_ok,
        "core_values": core_values,
        "duration_s": elapsed,
    }
