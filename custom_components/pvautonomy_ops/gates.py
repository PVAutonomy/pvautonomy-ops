"""PVAutonomy Ops Quality Gates (Action K: Run Gates).

Validates production readiness via automated checks.
"""
import logging
from datetime import datetime, timezone
from typing import TypedDict

from homeassistant.core import HomeAssistant

from .discovery import ContractInputReader

_LOGGER = logging.getLogger(__name__)


class GateResult(TypedDict):
    """Gate execution result."""

    gate_id: str
    gate_name: str
    status: str  # 'pass' | 'warn' | 'fail' | 'skip'
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

    async def run_all_gates(self, target_device: str | None = None, *, entry_id: str | None = None) -> GatesSummary:
        """Run all quality gates and return summary.

        In Factory mode (active_device_kind == 'factory'), production-only gates
        (GATE-002, GATE-003, GATE-004) are skipped and GATE-FACTORY-001 is run.
        In Production mode, all standard gates run; GATE-FACTORY-001 is NOT evaluated.

        D-OPS-FACTORY-TO-PRODUCTION-UI-001 / P3-9-001 C3

        Args:
            target_device: Optional device filter (not used in MVP gates)

        Returns:
            GatesSummary with overall status and details
        """
        results: list[GateResult] = []

        # Determine device kind (factory vs production)
        # Entry-scoped: prevents cross-entry bleed when target_device is None
        device_kind = await self.input_reader.get_selected_device_kind(
            device_name=target_device, entry_id=entry_id
        )
        is_factory = device_kind == "factory"

        if is_factory:
            _LOGGER.info("Factory mode detected — running factory-aware gates")

        # GATE-001: Device Discovery (MUST) — runs in both modes
        results.append(await self._gate_001_device_discovery(entry_id=entry_id))

        if is_factory:
            # Factory mode: skip production-only gates, run factory readiness
            now_str = datetime.now(timezone.utc).isoformat()
            results.append({
                "gate_id": "GATE-002",
                "gate_name": "Health Indicators",
                "status": "skip",
                "evidence": "Skipped in Factory mode (no health sensors)",
                "checked_at": now_str,
            })
            results.append({
                "gate_id": "GATE-003",
                "gate_name": "Entity Naming (I18N)",
                "status": "skip",
                "evidence": "Skipped in Factory mode (no production entities)",
                "checked_at": now_str,
            })
            results.append({
                "gate_id": "GATE-004",
                "gate_name": "Modbus Registers",
                "status": "skip",
                "evidence": "Skipped in Factory mode (no Modbus)",
                "checked_at": now_str,
            })

            # GATE-FACTORY-001: Factory Readiness
            results.append(await self._gate_factory_001_readiness(target_device, entry_id=entry_id))
        else:
            # Production mode: standard gates
            # GATE-002: Health Indicators (MUST)
            results.append(await self._gate_002_health_indicators())

            # GATE-003: Entity Naming (MUST)
            results.append(await self._gate_003_entity_naming())

            # GATE-004: Modbus Registers (SHOULD) - warn only for MVP
            results.append(await self._gate_004_modbus_registers())

        # Build summary
        summary = self._build_summary(results)
        return summary

    async def _gate_001_device_discovery(self, *, entry_id: str | None = None) -> GateResult:
        """GATE-001: Validate device discovery works.

        Requirements:
        - At least one Edge101 device discoverable
        - Uses Device Registry (P3-8-001) first, falls back to legacy sensor
        """
        gate_id = "GATE-001"
        gate_name = "Device Discovery"

        try:
            # P3-11-001: Try Device Registry first (works for factory + production)
            registry_result = await self.input_reader.get_registry_devices()
            factory_devices = registry_result.get("factory", [])
            production_devices = registry_result.get("production", [])
            total_registry = len(factory_devices) + len(production_devices)

            if total_registry > 0:
                names = [d.get("name", "?") for d in factory_devices + production_devices]
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "pass",
                    "evidence": f"{total_registry} device(s) via registry: {', '.join(names)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            # Fallback: legacy sensor (Contract Input A)
            devices = await self.input_reader.get_discovered_devices()
            validation = await self.input_reader.validate_inputs(entry_id=entry_id)

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
                    "evidence": "No devices discovered (registry empty, legacy sensor empty)",
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

        EPIC-005-A1 / PN-1: capability-based health via Entity Registry
        (device_class + state_class). Falls back to legacy template sensor.

        Requirements:
        - All production devices have health data (legacy or capability-based)
        - At least one device healthy (state=False means healthy)
        """
        gate_id = "GATE-002"
        gate_name = "Health Indicators"

        try:
            from .discovery import DEVICE_KIND_PRODUCTION

            all_devices = await self.input_reader.get_all_discovered_devices()
            # Only check production devices for health
            prod_devices = [d for d in all_devices if d.state == DEVICE_KIND_PRODUCTION]

            if len(prod_devices) == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "warn",
                    "evidence": "No production devices to check health",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            devices_healthy = 0
            devices_unhealthy = 0
            missing_health = []

            for dev in prod_devices:
                # Try legacy first (get_device_health falls back to compute_device_health)
                health = await self.input_reader.get_device_health(dev.name)

                _LOGGER.debug(
                    "GATE-002: device=%s available=%s state=%s missing=%s",
                    dev.name, health.get("available"), health.get("state"),
                    health.get("missing_sensors", []),
                )

                if not health.get("available", False):
                    missing_health.append(dev.name)
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
                    "evidence": f"Missing health data: {', '.join(missing_health)}",
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
            # Load legacy allowlist (async — HA 2026.2 blocks open() in event loop)
            legacy_allowlist = await self._load_legacy_allowlist()
            # EPIC-005-A1: use unified discovery
            all_devices = await self.input_reader.get_all_discovered_devices()
            devices = [d.name for d in all_devices]

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
            from .discovery import DEVICE_KIND_PRODUCTION

            # EPIC-005-A1: use unified discovery, only check production devices
            all_devices = await self.input_reader.get_all_discovered_devices()
            devices = [d.name for d in all_devices if d.state == DEVICE_KIND_PRODUCTION]

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

    async def _load_legacy_allowlist(self) -> list[str]:
        """Load legacy device allowlist from data file.

        HA 2026.2: blocking open() in event loop raises RuntimeError.
        File I/O moved to executor thread.

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

            def _read() -> list[str]:
                with open(allowlist_path, "r") as f:
                    data = json.load(f)
                    return data.get("legacy_device_names", [])

            return await self.hass.async_add_executor_job(_read)

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

    async def _gate_factory_001_readiness(
        self, target_device: str | None = None, *, entry_id: str | None = None
    ) -> GateResult:
        """GATE-FACTORY-001: Validate factory device is online and reachable.

        Pre-flight only checks device connectivity.  Model/location/number
        are validated later in the SETUP_CONFIGURE stage of the wizard.

        Requirements (all must pass):
        - A factory device is selected
        - Factory device has at least one entity with a non-unavailable state
          (proves the ESPHome connection is live)

        D-OPS-FACTORY-TO-PRODUCTION-UI-001 §5
        """
        gate_id = "GATE-FACTORY-001"
        gate_name = "Factory Readiness"

        try:
            from homeassistant.helpers import entity_registry as er

            missing: list[str] = []

            # Check 1: Factory device selected
            # Entry-scoped: prevents cross-entry bleed (P0 fix)
            selected = target_device or await self.input_reader.get_selected_device(entry_id=entry_id)
            if not selected:
                missing.append("No device selected")
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "fail",
                    "evidence": f"Missing: {'; '.join(missing)}",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            # Check 2: Resolve HA device ID and verify at least one entity is online
            registry_devices = await self.input_reader.get_registry_devices()
            ha_device_id = None
            for dev in registry_devices.get("factory", []):
                if dev["name"] == selected:
                    ha_device_id = dev["id"]
                    break

            if not ha_device_id:
                missing.append(f"Device '{selected}' not found in factory registry")
            else:
                # Find entities belonging to this device and check if any are online
                ent_reg = er.async_get(self.hass)
                device_entities = er.async_entries_for_device(ent_reg, ha_device_id)

                if not device_entities:
                    missing.append(f"No entities found for device '{selected}'")
                else:
                    has_online = False
                    for entry in device_entities:
                        state = self.hass.states.get(entry.entity_id)
                        if state is not None and state.state not in ("unavailable", "unknown"):
                            has_online = True
                            break

                    if not has_online:
                        missing.append(
                            f"Factory device offline — all {len(device_entities)} "
                            f"entities are unavailable"
                        )

            if len(missing) == 0:
                return {
                    "gate_id": gate_id,
                    "gate_name": gate_name,
                    "status": "pass",
                    "evidence": f"Factory device '{selected}' is online and reachable",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }

            return {
                "gate_id": gate_id,
                "gate_name": gate_name,
                "status": "fail",
                "evidence": f"Missing: {'; '.join(missing)}",
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

    def _build_summary(self, results: list[GateResult]) -> GatesSummary:
        """Build gates summary from individual results.

        Skipped gates (status='skip') do NOT count as failures.

        Args:
            results: List of gate check results

        Returns:
            GatesSummary with overall status (pass|warn|fail)
        """
        passed_gates = []
        warned_gates = []
        failed_gates = []
        skipped_gates = []
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
            elif status == "skip":
                skipped_gates.append(gate_id)

        # Determine overall status (skipped gates don't affect outcome)
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
