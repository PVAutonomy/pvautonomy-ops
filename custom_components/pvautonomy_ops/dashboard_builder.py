"""Dashboard builder for PVAutonomy customer dashboards.

EPIC-009 UI-001: Creates a runtime Lovelace storage dashboard after
successful Wizard completion. Registry-driven, model-appropriate,
idempotent, fail-safe.

[TASK-014M 2026-05-02] Hybrid card contract restored after live-QA:
SPH dashboards now render explicit Battery / Status / Operating Mode
cards plus Activate-button rows in Load First / Battery First / Grid
First Settings. Registry remains the source of truth for which
register entries exist, but the SPH layout (card titles, row order,
synthetic helper rows) is pinned in this builder so dashboard
regenerations are stable across registry insertion-order changes and
across the Wizard / refresh-service code paths. MIC600 dashboards keep
the registry-only layout (no battery, no Operating Mode).

Contract: ops-contract-v1.md (v1.0.0)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import asyncio

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Serialise concurrent dashboard creation attempts (avoid duplicate entries)
_CREATION_LOCK = asyncio.Lock()

# --- Group display order and titles ---
# [TASK-014Y 2026-05-11] Canonical mobile source order:
#   PV → Battery → Local Load → Grid Flow → AC Output → Operating Mode →
#   Load First → Battery First → Grid First → Control → Status.
# [TASK-014AF Phase-2A 2026-05-12] The former "AC / Grid" card was renamed
# to "AC Output": grid import/export power now lives exclusively in the
# "Grid Flow" card, so keeping "Grid" in this card's title was misleading.
# `ac`, `grid`, and `load` all map to the same "AC Output" title and are
# de-duplicated by `dict.fromkeys` in `build_cards()`.
GROUP_ORDER: list[str] = [
    "pv",
    "battery",
    "local_load",
    "grid_flow",
    "energy",
    "ac",
    "grid",
    "load",
    "status",
    "operating_mode",
    "load_first",
    "battery_first",
    "grid_first",
    "control",
]

GROUP_TITLES: dict[str, str] = {
    "pv": "PV",
    "battery": "Battery",
    "local_load": "Local Load",
    "grid_flow": "Grid Flow",
    "ac": "AC Output",
    "grid": "AC Output",
    "load": "AC Output",
    "energy": "Energy",
    "operating_mode": "Operating Mode",
    "load_first": "Load First Settings",
    "battery_first": "Battery First Settings",
    "grid_first": "Grid First Settings",
    "control": "Control",
    "mode": "Control",
    "status": "Status",
}

GROUP_ICONS: dict[str, str] = {
    "PV": "mdi:solar-power",
    "Battery": "mdi:battery",
    "Local Load": "mdi:home-lightning-bolt",
    "Grid Flow": "mdi:meter-electric",
    "AC Output": "mdi:power-plug",
    "Energy": "mdi:lightning-bolt",
    "Operating Mode": "mdi:flash-triangle",
    "Load First Settings": "mdi:battery-arrow-down",
    "Battery First Settings": "mdi:battery-charging-50",
    "Grid First Settings": "mdi:battery-sync",
    "Control": "mdi:tune",
    "Status": "mdi:information-outline",
}

# [EPIC-009 SPH-Ctrl-Regression 2026-05-01] Allowed values of the additive
# registry field `dashboard_section`. Entries with this field bypass the
# `category`-based fallback and are routed directly to the named section.
# Tier and enabled_by_default / entity_category gates still apply.
_VALID_DASHBOARD_SECTIONS: frozenset[str] = frozenset({
    "pv",
    "battery",
    "local_load",
    "grid_flow",
    "ac",
    "grid",
    "load",
    "energy",
    "operating_mode",
    "load_first",
    "battery_first",
    "grid_first",
    "control",
    "status",
})

# [fix/sph-export-limit-inline-warning] SPH layout contract: keep energy
# counters and local-load rows in their functional cards instead of a
# standalone "Energy" card / AC Output. The repo registry tags these with
# ``category: "energy"`` (energy_to_user/grid, battery charge/discharge
# energy) or ``category: "load"`` (local_load_power) — which would route
# them to "Energy" / "AC Output". This id-prefix override re-routes them to
# the correct functional section; the per-card ``_SECTION_ROW_ORDER`` then
# positions them. It is applied AFTER an explicit ``dashboard_section`` (the
# registry-driven SSOT still wins) but BEFORE ``category``. PV and AC output
# energy already carry category "pv"/"ac" and are unaffected.
_SECTION_OVERRIDE_BY_ID_PREFIX: dict[str, str] = {
    "local_load": "local_load",            # local_load_power + *_energy_*
    "energy_to_user": "grid_flow",
    "energy_to_grid": "grid_flow",
    "battery_charge_energy": "battery",
    "battery_discharge_energy": "battery",
}

# --- Fallback classification by entity id prefix (for MIC600 etc.) ---
FALLBACK_PREFIX_MAP: dict[str, str] = {
    "pv": "pv",
    "battery": "battery",
    "charging": "battery",
    "discharging": "battery",
    "ac_": "ac",
    "grid": "grid",
    "load": "load",
    "local_load": "load",
    "energy": "energy",
    "inverter_status": "status",
    "inverter_temperature": "status",
    "active_power_rate": "control",
    "export_limit": "control",
    "priority_control": "control",
    "onoff": "control",
}


# [P1f MIC-Dashboard 2026-06-09] MIC600 (non-battery) Status / Control surface.
#
# The MIC600 registry tags `inverter_status` / `inverter_temperature` as
# `diagnostic` and `active_power_rate` as `config`, and `_classify_entity`
# drops every diagnostic/config row — so these three customer-facing entities
# never reach the MIC dashboard. This allow-list promotes EXACTLY these
# registry IDs into the Status and Control cards for non-battery (MIC-style)
# builds WITHOUT changing the registry or the generated-firmware
# `entity_category` (no new entities, no generator/firmware change). The
# remaining diagnostic/config rows — `dc_bus_voltage`, `modbus_version`,
# `modbus_unlock`, `save_modbus_write` — are deliberately NOT listed and stay
# excluded. Each tuple is (registry_id, ha_domain); a row is surfaced only
# when that id actually exists in the registry, so a model lacking the entity
# yields no "Entität nicht gefunden" phantom row.
_MIC_STATUS_SURFACE: tuple[tuple[str, str], ...] = (
    ("inverter_status", "sensor"),
    ("inverter_temperature", "sensor"),
)
_MIC_CONTROL_SURFACE: tuple[tuple[str, str], ...] = (
    ("active_power_rate", "number"),
)


def _classify_entity(entry: dict[str, Any], bucket: str) -> str | None:
    """Classify a registry entry into a dashboard group.

    Returns the group key or None if the entry should be excluded.

    [EPIC-009 SPH-Ctrl-Regression 2026-05-01] Honor the additive
    `dashboard_section` registry field first; fall back to category-based
    grouping when absent. This is the registry-driven mechanism that routes
    Load First / Battery First / Operating Mode entries away from the
    Battery card without changing Modbus addresses, scales, write semantics,
    entity IDs, or tier classifications.
    """
    # Exclude disabled-by-default
    if not entry.get("enabled_by_default", True):
        return None

    # Exclude diagnostic / config entity categories
    ec = entry.get("entity_category", "")
    if ec in ("diagnostic", "config"):
        return None

    # 1) Explicit dashboard_section overrides everything (registry SSOT).
    section = entry.get("dashboard_section", "")
    if section:
        if section not in _VALID_DASHBOARD_SECTIONS:
            _LOGGER.warning(
                "Unknown dashboard_section %r on registry entry %r — falling "
                "back to category-based classification",
                section,
                entry.get("id", "?"),
            )
        else:
            return section

    # 1b) [fix/sph-export-limit-inline-warning] Functional-card override for
    # energy counters / local-load rows whose registry `category` would
    # otherwise route them to a standalone "Energy" card or "AC Output".
    eid = entry.get("id", "")
    for prefix, override in _SECTION_OVERRIDE_BY_ID_PREFIX.items():
        if eid.startswith(prefix):
            return override

    # 2) Explicit category from registry
    cat = entry.get("category", "")
    if cat:
        return cat

    # 3) Bucket-based fallback: numbers/switches/selects → control
    if bucket in ("numbers", "switches", "selects"):
        return "control"

    # 4) ID-prefix fallback
    eid = entry.get("id", "")
    for prefix, group in FALLBACK_PREFIX_MAP.items():
        if eid.startswith(prefix):
            return group

    # Unknown — include in status as catch-all
    return "status"


def _entity_id(device_name: str, entry: dict[str, Any], domain: str) -> str:
    """Build the HA entity_id for a registry entry."""
    return f"{domain}.{device_name}_{entry['id']}_device"


def _display_label(entry: dict[str, Any]) -> str:
    """Derive a concise Lovelace row label from a registry entry.

    Primary: registry 'name' field.
    Fallback: deterministic derivation from 'id' (title-cased, no device prefix).
    """
    name = entry.get("name", "").strip()
    if name:
        return name
    # Fallback: id → replace underscores → title case
    eid = entry.get("id", "unknown")
    return eid.replace("_", " ").title()


def _make_entities_card(
    title: str, entities: list[tuple[str, str]]
) -> dict[str, Any]:
    """Build a Lovelace entities card.

    Each entity is a (entity_id, display_label) tuple.
    """
    icon = GROUP_ICONS.get(title, "mdi:format-list-bulleted")
    return {
        "type": "entities",
        "title": title,
        "icon": icon,
        "show_header_toggle": False,
        "entities": [{"entity": eid, "name": label} for eid, label in entities],
    }


def _make_vertical_stack(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Lovelace vertical-stack card."""
    return {
        "type": "vertical-stack",
        "cards": cards,
    }


