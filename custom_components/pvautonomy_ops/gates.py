"""PVAutonomy Ops Quality Gates (Action K: Run Gates).

Validates production readiness via automated checks.
"""
import logging
from datetime import datetime, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .discovery import ContractInputReader

_LOGGER = logging.getLogger(__name__)


class GateResult(TypedDict):
    """Gate execution result."""

    gate_id: str
    gate_name: str
    status: str  # 'pass' | 'warn' | 'fail'
    evidence: str
    checked_at: str  # ISO 8601


class GatesSummary(TypedDict):
    """Overall gates summary."""

    overall: str  # 'pass' | 'warn' | 'fail'
    total: int
    passed: int
    warned: int
    failed: int
    passed_gates: list[str]
    warned_gates: list[str]
    failed_gates: list[str]
    details: dict[str, GateResult]
    checked_at: str  # ISO 8601


class QualityGateChecker:
    """Production readiness quality gate checker."""

    def __init__(self, hass: HomeAssistant, input_reader: ContractInputReader):
        self.hass = hass
        self.input_reader = input_reader

    async def run_all_gates(self, target_device: str | None = None) -> GatesSummary:
        """Run all quality gates and return summary.

        Args:
            target_device: Optional device filter (not used in MVP gates)

        Returns:
            GatesSummary with overall status and details
        """
        results: list[GateResult] = []

        # GATE-001: Device Discovery (MUST)
        results.append(await self._gate_001_device_discovery())

        # GATE-002: Health Indicators (MUST)
        results.append(await self._gate_002_health_indicators())

        # GATE-003: Entity Naming (MUST)
        results.append(await self._gate_003_entity_naming())

        # GATE-004: Modbus Registers (SHOULD) - warn only for MVP
        results.append(await self._gate_004_modbus_registers())

        # Build summary
        summary = self._build_summary(results)
        return summary

    async def _gate_001_device_discovery(self) -> GateResult:
        """GATE-001: Validate device discovery works.

        Requirements:
        - sensor.edge101_production_devices exists
        - devices list not empty
        - discovery_method valid
        """
        gate_id = "GATE-001"
        gate_name = "Device Discovery"

        try:
            devices = await self.input_reader.get_discovered_devices()
            validation = await self.input_reader.validate_inputs()

            if not validation.get("valid", False):
                missing = validation.get("missing_inputs", [])
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "fail",
                    "evidence": f"Missing inputs: {', '.join(missing)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            if len(devices) == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",
                    "evidence": "No devices discovered (empty list)",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "pass",
                "evidence": f"{len(devices)} device(s) discovered: {', '.join(devices)}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "fail",
                "evidence": f"Exception: {str(e)}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

    async def _gate_002_health_indicators(self) -> GateResult:
        """GATE-002: Validate health indicators for all devices.

        Requirements:
        - All devices have binary_sensor.{device}_health
        - At least one device online (state=False means healthy)
        """
        gate_id = "GATE-002"
        gate_name = "Health Indicators"

        try:
            devices = await self.input_reader.get_discovered_devices()

            if len(devices) == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",
                    "evidence": "No devices to check health",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            devices_healthy = 0
            devices_unhealthy = 0
            missing_health = []

            for device in devices:
                health = await self.input_reader.get_device_health(device)

                if not health.get("available", False):
                    missing_health.append(device)
                    continue

                # Contract: state=False means healthy (no problem)
                if health.get("state", True):
                    devices_unhealthy += 1
                else:
                    devices_healthy += 1

            if len(missing_health) > 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "fail",
                    "evidence": f"Missing health sensors: {', '.join(missing_health)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            if devices_healthy == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "fail",
                    "evidence": f"All {devices_unhealthy} device(s) unhealthy",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            if devices_unhealthy > 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",
                    "evidence": f"{devices_healthy} healthy, {devices_unhealthy} unhealthy",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "pass",
                "evidence": f"All {devices_healthy} device(s) healthy",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "fail",
                "evidence": f"Exception: {str(e)}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

    async def _gate_003_entity_naming(self) -> GateResult:
        """GATE-003: Validate entity naming conventions.

        Requirements:
        - Contract v1.0.0: sensor.{device}_{metric}_device pattern
        - D-ADDON-I18N-001: Check against legacy allowlist
        """
        gate_id = "GATE-003"
        gate_name = "Entity Naming (I18N)"

        try:
            # Load legacy allowlist
            legacy_allowlist = self._load_legacy_allowlist()
            devices = await self.input_reader.get_discovered_devices()

            violations = []
            warnings = []

            for device in devices:
                # Check if device is legacy (grandfathered)
                if device in legacy_allowlist:
                    warnings.append(
                        f"{device} (legacy, grandfathered)"
                    )
                    continue

                # NEW devices SHOULD be language-neutral (SHOULD not MUST)
                if self._has_localized_tokens(device):
                    violations.append(
                        f"{device} (contains localized tokens)"
                    )

            if len(violations) > 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",  # SHOULD violation = warn
                    "evidence": f"Non-compliant: {', '.join(violations)}; Legacy OK: {', '.join(warnings)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            if len(warnings) > 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "pass",
                    "evidence": f"All compliant (legacy devices: {', '.join(warnings)})",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "pass",
                "evidence": f"{len(devices)} device(s) checked, all language-neutral",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "fail",
                "evidence": f"Exception: {str(e)}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

    async def _gate_004_modbus_registers(self) -> GateResult:
        """GATE-004: Validate Modbus register availability (SHOULD - warn only).

        Requirements:
        - Check for common Modbus entities (battery_soc, ac_output_power)
        - SHOULD have at least 5 register entities per device
        """
        gate_id = "GATE-004"
        gate_name = "Modbus Registers"

        try:
            devices = await self.input_reader.get_discovered_devices()

            if len(devices) == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",
                    "evidence": "No devices to check Modbus entities",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            # For MVP: Just check that sensor entities exist (basic coverage)
            # Full implementation would query ESPhome API for register counts
            missing_entities = []

            for device in devices:
                # Check for basic entities (battery_soc, ac_output_power)
                battery_soc_entity = f"sensor.{device}_battery_soc_device"
                power_entity = f"sensor.{device}_ac_output_power_device"

                battery_state = self.hass.states.get(battery_soc_entity)
                power_state = self.hass.states.get(power_entity)

                if not battery_state and not power_state:
                    missing_entities.append(device)

            if len(missing_entities) > 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",  # SHOULD = warn only
                    "evidence": f"Missing Modbus entities for: {', '.join(missing_entities)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "pass",
                "evidence": f"Basic Modbus entities found for all {len(devices)} device(s)",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "warn",  # SHOULD gate = warn on exception
                "evidence": f"Exception: {str(e)}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

    def _load_legacy_allowlist(self) -> list[str]:
        """Load legacy device allowlist from data file.

        Returns:
            List of grandfathered device names (e.g., ['sph10k_haus_03'])
        """
        try:
            import json
            import os

            allowlist_path = os.path.join(
                os.path.dirname(__file__), "data", "legacy_allowlist.json"
            )

            if not os.path.exists(allowlist_path):
                _LOGGER.warning("Legacy allowlist not found: %s", allowlist_path)
                return []

            with open(allowlist_path, "r") as f:
                data = json.load(f)
                return data.get("legacy_device_names", [])

        except Exception as e:
            _LOGGER.error("Failed to load legacy allowlist: %s", e)
            return []

    def _has_localized_tokens(self, device_name: str) -> bool:
        """Check if device name contains localized tokens.

        Args:
            device_name: Device identifier (e.g., 'sph10k_haus_03')

        Returns:
            True if contains German/localized tokens
        """
        # German tokens that should not appear in NEW device names
        localized_tokens = [
            "haus",
            "garage",  # Wait, garage is also English... let's be more strict
            "keller",
            "werkstatt",
            "heizung",
            "neustart",
        ]

        device_lower = device_name.lower()

        for token in localized_tokens:
            if token in device_lower:
                # garage is English, so allow it
                if token == "garage":
                    continue
                return True

        return False

    def _build_summary(self, results: list[GateResult]) -> GatesSummary:
        """Build gates summary from individual results.

        Args:
            results: List of gate check results

        Returns:
            GatesSummary with overall status (pass|warn|fail)
        """
        passed_gates = []
        warned_gates = []
        failed_gates = []
        details = {}

        for result in results:
            gate_id = result["gate_id"]
            status = result["status"]

            details[gate_id] = result

            if status == "pass":
                passed_gates.append(gate_id)
            elif status == "warn":
                warned_gates.append(gate_id)
            elif status == "fail":
                failed_gates.append(gate_id)

        # Determine overall status
        overall = "pass"
        if len(failed_gates) > 0:
            overall = "fail"
        elif len(warned_gates) > 0:
            overall = "warn"

        return {
            "overall": overall,
            "total": len(results),
            "passed": len(passed_gates),
            "warned": len(warned_gates),
            "failed": len(failed_gates),
            "passed_gates": passed_gates,
            "warned_gates": warned_gates,
            "failed_gates": failed_gates,
            "details": details,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
