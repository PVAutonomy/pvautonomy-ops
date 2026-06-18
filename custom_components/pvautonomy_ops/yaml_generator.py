"""YAML Generator for per-device firmware configs (P3-12-001).

Wraps the logic from esphome/generator/generate_from_registry.py to produce
device-specific ESPHome YAML content as a string (no disk write).
The YAML is then sent to the Builder App for compilation.

Ref: WORKER-PROMPT-P3-12-001, Phase A2.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .const import TIER_ORDER, TIER_STANDARD
from .defs_paths import resolve_base_dir, resolve_registry_root
from .device_id import compute_node_name

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier filter (EPIC-010 Tiering v1.1)
# ---------------------------------------------------------------------------

def _tier_of(reg: dict) -> str:
    """Return the tier of a registry entity, defaulting to 'standard'.

    EC-2: Raises YamlGenerationError for unknown tier values — prevents silent
    pass-through of typos/bad registry data (e.g. tier: "expert").
    """
    raw = reg.get("tier", TIER_STANDARD)
    if raw not in TIER_ORDER:
        raise YamlGenerationError(
            f"Unknown tier '{raw}' on entity '{reg.get('id', '?')}'. "
            f"Valid tiers: {list(TIER_ORDER)}. Fix the registry JSON."
        )
    return raw


def _should_emit(reg: dict, selected_tier: str) -> bool:
    """Return True if this entity should be emitted for the selected tier.

    Tier hierarchy: standard (0) < extended (1) < unsafe (2).
    An entity is emitted when its tier rank <= the selected tier rank.
    Entities without a 'tier' field default to 'standard' (backwards-compatible).

    ``generator_skip: true`` is a hard block that suppresses emission
    regardless of tier. Use it for known-dangerous or semantically invalid
    mappings that must never reach firmware until explicitly repaired.

    EC-1: Unknown/invalid selected_tier silently falls back to 'standard' (fail-closed).
    A warning is logged so the caller is alerted without crashing.
    """
    if reg.get("generator_skip", False):
        _LOGGER.debug(
            "Generator skip: suppressing entity %s via registry flag",
            reg.get("id", "?"),
        )
        return False

    entity_rank = TIER_ORDER[_tier_of(reg)]  # _tier_of() already validated
    if selected_tier not in TIER_ORDER:
        _LOGGER.warning(
            "Unknown selected_tier '%s'; defaulting to 'standard' (fail-closed).",
            selected_tier,
        )
        selected_rank = 0
    else:
        selected_rank = TIER_ORDER[selected_tier]
    return entity_rank <= selected_rank


# ---------------------------------------------------------------------------
# Version gating (EPIC-010 vNext: Version-aware Registry)
# ---------------------------------------------------------------------------

def _version_ok(reg: dict, modbus_version: int | None) -> bool:
    """Return True if this entity passes version gating.

    If the entity has no min/max_modbus_version fields it is version-agnostic
    and always passes.  If modbus_version is None (unknown), any entity with
    version constraints is suppressed (fail-safe).
    """
    min_ver = reg.get("min_modbus_version")
    max_ver = reg.get("max_modbus_version")
    if min_ver is None and max_ver is None:
        return True  # version-agnostic
    if modbus_version is None:
        return False  # unknown device version → suppress version-gated entities
    if min_ver is not None and modbus_version < min_ver:
        return False
    if max_ver is not None and modbus_version > max_ver:
        return False
    return True


def _effective_tier(selected_tier: str, map_confirmed: bool) -> str:
    """Degrade tier to 'standard' only when map evidence is contradictory.

    TASK-20260327: The boolean ``map_confirmed`` now encodes a tri-state
    contract from the Config Flow:

    - ``True``  — map evidence is either *confirmed* (anchors pass) or
      *unknown* (no pre-build evidence available).  Both cases preserve
      the customer's tier choice.
    - ``False`` — map evidence is *contradictory* (anchors out of range,
      HR73 unparseable).  Non-standard tiers must be downgraded.

    This is a secondary safety net; the Config Flow already blocks
    contradictory evidence before reaching the build.
    """
    if selected_tier != TIER_STANDARD and not map_confirmed:
        _LOGGER.warning(
            "Map evidence contradictory — degrading tier from '%s' to 'standard' (fail-closed).",
            selected_tier,
        )
        return TIER_STANDARD
    return selected_tier

# Firmware-definition data locations are resolved by the shared resolver
# (bundle-first, /config D8 fallback) — see defs_paths.py.


class YamlGenerationError(Exception):
    """YAML generation failed."""


def generate_device_yaml(
    *,
    model: str,
    site: str,
    number: str | int,
    registry_file: str,
    mac_suffix: str | None = None,
    version: str | None = None,
    selected_tier: str = TIER_STANDARD,
    modbus_version: int | None = None,
    map_confirmed: bool = False,
    esphome_base_path: Path | None = None,
    registry_root: Path | None = None,
) -> str:
    """Generate device-specific ESPHome YAML content.

    This produces the YAML content as a string — it does NOT write to disk.
    The content is sent to the Builder App for compilation.

    Args:
        model: Inverter model slug (e.g. "sph10k")
        site: Installation location (e.g. "haus")
        number: Device number (e.g. "02" or 2)
        registry_file: Relative path to registry JSON (e.g. "growatt/sph/sph10k.json")
        mac_suffix: 6-char hex MAC suffix for device-specific secrets (e.g. "2eb1e4")
        version: Firmware version string (optional)
        selected_tier: Tier gate — "standard" | "extended" | "unsafe" (default: "standard").
            Only entities with tier <= selected_tier are emitted. Entities without a
            'tier' field default to "standard" (backwards-compatible).
        modbus_version: HR73 value read from device (None if unknown/fresh install).
            Used for version-gated entities (min/max_modbus_version in registry).
        map_confirmed: Tri-state-compatible safety flag from the Config Flow.
            True means the map evidence is either confirmed or currently unknown.
            False means the evidence is contradictory, so extended/unsafe
            entities are suppressed fail-closed.
        esphome_base_path: Override path to esphome/ directory
        registry_root: Override path to inverter-registry/ directory

    Returns:
        Complete ESPHome YAML content as string (ready for compilation).

    Raises:
        YamlGenerationError: If generation fails.
    """
    node_name = compute_node_name(model, site, number)
    _LOGGER.info(
        "Generating YAML for node: %s (registry: %s, tier: %s, "
        "modbus_version: %s, map_confirmed: %s)",
        node_name, registry_file, selected_tier, modbus_version, map_confirmed,
    )

    try:
        # 1. Load Production Base
        base = _load_production_base(esphome_base_path)

        # 2. Load Registry
        registry = _load_registry(registry_file, registry_root)

        # 3. Update device info
        _update_device_info(base, node_name, registry, version)

        # 4. Inject substitutions (API key, OTA password via !secret)
        _inject_substitutions(base, node_name, registry, mac_suffix)

        # 5. Production WiFi (NVS-based, AP fallback)
        _add_production_wifi(base)

        # 6. Modbus UART
        _add_modbus_uart(base, registry)

        # 7. Modbus Controller
        _add_modbus_controller(base, registry)

        # 8. Registers (sensors, numbers, switches) — filtered by tier + version
        _add_registers(
            base, registry,
            selected_tier=selected_tier,
            modbus_version=modbus_version,
            map_confirmed=map_confirmed,
        )

        # 9. Render to YAML string
        yaml_content = yaml.dump(
            base, default_flow_style=False, sort_keys=False, allow_unicode=True
        )

        # 10. Post-render: __SECRET_xxx__ → !secret xxx
        yaml_content = _patch_secret_tokens(yaml_content)

        _LOGGER.info(
            "YAML generated: %d lines, %d sensors, %d numbers, %d switches, %d selects",
            yaml_content.count("\n"),
            len(base.get("sensor", [])),
            len(base.get("number", [])),
            len(base.get("switch", [])),
            len(base.get("select", [])),
        )
        return yaml_content

    except Exception as exc:
        msg = f"YAML generation failed for {node_name}: {exc}"
        _LOGGER.error(msg)
        raise YamlGenerationError(msg) from exc


# ---------------------------------------------------------------------------
# Internal helpers — mirrored from generate_from_registry.py
# ---------------------------------------------------------------------------

def _load_production_base(base_path: Path | None = None) -> dict[str, Any]:
    """Load edge101-production-base.yaml."""
    if base_path is None:
        base_path = resolve_base_dir()

    path = base_path / "edge101-production-base.yaml"
    if not path.exists():
        raise YamlGenerationError(f"Production base not found: {path}")

    # Use a custom loader that treats !secret and !include as plain strings
    # (ESPHome tags are not standard YAML — safe_load would reject them)
    class _ESPHomeLoader(yaml.SafeLoader):
        pass

    for tag in ("secret", "include", "lambda", "extend"):
        _ESPHomeLoader.add_constructor(
            f"!{tag}",
            lambda loader, node, t=tag: f"__{t.upper()}_{loader.construct_scalar(node)}__",  # type: ignore[arg-type]
        )

    with open(path) as fh:
        try:
            data = yaml.load(fh, Loader=_ESPHomeLoader)  # noqa: S506
        except yaml.YAMLError as exc:
            raise YamlGenerationError(
                f"Production base YAML parse error: {exc}"
            ) from exc
    if not isinstance(data, dict):
        raise YamlGenerationError(
            f"Production base YAML is empty or malformed: {path}"
        )
    return data


def _load_registry(
    registry_file: str, registry_root: Path | None = None
) -> dict[str, Any]:
    """Load inverter registry JSON."""
    if registry_root is None:
        registry_root = resolve_registry_root()

    path = registry_root / registry_file
    if not path.exists():
        raise YamlGenerationError(f"Registry file not found: {path}")

    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise YamlGenerationError(
            f"Registry file is not a valid JSON object: {path}"
        )
    return data


def _update_device_info(
    config: dict, device_name: str, registry: dict, version: str | None
) -> None:
    """Set ESPHome device metadata."""
    # Defensive: ensure 'esphome' is a dict (yaml.safe_load returns None for empty keys)
    if not isinstance(config.get("esphome"), dict):
        config["esphome"] = {}
    config["esphome"]["name"] = device_name
    config["esphome"]["name_add_mac_suffix"] = False
    # TASK-20260522B: do NOT bake an absolute build_path. This YAML is
    # authoritative under build_contract=yaml_authority and shipped verbatim
    # to the remote GitHub Actions runner, where an HAOS/add-on path like
    # /data/build/<name> is not writable (PermissionError). Omitting
    # build_path lets each environment use its own writable default — the
    # remote runner uses its workspace default, and the local ESPHome add-on
    # (config_flow-written YAML) already defaults to /data/build/<name>, so
    # there is no behaviour change locally.

    manufacturer = registry.get("manufacturer", "Unknown")
    model_data = registry.get("model", "Unknown")
    model = model_data.get("name", "Unknown") if isinstance(model_data, dict) else str(model_data)

    # CRITICAL: friendly_name controls HA entity ID prefix.
    # Must be node-identity-based so entity IDs match 3-layer convention:
    #   sph10k_haus_02_* (from "sph10k-haus-02")
    # NOT model-based ("PVAutonomy SPH10000TL3-BH" → pvautonomy_sph10000tl3_bh_*)
    # See BLOCKER-REPORT-entity-naming-convention-violation.md
    friendly = device_name.replace("-", " ").title()  # "sph10k-haus-02" → "Sph10K Haus 02"
    config["esphome"]["friendly_name"] = friendly
    config["esphome"]["comment"] = f"PVAutonomy Edge101 - {manufacturer} {model}"

    # CRITICAL: project block MUST always be set so HA Device Registry
    # registers manufacturer="PVAutonomy", model="Edge101".
    # Without this, ESPHome defaults to "Espressif"/"esp32dev" and
    # pvautonomy_ops discovery cannot find the device.
    config["esphome"]["project"] = {
        "name": "PVAutonomy.Edge101",
        "version": version or "v1.0.0",
    }


def _inject_substitutions(
    config: dict, device_name: str, registry: dict, device_id: str | None
) -> None:
    """Inject substitutions for Production Base."""
    config.setdefault("substitutions", {})
    config["substitutions"]["devicename"] = device_name
    config["substitutions"]["friendly_name"] = config["esphome"]["friendly_name"]
    config["substitutions"]["comment"] = config["esphome"]["comment"]

    # API encryption key (device-specific via !secret)
    # Defensive: ensure api.encryption dict chain exists
    # (yaml.safe_load returns None for bare `api:` or `encryption:` keys)
    if not isinstance(config.get("api"), dict):
        config["api"] = {}
    if not isinstance(config["api"].get("encryption"), dict):
        config["api"]["encryption"] = {}

    # Resolve MAC suffix once, used for both API key and OTA password
    suffix: str | None = None
    if device_id:
        from .mac_utils import InvalidMACError, canonical_mac_last6
        try:
            suffix = canonical_mac_last6(device_id)
        except InvalidMACError as exc:
            raise YamlGenerationError(
                f"Invalid mac_suffix for secret naming: {exc}"
            ) from exc
        config["api"]["encryption"]["key"] = f"__SECRET_edge101_api_key_{suffix}__"
    else:
        config["api"]["encryption"]["key"] = "__SECRET_api_encryption_key__"

    # OTA password (device-specific via !secret)
    ota_secret = (
        f"__SECRET_edge101_ota_password_{suffix}__"
        if suffix
        else "__SECRET_ota_password__"
    )

    if not config.get("ota"):
        config["ota"] = []

    if isinstance(config["ota"], list):
        esphome_ota = next(
            (e for e in config["ota"] if e.get("platform") == "esphome"), None
        )
        if esphome_ota is None:
            esphome_ota = {"platform": "esphome"}
            config["ota"].append(esphome_ota)
        esphome_ota["password"] = ota_secret
    elif isinstance(config["ota"], dict):
        config["ota"]["password"] = ota_secret


def _add_production_wifi(config: dict) -> None:
    """Production WiFi: AP + Captive Portal (NVS Station credentials from Factory)."""
    config["wifi"] = {
        "ap": {"ssid": "PVA-Edge", "password": "${wifi_fallback_password}"}
    }
    config["captive_portal"] = None

    for key in ("improv_serial",):
        config.pop(key, None)

    # Defensive: bare `button:` in YAML → None, not a list
    if "button" in config and isinstance(config.get("button"), list):
        config["button"] = [
            btn for btn in config["button"]
            if isinstance(btn, dict) and btn.get("name") != "Restart after manifest URL change"
        ]
        if not config["button"]:
            del config["button"]
    elif "button" in config and config["button"] is None:
        del config["button"]

    # Defensive: bare `logger:` in YAML → None, not a dict
    if not isinstance(config.get("logger"), dict):
        config["logger"] = {}
    config["logger"]["baud_rate"] = 0
    config["logger"]["level"] = "DEBUG"


def _get_protocol_config(registry: dict) -> dict:
    """Extract protocol config from registry.

    Schema-B uses 'protocol' key with 'slave_id', 'baud_rate', etc.
    Legacy schemas may use 'modbus' key with 'slave_address'.
    """
    proto = registry.get("protocol")
    if isinstance(proto, dict) and proto:
        return {
            "baud_rate": proto.get("baud_rate", 9600),
            "slave_address": proto.get("slave_id", proto.get("slave_address", 1)),
            "parity": proto.get("parity", "NONE"),
            "data_bits": proto.get("data_bits", 8),
            "stop_bits": proto.get("stop_bits", 1),
        }
    # Fallback: legacy 'modbus' key
    modbus = registry.get("modbus")
    return modbus if isinstance(modbus, dict) else {}


def _add_modbus_uart(config: dict, registry: dict) -> None:
    """Add UART for Modbus RTU."""
    modbus_config = _get_protocol_config(registry)
    uart = {
        "id": "uart_modbus",
        "baud_rate": modbus_config.get("baud_rate", 9600),
        "tx_pin": 17,
        "rx_pin": 36,
        "stop_bits": modbus_config.get("stop_bits", 1),
    }
    parity = modbus_config.get("parity", "NONE")
    if parity and str(parity).upper() != "NONE":
        uart["parity"] = parity
    config["uart"] = [uart]


def _add_modbus_controller(config: dict, registry: dict) -> None:
    """Add Modbus controller."""
    modbus_config = _get_protocol_config(registry)
    config["modbus"] = [
        {"id": "modbus1", "uart_id": "uart_modbus", "flow_control_pin": 16}
    ]
    config["modbus_controller"] = [
        {
            "id": "inverter",
            "address": modbus_config.get("slave_address", 1),
            "modbus_id": "modbus1",
            "update_interval": "10s",
            "command_throttle": "200ms",
        }
    ]


def _add_registers(
    config: dict,
    registry: dict,
    selected_tier: str = TIER_STANDARD,
    modbus_version: int | None = None,
    map_confirmed: bool = False,
) -> None:
    """Add tier+version-filtered registers from registry (Schema-B).

    Emit decision: TierGate AND VersionGate AND (tier != standard → MapConfirmed).
    - TierGate: entity tier <= effective_tier (EPIC-010 Tiering v1.1)
    - VersionGate: min/max_modbus_version satisfied (EPIC-010 vNext)
    - MapConfirmed: anchors verified; if False and tier != standard, tier degrades
      to 'standard' (fail-closed per Version-aware Registry strategy).
    """
    tier = _effective_tier(selected_tier, map_confirmed)
    regs = registry.get("registers")
    if not isinstance(regs, dict):
        regs = {}
    _raw_sensors = regs.get("sensors")
    sensors: list = _raw_sensors if isinstance(_raw_sensors, list) else []
    _raw_numbers = regs.get("numbers")
    numbers: list = _raw_numbers if isinstance(_raw_numbers, list) else []
    _raw_switches = regs.get("switches")
    switches: list = _raw_switches if isinstance(_raw_switches, list) else []
    _raw_selects = regs.get("selects")
    selects: list = _raw_selects if isinstance(_raw_selects, list) else []

    # Ensure target lists exist in config (may be None from bare YAML keys)
    if not isinstance(config.get("sensor"), list):
        config["sensor"] = []
    if not isinstance(config.get("number"), list):
        config["number"] = []
    if not isinstance(config.get("switch"), list):
        config["switch"] = []
    if not isinstance(config.get("select"), list):
        config["select"] = []

    for s in sensors:
        if not isinstance(s, dict):
            continue
        if not _should_emit(s, tier):
            _LOGGER.debug("Tier gate: skipping sensor %s (tier=%s > effective=%s)",
                          s.get("id", "?"), _tier_of(s), tier)
            continue
        if not _version_ok(s, modbus_version):
            _LOGGER.debug("Version gate: skipping sensor %s (min_ver=%s, modbus_version=%s)",
                          s.get("id", "?"), s.get("min_modbus_version"), modbus_version)
            continue
        sensor_id = s.get("id")
        if "address" not in s:
            _LOGGER.warning("Skipping sensor without address: %s", s.get("id") or s.get("name", "?"))
            continue
        sensor_name = f"{sensor_id}_device" if sensor_id else s.get("name", "unknown")
        sensor = {
            "platform": "modbus_controller",
            "modbus_controller_id": "inverter",
            "id": sensor_id,
            "name": sensor_name,
            "address": int(s["address"]),
            "register_type": s.get("register_type", "read").lower(),
            "value_type": s.get("value_type", "U_WORD"),
        }
        for key in ("unit", "device_class", "state_class", "icon", "entity_category"):
            yaml_key = "unit_of_measurement" if key == "unit" else key
            if s.get(key):
                sensor[yaml_key] = s[key]
        # [P1g] Propagate display precision so HA renders sub-unit values at the
        # intended resolution instead of the integer default. The MIC600 Iac1
        # current (scale 0.1) otherwise rounds 0.3 A → "0 A". `is not None` so an
        # explicit accuracy_decimals of 0 is honored too (0 is falsy).
        if s.get("accuracy_decimals") is not None:
            sensor["accuracy_decimals"] = s["accuracy_decimals"]
        _apply_disabled_by_default(sensor, s)
        scale = s.get("scale", 1)
        if scale != 1:
            sensor["filters"] = [{"multiply": scale}]
        config["sensor"].append(sensor)

    for n in numbers:
        if not isinstance(n, dict):
            continue
        if not _should_emit(n, tier):
            _LOGGER.debug("Tier gate: skipping number %s (tier=%s > effective=%s)",
                          n.get("id", "?"), _tier_of(n), tier)
            continue
        if not _version_ok(n, modbus_version):
            _LOGGER.debug("Version gate: skipping number %s (min_ver=%s, modbus_version=%s)",
                          n.get("id", "?"), n.get("min_modbus_version"), modbus_version)
            continue
        number_id = n.get("id")
        if "address" not in n:
            _LOGGER.warning("Skipping number without address: %s", number_id or n.get("name", "?"))
            continue
        number_name = f"{number_id}_device" if number_id else n.get("name", "unknown")
        number = {
            "platform": "modbus_controller",
            "modbus_controller_id": "inverter",
            "id": number_id,
            "name": number_name,
            "address": int(n["address"]),
            "register_type": n.get("register_type", "holding").lower(),
            "value_type": n.get("value_type", "U_WORD"),
        }
        for key in ("min_value", "max_value", "step"):
            if key in n:
                number[key] = n[key]
        for key in ("unit", "icon", "entity_category"):
            yaml_key = "unit_of_measurement" if key == "unit" else key
            if n.get(key):
                number[yaml_key] = n[key]
        _apply_disabled_by_default(number, n)
        scale = n.get("scale", 1)
        if scale != 1:
            number["multiply"] = scale
        # Guardrails for holding register writes: prevent read-back polling,
        # force separate Modbus command grouping, use FC16 (write multiple).
        if number["register_type"] == "holding":
            number.setdefault("use_write_multiple", n.get("use_write_multiple", True))
            number.setdefault("skip_updates", n.get("skip_updates", 999))
            number.setdefault("force_new_range", n.get("force_new_range", True))
        config["number"].append(number)

    # Derived watt sensors (from percent-of-nominal-power numbers)
    _emit_derived_watt_sensors(config, numbers, registry, tier, modbus_version)

    for sw in switches:
        if not isinstance(sw, dict):
            continue
        if not _should_emit(sw, tier):
            _LOGGER.debug("Tier gate: skipping switch %s (tier=%s > effective=%s)",
                          sw.get("id", "?"), _tier_of(sw), tier)
            continue
        if not _version_ok(sw, modbus_version):
            _LOGGER.debug("Version gate: skipping switch %s (min_ver=%s, modbus_version=%s)",
                          sw.get("id", "?"), sw.get("min_modbus_version"), modbus_version)
            continue
        switch_id = sw.get("id")
        if "address" not in sw:
            _LOGGER.warning("Skipping switch without address: %s", switch_id or sw.get("name", "?"))
            continue
        switch_name = f"{switch_id}_device" if switch_id else sw.get("name", "unknown")
        switch = {
            "platform": "modbus_controller",
            "modbus_controller_id": "inverter",
            "id": switch_id,
            "name": switch_name,
            "address": int(sw["address"]),
            "register_type": sw.get("register_type", "holding").lower(),
            "bitmask": 1,
        }
        if "on_value" in sw:
            switch["write_lambda"] = f"return {sw['on_value']};"
        for key in ("icon", "entity_category"):
            if sw.get(key):
                switch[key] = sw[key]
        _apply_disabled_by_default(switch, sw)
        # Guardrails for holding register writes (same as numbers).
        if switch["register_type"] == "holding":
            switch.setdefault("use_write_multiple", sw.get("use_write_multiple", True))
            switch.setdefault("skip_updates", sw.get("skip_updates", 999))
            switch.setdefault("force_new_range", sw.get("force_new_range", True))
        config["switch"].append(switch)

    # EPIC-012 / TASK-014Q: Emit registry-defined selects.
    # ``modbus_controller`` select expects an ``optionsmap`` keyed by label
    # with the raw integer value as the value. The registry stores the
    # inverse direction (raw → label), so we invert it here.
    # Tier, version, and ``generator_skip`` gates apply identically to
    # other entity buckets. Unsafe selects are blocked in extended; entries
    # with ``generator_skip: true`` (e.g. work_mode HR1080 address conflict)
    # are blocked even in unsafe builds.
    for sel in selects:
        if not isinstance(sel, dict):
            continue
        if not _should_emit(sel, tier):
            _LOGGER.debug(
                "Tier gate: skipping select %s (tier=%s > effective=%s)",
                sel.get("id", "?"), _tier_of(sel), tier,
            )
            continue
        if not _version_ok(sel, modbus_version):
            _LOGGER.debug(
                "Version gate: skipping select %s (min_ver=%s, modbus_version=%s)",
                sel.get("id", "?"), sel.get("min_modbus_version"), modbus_version,
            )
            continue
        select_id = sel.get("id")
        if "address" not in sel:
            _LOGGER.warning(
                "Skipping select without address: %s",
                select_id or sel.get("name", "?"),
            )
            continue
        options = sel.get("options")
        if not isinstance(options, dict) or not options:
            _LOGGER.warning(
                "Skipping select without options map: %s",
                select_id or sel.get("name", "?"),
            )
            continue
        # Registry stores raw_value(str) → label; ESPHome optionsmap is label → raw_value(int).
        try:
            optionsmap = {label: int(raw) for raw, label in options.items()}
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Skipping select with non-integer option keys: %s",
                select_id or sel.get("name", "?"),
            )
            continue
        select_name = f"{select_id}_device" if select_id else sel.get("name", "unknown")
        # TASK-20260522: `register_type` is NOT a valid option for
        # `select.modbus_controller` (ESPHome 2025.12.0 rejects it —
        # select implies a holding-register read/write). Keep the registry's
        # register_type as an internal-only value to drive the holding-write
        # guardrails, but never emit it into the generated YAML. All current
        # SPH10K selects are register_type=holding, so write semantics are
        # unchanged by this fix.
        reg_type = sel.get("register_type", "holding").lower()
        select_entry = {
            "platform": "modbus_controller",
            "modbus_controller_id": "inverter",
            "id": select_id,
            "name": select_name,
            "address": int(sel["address"]),
            "value_type": sel.get("value_type", "U_WORD"),
            "optionsmap": optionsmap,
        }
        for key in ("icon", "entity_category"):
            if sel.get(key):
                select_entry[key] = sel[key]
        _apply_disabled_by_default(select_entry, sel)
        # Guardrails for holding register writes (same as numbers/switches).
        if reg_type == "holding":
            select_entry.setdefault("use_write_multiple", sel.get("use_write_multiple", True))
            select_entry.setdefault("skip_updates", sel.get("skip_updates", 999))
            select_entry.setdefault("force_new_range", sel.get("force_new_range", True))
        config["select"].append(select_entry)


def _emit_derived_watt_sensors(
    config: dict,
    numbers: list,
    registry: dict,
    tier: str,
    modbus_version: int | None,
) -> None:
    """Emit template sensors for number entities with derived_watt_sensor config.

    Uses nominal_ac_power_w from the registry header to compute:
        watt = rate_percent * nominal_ac_power_w / 100.0

    Fail-closed: raises YamlGenerationError if nominal_ac_power_w is missing
    when a derived watt sensor is requested.
    """
    for n in numbers:
        if not isinstance(n, dict):
            continue
        dws = n.get("derived_watt_sensor")
        if not dws or not isinstance(dws, dict):
            continue
        # Only emit if the source number passes tier + version gates
        if not _should_emit(n, tier):
            continue
        if not _version_ok(n, modbus_version):
            continue

        source_id = n.get("id")
        if not source_id:
            continue

        nominal_w = registry.get("nominal_ac_power_w")
        if not nominal_w or not isinstance(nominal_w, (int, float)) or nominal_w <= 0:
            raise YamlGenerationError(
                f"nominal_ac_power_w is missing or invalid in registry header "
                f"(required for derived watt sensor from '{source_id}'). "
                f"Add nominal_ac_power_w to the registry JSON."
            )

        sensor_id = dws["id"]
        sensor_name = f"{sensor_id}_device"
        lambda_code = (
            f"if (id({source_id}).has_state()) "
            f"return id({source_id}).state * {float(nominal_w)} / 100.0; "
            f"return NAN;"
        )

        sensor = {
            "platform": "template",
            "id": sensor_id,
            "name": sensor_name,
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "entity_category": "diagnostic",
            "update_interval": "10s",
            "lambda": lambda_code,
        }
        if dws.get("icon"):
            sensor["icon"] = dws["icon"]

        config["sensor"].append(sensor)
        _LOGGER.info(
            "Derived watt sensor emitted: %s (source=%s, nominal=%dW)",
            sensor_id, source_id, nominal_w,
        )


def _apply_disabled_by_default(node: dict, reg: dict) -> None:
    """Map registry enabled_by_default / disabled_by_default → ESPHome disabled_by_default.

    Rules:
    - disabled_by_default present in reg → wins (explicit override)
    - enabled_by_default: false → emit disabled_by_default: true
    - enabled_by_default: true (or absent) → omit key (keep YAML clean)
    """
    if "disabled_by_default" in reg:
        node["disabled_by_default"] = bool(reg["disabled_by_default"])
    elif "enabled_by_default" in reg:
        if not bool(reg["enabled_by_default"]):
            node["disabled_by_default"] = True
        # enabled_by_default: true → omit


def _patch_secret_tokens(yaml_text: str) -> str:
    """Convert __SECRET_xxx__ → !secret xxx in rendered YAML."""
    return re.sub(
        r'(?m)^(\s*[\w\-]+:\s*)["\']?__SECRET_([A-Za-z0-9_\-]+)__["\']?\s*$',
        r"\1!secret \2",
        yaml_text,
    )