_LOCAL_REGISTRY = Path("inverter-registry")
_SERVER_REGISTRY = Path("/config/inverter-registry")


def _control_sort_key(entity: tuple[str, str]) -> tuple[int, str]:
    """Keep export limit power rate last within the Control card."""
    entity_id, _label = entity
    if "export_limit_power_rate" in entity_id:
        return (1, entity_id)
    return (0, entity_id)


# [TASK-014M 2026-05-02] Hybrid SPH card contract.
#
# Each entry maps a section title → ordered list of register IDs.  The
# builder uses these to filter and order the registry-derived rows, then
# overlays synthetic rows / Activate buttons / time entities pinned by
# the SPH-only "post-process" layer below.
_SECTION_ROW_ORDER: dict[str, list[str]] = {
    "Battery First Settings": [
        "battery_first_charge_power_rate",
        "battery_first_charge_stop_soc",
        "battery_first_ac_charge_enabled",
    ],
    "Load First Settings": [
        "battery_stop_charge_soc",
        "load_first_discharge_stop_soc",
    ],
    "Grid First Settings": [
        "grid_first_discharge_power_rate",
        "grid_first_stop_soc",
    ],
    "Battery": [
        "battery_soc",
        "battery_charging_power",
        "battery_discharging_power",
        "battery_discharge_energy_today",
        "battery_discharge_energy_total",
        "battery_charge_energy_today",
        "battery_charge_energy_total",
        "battery_voltage",
        "battery_current",
    ],
    "Grid Flow": [
        "grid_power",          # → "Grid Import Power" (HR1021/1022 PactouserTotal)
        # [TASK-014AF Phase-2A] HR1029/1030 Pactogridtotal = Grid Export
        # Power. The live entity ID stays `ac_output_power` (legacy
        # PVAutonomy label) — the dashboard label/section is what changes.
        "ac_output_power",     # → "Grid Export Power" (HR1029/1030 Pactogridtotal)
        "energy_to_user_today",
        "energy_to_user_total",
        "energy_to_grid_today",
        "energy_to_grid_total",
    ],
    # [TASK-014AF Phase-2A] "AC Output" card (formerly "AC / Grid"): total
    # AC active power first, then AC output energy counters, then the
    # electrical detail (frequency, voltages, currents). Grid import/export
    # power deliberately stays in "Grid Flow".
    # [fix/mic-ac-output-native-current-row] The MIC600 single-phase tokens
    # (ac_power / ac_frequency / ac_voltage / ac_current) are interleaved with
    # the SPH tokens so a MIC build renders exactly
    # AC Power -> AC Frequency -> AC Voltage -> AC Current. The sort matcher
    # requires a full `_{token}_device` match, so `ac_power` never captures the
    # SPH `ac_power_total` row (and `ac_current` never captures `ac_current_l1`)
    # — the SPH relative order below is unchanged.
    "AC Output": [
        "ac_power",
        "ac_power_total",
        "ac_output_energy_today",
        "ac_output_energy_total",
        "ac_frequency",
        "ac_voltage",
        "ac_voltage_l1",
        "ac_voltage_l2",
        "ac_voltage_l3",
        "ac_current",
        "ac_current_l1",
        "ac_current_l2",
        "ac_current_l3",
    ],
}


def _section_sort_key_factory(title: str):
    """Build a sort key function for a section using `_SECTION_ROW_ORDER`."""
    order = _SECTION_ROW_ORDER.get(title, [])
    rank = {name: idx for idx, name in enumerate(order)}

    def _key(entity: tuple[str, str]) -> tuple[int, int, str]:
        entity_id, _label = entity
        for name, idx in rank.items():
            # Match the registry id token between the device prefix and the
            # `_device` suffix so we don't accidentally match similarly named
            # entities (e.g. battery_soc vs battery_soc_min).
            if f"_{name}_device" in entity_id or entity_id.endswith(f".{name}"):
                return (0, idx, entity_id)
        return (1, 0, entity_id)

    return _key


# [TASK-014M 2026-05-02] Compact dashboard labels for mode-section rows
# and for synthetic Status entries.
_COMPACT_LABELS: dict[str, str] = {
    # Load First / Battery First / Grid First — short, mode-context labels
    "battery_stop_charge_soc": "Stop Charge SoC",
    "load_first_discharge_stop_soc": "Stop Discharge SoC",
    "battery_first_charge_power_rate": "Charge Rate",
    "battery_first_charge_stop_soc": "Stop Charge SoC",
    "battery_first_ac_charge_enabled": "AC Charge",
    "grid_first_discharge_power_rate": "Discharge Rate",
    "grid_first_stop_soc": "Stop Discharge SoC",
    # Operating Mode card row
    "priority_control": "Mode",
    # Status card synthetic / pinned labels
    "inverter_temperature": "Inverter Temperature",
    "battery_temperature": "Battery Temperature",
    "wifi_signal": "Edge WiFi Signal",
    "uptime": "Edge Uptime",
}


# [TASK-014M] Status card row order for SPH (battery_storage devices).
# Each tuple is (synthetic_id, domain). Entities are rendered if the
# resolved entity_id is non-empty; rows that cannot be resolved are
# skipped silently so MIC600 / atypical devices do not crash.
_SPH_STATUS_ROWS: tuple[tuple[str, str], ...] = (
    ("inverter_temperature", "sensor"),
    ("battery_temperature", "sensor"),
    ("wifi_signal", "sensor"),
    ("uptime", "sensor"),
)


# [TASK-014M] Mode-card icons reused by Activate buttons.
_MODE_BUTTON_ICONS: dict[str, str] = {
    "Load First": "mdi:home-lightning-bolt",
    "Battery First": "mdi:battery-arrow-down",
    "Grid First": "mdi:transmission-tower-export",
}


def _priority_control_entity_id(device_name: str) -> str:
    """Return the canonical priority_control HA select entity ID."""
    return f"select.{device_name}_priority_control_device"


def _export_limit_mode_sensor_entity_id(device_name: str) -> str:
    """Return the Export Limit mode SENSOR entity ID (HR122 readback source).

    [issue #50] HR122 is emitted as a read-only diagnostic sensor (raw value
    0–3); the former writable select was retired. The customer dashboard
    renders Export Limit READ-ONLY: this sensor's state is referenced ONLY
    inside the status markdown template
    (:func:`_build_export_limit_status_card`) — never as a Lovelace entity row.
    HR122 mode changes are made at the inverter / Growatt app (product
    decision 2026-06-12, issues #50/#51)."""
    return f"sensor.{device_name}_export_limit_enable_device"


def _grid_first_schedule_enabled_entity_id(device_name: str) -> str:
    """Return the virtual Grid First schedule-draft switch entity ID.

    Created externally (HA template / EPIC-010 helper) — not in registry.
    """
    return f"switch.{device_name}_grid_first_schedule_enabled_draft"


def _grid_first_time_entity_id(device_name: str, slot: int, edge: str) -> str:
    """Return the time entity ID for a Grid First slot start/stop.

    `edge` is "start" or "stop". The pattern matches the helper
    instances installed alongside the SPH device on EDATEC.
    """
    return f"time.{device_name}_grid_first_timeslot_{slot}_{edge}_time"


def _dashboard_entity_available(
    entity_id: str,
    *,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
) -> bool:
    """Return whether a dashboard helper row should be rendered.

    Live state is preferred because it proves a platform currently
    produces the entity. Active entity-registry presence is accepted as
    a fallback for pvautonomy_ops helper rows because dashboard refresh
    can race platform setup during startup; disabled/hidden registry
    entries are filtered before ``existing_entity_ids`` is passed in.
    """
    if live_entity_ids is None and existing_entity_ids is None:
        return True
    if live_entity_ids is not None and entity_id in live_entity_ids:
        return True
    return existing_entity_ids is not None and entity_id in existing_entity_ids


# [fix/sph-dashboard-tier-live-gating] AC Output phase rows L2/L3 are optional:
# a single-phase-reporting (or older Standard-tier) firmware publishes only L1
# (+ total). Treat the L2/L3 voltage/current rows as optional so they render
# only when actually live/registered and never surface "Entität nicht gefunden"
# placeholders. L1 / total / frequency / output-energy stay unconditional.
_OPTIONAL_AC_PHASE_RE = re.compile(r"_ac_(?:voltage|current)_l[23]_device$")


def _is_optional_ac_phase_row(entity_id: str) -> bool:
    """Return True for AC Output L2/L3 voltage/current rows (optional)."""
    return bool(_OPTIONAL_AC_PHASE_RE.search(entity_id))


def _resolve_bridge_diagnostic_entity_id(
    device_name: str,
    sensor_key: str,
    *,
    mac_suffix: str | None = None,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
) -> str | None:
    """Resolve a live Edge *bridge diagnostic* sensor entity ID.

    Generic over ``sensor_key`` (e.g. ``"wifi_signal"``, ``"uptime"``) so the
    customer dashboard surfaces optional Edge bridge diagnostics uniformly and
    only when they are actually live — never as a stale or phantom row.

    Live state is the source of truth: a row is only rendered when an actual
    entity exists in ``hass.states`` at the moment the dashboard is generated.
    The entity registry is intentionally NOT consulted here — orphan registry
    entries (e.g. virtual entities left behind by an unloaded platform) would
    otherwise produce an "Entität nicht gefunden" row in the customer dashboard.

    Resolution order when ``live_entity_ids`` is supplied:
      1. If ``mac_suffix`` is given, prefer the deterministic bridge entity
         ``sensor.pvautonomy_modbus_bridge_{suffix}_{sensor_key}`` when live.
      2. Otherwise, the first live entity matching
         ``sensor.pvautonomy_modbus_bridge_*_{sensor_key}``.
      3. The synthetic ``sensor.{device_name}_{sensor_key}_device`` when live.
      4. ``None`` — caller MUST skip the row.

    When ``live_entity_ids`` is ``None`` (no runtime snapshot could be captured
    — e.g. ``hass.states`` was unreadable at build time), return ``None`` so the
    caller omits the row. Optional bridge diagnostics are NEVER synthesized from
    ``mac_suffix`` or the device name alone: a guessed entity ID would render as
    an "Entität nicht gefunden" phantom row on the customer dashboard. The legacy
    ``existing_entity_ids`` keyword is still accepted for backward compatibility
    and treated as live.
    """
    synthetic = f"sensor.{device_name}_{sensor_key}_device"
    # Backward compat: accept either argument name; live_entity_ids wins.
    live = live_entity_ids if live_entity_ids is not None else existing_entity_ids

    if live is not None:
        if mac_suffix:
            suffix = mac_suffix.replace(":", "").lower()
            preferred = (
                f"sensor.pvautonomy_modbus_bridge_{suffix}_{sensor_key}"
            )
            if preferred in live:
                return preferred
        bridge_re = re.compile(
            rf"^sensor\.pvautonomy_modbus_bridge_[0-9a-fA-F]{{6,12}}_"
            rf"{re.escape(sensor_key)}$"
        )
        for eid in live:
            if bridge_re.match(eid):
                return eid
        if synthetic in live:
            return synthetic
        return None

    # Unknown live snapshot (``live_entity_ids is None``): we cannot confirm the
    # optional bridge diagnostic actually exists, so omit the row rather than
    # emit a guess that would render as an "Entität nicht gefunden" phantom on
    # the customer dashboard. Optional bridge diagnostics (WiFi Signal / Uptime)
    # are never synthesized.
    return None


def _resolve_wifi_signal_entity_id(
    device_name: str,
    *,
    mac_suffix: str | None = None,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
) -> str | None:
    """Resolve the runtime Edge WiFi Signal sensor entity ID (live-gated).

    Thin wrapper over :func:`_resolve_bridge_diagnostic_entity_id` for the
    ``wifi_signal`` diagnostic; see it for the full resolution order/rationale.

    [TASK-014P 2026-05-03] The bridge firmware on EDATEC does not currently
    publish a ``*_wifi_signal`` sensor (only ssid/version/ip/mac), so the row
    is omitted on the live dashboard; the contract leaves room for it to appear
    automatically once such a sensor ships.
    """
    return _resolve_bridge_diagnostic_entity_id(
        device_name,
        "wifi_signal",
        mac_suffix=mac_suffix,
        live_entity_ids=live_entity_ids,
        existing_entity_ids=existing_entity_ids,
    )


def _resolve_uptime_entity_id(
    device_name: str,
    *,
    mac_suffix: str | None = None,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
) -> str | None:
    """Resolve the runtime Edge Uptime sensor entity ID (live-gated).

    Thin wrapper over :func:`_resolve_bridge_diagnostic_entity_id` for the
    ``uptime`` diagnostic. Prefers the deterministic bridge sensor
    ``sensor.pvautonomy_modbus_bridge_{suffix}_uptime`` and renders only when a
    matching (or synthetic) sensor is live, so a customer system shows Edge
    Uptime automatically once the bridge publishes it — and never otherwise.
    """
    return _resolve_bridge_diagnostic_entity_id(
        device_name,
        "uptime",
        mac_suffix=mac_suffix,
        live_entity_ids=live_entity_ids,
        existing_entity_ids=existing_entity_ids,
    )


def _make_mode_activate_button_row(
    entity_id: str, mode_label: str
) -> dict[str, Any]:
    """Activate-button row that calls ``select.select_option``."""
    return {
        "type": "buttons",
        "entities": [
            {
                "entity": entity_id,
                "name": "Activate",
                "icon": _MODE_BUTTON_ICONS.get(mode_label, "mdi:play"),
                "show_name": True,
                "show_icon": True,
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "select.select_option",
                    "target": {"entity_id": entity_id},
                    "data": {"option": mode_label},
                },
            }
        ],
    }


def _make_grid_first_activate_button_row(
    device_name: str,
    *,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Activate-button row for Grid First.

    Calls ``pvautonomy_ops.activate_grid_first_draft`` (the service that
    publishes the EDATEC virtual draft schedule). Service registration
    is out of scope for this task; the row matches the validated
    EDATEC backup so the existing helper service handles the click.
    """
    action_data = {"device_name": device_name}
    if entry_id:
        action_data["entry_id"] = entry_id

    return {
        "type": "buttons",
        "entities": [
            {
                "entity": _priority_control_entity_id(device_name),
                "name": "Activate",
                "icon": _MODE_BUTTON_ICONS["Grid First"],
                "show_name": True,
                "show_icon": True,
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "pvautonomy_ops.activate_grid_first_draft",
                    "data": action_data,
                },
            }
        ],
    }


def _pv_sort_key(entity: tuple[str, str]) -> tuple[int, int, str]:
    """Order PV rows by total first, then per-string summary before electrical detail."""
    entity_id, _label = entity
    pv_order = {
        "power": 0,
        "energy_today": 1,
        "energy_total": 2,
        "voltage": 3,
        "current": 4,
    }

    if "pv_total_power" in entity_id or "pv_power" in entity_id:
        return (0, 0, entity_id)

    for string_index in range(1, 10):
        prefix = f"pv{string_index}_"
        if prefix not in entity_id:
            continue
        for suffix, order in pv_order.items():
            if f"{prefix}{suffix}" in entity_id:
                return (string_index, order, entity_id)
        return (string_index, len(pv_order), entity_id)

    return (99, 99, entity_id)


# TASK-20260329: Mode-contextual settings for Standard + Erweitert model.
# Maps priority_control option *labels* (= HA select entity state) to the
# number IDs that are relevant for that mode (shown in Battery Mode Settings card).
# Labels are derived from the registry at runtime via _resolve_mode_soc_map();
# this constant is the fallback when the registry is unavailable.
#
# [EPIC-010 2026-03-29] Tier stabilization:
# - HR1091 / HR1090 Battery First controls are part of the Extended customer tier.
# - Grid First excluded from user-facing product path (tier=unsafe)
# - priority_control (HR1044) is the confirmed mode entity (tier=extended)
# - work_mode (HR1080) is tier=unsafe: address conflict (HR1080 = Grid First Timeslot 1
#   Start Time per Growatt SPH v1.24)
#
# RUNTIME PREREQUISITE: Conditional mode-settings cards require the HA select entity
# select.{device}_priority_control_device. This entity only exists when the ESPHome
# generator emits selects. The generator currently skips selects (see
# _GENERATOR_EMITTED_BUCKETS). In the current stabilized contract, Battery First
# Settings remain visible as normal Extended-tier dashboard rows. Contextual
# cards can be activated later when generator select support ships.
_MODE_SOC_SETTINGS_BY_LABEL: dict[str, list[str]] = {
    "Battery First": ["battery_first_charge_stop_soc", "battery_first_charge_power_rate"],
    # Grid First: removed from user-facing path (tier=unsafe). No contextual card.
    # Load First: no confirmed SoC controls.
}

# Registry buckets that the ESPHome generator currently emits as HA entities.
# "selects" is NOT included because the generator skips selects (generate_from_registry.py
# line ~335: "skip safely"). Add "selects" here only after BOTH conditions hold:
# (1) the generator truly emits select entities for the product path, and
# (2) the intended select registry entries are no longer hard-blocked or
# customer-gated (for example via generator_skip / enabled_by_default:false).
# This gates the conditional card mechanism: if "selects" is absent,
# has_priority_control evaluates to False and no conditional cards are generated.
_GENERATOR_EMITTED_BUCKETS: frozenset[str] = frozenset({"sensors", "numbers", "switches"})

# The set of all mode-contextual IDs (for removing from Battery card)
_ALL_MODE_SOC_IDS: set[str] = set()
for _ids in _MODE_SOC_SETTINGS_BY_LABEL.values():
    _ALL_MODE_SOC_IDS.update(_ids)


def _resolve_mode_soc_map(
    registers: dict[str, Any],
) -> dict[str, list[str]]:
    """Build the mode → SoC-IDs mapping using labels from the registry.

    HA select entities expose the option *label* as their state, not
    the raw register value.  We read the ``priority_control`` options map
    from the registry and translate our raw-value knowledge into label-keyed
    entries.

    Falls back to ``_MODE_SOC_SETTINGS_BY_LABEL`` if the registry entry
    cannot be resolved.
    """
    # Raw-value → number IDs (internal knowledge, must match priority_control select options)
    # [EPIC-010 2026-03-29] raw "1" = Battery First → battery_first_charge_stop_soc + power_rate
    # Grid First ("2") removed from user-facing path — no contextual card.
    _RAW_MAP: dict[str, list[str]] = {
        "1": ["battery_first_charge_stop_soc", "battery_first_charge_power_rate"],
    }

    priority_control_entry: dict[str, Any] | None = None
    for entry in registers.get("selects", []):
        if entry.get("id") == "priority_control":
            priority_control_entry = entry
            break

    if priority_control_entry is None:
        return dict(_MODE_SOC_SETTINGS_BY_LABEL)

    options: dict[str, str] = priority_control_entry.get("options", {})
    result: dict[str, list[str]] = {}
    for raw_val, soc_ids in _RAW_MAP.items():
        label = options.get(raw_val)
        if label:
            result[label] = soc_ids
    return result or dict(_MODE_SOC_SETTINGS_BY_LABEL)


def _find_registry_root() -> Path:
    """Find the inverter-registry root directory.

    Same strategy as yaml_generator.py: prefer local repo path,
    fall back to /config/ for Home Assistant OS installs.
    """
    if _LOCAL_REGISTRY.exists():
        return _LOCAL_REGISTRY
    return _SERVER_REGISTRY


def load_registry(registry_file: str) -> dict[str, Any]:
    """Load the inverter registry JSON.

    Args:
        registry_file: relative path within inverter-registry/,
            e.g. "growatt/sph/sph10k.json"
    """
    registry_root = _find_registry_root()
    path = registry_root / registry_file
    if not path.exists():
        raise FileNotFoundError(f"Registry file not found: {path}")
    with open(path) as f:
        return json.load(f)


def _mic_surface_rows(
    device_name: str,
    registers: dict[str, Any],
    surface: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """Resolve an explicit MIC allow-list to ``(entity_id, label)`` rows.

    ``surface`` is a tuple of ``(registry_id, ha_domain)`` pairs (see
    :data:`_MIC_STATUS_SURFACE` / :data:`_MIC_CONTROL_SURFACE`). A row is
    emitted only when the id exists in the matching registry bucket and is not
    ``unsafe``-tier. Unlike :func:`_classify_entity`, this intentionally
    ignores ``entity_category``: it is the curated promotion path for the
    MIC600 status/control entities the generic classifier drops as
    diagnostic/config. Ids absent from the registry yield no row, so the
    dashboard never shows an "Entität nicht gefunden" phantom.
    """
    bucket_by_domain = {
        "sensor": "sensors",
        "number": "numbers",
        "switch": "switches",
        "select": "selects",
    }
    rows: list[tuple[str, str]] = []
    for reg_id, domain in surface:
        bucket = bucket_by_domain.get(domain)
        if bucket is None:
            continue
        for entry in registers.get(bucket, []):
            if entry.get("id") != reg_id:
                continue
            if entry.get("tier") == "unsafe":
                break
            rows.append(
                (f"{domain}.{device_name}_{reg_id}_device", _display_label(entry))
            )
            break
    return rows


def build_cards(
    device_name: str,
    registry: dict[str, Any],
    *,
    existing_entity_ids: set[str] | None = None,
    live_entity_ids: set[str] | None = None,
    mac_suffix: str | None = None,
    entry_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build Lovelace cards from a registry.

    Args:
        device_name: canonical device name (e.g. 'sph10k_home_02')
        registry: parsed inverter-registry JSON
        existing_entity_ids: optional set of active entity-registry IDs.
            Used as a fallback for pvautonomy_ops helper rows when a
            startup refresh races platform state creation.
        live_entity_ids: optional set of entity IDs currently in
            ``hass.states`` (i.e. actually live, not orphan registry
            entries). Used to gate helper rows whose presence would
            otherwise produce broken "Entität nicht gefunden" or
            permanently grey rows on the customer dashboard:
              - Status card Edge WiFi Signal row
              - Control card Export Limit Mode + Export Limit toggle
              - Grid First schedule/time helper rows
        mac_suffix: optional 6+ hex MAC suffix (e.g. ``2eb1e4``). Used
            as a deterministic preferred candidate for the bridge WiFi
            Signal entity.
        entry_id: optional PVAutonomy config entry ID. Included in
            Grid First activation service data to avoid multi-entry
            ambiguity.

    Returns:
        list of Lovelace card dicts ready for a view.

    [TASK-014Y 2026-05-11] Runtime helper rows prefer live state but
    also accept active registry entries. Dashboard refresh can run
    before helper platforms publish states; disabled/hidden registry
    entries are filtered at the callsite so stale helpers still stay out.

    [TASK-014M 2026-05-02] SPH (battery_storage feature) builds receive
    the hybrid card contract restored from the EDATEC backup: explicit
    Battery / Status / Operating Mode cards plus Activate-button rows
    in Load First / Battery First / Grid First Settings. MIC600 builds
    keep the registry-only layout (no battery, no Operating Mode, no
    activate buttons).
    """
    from collections import defaultdict

    has_battery = registry.get("features", {}).get("battery_storage", False)
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    registry_entries: dict[str, dict[str, Any]] = {}

    registers = registry.get("registers", {})
    domain_map = {
        "sensors": "sensor",
        "numbers": "number",
        "switches": "switch",
        "selects": "select",
    }

    for bucket, domain in domain_map.items():
        # Suppress selects entirely while the generator does not emit them.
        # The generator skips selects (_GENERATOR_EMITTED_BUCKETS excludes
        # "selects"), so select.* entities don't exist at runtime. The
        # SPH hybrid layer below injects priority_control explicitly into
        # the Operating Mode card without flipping this bucket gate.
        if bucket == "selects" and "selects" not in _GENERATOR_EMITTED_BUCKETS:
            continue
        for entry in registers.get(bucket, []):
            # [EPIC-010 2026-03-29] Dashboard shows Standard + Erweitert only.
            if entry.get("tier") == "unsafe":
                continue
            group = _classify_entity(entry, bucket)
            if group is None:
                continue
            eid = _entity_id(device_name, entry, domain)
            label = _display_label(entry)
            groups[group].append((eid, label))
            registry_entries[entry["id"]] = entry

    # Merge groups with same display title, preserving order
    merged: dict[str, list[tuple[str, str]]] = {}
    for key in GROUP_ORDER:
        title = GROUP_TITLES.get(key, key.title())
        if key in groups:
            merged.setdefault(title, []).extend(groups.pop(key))
    if "mode" in groups:
        merged.setdefault("Control", []).extend(groups.pop("mode"))
    for key, entities in groups.items():
        title = GROUP_TITLES.get(key, key.title())
        merged.setdefault(title, []).extend(entities)

    # ── MIC surface layer (P1f 2026-06-09) ────────────────────────────
    # Non-battery (MIC600-style) builds promote the customer-facing Status
    # (Inverter Status, Inverter Temperature) and Control (Active Power Rate)
    # rows that the registry tags diagnostic/config — see _MIC_STATUS_SURFACE /
    # _MIC_CONTROL_SURFACE. SPH (has_battery) uses its own hybrid Status/Control
    # builders below and is unaffected. Raw/technical rows (modbus_unlock,
    # save_modbus_write, dc_bus_voltage, modbus_version) are not allow-listed
    # and remain excluded. The "Status"/"Control" cards then render through the
    # existing title loop (generic entities card / _build_sph_control_card).
    if not has_battery:
        for card_title, surface in (
            ("Status", _MIC_STATUS_SURFACE),
            ("Control", _MIC_CONTROL_SURFACE),
        ):
            target = merged.setdefault(card_title, [])
            present = {eid for eid, _ in target}
            for eid, label in _mic_surface_rows(device_name, registers, surface):
                if eid not in present:
                    target.append((eid, label))
                    present.add(eid)

    # ── SPH hybrid layer (TASK-014M) ──────────────────────────────────
    # Battery card: pin to (SoC, Charging Power, Discharging Power,
    # Voltage, Current). Battery Temperature moves to the Status card so
    # both temperature readings live next to each other.
    if has_battery:
        battery_pinned = _SECTION_ROW_ORDER["Battery"]
        registry_battery_rows = list(merged.get("Battery", []))
        battery_keep: list[tuple[str, str]] = []
        for reg_id in battery_pinned:
            for eid, label in registry_battery_rows:
                if f"_{reg_id}_device" in eid:
                    battery_keep.append((eid, label))
                    break
        merged["Battery"] = battery_keep

    # Inject `priority_control` explicitly into Operating Mode for SPH
    # without flipping the global selects-bucket gate. The HA select
    # entity is created by the EPIC-010 helper / template layer; the
    # dashboard renders the row regardless because the entity exists at
    # runtime on the validated EDATEC target.
    has_priority_control = (
        has_battery
        and any(
            entry.get("id") == "priority_control"
            and entry.get("tier") != "unsafe"
            for entry in registers.get("selects", [])
        )
    )
    priority_control_eid = (
        _priority_control_entity_id(device_name)
        if has_priority_control
        else None
    )

    # [fix/sph-dashboard-tier-live-gating] The ESPHome generator does not emit
    # selects (`_GENERATOR_EMITTED_BUCKETS` excludes "selects"), so the
    # priority_control select entity does not exist at runtime — neither on a
    # Standard- nor on the current Extended-tier flash. Gate the Operating Mode
    # row AND the per-mode Activate buttons on the select actually being
    # live/registered so they self-heal once select support ships, without
    # leaking a broken row meanwhile. When no runtime snapshot is supplied
    # (fresh install / unit tests) the select is treated as available and the
    # full surface renders unchanged.
    if priority_control_eid is not None and not _dashboard_entity_available(
        priority_control_eid,
        live_entity_ids=live_entity_ids,
        existing_entity_ids=existing_entity_ids,
    ):
        priority_control_eid = None

    if priority_control_eid:
        merged["Operating Mode"] = [(priority_control_eid, "Mode")]

    has_export_limit = any(
        entry.get("id") == "export_limit_power_rate"
        for entry in registers.get("numbers", [])
    )
    # Export Limit is rendered READ-ONLY from the HR122 readback sensor's
    # state; no writable HR122 surface is exposed (issue #51 retired the
    # PR #21–#24 toggle/unlock path, issue #50 turned the select into a
    # diagnostic sensor — mode changes happen at the inverter / Growatt app).
    #
    # [TASK-014W/TASK-014Y] The source sensor
    # (sensor.{device_name}_export_limit_enable_device) is emitted enabled by
    # default in Extended-tier YAML, but Home Assistant may persist a previously
    # disabled entity-registry entry from older builds; the active-registry
    # fallback covers startup refresh races before hass.states contains the
    # forwarded helper.
    export_limit_status_eid = (
        _export_limit_mode_sensor_entity_id(device_name)
        if has_export_limit
        else None
    )
    if export_limit_status_eid is not None and not _dashboard_entity_available(
        export_limit_status_eid,
        live_entity_ids=live_entity_ids,
        existing_entity_ids=existing_entity_ids,
    ):
        export_limit_status_eid = None

    # Build cards
    # [TASK-014O 2026-05-02] Battery SoC gauge retired — the Battery card's
    # SoC row already conveys the same information without consuming the
    # additional vertical real-estate the gauge required on mobile.
    cards: list[dict[str, Any]] = []

    for title in dict.fromkeys(
        [GROUP_TITLES.get(k, k.title()) for k in GROUP_ORDER]
    ):
        if title == "Status" and has_battery:
            status_card = _build_sph_status_card(
                device_name=device_name,
                registry_entries=registry_entries,
                live_entity_ids=live_entity_ids,
                mac_suffix=mac_suffix,
            )
            if status_card:
                cards.append(status_card)
            continue

        if title == "Status" and not has_battery:
            # [P1g] MIC (non-battery) Status as a customer-readable markdown
            # card: Inverter Status mapped 0/1/3 -> Standby/Normal/Fault (the raw
            # number "1.0" is never shown), Inverter Temperature with °C. Driven
            # by the surfaced rows (_MIC_STATUS_SURFACE) so absent entities yield
            # no line. AC Current precision is fixed at the registry/generator
            # metadata layer (accuracy_decimals), not here.
            status_card = _build_mic_status_card(list(merged.get("Status", [])))
            if status_card:
                cards.append(status_card)
            continue

        if title == "Control":
            entities = list(merged.get(title, []))
            if not entities:
                continue
            cards.append(
                _build_sph_control_card(
                    entities,
                    live_entity_ids=live_entity_ids,
                    existing_entity_ids=existing_entity_ids,
                )
            )
            # Read-only Export Limit Mode + Active/Inactive/Unknown status as a
            # markdown card. The HR122 select is referenced ONLY inside the
            # Jinja template — never as a Lovelace entity row — so the customer
            # dashboard exposes no writable Export Limit control and tapping
            # the card never opens a More-Info/select dialog.
            if export_limit_status_eid is not None:
                cards.append(
                    _build_export_limit_status_card(export_limit_status_eid)
                )
            continue

        if title == "Operating Mode":
            if priority_control_eid is None:
                continue
            # [TASK-014O 2026-05-02] Read-only display: tap/hold actions
            # disabled so the row shows the active mode without acting as
            # the activation affordance. Mode activation lives in the
            # per-mode Settings cards (Load First / Battery First / Grid
            # First) — keeping the dropdown out of this card prevents
            # customers from accidentally switching modes here.
            cards.append(
                _build_operating_mode_card(priority_control_eid)
            )
            continue

        if title in ("Load First Settings", "Battery First Settings", "Grid First Settings"):
            if not has_battery:
                continue
            card = _build_sph_mode_settings_card(
                title,
                rows=list(merged.get(title, [])),
                device_name=device_name,
                priority_control_eid=priority_control_eid,
                live_entity_ids=live_entity_ids,
                existing_entity_ids=existing_entity_ids,
                entry_id=entry_id,
            )
            if card:
                cards.append(card)
            continue

        if title not in merged or not merged[title]:
            continue
        if title == "Battery" and not has_battery:
            continue

        entities = merged[title]
        if title == "PV":
            entities = sorted(entities, key=_pv_sort_key)
        elif title in _SECTION_ROW_ORDER:
            entities = sorted(entities, key=_section_sort_key_factory(title))

        # [fix/sph-dashboard-tier-live-gating] Keep AC Output in both tiers with
        # L1 / total / frequency / output-energy intact, but render the optional
        # L2/L3 voltage/current rows only when the entity is live/registered.
        # Missing L2/L3 must not produce "Entität nicht gefunden" rows.
        if title == "AC Output":
            entities = [
                (eid, label)
                for eid, label in entities
                if not _is_optional_ac_phase_row(eid)
                or _dashboard_entity_available(
                    eid,
                    live_entity_ids=live_entity_ids,
                    existing_entity_ids=existing_entity_ids,
                )
            ]
            # [fix/mic-ac-output-native-current-row] The former P1i special
            # case (MIC AC Current pulled into a one-decimal markdown card)
            # is retired: AC Current renders as a native entity row like the
            # other AC rows. Sub-amp display precision comes from the
            # registry/generator `accuracy_decimals: 1` on the MIC `ac_current`
            # sensor (firmware-surface contract), not from a dashboard
            # workaround. Row order is owned by _SECTION_ROW_ORDER above.

        if not entities:
            continue

        cards.append(_make_entities_card(title, entities))

    # [TASK-014X/TASK-014Y] Keep SPH as a native Lovelace/Masonry card
    # list. A top-level horizontal-stack made desktop narrow and mobile
    # unusable. The flat source order is the exact mobile order; desktop
    # remains native three-column Masonry where Home Assistant chooses
    # the column placement based on card height.
    if has_battery:
        cards = _order_sph_cards_for_masonry(cards)

    return cards


_SPH_MOBILE_CARD_ORDER: tuple[str, ...] = (
    "PV",
    "Battery",
    "Local Load",
    "Grid Flow",
    "AC Output",
    "Operating Mode",
    "Load First Settings",
    "Battery First Settings",
    "Grid First Settings",
    "Control",
    "Export Limit",
    "Status",
)


def _order_sph_cards_for_masonry(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order SPH cards for native Lovelace/Masonry responsiveness.

    Lovelace Masonry controls the actual desktop columns and mobile
    collapse. Keeping cards flat avoids narrow nested columns; this
    source order is the customer-facing mobile order.
    """
    by_title: dict[str, dict[str, Any]] = {}
    overflow: list[dict[str, Any]] = []
    for card in cards:
        title = card.get("title")
        if title in _SPH_MOBILE_CARD_ORDER and title not in by_title:
            by_title[title] = card
        else:
            overflow.append(card)

    ordered = [by_title[t] for t in _SPH_MOBILE_CARD_ORDER if t in by_title]
    return [*ordered, *overflow]


_SPH_DESKTOP_CARD_COLUMNS: tuple[tuple[str, ...], ...] = (
    ("PV", "Local Load", "Grid Flow"),
    ("Battery", "AC Output", "Status"),
    (
        "Operating Mode",
        "Load First Settings",
        "Battery First Settings",
        "Grid First Settings",
        "Control",
        "Export Limit",
    ),
)

_SPH_DESKTOP_MEDIA_QUERY = "(min-width: 1200px)"
_SPH_MOBILE_MEDIA_QUERY = "(max-width: 1199px)"


def _sph_cards_by_title(
    cards: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Split SPH cards into known titles plus overflow."""
    known_titles = set(_SPH_MOBILE_CARD_ORDER)
    by_title: dict[str, dict[str, Any]] = {}
    overflow: list[dict[str, Any]] = []
    for card in cards:
        title = card.get("title")
        if title in known_titles and title not in by_title:
            by_title[title] = card
        else:
            overflow.append(card)
    return by_title, overflow


def _build_sph_responsive_layout_card(
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the responsive SPH dashboard root card.

    Mobile keeps the canonical source order. Desktop uses a deterministic
    three-column layout: production/storage, grid/status, and operations.
    The view uses panel mode so the desktop horizontal stack can consume
    the full dashboard width instead of being constrained as one Masonry
    card.
    """
    by_title, overflow = _sph_cards_by_title(cards)

    desktop_columns: list[dict[str, Any]] = []
    used_titles: set[str] = set()
    for column_titles in _SPH_DESKTOP_CARD_COLUMNS:
        column_cards = [
            by_title[title]
            for title in column_titles
            if title in by_title
        ]
        used_titles.update(column_titles)
        if column_cards:
            desktop_columns.append({"type": "vertical-stack", "cards": column_cards})

    desktop_overflow = [
        card
        for title, card in by_title.items()
        if title not in used_titles
    ] + overflow
    if desktop_overflow:
        if desktop_columns:
            desktop_columns[-1]["cards"].extend(desktop_overflow)
        else:
            desktop_columns.append(
                {"type": "vertical-stack", "cards": desktop_overflow}
            )

    mobile_cards = [
        by_title[title]
        for title in _SPH_MOBILE_CARD_ORDER
        if title in by_title
    ] + overflow

    return {
        "type": "vertical-stack",
        "cards": [
            {
                "type": "conditional",
                "conditions": [
                    {
                        "condition": "screen",
                        "media_query": _SPH_DESKTOP_MEDIA_QUERY,
                    }
                ],
                "card": {
                    "type": "horizontal-stack",
                    "cards": desktop_columns,
                },
            },
            {
                "type": "conditional",
                "conditions": [
                    {
                        "condition": "screen",
                        "media_query": _SPH_MOBILE_MEDIA_QUERY,
                    }
                ],
                "card": {
                    "type": "vertical-stack",
                    "cards": mobile_cards,
                },
            },
        ],
    }


# ── SPH hybrid card builders (TASK-014M) ─────────────────────────────


def _build_sph_status_card(
    *,
    device_name: str,
    registry_entries: dict[str, dict[str, Any]],
    live_entity_ids: set[str] | None,
    mac_suffix: str | None,
) -> dict[str, Any] | None:
    """Build the SPH Status card with synthetic + registry-derived rows.

    Order: Inverter Temperature, Battery Temperature, Edge WiFi Signal,
    Edge Uptime. The Edge WiFi Signal and Edge Uptime rows are optional
    bridge diagnostics: each is rendered only when its resolver finds a
    live entity (otherwise the row is skipped — never shown as missing).
    """
    rows: list[tuple[str, str]] = []
    for reg_id, domain in _SPH_STATUS_ROWS:
        if reg_id == "wifi_signal":
            eid = _resolve_wifi_signal_entity_id(
                device_name,
                mac_suffix=mac_suffix,
                live_entity_ids=live_entity_ids,
            )
            if eid is None:
                # [TASK-014P 2026-05-03] Skip the row entirely when no
                # live WiFi Signal sensor is present in hass.states.
                # The bridge firmware on EDATEC does not currently
                # publish one (only ssid/version/ip/mac), so the row
                # would otherwise appear as "Entität nicht gefunden".
                continue
        elif reg_id == "uptime":
            # [P1b] Edge Uptime is an optional bridge diagnostic: render it
            # only when a live uptime sensor exists, so it never surfaces an
            # "Entität nicht gefunden" row and appears automatically once the
            # bridge publishes it on a customer system.
            eid = _resolve_uptime_entity_id(
                device_name,
                mac_suffix=mac_suffix,
                live_entity_ids=live_entity_ids,
            )
            if eid is None:
                continue
        else:
            eid = f"{domain}.{device_name}_{reg_id}_device"
        label = _COMPACT_LABELS.get(reg_id) or _display_label(
            registry_entries.get(reg_id, {"id": reg_id})
        )
        rows.append((eid, label))

    if not rows:
        return None
    return _make_entities_card("Status", rows)


# [P1g 2026-06-09] MIC600 inverter_status raw value -> customer-readable label.
# Source of truth: Growatt MIC600 Modbus protocol v3.14 §4.2, Input Reg 00
# (Inverter run state): 0 = waiting, 1 = normal, 3 = fault.
_MIC_STATUS_LABELS: dict[int, str] = {0: "Standby", 1: "Normal", 3: "Fault"}


def _mic_inverter_status_label(state: Any) -> str:
    """Map the MIC600 ``inverter_status`` raw value to a customer label.

    The firmware sensor publishes the bare number (e.g. ``1.0``); the customer
    dashboard shows ``Normal`` instead. ``0 -> Standby``, ``1 -> Normal``,
    ``3 -> Fault`` (per :data:`_MIC_STATUS_LABELS`). Anything else degrades to a
    safe ``Unknown`` (``unknown``/``unavailable``/missing) or ``Unknown (<raw>)``
    for an unexpected number. Pure + never raises; the Status-card markdown
    renders the equivalent mapping via Jinja so the displayed text matches.
    """
    if state is None:
        return "Unknown"
    raw = str(state).strip()
    if raw.lower() in ("", "unknown", "unavailable", "none"):
        return "Unknown"
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return f"Unknown ({raw})"
    label = _MIC_STATUS_LABELS.get(num)  # 0.0 == 0, 1.0 == 1, 3.0 == 3
    return label if label is not None else f"Unknown ({raw})"


def _build_mic_status_card(
    rows: list[tuple[str, str]],
) -> dict[str, Any] | None:
    """Build the MIC (non-battery) Status card as customer-readable markdown.

    [P1g] One bold-labelled line per surfaced row:
      * ``inverter_status`` -> Standby/Normal/Fault via a Jinja mapping that
        mirrors :func:`_mic_inverter_status_label` (raw numbers like ``1.0`` are
        never shown; unexpected/unknown -> ``Unknown``/``Unknown (<raw>)``).
      * ``inverter_temperature`` -> ``<value> °C`` rounded to one decimal
        ([P1h]: never a raw long float; ``Unknown`` when missing).
      * any other surfaced row -> its raw state, gracefully.
    Markdown never produces a broken "Entität nicht gefunden" row, and only the
    surfaced entities are referenced (absent ids produce no line). Returns
    ``None`` when there are no rows.
    """
    if not rows:
        return None
    lines: list[str] = []
    for eid, label in rows:
        if eid.endswith("_inverter_status_device"):
            lines.append(
                f"**{label}:** "
                f"{{% set raw = states('{eid}') %}}"
                f"{{% set n = raw | float(none) %}}"
                f"{{% if n == 0 %}}Standby"
                f"{{% elif n == 1 %}}Normal"
                f"{{% elif n == 3 %}}Fault"
                f"{{% elif raw in ['unknown', 'unavailable', '', none] %}}Unknown"
                f"{{% else %}}Unknown ({{{{ raw }}}}){{% endif %}}"
            )
        elif eid.endswith("_inverter_temperature_device"):
            # [P1h] Format to one decimal so the raw float32 register value
            # (e.g. 33.4000015258789) renders as a clean "33.4 °C" — never a
            # long float. `float(none)` collapses unknown/unavailable/missing
            # (any non-numeric state) to "Unknown".
            lines.append(
                f"**{label}:** "
                f"{{% set t = states('{eid}') | float(none) %}}"
                f"{{% if t is none %}}Unknown"
                f"{{% else %}}{{{{ t | round(1) }}}} °C{{% endif %}}"
            )
        else:
            lines.append(
                f"**{label}:** "
                f"{{% set v = states('{eid}') %}}"
                f"{{% if v in ['unknown', 'unavailable', '', none] %}}Unknown"
                f"{{% else %}}{{{{ v }}}}{{% endif %}}"
            )
    return {
        "type": "markdown",
        "title": "Status",
        "content": "\n\n".join(lines),
    }


# [issue #50/#51] Export Limit is READ-ONLY and rendered entirely as a markdown
# card — NOT as any Lovelace entity row. Both lines are sourced from the HR122
# readback sensor's numeric state via Jinja ``states()`` (raw values:
# 0=Disabled / 1=RS485 Limit / 2=RS232 Limit / 3=CT Meter, per the registry
# options map):
#   * Export Limit Mode   = markdown text line (0 → Off, 1 → RS485, 2 → RS232,
#     3 → CT Meter — same customer wording as before).
#   * Export Limit status = markdown text line (Active/Inactive/Unknown).
# The sensor is referenced ONLY inside the Jinja template, so the customer
# dashboard exposes no writable control. Mode changes happen at the inverter /
# Growatt app (product decision 2026-06-12); there is deliberately NO "change
# via the Growatt app" / restart hint.
#: Raw HR122 value meaning the export limit is OFF.
_EXPORT_LIMIT_INACTIVE_VALUE = 0
#: Raw HR122 value → customer-facing mode label (limiting modes).
_EXPORT_LIMIT_MODE_LABELS = {1: "RS485", 2: "RS232", 3: "CT Meter"}


def _export_limit_active_label(state: str | float | int | None) -> str:
    """Map a raw HR122 sensor state to a read-only status label.

    Accepts the HA state string ("1.0"), a bare number, or None:

    - ``0`` → ``"Inactive"``
    - ``1`` / ``2`` / ``3`` (RS485 / RS232 / CT Meter) → ``"Active"``
    - anything else (unknown / unavailable / missing / unmapped) → ``"Unknown"``

    Pure + unit-tested; the status markdown renders the equivalent mapping via
    Jinja so the displayed status matches.
    """
    try:
        value = int(float(state))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Unknown"
    if value == _EXPORT_LIMIT_INACTIVE_VALUE:
        return "Inactive"
    if value in _EXPORT_LIMIT_MODE_LABELS:
        return "Active"
    return "Unknown"


def _build_export_limit_status_card(status_eid: str) -> dict[str, Any]:
    """Read-only Export Limit card (markdown): Mode line + Active/Inactive/Unknown.

    Both lines are derived from the HR122 readback sensor's numeric state via a
    Jinja ``states()`` read (the Active/Inactive/Unknown line uses the same
    mapping as :func:`_export_limit_active_label`; the Mode line keeps the
    pre-#50 customer wording — ``0`` → ``Off``, ``1`` → ``RS485``, ``2`` →
    ``RS232``, ``3`` → ``CT Meter``). The sensor is referenced ONLY inside this
    template — never as a Lovelace entity row — so the customer dashboard
    exposes no writable Export Limit control. No HR122 write is exposed and no
    new HA entity is created. Title ``Export Limit`` so the layout places it
    deterministically next to the Control card.
    """
    active_values = sorted(_EXPORT_LIMIT_MODE_LABELS)
    mode_branches = "".join(
        f"{{% elif v == {raw} %}}{label}"
        for raw, label in sorted(_EXPORT_LIMIT_MODE_LABELS.items())
    )
    content = (
        f"{{% set v = states('{status_eid}') | float(-1) | int %}}"
        f"**Export Limit Mode:** "
        f"{{% if v == {_EXPORT_LIMIT_INACTIVE_VALUE} %}}Off"
        f"{mode_branches}"
        f"{{% else %}}Unknown{{% endif %}}\n\n"
        f"**Export Limit:** "
        f"{{% if v == {_EXPORT_LIMIT_INACTIVE_VALUE} %}}Inactive"
        f"{{% elif v in {active_values} %}}Active"
        f"{{% else %}}Unknown{{% endif %}}"
    )
    return {"type": "markdown", "title": "Export Limit", "content": content}


def _build_sph_control_card(
    rows: list[tuple[str, str]],
    *,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build the SPH Control card.

    Order: Active Power Rate → Export Limit Power Rate → any remaining registry
    rows. The Export Limit Enable (HR122) write/toggle is NOT exposed, and the
    read-only Export Limit Mode/status is rendered as a separate markdown card
    (:func:`_build_export_limit_status_card`) — NOT as an entity/attribute row
    here — so the customer dashboard carries no Export Limit switch surface.

    [TASK-014Y 2026-05-11] Live state is preferred, but active registry
    presence is accepted to survive startup refresh races. Disabled or hidden
    entries are filtered out before ``existing_entity_ids`` is supplied.
    """
    active_pr: tuple[str, str] | None = None
    export_pr: tuple[str, str] | None = None
    rest: list[tuple[str, str]] = []
    for eid, label in rows:
        # [P1b] The Export Limit Power (W) live readback
        # (`*_export_limit_power_w_device`) is intentionally kept OUT of the
        # customer dashboard for now. It is grouped to "control" via the
        # `export_limit` id-prefix fallback and would otherwise render once it
        # goes live — but it is distinct from the Export Limit Power *Rate*
        # number, which IS shown. Drop only the power-W sensor row.
        if "_export_limit_power_w_device" in eid:
            continue
        # [fix/sph-dashboard-tier-live-gating] Skip registry control rows whose
        # entity is not live/registered. On a Standard-tier flash the Extended
        # `export_limit_power_rate` number is never published, so its row would
        # otherwise render as "Entität nicht gefunden". `active_power_rate`
        # (Standard) stays because it is live on the device. No snapshot
        # (fresh install / unit tests) → every row treated as available.
        if not _dashboard_entity_available(
            eid,
            live_entity_ids=live_entity_ids,
            existing_entity_ids=existing_entity_ids,
        ):
            continue
        if "_active_power_rate_device" in eid:
            active_pr = (eid, label)
        elif "_export_limit_power_rate_device" in eid:
            export_pr = (eid, label)
        else:
            rest.append((eid, label))

    entities: list[Any] = []
    if active_pr:
        entities.append({"entity": active_pr[0], "name": active_pr[1]})
    if export_pr:
        entities.append({"entity": export_pr[0], "name": export_pr[1]})
    # Export Limit Mode/status is rendered read-only in a markdown card
    # (_build_export_limit_status_card); the switch is deliberately NOT added as
    # an entity/attribute row here, so the customer cannot open its More-Info /
    # toggle dialog from the Control card.
    for eid, label in sorted(rest):
        entities.append({"entity": eid, "name": label})

    return {
        "type": "entities",
        "title": "Control",
        "icon": GROUP_ICONS["Control"],
        "show_header_toggle": False,
        "entities": entities,
    }


def _build_operating_mode_card(priority_control_eid: str) -> dict[str, Any]:
    """Build a read-only Operating Mode entities card.

    [TASK-014P 2026-05-03] The row uses ``type: simple-entity`` with
    ``tap_action`` / ``hold_action`` set to ``none``. ``simple-entity``
    forces a non-interactive presentation (no select dropdown), which
    the previous default entity row did not — Lovelace happily rendered
    the underlying ``select.priority_control`` as a dropdown despite
    the action overrides. Mode activation lives in the per-mode
    Settings cards (Load First / Battery First / Grid First); leaving
    the dropdown active here invited mistaken in-place mode switches.
    """
    return {
        "type": "entities",
        "title": "Operating Mode",
        "icon": GROUP_ICONS["Operating Mode"],
        "show_header_toggle": False,
        "entities": [
            {
                "type": "simple-entity",
                "entity": priority_control_eid,
                "name": "Mode",
                "icon": GROUP_ICONS["Operating Mode"],
                "tap_action": {"action": "none"},
                "hold_action": {"action": "none"},
            }
        ],
    }


def _build_sph_mode_settings_card(
    title: str,
    *,
    rows: list[tuple[str, str]],
    device_name: str,
    priority_control_eid: str | None,
    live_entity_ids: set[str] | None = None,
    existing_entity_ids: set[str] | None = None,
    entry_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a mode-section card with Activate button + compact rows.

    All three mode-section cards open with the Activate button. For
    Load First / Battery First the button calls ``select.select_option``;
    for Grid First it calls ``pvautonomy_ops.activate_grid_first_draft``
    which atomically writes rate, stop SoC, slot 1 start/stop, schedule
    enable, then ``priority_control = "Grid First"``.

    [EPIC-012 / TASK-014Q] Grid First Settings restores Schedule
    Enabled / Slot 1 Start / Slot 1 Stop rows backed by the helper
    platforms (``switch`` + ``time``) that ``__init__.py`` forwards.
    Live state is preferred; active registry presence is accepted for
    startup refresh races. Raw unsafe timeslot ENABLE switches stay
    hidden.
    """
    mode_label_by_title = {
        "Load First Settings": "Load First",
        "Battery First Settings": "Battery First",
        "Grid First Settings": "Grid First",
    }
    mode_label = mode_label_by_title[title]

    body_rows: list[Any] = []
    pinned = _SECTION_ROW_ORDER.get(title, [])
    rank = {name: idx for idx, name in enumerate(pinned)}

    def _row_sort_key(item: tuple[str, str]) -> tuple[int, int, str]:
        eid, _ = item
        for name, idx in rank.items():
            if f"_{name}_device" in eid:
                return (0, idx, eid)
        return (1, 0, eid)

    sorted_rows = sorted(rows, key=_row_sort_key)
    # [fix/sph-dashboard-tier-live-gating] These mode-settings cards expose
    # Extended-tier number/switch controls. On a Standard-tier flash those
    # entities are never published, so gate each registry control row on
    # live/registry presence and drop the WHOLE card when none of its control
    # entities are available. Draft-schedule helpers (Grid First) alone do NOT
    # justify the card — without the underlying mode controls there is nothing
    # actionable. No snapshot (fresh install / unit tests) → every row is
    # treated as available and the full surface renders unchanged.
    available_rows = [
        (eid, label)
        for eid, label in sorted_rows
        if _dashboard_entity_available(
            eid,
            live_entity_ids=live_entity_ids,
            existing_entity_ids=existing_entity_ids,
        )
    ]
    if not available_rows:
        return None
    for eid, _ in available_rows:
        # Compact label by register ID; fall back to existing label.
        reg_id = ""
        m = re.match(
            rf"^[^.]+\.{re.escape(device_name)}_(.+)_device$", eid
        )
        if m:
            reg_id = m.group(1)
        compact = _COMPACT_LABELS.get(reg_id)
        body_rows.append({"entity": eid, "name": compact or _label_from_eid(eid)})

    if title == "Grid First Settings":
        # [EPIC-012 / TASK-014Q] Insert Schedule Enabled, Slot 1 Start,
        # Slot 1 Stop as the FIRST registry-driven rows (above Discharge
        # Rate / Stop Discharge SoC). The rows reference the helper
        # entities produced by ``switch.py`` (schedule draft) and
        # ``time.py`` (slot 1 start/stop). Helpers render when live or
        # active in the entity registry; disabled/hidden registry rows
        # are filtered out before ``existing_entity_ids`` is supplied.
        schedule_eid = _grid_first_schedule_enabled_entity_id(device_name)
        start_eid = _grid_first_time_entity_id(device_name, slot=1, edge="start")
        stop_eid = _grid_first_time_entity_id(device_name, slot=1, edge="stop")
        helper_rows: list[tuple[str, str]] = [
            (schedule_eid, "Schedule Enabled"),
            (start_eid, "Slot 1 Start"),
            (stop_eid, "Slot 1 Stop"),
        ]
        helper_payloads: list[dict[str, str]] = []
        for eid, label in helper_rows:
            if _dashboard_entity_available(
                eid,
                live_entity_ids=live_entity_ids,
                existing_entity_ids=existing_entity_ids,
            ):
                helper_payloads.append({"entity": eid, "name": label})
        # Registry rows (Discharge Rate / Stop Discharge SoC) stay
        # directly below Activate; schedule and timeslot helpers follow.
        body_rows = body_rows + helper_payloads

    # [fix/sph-dashboard-tier-live-gating] All three Activate buttons display
    # the priority_control select as their target entity, so only render them
    # when that select is live/registered (priority_control_eid is gated to
    # None upstream when it is not). This keeps a Standard-tier card from
    # showing an Activate row that points at a non-existent select. No snapshot
    # (fresh install / unit tests) → priority_control_eid stays set and the
    # Activate buttons render as before.
    activate_row: dict[str, Any] | None = None
    if priority_control_eid:
        if title == "Grid First Settings":
            activate_row = _make_grid_first_activate_button_row(
                device_name,
                entry_id=entry_id,
            )
        else:
            activate_row = _make_mode_activate_button_row(
                priority_control_eid, mode_label
            )

    if activate_row:
        body_rows.insert(0, activate_row)

    if not body_rows:
        return None

    return {
        "type": "entities",
        "title": title,
        "icon": GROUP_ICONS.get(title, "mdi:cog-outline"),
        "show_header_toggle": False,
        "entities": body_rows,
    }


def _label_from_eid(entity_id: str) -> str:
    """Last-resort label derivation when no registry entry is present."""
    obj = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    return obj.replace("_device", "").replace("_", " ").title()


def build_dashboard_config(
    device_name: str,
    display_title: str,
    registry: dict[str, Any],
    *,
    existing_entity_ids: set[str] | None = None,
    live_entity_ids: set[str] | None = None,
    mac_suffix: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Build the full Lovelace storage config payload.

    Returns a dict suitable for writing to .storage/lovelace.{url_path}.

    [TASK-014P 2026-05-03] ``live_entity_ids`` reflects ``hass.states``
    at generation time and is used to gate helper rows (Edge WiFi
    Signal, Export Limit Mode/toggle) whose registry entries can become
    orphans across deployments.
    """
    cards = build_cards(
        device_name,
        registry,
        existing_entity_ids=existing_entity_ids,
        live_entity_ids=live_entity_ids,
        mac_suffix=mac_suffix,
        entry_id=entry_id,
    )
    has_battery = registry.get("features", {}).get("battery_storage", False)
    view_cards = (
        [_build_sph_responsive_layout_card(cards)]
        if has_battery
        else cards
    )
    view: dict[str, Any] = {
        "title": display_title,
        "cards": view_cards,
    }
    if has_battery:
        view["panel"] = True

    return {
        "version": 1,
        "minor_version": 1,
        "key": "",  # filled by caller
        "data": {
            "config": {
                "views": [view]
            }
        },
    }


def compute_dashboard_url(device_name: str) -> str:
    """Compute a deterministic dashboard URL path.

    Must contain a hyphen (HA requirement).
    """
    slug = device_name.replace("_", "-")
    return f"pva-{slug}"


async def async_create_dashboard(
    hass: HomeAssistant,
    device_name: str,
    display_title: str,
    registry_file: str,
    modbus_version: int | None = None,
    *,
    pv_strings: int | None = None,
    model_slug: str | None = None,
    selected_tier: str | None = None,
    entry_id: str | None = None,
) -> bool:
    """Create or refresh a customer dashboard for a PVAutonomy device.

    TASK-20260328: On repeated setup/reflash the dashboard registry entry
    is kept idempotent (no duplicates), but the stored Lovelace view/card
    config is always rewritten so contract changes (removed entities, new
    hint cards, safe-by-default adjustments) take effect.

    Fail-safe: logs errors but never raises.

    EPIC-012 dashboard API safety contract: ``modbus_version``, ``pv_strings``,
    ``model_slug``, and ``selected_tier`` are accepted to keep observed
    callsites compatible. They are reserved for future dashboard policy and do
    not alter dashboard generation in this minimal safety port.

    Returns True if dashboard was created or refreshed, False if failed.
    """
    try:
        async with _CREATION_LOCK:
            return await _create_dashboard_impl(
                hass,
                device_name,
                display_title,
                registry_file,
                entry_id=entry_id,
            )
    except Exception:
        _LOGGER.warning(
            "Dashboard creation failed for %s (non-fatal, device setup continues)",
            device_name,
            exc_info=True,
        )
        return False


async def _create_dashboard_impl(
    hass: HomeAssistant,
    device_name: str,
    display_title: str,
    registry_file: str,
    *,
    entry_id: str | None = None,
) -> bool:
    """Internal: create or refresh dashboard.

    HA's LovelaceData stores dashboards in two layers:
    1. .storage/lovelace_dashboards — registry of all storage-mode dashboards
    2. .storage/lovelace.{url_path} — view/card config per dashboard

    TASK-20260328: The registry entry (layer 1) is idempotent — we never
    create a duplicate.  But the view/card config (layer 2) is always
    rewritten so contract changes propagate on repeated setup/reflash.
    """
    from homeassistant.helpers.storage import Store

    url_path = compute_dashboard_url(device_name)
    sidebar_title = f"PVAutonomy - {display_title}"

    # --- Idempotent registry entry (layer 1) ---
    dashboards_store = Store(hass, version=1, key="lovelace_dashboards", minor_version=1)
    dashboards_data = await dashboards_store.async_load()

    already_registered = False
    if dashboards_data is not None:
        existing_items = dashboards_data.get("items", [])
        existing_urls = {item.get("url_path") for item in existing_items}
        already_registered = url_path in existing_urls
    else:
        existing_items = []

    # --- Load registry and build cards (via executor to avoid blocking I/O) ---
    registry = await hass.async_add_executor_job(load_registry, registry_file)

    # [TASK-014M] Resolve runtime helpers for the SPH hybrid layer:
    # - existing_entity_ids: best-effort active entity-registry snapshot
    #   (disabled/hidden rows excluded). Used as a fallback when a
    #   startup dashboard refresh races platform state creation.
    # - live_entity_ids: best-effort snapshot of ``hass.states`` —
    #   actual live entities, not orphan registry entries. Used by the
    #   Status / Control card builders to gate helper rows whose
    #   registry entries can survive across deployments even after the
    #   producing platform stops loading.
    # - mac_suffix: deterministic preferred candidate for the bridge
    #   WiFi Signal entity when present in live state.
    existing_entity_ids: set[str] | None = None
    live_entity_ids: set[str] | None = None
    mac_suffix: str | None = None
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        existing_entity_ids = {
            entry.entity_id
            for entry in ent_reg.entities.values()
            if getattr(entry, "disabled_by", None) is None
            and getattr(entry, "hidden_by", None) is None
        }
    except Exception:  # noqa: BLE001 — registry is best-effort here
        existing_entity_ids = None

    try:
        # ``hass.states.async_entity_ids()`` returns the entity IDs that
        # are currently live (i.e. have a State object). Anything that
        # only exists in the entity registry but is not currently
        # produced by a loaded platform will NOT appear here — exactly
        # the orphan/grey-row case TASK-014P targets.
        live_entity_ids = set(hass.states.async_entity_ids())
    except Exception:  # noqa: BLE001 — live state is best-effort here
        live_entity_ids = None

    try:
        from .metadata import async_get_metadata_store
        store = await async_get_metadata_store(hass)
        meta = await store.lookup(device_name)
        if meta is not None:
            mac_suffix = (meta.mac_suffix or None)
    except Exception:  # noqa: BLE001 — metadata is best-effort here
        mac_suffix = None

    config = build_dashboard_config(
        device_name,
        display_title,
        registry,
        existing_entity_ids=existing_entity_ids,
        live_entity_ids=live_entity_ids,
        mac_suffix=mac_suffix,
        entry_id=entry_id,
    )

    # --- Step 1: Add registry entry only if not yet present ---
    if not already_registered:
        new_entry = {
            "id": url_path,
            "url_path": url_path,
            "title": sidebar_title,
            "icon": "mdi:solar-power",
            "mode": "storage",
            "show_in_sidebar": True,
            "require_admin": False,
        }
        updated_items = existing_items + [new_entry]
        await dashboards_store.async_save({"items": updated_items})
        _LOGGER.info("Dashboard entry created: %s (%s)", url_path, sidebar_title)
    else:
        _LOGGER.info(
            "Dashboard entry '%s' already registered — refreshing config only",
            url_path,
        )

    # --- Step 2: Write view/card config ---
    config_store = Store(hass, version=1, key=f"lovelace.{url_path}", minor_version=1)
    await config_store.async_save(config["data"])
    num_cards = len(config["data"]["config"]["views"][0]["cards"])
    _LOGGER.info(
        "Dashboard config written for %s (%d cards)", url_path, num_cards
    )

    # --- Step 3: Register panel so sidebar updates immediately ---
    try:
        from homeassistant.components.frontend import async_register_built_in_panel

        async_register_built_in_panel(
            hass,
            component_name="lovelace",
            frontend_url_path=url_path,
            sidebar_title=sidebar_title,
            sidebar_icon="mdi:solar-power",
            config={"mode": "storage"},
            require_admin=False,
        )
        _LOGGER.info("Panel registered for immediate sidebar visibility: %s", url_path)
    except Exception:
        _LOGGER.debug(
            "Panel registration skipped for %s (dashboard will appear after restart)",
            url_path,
            exc_info=True,
        )

    return True
