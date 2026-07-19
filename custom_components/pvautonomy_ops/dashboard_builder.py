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

import copy
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import asyncio

from homeassistant.core import HomeAssistant

from .const import DOMAIN, ENTITY_STATUS_SENSOR
from .defs_paths import (
    BUNDLED_REGISTRY_ROOT,
    DefsNotFoundError,
    resolve_registry_root,
)

_LOGGER = logging.getLogger(__name__)

# Serialise concurrent dashboard creation attempts (avoid duplicate entries)
_CREATION_LOCK = asyncio.Lock()

# --- Managed-dashboard foundations (Ops Contract §9.7; M2/#168 WP1) ---
# Namespaced key stored as ordinary JSON-compatible data inside the
# Lovelace config dict (sibling of "views"). The managed-dashboard
# lifecycle recovers it through the Home Assistant storage read-back path
# (``Store.async_load``), which returns the saved configuration dict
# verbatim. WP1 relies only on that storage round-trip and makes no claim
# about how the frontend interprets the marker.
MANAGED_SCHEMA_KEY = "pvautonomy_managed"
# Schema version of integration-managed dashboard content. Independent of
# the integration package version: bump ONLY when the managed dashboard
# structure changes in a way that requires full regeneration (§9.7).
# History: 1 = WP2A notice-only shell; 2 = WP2B1 Maintenance surface;
# 3 = WP2B2 Help / Setup Guidance surface; 4 = WP4 legacy-aware Help
# (one-time regeneration so every managed dashboard carries the
# legacy-state fingerprint and, when applicable, the migration notice).
MANAGED_SCHEMA_VERSION = 4

# Marker key holding the deterministic maintenance-target-roster
# fingerprint (WP2B1). A schema bump regenerates structure; the
# fingerprint regenerates content when the eligible config-entry roster
# (targets, labels, status entities) changes without a schema change.
TARGET_FINGERPRINT_KEY = "target_fingerprint"
# Marker key holding the deterministic legacy-detection fingerprint (WP4).
# Legacy artifacts appearing or disappearing changes the rendered Help
# guidance, so the detection result participates in currentness exactly
# like the target roster: schema 4 is "current" only when BOTH stored
# fingerprints match the freshly computed ones.
LEGACY_FINGERPRINT_KEY = "legacy_fingerprint"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

# --- Legacy System-Setup dashboard signature (OCD-3 / PD-01; M2/#168 WP4) ---
# High-confidence signature of the ONLY authoritatively documented legacy
# PVAutonomy dashboard artifact: the operator-staged static YAML-mode
# System-Setup dashboard. Evidence (all in this repository):
# - docs/architecture/customer-installation-pipeline.md ("Form: static
#   YAML-mode dashboard (`lovelace-system-setup`)");
# - lovelace/system-setup-dashboard.yaml header (canonical registration
#   snippet: url `lovelace-system-setup`, mode yaml, filename
#   `lovelace/system-setup-dashboard.yaml`, created 2025-12-09);
# - deploy_to_edatec.sh SETUP_UI_FILES allowlist (exact staged path);
# - PD-01 / Ops Contract §9.9 ("Static legacy setup YAML may remain
#   temporarily for migration detection").
# Detection requires the FULL conjunction (exact URL path + YAML mode +
# exact filename) — never a `pva-` prefix, a title substring, or the YAML
# mode alone. The customer-editable title is deliberately NOT part of the
# signature (a customized title must not hide the artifact) and not part
# of the descriptor (a title edit must not force a regeneration).
LEGACY_SETUP_DASHBOARD_URL_PATH = "lovelace-system-setup"
LEGACY_SETUP_DASHBOARD_FILENAME = "lovelace/system-setup-dashboard.yaml"
LEGACY_SETUP_DASHBOARD_KIND = "legacy_system_setup_yaml"


class LegacyDetectionError(Exception):
    """Legacy classification could not be completed (fail-closed).

    [M2/#168 WP4] Raised when the runtime dashboard mapping is unreadable
    or a candidate at the reserved historical URL path carries unsafe
    field types. Never converted into "no legacy detected": the ensure
    lifecycle surfaces it as a read error and performs no write.
    """

# --- System Dashboard identity (Ops Contract §9.7; PD-01; M2/#168 WP2A) ---
# Stable, non-configurable identity of the integration-wide managed
# dashboard. Never derived from a config entry or device.
SYSTEM_DASHBOARD_URL_PATH = "pva-system"
SYSTEM_DASHBOARD_TITLE = "PVAutonomy"
SYSTEM_DASHBOARD_ICON = "mdi:solar-power"

# Serialise concurrent System Dashboard lifecycle decisions (multiple config
# entries may set up at once; classification and create/save must not race).
_SYSTEM_DASHBOARD_LOCK = asyncio.Lock()

# Stable ensure() outcomes (returned to callers/tests; also logged).
SYSTEM_DASHBOARD_CREATED = "created"
SYSTEM_DASHBOARD_CURRENT = "current"
SYSTEM_DASHBOARD_REGENERATED = "regenerated"
SYSTEM_DASHBOARD_NEWER_SCHEMA = "newer_schema"
SYSTEM_DASHBOARD_UNMANAGED_COLLISION = "unmanaged_collision"
SYSTEM_DASHBOARD_READ_ERROR = "read_error"
SYSTEM_DASHBOARD_ERROR = "error"

# --- OCD-3 suppression / explicit removal (M2/#168 WP3) ---
# Integration-global suppression state lives in its OWN Store (never in a
# per-config-entry option): pva-system is installation-global, so every
# entry observes the same state. Strict Boolean payload {"suppressed": ...}.
SUPPRESSION_STORE_KEY = "pvautonomy_ops_system_dashboard"
SUPPRESSION_STORE_VERSION = 1
_SUPPRESSED_FIELD = "suppressed"

# Additional lifecycle outcomes for suppression + explicit removal.
SYSTEM_DASHBOARD_SUPPRESSED = "suppressed"          # ensure no-op while off
SYSTEM_DASHBOARD_REMOVED = "removed"                # disable+remove success
SYSTEM_DASHBOARD_SUPPRESSED_ABSENT = "suppressed_absent"  # nothing to delete
SYSTEM_DASHBOARD_ALREADY_REMOVED = "already_removed"  # idempotent retry done
SYSTEM_DASHBOARD_PARTIAL_REMOVAL = "partial_removal"  # persistent done, panel pending
SYSTEM_DASHBOARD_ENABLED = "enabled"                # re-enable+regenerate success

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


# Registry root resolved by the shared resolver — see defs_paths.py.


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
    # [fix/edge-restart-label] The uptime sensor is now device_class: timestamp
    # (the boot time), which HA renders natively as a localized relative time
    # ("vor X" / "X ago"). Label it "Edge Restart" so that reads correctly —
    # "Edge Restart: vor 4 Stunden" — instead of the literal "Edge Uptime: vor
    # 4 Stunden". The entity_id stays language-neutral `…_uptime`; only the
    # display label changes (D-ADDON-I18N-001).
    "uptime": "Edge Restart",
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

    [updated 2026-06-16] The Edge101 bridge firmware DOES publish a
    ``*_wifi_signal`` sensor — verified live on EDATEC for both the SPH
    (``…_2eb1e4_wifi_signal``) and MIC (``…_17e9c4_wifi_signal``) bridges, so
    the row renders on both dashboards. (Supersedes the stale TASK-014P
    2026-05-03 note that claimed only ssid/version/ip/mac were published.) The
    row stays live-gated, so it is still omitted on any system whose bridge does
    not publish it.
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
    """Resolve the runtime Edge Restart (uptime) sensor entity ID (live-gated).

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

    Delegates to the shared resolver (bundle-only, fail-closed). When
    the bundle is absent, raises :class:`DefsNotFoundError` via the resolver
    before this function returns — matching the
    previous "return a path, caller checks" contract.
    """
    try:
        return resolve_registry_root()
    except DefsNotFoundError:
        return BUNDLED_REGISTRY_ROOT


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
    selected_tier: str | None = None,
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
            # [issue #67/#68] Tier-aware: on a Standard-tier device, drop
            # extended-only controls so a tier-down re-flash does not leave
            # stale "Entität nicht gefunden" rows until a HA restart prunes the
            # registry — the refresh reads the entry's tier immediately. Guarded
            # by live state: a device whose entry tier is unset/mis-tagged but
            # actually runs Extended keeps any control that is genuinely live.
            if entry.get("tier") == "extended" and selected_tier == "standard":
                _eid = _entity_id(device_name, entry, domain)
                if not (live_entity_ids is not None and _eid in live_entity_ids):
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
    # [issue #55] This card is gated ONLY on the registry contract
    # (``has_export_limit``), NOT on runtime entity presence. It is a read-only
    # *markdown* card whose Jinja reads the sensor's state and degrades to
    # "Unknown" when the sensor is absent — so it must NOT be live-gated like an
    # entity row. The previous ``_dashboard_entity_available`` gate dropped the
    # card entirely during the reflash window: an OTA reflash reboots the
    # device, so on a near-simultaneous HA restart the esphome sensor is in
    # neither ``hass.states`` nor the entity registry yet (TASK-014W/Y's
    # active-registry fallback only helps once the entry is registered), and the
    # card vanished instead of showing "Unknown" → "RS485" once the device
    # reconnects. The registry-contract gate keeps the card present and lets the
    # markdown degrade gracefully. (Entity rows / priority_control keep their
    # live-gate, where a missing entity would render an "Entität nicht gefunden"
    # placeholder.)
    export_limit_status_eid = (
        _export_limit_mode_sensor_entity_id(device_name)
        if has_export_limit
        else None
    )
    # [issue #68] Tier-aware suppression. A Standard-tier device never builds the
    # extended export_limit register, so the registry-contract gate above would
    # render a permanent "Export Limit: Unknown" card (not acceptable). Suppress
    # the card when the selected tier is Standard AND the mode sensor is not
    # actually live. For Extended (selected_tier != "standard") this is a no-op,
    # so #55 is preserved verbatim: the card stays on the registry contract and
    # degrades gracefully ("Unknown" → "RS485") across the reflash window. The
    # live check keeps a device whose entry tier is unset/mis-tagged but actually
    # runs Extended from losing the card.
    if export_limit_status_eid is not None and selected_tier == "standard" and not (
        live_entity_ids is not None and export_limit_status_eid in live_entity_ids
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
            # [I1 2026-06-16] Edge bridge diagnostics (Edge WiFi Signal + Edge
            # Restart) as a native *entities* card so HA renders the timestamp as
            # localized relative time and the WiFi signal with its dBm unit (the
            # markdown Status card above would show their raw state). Live-gated,
            # mac_suffix-aware — same resolver contract SPH uses.
            edge_card = _build_mic_edge_status_card(
                device_name,
                live_entity_ids=live_entity_ids,
                mac_suffix=mac_suffix,
            )
            if edge_card:
                cards.append(edge_card)
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
    Edge Restart. The Edge WiFi Signal and Edge Restart rows are optional
    bridge diagnostics: each is rendered only when its resolver finds a
    live entity (otherwise the row is skipped — never shown as missing).
    (Edge Restart is the ``uptime`` timestamp sensor; see _COMPACT_LABELS.)
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
                # Skip the row entirely when no live WiFi Signal sensor is
                # present in hass.states, so it is never an "Entität nicht
                # gefunden" row. (The EDATEC bridges DO publish wifi_signal —
                # verified 2026-06-16; the gate guards systems that don't.)
                continue
        elif reg_id == "uptime":
            # [P1b] Edge Restart (the uptime timestamp sensor) is an optional
            # bridge diagnostic: render it only when a live uptime sensor
            # exists, so it never surfaces an "Entität nicht gefunden" row and
            # appears automatically once the bridge publishes it.
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


def _build_mic_edge_status_card(
    device_name: str,
    *,
    live_entity_ids: set[str] | None,
    mac_suffix: str | None,
) -> dict[str, Any] | None:
    """Build the MIC Edge-diagnostics card (Edge WiFi Signal + Edge Restart).

    [I1 2026-06-16] MIC's primary Status card is *markdown* (it maps
    ``inverter_status`` 0/1/3 → text and rounds the temperature), which would
    render a ``device_class: timestamp`` / dBm sensor as its bare raw state
    (e.g. a raw ISO datetime). The optional Edge bridge diagnostics are therefore
    surfaced in a separate *entities* card so Home Assistant renders them
    natively — **Edge Restart** as a localized relative time ("vor X" / "X ago")
    and **Edge WiFi Signal** with its dBm unit. This mirrors the SPH contract
    (:func:`_build_sph_status_card`) for the non-battery layout.

    Each row is live-gated by its resolver (mac_suffix-aware): rendered only when
    the live bridge sensor exists, so a bridge that publishes neither yields no
    card (``None``) and never an "Entität nicht gefunden" phantom row. The card
    is intentionally **untitled** so it reads as a continuation of the markdown
    Status card above it rather than a second "Status" header.
    """
    rows: list[tuple[str, str]] = []
    wifi_eid = _resolve_wifi_signal_entity_id(
        device_name, mac_suffix=mac_suffix, live_entity_ids=live_entity_ids
    )
    if wifi_eid is not None:
        rows.append((wifi_eid, _COMPACT_LABELS["wifi_signal"]))
    uptime_eid = _resolve_uptime_entity_id(
        device_name, mac_suffix=mac_suffix, live_entity_ids=live_entity_ids
    )
    if uptime_eid is not None:
        rows.append((uptime_eid, _COMPACT_LABELS["uptime"]))
    if not rows:
        return None
    return {
        "type": "entities",
        "show_header_toggle": False,
        "entities": [{"entity": eid, "name": label} for eid, label in rows],
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


def build_lovelace_payload(
    views: list[dict[str, Any]],
    *,
    managed_schema_version: int | None = None,
) -> dict[str, Any]:
    """Assemble the full Lovelace storage payload from ordered, complete views.

    [M2/#168 WP1] Generic payload constructor shared by the per-device
    dashboard path and future integration-managed (non-device) dashboards
    (Ops Contract §9.7). Pure and deterministic: identical inputs produce
    identical output; no filesystem, storage, or Home Assistant access.

    - ``views`` must be a non-empty list of complete Lovelace view dicts;
      caller order is preserved exactly (no dedup, no reordering).
    - Views are deep-copied so caller-owned objects are never shared or
      mutated through the returned payload.
    - ``managed_schema_version`` adds the namespaced managed-content marker
      (``MANAGED_SCHEMA_KEY``) to the config dict; per-device dashboards
      pass ``None`` and stay marker-free (payloads byte-identical to the
      pre-WP1 builder output).
    """
    if not isinstance(views, list) or not views:
        raise ValueError("views must be a non-empty list of Lovelace view dicts")
    if not all(isinstance(view, dict) for view in views):
        raise ValueError("every view must be a Lovelace view dict")

    config: dict[str, Any] = {"views": copy.deepcopy(views)}
    if managed_schema_version is not None:
        config[MANAGED_SCHEMA_KEY] = {"schema_version": managed_schema_version}

    return {
        "version": 1,
        "minor_version": 1,
        "key": "",  # filled by caller
        "data": {
            "config": config
        },
    }


def build_managed_dashboard(
    *,
    title: str,
    url_path: str,
    views: list[dict[str, Any]],
    schema_version: int | None = MANAGED_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Construct — but never register or save — a managed non-device dashboard.

    [M2/#168 WP1] Foundation for the future integration-wide System
    Dashboard (§9.7; identity decided by PD-01/OCD-3). Requires no device
    name, registry file, model, or tier. Returns a plain descriptor::

        {"url_path": ..., "title": ..., "payload": <storage payload>}

    Persistence stays a separate, explicit step (WP2) — calling this
    function has no side effects and creates nothing in Home Assistant.
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(url_path, str) or "-" not in url_path:
        raise ValueError("url_path must be a string containing '-' (HA requirement)")

    return {
        "url_path": url_path,
        "title": title,
        "payload": build_lovelace_payload(
            views, managed_schema_version=schema_version
        ),
    }


def get_managed_schema_version(lovelace_config: Any) -> int | None:
    """Extract the managed schema version from a Lovelace config dict.

    [M2/#168 WP1] A well-formed managed schema version is a **positive
    integer** (``>= 1``, and never a ``bool`` — ``bool`` is a subclass of
    ``int``). Returns that integer when the namespaced marker carries one.
    Returns ``None`` when the marker is absent OR malformed — including a
    non-integer, ``0``, or a negative value — deliberately treating a
    malformed marker as "not managed / unknown" (fail-closed rule: later
    lifecycle logic must never full-rewrite content it cannot positively
    identify as managed). Version-comparison policy (older/current/newer)
    is WP2 scope; this helper only recovers a valid version.
    """
    if not isinstance(lovelace_config, dict):
        return None
    marker = lovelace_config.get(MANAGED_SCHEMA_KEY)
    if not isinstance(marker, dict):
        return None
    version = marker.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    if version < 1:
        return None
    return version


# Customer-facing managed-content notice (§9.7: regeneration is a full
# managed rewrite and the dashboard MUST carry a managed-content notice).
MANAGED_NOTICE_TEXT = (
    "This dashboard is managed by PVAutonomy and is generated "
    "automatically. Manual changes may be replaced whenever PVAutonomy "
    "updates this dashboard."
)


def build_managed_notice_card() -> dict[str, Any]:
    """Return the managed-content notice as a standard Markdown card.

    [M2/#168 WP1] Deterministic, opt-in: intended as the first card of a
    managed view (WP2). Never injected into existing per-device
    dashboards by this module.
    """
    return {
        "type": "markdown",
        "content": f"ℹ️ {MANAGED_NOTICE_TEXT}",
    }


def compute_target_fingerprint(targets: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 over the logical maintenance-target roster.

    [M2/#168 WP2B1] Inputs are the stable target descriptors only
    (``entry_id``, ``device_name``, ``label``, ``status_entity_id``) —
    never runtime state, availability, timestamps or operation results,
    and never secrets. The descriptor list is sorted canonically before
    hashing, so input order cannot change the fingerprint; any change to
    a target id, customer label or resolved status entity does.
    """
    canonical = sorted(
        (
            {
                "entry_id": t.get("entry_id"),
                "device_name": t.get("device_name"),
                "label": t.get("label"),
                "status_entity_id": t.get("status_entity_id"),
            }
            for t in targets
        ),
        key=lambda d: (str(d["entry_id"]), str(d["device_name"])),
    )
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get_target_fingerprint(lovelace_config: Any) -> str | None:
    """Extract the stored target fingerprint (strict; fail-closed).

    Returns the fingerprint string only when the managed marker carries a
    value in the exact deterministic format (64 lowercase hex chars).
    Anything else — absent, wrong type, malformed — returns ``None`` and
    is treated as "fingerprint unknown" (which triggers regeneration only
    under a valid current PVAutonomy schema marker; ownership rules are
    unaffected).
    """
    if not isinstance(lovelace_config, dict):
        return None
    marker = lovelace_config.get(MANAGED_SCHEMA_KEY)
    if not isinstance(marker, dict):
        return None
    value = marker.get(TARGET_FINGERPRINT_KEY)
    if not isinstance(value, str) or not _FINGERPRINT_RE.match(value):
        return None
    return value


def compute_legacy_fingerprint(artifacts: list[dict[str, Any]]) -> str:
    """Deterministic SHA-256 over the normalized legacy detection result.

    [M2/#168 WP4] Inputs are the stable descriptor fields only (``kind``,
    ``url_path``, ``mode``, ``filename``) — never card payloads, runtime
    state, timestamps or secrets. The descriptor list is sorted
    canonically before hashing, so input order cannot change the
    fingerprint; an artifact appearing, disappearing, or changing a
    descriptor field does. The empty set has a stable fingerprint.
    """
    canonical = sorted(
        (
            {
                "kind": a.get("kind"),
                "url_path": a.get("url_path"),
                "mode": a.get("mode"),
                "filename": a.get("filename"),
            }
            for a in artifacts
        ),
        key=lambda d: (str(d["kind"]), str(d["url_path"])),
    )
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get_legacy_fingerprint(lovelace_config: Any) -> str | None:
    """Extract the stored legacy fingerprint (strict; fail-closed).

    Same strict contract as :func:`get_target_fingerprint`: the value is
    returned only in the exact deterministic format (64 lowercase hex
    chars); absent, wrong-typed or malformed values return ``None`` and
    are treated as "legacy state unknown", which triggers regeneration
    only under a valid current PVAutonomy schema marker.
    """
    if not isinstance(lovelace_config, dict):
        return None
    marker = lovelace_config.get(MANAGED_SCHEMA_KEY)
    if not isinstance(marker, dict):
        return None
    value = marker.get(LEGACY_FINGERPRINT_KEY)
    if not isinstance(value, str) or not _FINGERPRINT_RE.match(value):
        return None
    return value


def _get_runtime_dashboards(hass) -> dict[Any, Any]:
    """Return Home Assistant's runtime dashboard mapping (read-only).

    [M2/#168 WP4] ``hass.data["lovelace"].dashboards`` is the only place
    where YAML-mode dashboards appear (the ``lovelace_dashboards`` Store
    holds storage-mode records only), and this module already consumes it
    in ``_async_save_system_dashboard_config`` — no filesystem access, no
    second registry read. When the lovelace integration has not populated
    its runtime data yet there is no dashboard surface at all, so an
    absent entry reads as an empty mapping; a present entry with an
    unrecognized shape is a detection error, never "no legacy".
    """
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        raise LegacyDetectionError("hass.data is unavailable")
    lovelace_data = data.get("lovelace")
    if lovelace_data is None:
        return {}
    dashboards = getattr(lovelace_data, "dashboards", None)
    if dashboards is None and isinstance(lovelace_data, dict):
        dashboards = lovelace_data.get("dashboards")
    if not isinstance(dashboards, dict):
        raise LegacyDetectionError(
            "lovelace runtime data has an unrecognized shape"
        )
    return dashboards


def detect_legacy_dashboard_artifacts(hass) -> list[dict[str, Any]]:
    """Detect positively proven legacy PVAutonomy dashboard artifacts.

    [M2/#168 WP4] Read-only classification against the full documented
    signature (see the LEGACY_SETUP_DASHBOARD_* evidence block). Only a
    dashboard at exactly the historical URL path is ever a candidate;
    ``pva-system``, generated ``pva-*`` device dashboards and every
    unrelated customer dashboard are structurally excluded by the exact
    key match. A candidate whose classification fields cannot be read
    safely raises :class:`LegacyDetectionError` (fail-closed); a readable
    candidate that does not satisfy the complete conjunction is a proven
    non-match and is silently excluded. Never modifies, adopts, renames
    or removes anything.
    """
    dashboards = _get_runtime_dashboards(hass)
    artifacts: list[dict[str, Any]] = []
    for url_path, dashboard in dashboards.items():
        if url_path != LEGACY_SETUP_DASHBOARD_URL_PATH:
            continue
        mode = getattr(dashboard, "mode", None)
        if mode is None and isinstance(dashboard, dict):
            mode = dashboard.get("mode")
        if not isinstance(mode, str):
            raise LegacyDetectionError(
                "candidate at the historical URL path has an unreadable mode"
            )
        if mode != "yaml":
            # Complete negative classification: a storage-mode (or other
            # non-YAML) dashboard at this URL is a customer dashboard.
            continue
        conf = getattr(dashboard, "config", None)
        if conf is None and isinstance(dashboard, dict):
            conf = dashboard.get("config")
        if not isinstance(conf, dict):
            raise LegacyDetectionError(
                "candidate at the historical URL path has an unreadable config"
            )
        filename = conf.get("filename")
        if not isinstance(filename, str):
            raise LegacyDetectionError(
                "candidate at the historical URL path has an unreadable filename"
            )
        if filename != LEGACY_SETUP_DASHBOARD_FILENAME:
            # Complete negative classification: a different YAML dashboard.
            continue
        artifacts.append(
            {
                "kind": LEGACY_SETUP_DASHBOARD_KIND,
                "url_path": url_path,
                "mode": mode,
                "filename": filename,
            }
        )
    artifacts.sort(key=lambda a: (a["kind"], a["url_path"]))
    return artifacts


def _build_maintenance_action_button(
    *,
    name: str,
    icon: str,
    service: str,
    data: dict[str, Any],
    confirmation_text: str | None,
) -> dict[str, Any]:
    """One core Lovelace button card performing exactly one service action."""
    tap_action: dict[str, Any] = {
        "action": "perform-action",
        "perform_action": service,
        "data": data,
    }
    if confirmation_text is not None:
        tap_action["confirmation"] = {"text": confirmation_text}
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "show_state": False,
        "tap_action": tap_action,
    }


def _build_maintenance_target_section(target: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-device Maintenance section (WP2B1).

    Heading, optional real status row (only when an exact enabled
    entry-scoped status entity was resolved — never a guessed entity id),
    and the three explicit-target actions. Prepare is build-only (no
    ``force_rebuild``); Install is install-only and carries the service's
    required ``confirmed: true`` plus a UI confirmation (CJ-02·7); every
    action bakes this device's ``entry_id`` + ``device_name`` (CJ-07 —
    no shared mutable target selector).
    """
    entry_id = target["entry_id"]
    device_name = target["device_name"]
    label = target["label"]
    action_target = {"entry_id": entry_id, "device_name": device_name}

    cards: list[dict[str, Any]] = [
        {"type": "markdown", "content": f"### {label}"},
    ]
    status_entity_id = target.get("status_entity_id")
    if status_entity_id:
        cards.append(
            {
                "type": "entities",
                "entities": [
                    {"entity": status_entity_id, "name": "Status"}
                ],
            }
        )
    cards.append(
        {
            "type": "horizontal-stack",
            "cards": [
                _build_maintenance_action_button(
                    name="Prepare firmware",
                    icon="mdi:progress-wrench",
                    service=f"{DOMAIN}.build_firmware",
                    data=dict(action_target),
                    confirmation_text=(
                        f"Prepare new firmware for {label}? This only "
                        "prepares the firmware — nothing is installed on "
                        "the device yet. Preparation can take several "
                        "minutes; watch the status above."
                    ),
                ),
                _build_maintenance_action_button(
                    name="Install prepared firmware",
                    icon="mdi:download-circle",
                    service=f"{DOMAIN}.install_prepared_firmware",
                    data={**action_target, "confirmed": True},
                    confirmation_text=(
                        f"Install the prepared firmware on {label}? The "
                        "device will restart and be briefly unavailable "
                        "while it reconnects."
                    ),
                ),
                _build_maintenance_action_button(
                    name="Refresh device dashboard",
                    icon="mdi:refresh",
                    service=f"{DOMAIN}.refresh_customer_dashboard",
                    data=dict(action_target),
                    confirmation_text=None,
                ),
            ],
        }
    )
    return {"type": "vertical-stack", "cards": cards}


_MAINTENANCE_INTRO = (
    "Maintain your PVAutonomy devices here.\n\n"
    "1. **Prepare firmware** — builds new firmware for the device "
    "(nothing is installed yet).\n"
    "2. Watch the device **status** until preparation has completed.\n"
    "3. **Install prepared firmware** — installs it after your "
    "confirmation; the device restarts and reconnects.\n"
    "4. If the device dashboard looks outdated afterwards, use "
    "**Refresh device dashboard**."
)

_NO_TARGETS_TEXT = (
    "No configured PVAutonomy devices are currently available. Set up a "
    "device first — its maintenance actions will appear here "
    "automatically."
)


# --- Help / Setup Guidance view (M2/#168 WP2B2; PD-05, PD-06) ---
# Guidance and navigation ONLY: the wizard (config flow) stays the single
# canonical commissioning path (PD-06) and the Maintenance view owns the
# firmware actions. Help never calls a service. Factory-WiFi / captive-
# portal / connection guidance appears as concise contextual help text
# (PD-05) — never as a recreated WiFi tab.

_HELP_START_HERE = (
    "## Start here\n\n"
    "1. Pick your device in the sections below and open its dashboard.\n"
    "2. To set up a new device — or to change an existing one — open "
    "**Settings → Devices & Services → PVAutonomy** and follow the guided "
    "Setup or Reconfigure steps.\n"
    "3. For firmware updates use the **Maintenance** view: prepare the "
    "firmware first, install it after preparation has completed, then let "
    "the device reconnect.\n\n"
    "No developer tools and no manual configuration files are required — "
    "everything runs through these guided steps."
)

_HELP_NO_DEVICES_TEXT = (
    "No configured PVAutonomy device is currently available. Set up your "
    "first device via **Settings → Devices & Services → PVAutonomy** — its "
    "guidance will appear here automatically."
)

_HELP_RECOVERY = (
    "## If something needs attention\n\n"
    "- Check the status shown for the device before repeating an action.\n"
    "- Setup or Reconfigure can safely be re-run at any time from "
    "**Settings → Devices & Services → PVAutonomy**.\n"
    "- After a firmware installation, allow the device to restart and "
    "reconnect — this can take a few minutes. Do not disconnect power "
    "while an installation is running.\n"
    "- If a device dashboard looks outdated, use **Refresh device "
    "dashboard** in the Maintenance view.\n"
    "- If a brand-new device is not found during setup, check that it is "
    "powered on and connected to your WiFi. A factory-fresh device first "
    "provides its own temporary setup network — the guided Setup steps "
    "walk you through connecting it to your home network."
)

# Contextual guidance mapped ONLY to verified status-sensor states
# (const.py: ok / warn / error / degraded; operation progress lives in
# attributes, which core conditional cards cannot condition on — that
# category is covered by the always-visible static guidance instead).
_HELP_CONTEXT_TEXT: dict[str, str] = {
    "degraded": (
        "**Setup needed:** Some information for this device is missing or "
        "incomplete. Open **Settings → Devices & Services → PVAutonomy** "
        "and continue Setup or Reconfigure for this device."
    ),
    "warn": (
        "**Attention:** The device may be offline or not fully connected. "
        "Check its power and network connection. If a firmware "
        "installation has just finished, give the device a few minutes to "
        "reconnect."
    ),
    "error": (
        "**Something needs attention:** Check the status shown above for "
        "details. If a preparation or installation is still running, let "
        "it finish before trying again — then retry from the Maintenance "
        "view, or re-run Setup/Reconfigure."
    ),
    "ok": (
        "**Ready:** No setup action is needed right now. Use the device "
        "dashboard above for day-to-day operation."
    ),
}


def _build_help_target_section(target: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-device Help section (WP2B2).

    Heading, optional real status row (same safe resolution as
    Maintenance), a navigation-only button to the generated device
    dashboard (route from the authoritative ``compute_dashboard_url``),
    and contextual guidance rendered by core ``conditional`` cards on the
    exact verified status states. Without a status entity the section
    stays useful: heading + navigation + static guidance, no guessed
    entity, no conditional cards.
    """
    label = target["label"]
    device_name = target["device_name"]
    status_entity_id = target.get("status_entity_id")

    cards: list[dict[str, Any]] = [
        {"type": "markdown", "content": f"### {label}"},
    ]
    if status_entity_id:
        cards.append(
            {
                "type": "entities",
                "entities": [
                    {"entity": status_entity_id, "name": "Status"}
                ],
            }
        )
    cards.append(
        {
            "type": "button",
            "name": "Open device dashboard",
            "icon": "mdi:view-dashboard",
            "show_state": False,
            "tap_action": {
                "action": "navigate",
                "navigation_path": f"/{compute_dashboard_url(device_name)}",
            },
        }
    )
    if status_entity_id:
        for state, text in _HELP_CONTEXT_TEXT.items():
            cards.append(
                {
                    "type": "conditional",
                    "conditions": [
                        {
                            "condition": "state",
                            "entity": status_entity_id,
                            "state": state,
                        }
                    ],
                    "card": {"type": "markdown", "content": text},
                }
            )
    else:
        cards.append(
            {
                "type": "markdown",
                "content": (
                    "Status is not available for this device right now. You "
                    "can still open its dashboard above, re-run the guided "
                    "Setup/Reconfigure, or use the Maintenance view."
                ),
            }
        )
    return {"type": "vertical-stack", "cards": cards}


# Customer-facing legacy migration guidance (M2/#168 WP4; OCD-3/PD-01).
# One consolidated card, rendered only while at least one positively
# detected legacy artifact exists. Informational only: no action, no
# navigation, no internal identifiers, no removal instruction beyond the
# ordinary Home Assistant surface, and no claim that anything was
# migrated, is invalid, or is removed automatically.
_LEGACY_MIGRATION_TEXT = (
    "## Older PVAutonomy dashboard detected\n\n"
    "An older PVAutonomy dashboard from a previous setup generation was "
    "detected on this Home Assistant system. PVAutonomy has not changed "
    "it, and nothing was migrated or deleted automatically.\n\n"
    "This System Dashboard and the generated device dashboards are the "
    "current, supported PVAutonomy surfaces. Please check that they cover "
    "everything you need before removing anything.\n\n"
    "If you no longer need the older dashboard, you can remove it "
    "manually through your normal Home Assistant dashboard management. "
    "Removing it is optional and entirely your decision."
)


def build_legacy_migration_card() -> dict[str, Any]:
    """Return the consolidated legacy migration notice (core Markdown card)."""
    return {"type": "markdown", "content": _LEGACY_MIGRATION_TEXT}


def _build_help_view(
    targets: list[dict[str, Any]],
    legacy_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The Help view: notice, Start Here, optional legacy migration
    guidance, per-device guidance, recovery.

    [M2/#168 WP4] With an empty legacy detection result the rendered view
    is byte-identical to the WP2B2/WP3 Help view — the migration notice is
    strictly presence-gated and always a single consolidated card.
    """
    cards: list[dict[str, Any]] = [
        build_managed_notice_card(),
        {"type": "markdown", "content": _HELP_START_HERE},
    ]
    if legacy_artifacts:
        cards.append(build_legacy_migration_card())
    if targets:
        cards.extend(_build_help_target_section(target) for target in targets)
        cards.append({"type": "markdown", "content": _HELP_RECOVERY})
    else:
        cards.append({"type": "markdown", "content": _HELP_NO_DEVICES_TEXT})
    return {"path": "help", "title": "Help", "cards": cards}


def _build_maintenance_view(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """The Maintenance view: notice, orientation, one section per target."""
    cards: list[dict[str, Any]] = [build_managed_notice_card()]
    if targets:
        cards.append({"type": "markdown", "content": _MAINTENANCE_INTRO})
        cards.extend(
            _build_maintenance_target_section(target) for target in targets
        )
    else:
        cards.append({"type": "markdown", "content": _NO_TARGETS_TEXT})
    return {"path": "maintenance", "title": "Maintenance", "cards": cards}


def build_system_dashboard_payload(
    targets: list[dict[str, Any]] | None = None,
    *,
    legacy_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the System Dashboard payload (deterministic).

    [M2/#168 WP2B1+WP2B2+WP4] Two ordered views: the functional
    *Maintenance* surface (explicit per-device actions from ``targets``)
    and the *Help / Setup Guidance* surface (navigation + contextual
    guidance + presence-gated legacy migration notice, never a service
    action). The managed marker carries the current schema version, the
    target-roster fingerprint AND the legacy-detection fingerprint.

    Pure: no services, storage, registries, caller mutation, or lock.
    """
    targets = list(targets or [])
    legacy_artifacts = list(legacy_artifacts or [])
    views = [
        _build_maintenance_view(targets),
        _build_help_view(targets, legacy_artifacts),
    ]
    result = build_managed_dashboard(
        title=SYSTEM_DASHBOARD_TITLE,
        url_path=SYSTEM_DASHBOARD_URL_PATH,
        views=views,
        schema_version=MANAGED_SCHEMA_VERSION,
    )
    marker = result["payload"]["data"]["config"][MANAGED_SCHEMA_KEY]
    marker[TARGET_FINGERPRINT_KEY] = compute_target_fingerprint(targets)
    marker[LEGACY_FINGERPRINT_KEY] = compute_legacy_fingerprint(
        legacy_artifacts
    )
    return result


async def _async_collect_maintenance_targets(hass) -> list[dict[str, Any]]:
    """Collect the eligible maintenance-target roster (async, read-only).

    [M2/#168 WP2B1] Eligible = non-disabled PVAutonomy config entries with
    a resolvable canonical device slug (this excludes the legacy YAML
    import stub). Runtime online/offline state is deliberately NOT a
    filter — a temporarily offline device stays maintainable. The status
    entity is resolved through the entity registry by exact platform +
    unique_id (never guessed; disabled entities are omitted).
    Deterministic order; duplicate customer labels are disambiguated with
    the canonical device slug (customer metadata, not an internal id).

    **Discovery-failure policy (adversarial-review B1):** exceptions from
    config-entry enumeration, slug resolution, registry acquisition,
    status lookup or label normalization are UNTRUSTED discovery failures
    and deliberately PROPAGATE — the ensure boundary converts them into a
    non-write ``SYSTEM_DASHBOARD_ERROR``, so an existing dashboard is
    never rewritten from a partial, empty or degraded roster. Only
    *genuine* absence is a legitimate result: a registry that works but
    has no matching enabled status entity yields ``status_entity_id =
    None``; a successfully-determined slug-less entry stays ineligible.
    """
    from .config_flow import get_device_slug_from_entry
    from homeassistant.helpers import entity_registry as er

    # Acquire the registry ONCE, before iterating: an acquisition failure
    # must abort discovery, never degrade per-entry results.
    ent_reg = er.async_get(hass)

    raw: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "disabled_by", None):
            continue
        # Slug resolution failures propagate (untrusted discovery result);
        # a successful `None` return means canonically ineligible.
        device_name = get_device_slug_from_entry(entry)
        if not device_name:
            continue

        label = (getattr(entry, "title", "") or "").strip() or device_name

        # Genuine absence handling only — lookup exceptions propagate.
        status_entity_id: str | None = None
        unique_id = f"{entry.entry_id}_{ENTITY_STATUS_SENSOR}"
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            reg_entry = ent_reg.entities.get(entity_id)
            if reg_entry is not None and getattr(
                reg_entry, "disabled_by", None
            ) is None:
                status_entity_id = entity_id
        if status_entity_id is None:
            _LOGGER.debug(
                "Maintenance targets: no enabled status entity for entry %s "
                "— omitting the status card",
                entry.entry_id,
            )

        raw.append(
            {
                "entry_id": entry.entry_id,
                "device_name": device_name,
                "label": label,
                "status_entity_id": status_entity_id,
            }
        )

    # Deterministic customer ordering + duplicate-label disambiguation via
    # the canonical device slug (derived from model/site/number metadata).
    label_counts: dict[str, int] = {}
    for target in raw:
        label_counts[target["label"]] = label_counts.get(target["label"], 0) + 1
    for target in raw:
        if label_counts[target["label"]] > 1:
            target["label"] = f"{target['label']} ({target['device_name']})"
    raw.sort(key=lambda t: (t["label"].lower(), t["device_name"], t["entry_id"]))
    return raw


async def _async_save_system_dashboard_config(
    hass,
    payload: dict[str, Any],
    item_id: str | None = None,
    allow_direct_fallback: bool = True,
) -> None:
    """Write the complete System Dashboard config (full deterministic save).

    Generic managed-dashboard variant of the device layer-2 write: prefer
    Home Assistant's in-memory LovelaceStorage object (open frontends
    auto-reload), fall back to the direct Store write. Always a full rewrite
    — never card-level patching or user-edit merging. No device-specific
    assumptions (logs view/card totals, not ``views[0]``).

    ``item_id`` is the dashboard-registration item ID, which keys the
    lovelace config store ([WP6B-A1]; collection-generated IDs are slugified
    — e.g. ``pva_system`` — while pre-WP6B-A1 records carry id == url_path).
    Defaults to the url_path for the legacy direct-write layout.

    ``allow_direct_fallback=False`` ([WP6B-A1 amendment], fresh-create path):
    the config MUST reach the runtime ``LovelaceStorage`` object created by
    the collection listener; if that save is impossible a ``RuntimeError``
    is raised so the caller rolls the registration back — a direct config
    write would persist content the running boot cannot serve. The default
    (``True``) keeps the direct write ONLY as the content-refresh path for a
    dashboard that is already registered (regeneration; registry untouched).
    """
    from homeassistant.helpers.storage import Store

    url_path = SYSTEM_DASHBOARD_URL_PATH
    lovelace_config = payload["data"]["config"]
    num_views = len(lovelace_config["views"])
    num_cards = sum(len(v.get("cards", [])) for v in lovelace_config["views"])

    saved_via_lovelace = False
    try:
        lovelace_data = hass.data.get("lovelace")
        dashboards = getattr(lovelace_data, "dashboards", None)
        if dashboards is None and isinstance(lovelace_data, dict):
            dashboards = lovelace_data.get("dashboards")
        dashboard_obj = dashboards.get(url_path) if dashboards else None
        if dashboard_obj is not None and hasattr(dashboard_obj, "async_save"):
            await dashboard_obj.async_save(lovelace_config)
            saved_via_lovelace = True
            _LOGGER.info(
                "System dashboard config saved via LovelaceStorage (%s: "
                "%d views, %d cards)",
                url_path,
                num_views,
                num_cards,
            )
    except Exception:  # noqa: BLE001 — HA-internal API; fall back below
        _LOGGER.debug(
            "LovelaceStorage save path unavailable for %s; falling back to "
            "direct store write",
            url_path,
            exc_info=True,
        )
        saved_via_lovelace = False

    if not saved_via_lovelace:
        if not allow_direct_fallback:
            raise RuntimeError(
                "system dashboard runtime config save unavailable "
                "(strict mode — caller rolls back)"
            )
        config_store = Store(
            hass,
            version=1,
            key=f"lovelace.{item_id or url_path}",
            minor_version=1,
        )
        await config_store.async_save(payload["data"])
        _LOGGER.info(
            "System dashboard config written (direct store) for %s "
            "(%d views, %d cards)",
            url_path,
            num_views,
            num_cards,
        )


def _register_system_dashboard_panel(hass) -> None:
    """Register the System Dashboard panel (best-effort, never raises)."""
    try:
        from homeassistant.components.frontend import async_register_built_in_panel

        async_register_built_in_panel(
            hass,
            component_name="lovelace",
            frontend_url_path=SYSTEM_DASHBOARD_URL_PATH,
            sidebar_title=SYSTEM_DASHBOARD_TITLE,
            sidebar_icon=SYSTEM_DASHBOARD_ICON,
            config={"mode": "storage"},
            require_admin=False,
        )
        _LOGGER.info(
            "System dashboard panel registered: %s", SYSTEM_DASHBOARD_URL_PATH
        )
    except Exception:  # noqa: BLE001 — best-effort on the fallback path
        _LOGGER.debug(
            "Panel registration skipped for %s (fallback path; lovelace "
            "loads the persisted registration on its next start)",
            SYSTEM_DASHBOARD_URL_PATH,
            exc_info=True,
        )


def _get_runtime_dashboards_collection(hass):
    """Locate the live lovelace ``DashboardsCollection`` of this HA process.

    [M2/#168 WP6B-A1] Same-boot correctness requires mutating the dashboard
    registry through the running collection: only its change listener creates
    the runtime ``LovelaceStorage`` and panel, and only its in-memory ``data``
    is authoritative during the current boot (the collection persists via a
    DELAYED store write, and any later collection mutation rewrites the store
    from ``data`` — a direct store write is both invisible to the running
    frontend and at risk of being clobbered).

    Home Assistant (2026.6.x) keeps the collection instance only in the
    closure of ``lovelace.async_setup`` — there is no public accessor for
    other integrations. Acquisition is therefore layered and fail-closed:

    1. ``hass.data["lovelace"].dashboards_collection`` — future-proofing in
       case HA exposes the instance directly.
    2. The registered ``lovelace/dashboards/list`` websocket handler, which
       is the bare bound method of HA's ``StorageCollectionWebsocket``
       wrapper; its ``__self__.storage_collection`` is the live collection
       (the same instance HA's Settings UI mutates).

    Every candidate is verified against the public surface actually used
    (``async_create_item``/``async_delete_item``, dict ``data``, and the
    ``lovelace_dashboards`` store key) before being returned. Returns
    ``None`` when no verified instance is found — callers then fall back to
    the direct-store path (pre-WP6B-A1 behavior: persisted correctly, picked
    up by lovelace on its next start).
    """
    candidates = []

    try:
        lovelace_data = hass.data.get("lovelace")
        direct = getattr(lovelace_data, "dashboards_collection", None)
        if direct is None and isinstance(lovelace_data, dict):
            direct = lovelace_data.get("dashboards_collection")
        if direct is not None:
            candidates.append(direct)

        ws_commands = hass.data.get("websocket_api") or {}
        entry = ws_commands.get("lovelace/dashboards/list")
        handler = entry[0] if isinstance(entry, (tuple, list)) else entry
        wrapper = getattr(handler, "__self__", None)
        ws_coll = getattr(wrapper, "storage_collection", None)
        if ws_coll is not None:
            candidates.append(ws_coll)
    except Exception:  # noqa: BLE001 — acquisition is strictly best-effort
        pass

    for coll in candidates:
        store_key = getattr(getattr(coll, "store", None), "key", None)
        if (
            store_key == "lovelace_dashboards"
            and hasattr(coll, "async_create_item")
            and hasattr(coll, "async_delete_item")
            and isinstance(getattr(coll, "data", None), dict)
        ):
            return coll
    return None


async def async_ensure_system_dashboard(hass) -> str:
    """Ensure the managed System Dashboard exists and is current.

    [M2/#168 WP2A] Idempotent lifecycle operation called from every
    successful ``async_setup_entry``. Serialised by a dedicated lock so
    concurrent entry setups cannot double-create or race classification.
    Fail-safe: never raises — any unexpected failure logs a warning and
    returns ``SYSTEM_DASHBOARD_ERROR`` so device setup is never broken by
    dashboard work.

    Never deletes, disables or suppresses the dashboard (OCD-3
    disable/remove/re-enable is WP3 scope).
    """
    try:
        async with _SYSTEM_DASHBOARD_LOCK:
            return await _ensure_system_dashboard_impl(hass)
    except Exception:  # noqa: BLE001 — lifecycle must never break setup
        _LOGGER.warning(
            "System dashboard ensure failed (non-fatal; device setup "
            "continues)",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_ERROR


async def _ensure_system_dashboard_impl(hass) -> str:
    """Classify the stored System Dashboard state and act per the WP2A matrix.

    State matrix (fail-closed — never overwrite content that cannot be
    positively identified as PVAutonomy-managed; never delete):

    - missing (no registration, no stored config)  -> create
    - registered, stored marker == current         -> no-op ("current")
    - registered, stored marker older positive     -> full regeneration
    - registered, stored marker newer than current -> warn, no write
    - stored config without a valid marker         -> warn, no write
    - registration/config unreadable               -> warn, no write
    - registered but no stored config              -> warn, no write
      (a registry record alone does not prove PVAutonomy ownership;
      automatic adoption is refused)
    - duplicate pva-system registry records        -> warn, no write
      (ambiguous registration ownership)
    - stored config present without registration   -> warn, no write
    - suppression state is on                       -> no-op ("suppressed")
    - suppression state unreadable/malformed        -> read_error, no write
    - legacy detection fails (WP4)                  -> read_error, no write
    - legacy artifact appears/disappears (WP4)      -> one regeneration
    """
    from homeassistant.helpers.storage import Store

    # --- OCD-3 suppression gate (WP3): honored before any registry read
    # or target discovery. Fail-closed on an unreadable/malformed state:
    # never assume "enabled" and never write. ---
    try:
        if await _read_suppression_state(hass):
            _LOGGER.debug(
                "System dashboard suppressed — no automatic ensure action"
            )
            return SYSTEM_DASHBOARD_SUPPRESSED
    except Exception:  # noqa: BLE001 — fail closed on unreadable suppression
        _LOGGER.warning(
            "System dashboard: suppression state unreadable/malformed — "
            "fail closed (no write)",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_READ_ERROR

    url_path = SYSTEM_DASHBOARD_URL_PATH

    # [WP6B-A1] Prefer the live collection for BOTH reads and writes: its
    # in-memory ``data`` is the in-process truth (the backing store is
    # written with a delay), and mutations through it are the only way the
    # running boot's frontend learns about the dashboard.
    runtime_collection = _get_runtime_dashboards_collection(hass)

    dashboards_store = Store(
        hass, version=1, key="lovelace_dashboards", minor_version=1
    )
    try:
        dashboards_data = await dashboards_store.async_load()
    except Exception:  # noqa: BLE001 — fail closed on unreadable registry
        _LOGGER.warning(
            "System dashboard: dashboard registry unreadable — fail closed "
            "(no write)",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_READ_ERROR

    existing_items = (dashboards_data or {}).get("items", [])
    if runtime_collection is not None:
        matching_records = [
            dict(item)
            for item in runtime_collection.data.values()
            if item.get("url_path") == url_path
        ]
    else:
        matching_records = [
            item for item in existing_items if item.get("url_path") == url_path
        ]
    if len(matching_records) > 1:
        # Ambiguous registration ownership: never pick one record
        # arbitrarily and never rewrite under ambiguity (WP2A review R1).
        # Deduplication would be a separately authorized lifecycle
        # operation, not part of ensure.
        _LOGGER.warning(
            "System dashboard: %d registry records exist for %s — ambiguous "
            "registration ownership, fail closed (no write, no delete)",
            len(matching_records),
            url_path,
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION
    registered = len(matching_records) == 1

    # [WP6B-A1] The lovelace config store is keyed by the registration
    # item's ID (collection-generated IDs are slugified, e.g. "pva_system";
    # records written by the pre-WP6B-A1 direct path carry id == url_path).
    config_key_id = (
        (matching_records[0].get("id") or url_path) if registered else url_path
    )
    config_store = Store(
        hass, version=1, key=f"lovelace.{config_key_id}", minor_version=1
    )
    try:
        stored = await config_store.async_load()
    except Exception:  # noqa: BLE001 — fail closed on unreadable config
        _LOGGER.warning(
            "System dashboard: stored config for %s unreadable — fail closed "
            "(no write)",
            url_path,
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_READ_ERROR

    if not registered and stored is None:
        # A. Fresh creation. Legacy detection (WP4) happens BEFORE any
        # write; a detection error is a read error, never "no legacy".
        targets = await _async_collect_maintenance_targets(hass)
        try:
            legacy = detect_legacy_dashboard_artifacts(hass)
        except LegacyDetectionError:
            _LOGGER.warning(
                "System dashboard: legacy classification failed — fail "
                "closed (no write)",
                exc_info=True,
            )
            return SYSTEM_DASHBOARD_READ_ERROR
        payload = build_system_dashboard_payload(
            targets, legacy_artifacts=legacy
        )["payload"]

        if runtime_collection is None:
            # [WP6B-A1 amendment] Creation is collection-backed ONLY. A
            # direct registry-store write would recreate the same-boot
            # startup race (persisted-but-invisible dashboard, empty
            # editable fallback) and could be clobbered by any later
            # collection mutation — that is not fail-closed. Abort cleanly
            # with NO mutation; ensure runs again on every entry
            # setup/reload, so the creation retries through the supported
            # lifecycle once the lovelace runtime is available.
            _LOGGER.warning(
                "System dashboard: live lovelace dashboards collection "
                "unavailable — creation aborted fail-closed (no write; "
                "retried on the next ensure)"
            )
            return SYSTEM_DASHBOARD_ERROR

        # [WP6B-A1] Same-boot creation through the live collection: HA
        # validates the record, persists it, and its awaited change
        # listener creates the runtime LovelaceStorage and registers the
        # panel — the dashboard is immediately resolvable by the running
        # frontend, no restart involved. The config is then saved via the
        # runtime object (open frontends auto-reload); a direct config
        # write is NOT permitted on this path (strict mode) — if the
        # runtime save is impossible the creation rolls back completely.
        item = None
        try:
            item = await runtime_collection.async_create_item(
                {
                    "url_path": url_path,
                    "title": SYSTEM_DASHBOARD_TITLE,
                    "icon": SYSTEM_DASHBOARD_ICON,
                    "show_in_sidebar": True,
                    "require_admin": False,
                }
            )
            await _async_save_system_dashboard_config(
                hass, payload, item_id=item.get("id"), allow_direct_fallback=False
            )
        except Exception:  # noqa: BLE001 — never leave a half-created state
            _LOGGER.warning(
                "System dashboard: runtime creation failed%s",
                " — rolling back the registration" if item else "",
                exc_info=True,
            )
            if item is not None:
                try:
                    await runtime_collection.async_delete_item(item["id"])
                except Exception:  # noqa: BLE001 — surfaced via ERROR
                    _LOGGER.warning(
                        "System dashboard: rollback of the runtime "
                        "registration failed",
                        exc_info=True,
                    )
            return SYSTEM_DASHBOARD_ERROR
        _LOGGER.info(
            "System dashboard created (runtime-registered): %s", url_path
        )
        return SYSTEM_DASHBOARD_CREATED

    if not registered and stored is not None:
        # Ownership unknown: a config exists at our reserved key without a
        # registration we can attribute. Fail closed.
        _LOGGER.warning(
            "System dashboard: stored config exists at lovelace.%s without a "
            "dashboard registration — ownership unknown, fail closed (no "
            "write, no delete)",
            url_path,
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    if stored is None:
        # Registered but no stored config. A registry record at the reserved
        # URL path does NOT prove PVAutonomy ownership — this state is
        # indistinguishable from a foreign dashboard registered at
        # pva-system, so automatic adoption is refused (WP2A review B1:
        # fail-closed takes precedence over automatic repair).
        _LOGGER.warning(
            "System dashboard: a registration exists for %s but no valid "
            "PVAutonomy-managed configuration is stored — ownership unknown, "
            "refusing automatic adoption (no write, no delete)",
            url_path,
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    stored_config = stored.get("config") if isinstance(stored, dict) else None
    version = get_managed_schema_version(stored_config)

    if version is None:
        _LOGGER.warning(
            "System dashboard %s exists without a valid PVAutonomy managed "
            "marker — treating as unmanaged, fail closed (no write, no "
            "delete)",
            url_path,
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    if version == MANAGED_SCHEMA_VERSION:
        # Ownership is proven by the valid current PVAutonomy marker.
        # Compare the stored target-roster AND legacy-detection
        # fingerprints against the freshly computed ones: a full match
        # preserves manual edits (true no-op); a missing, malformed or
        # different fingerprint means the eligible roster or the legacy
        # detection result changed — regenerate the complete deterministic
        # payload (§9.7 full managed rewrite; never a card-level merge,
        # never an ownership collision). A legacy-detection error is a
        # read error, never "no legacy" (no write).
        targets = await _async_collect_maintenance_targets(hass)
        try:
            legacy = detect_legacy_dashboard_artifacts(hass)
        except LegacyDetectionError:
            _LOGGER.warning(
                "System dashboard: legacy classification failed — fail "
                "closed (no write)",
                exc_info=True,
            )
            return SYSTEM_DASHBOARD_READ_ERROR
        desired_fingerprint = compute_target_fingerprint(targets)
        stored_fingerprint = get_target_fingerprint(stored_config)
        desired_legacy = compute_legacy_fingerprint(legacy)
        stored_legacy = get_legacy_fingerprint(stored_config)
        if (
            stored_fingerprint == desired_fingerprint
            and stored_legacy == desired_legacy
        ):
            _LOGGER.debug(
                "System dashboard %s is current (schema %d, roster and "
                "legacy state unchanged) — no rewrite",
                url_path,
                version,
            )
            return SYSTEM_DASHBOARD_CURRENT
        payload = build_system_dashboard_payload(
            targets, legacy_artifacts=legacy
        )["payload"]
        await _async_save_system_dashboard_config(
            hass, payload, item_id=config_key_id
        )
        _register_system_dashboard_panel(hass)
        _LOGGER.info(
            "System dashboard %s regenerated: %s changed (stored "
            "fingerprint %s)",
            url_path,
            "target roster"
            if stored_fingerprint != desired_fingerprint
            else "legacy detection state",
            "missing/invalid"
            if stored_fingerprint is None or stored_legacy is None
            else "differs",
        )
        return SYSTEM_DASHBOARD_REGENERATED

    if version > MANAGED_SCHEMA_VERSION:
        _LOGGER.warning(
            "System dashboard %s carries schema %d, newer than supported %d "
            "— refusing downgrade rewrite",
            url_path,
            version,
            MANAGED_SCHEMA_VERSION,
        )
        return SYSTEM_DASHBOARD_NEWER_SCHEMA

    # Older positive schema: full deterministic regeneration (§9.7 —
    # regeneration is a full managed rewrite; no partial merge). Legacy
    # detection (WP4) happens BEFORE any write; a detection error is a
    # read error, never "no legacy".
    targets = await _async_collect_maintenance_targets(hass)
    try:
        legacy = detect_legacy_dashboard_artifacts(hass)
    except LegacyDetectionError:
        _LOGGER.warning(
            "System dashboard: legacy classification failed — fail closed "
            "(no write)",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_READ_ERROR
    payload = build_system_dashboard_payload(
        targets, legacy_artifacts=legacy
    )["payload"]
    await _async_save_system_dashboard_config(hass, payload, item_id=config_key_id)
    _register_system_dashboard_panel(hass)
    _LOGGER.info(
        "System dashboard %s regenerated (schema %d -> %d)",
        url_path,
        version,
        MANAGED_SCHEMA_VERSION,
    )
    return SYSTEM_DASHBOARD_REGENERATED


# ---------------------------------------------------------------------------
# OCD-3 explicit removal & persistent suppression (M2/#168 WP3)
# ---------------------------------------------------------------------------


def _suppression_store(hass):
    from homeassistant.helpers.storage import Store

    return Store(
        hass,
        version=SUPPRESSION_STORE_VERSION,
        key=SUPPRESSION_STORE_KEY,
        minor_version=1,
    )


async def _read_suppression_state(hass) -> bool:
    """Read the strict integration-global suppression state.

    Returns ``True`` (suppressed) / ``False`` (enabled). Absent Store means
    enabled. Raises ``ValueError`` on a malformed payload (non-mapping, or
    ``suppressed`` not an actual ``bool``); propagates IO exceptions. Callers
    must fail closed on any exception — never assume enabled.
    """
    data = await _suppression_store(hass).async_load()
    if data is None:
        return False
    value = data.get(_SUPPRESSED_FIELD) if isinstance(data, dict) else None
    if not isinstance(value, bool):  # rejects str/int/None/list/missing
        raise ValueError("malformed suppression state")
    return value


async def _write_suppression_state(hass, suppressed: bool) -> None:
    await _suppression_store(hass).async_save({_SUPPRESSED_FIELD: bool(suppressed)})


async def _read_system_dashboard_stores(hass, runtime_collection=None):
    """Read the registry matches and stored config for pva-system.

    Returns ``(matching_records, stored)``. Raises on unreadable stores so
    callers fail closed. When the live dashboards collection is supplied
    ([WP6B-A1]) its in-memory ``data`` is authoritative for the registry
    (the backing store is written with a delay); the config store is read at
    the key derived from the matched registration's item ID.
    """
    from homeassistant.helpers.storage import Store

    url_path = SYSTEM_DASHBOARD_URL_PATH
    if runtime_collection is not None:
        matching = [
            dict(i)
            for i in runtime_collection.data.values()
            if i.get("url_path") == url_path
        ]
    else:
        dashboards_store = Store(
            hass, version=1, key="lovelace_dashboards", minor_version=1
        )
        dashboards_data = await dashboards_store.async_load()
        existing_items = (dashboards_data or {}).get("items", [])
        matching = [i for i in existing_items if i.get("url_path") == url_path]

    config_key_id = (
        (matching[0].get("id") or url_path) if len(matching) == 1 else url_path
    )
    config_store = Store(
        hass, version=1, key=f"lovelace.{config_key_id}", minor_version=1
    )
    stored = await config_store.async_load()
    return matching, stored


async def _remove_system_dashboard_config(hass, item_id: str | None = None) -> None:
    from homeassistant.helpers.storage import Store

    store = Store(
        hass,
        version=1,
        key=f"lovelace.{item_id or SYSTEM_DASHBOARD_URL_PATH}",
        minor_version=1,
    )
    await store.async_remove()


async def _remove_system_dashboard_panel(hass) -> bool:
    """Remove the runtime panel (idempotent, best-effort). Returns success."""
    try:
        from homeassistant.components.frontend import async_remove_panel

        async_remove_panel(hass, SYSTEM_DASHBOARD_URL_PATH)
        return True
    except Exception:  # noqa: BLE001 — panel may already be absent
        _LOGGER.debug(
            "System dashboard panel removal skipped/failed for %s "
            "(may already be absent)",
            SYSTEM_DASHBOARD_URL_PATH,
            exc_info=True,
        )
        return False


async def async_disable_and_remove_system_dashboard(hass) -> str:
    """Persistently suppress and remove the managed System Dashboard (OCD-3).

    Explicit, confirmation-gated (the Options Flow enforces confirmation).
    Serialised by the shared lifecycle lock so a concurrent ensure cannot
    recreate the dashboard mid-removal. Fail-safe: never raises into the
    Options Flow — unexpected failures return ``SYSTEM_DASHBOARD_ERROR``.
    Removal happens only when ownership is safely established; ambiguous
    ownership fails closed with NO suppression change and NO deletion.
    """
    try:
        async with _SYSTEM_DASHBOARD_LOCK:
            return await _disable_and_remove_impl(hass)
    except Exception:  # noqa: BLE001 — never raise into Options Flow
        _LOGGER.warning(
            "System dashboard disable/remove failed unexpectedly", exc_info=True
        )
        return SYSTEM_DASHBOARD_ERROR


async def _disable_and_remove_impl(hass) -> str:
    # Read suppression + stores first; fail closed on unreadable state.
    runtime_collection = _get_runtime_dashboards_collection(hass)
    try:
        already_suppressed = await _read_suppression_state(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "System dashboard: suppression state unreadable — cannot remove",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_READ_ERROR
    try:
        matching, stored = await _read_system_dashboard_stores(
            hass, runtime_collection
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "System dashboard: stores unreadable — cannot remove", exc_info=True
        )
        return SYSTEM_DASHBOARD_READ_ERROR

    match_count = len(matching)
    stored_config = stored.get("config") if isinstance(stored, dict) else None
    version = get_managed_schema_version(stored_config)

    # Ambiguous: duplicate registrations — never pick one arbitrarily.
    if match_count > 1:
        _LOGGER.warning(
            "System dashboard: duplicate registrations — removal fails closed"
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    # A. Completely absent — persist suppression, nothing to delete.
    if match_count == 0 and stored is None:
        await _write_suppression_state(hass, True)
        if already_suppressed:
            await _remove_system_dashboard_panel(hass)
            return SYSTEM_DASHBOARD_ALREADY_REMOVED
        await _remove_system_dashboard_panel(hass)
        return SYSTEM_DASHBOARD_SUPPRESSED_ABSENT

    # Config present without registration.
    if match_count == 0 and stored is not None:
        if already_suppressed and version is not None:
            # Interrupted removal (registry gone, config remains, valid
            # marker). Suppression already persisted — safe to finish.
            await _remove_system_dashboard_config(hass)
            panel_ok = await _remove_system_dashboard_panel(hass)
            return (
                SYSTEM_DASHBOARD_REMOVED
                if panel_ok
                else SYSTEM_DASHBOARD_PARTIAL_REMOVAL
            )
        _LOGGER.warning(
            "System dashboard: stored config without registration and not a "
            "resumable removal — fails closed"
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    # match_count == 1 (registered).
    if stored is None:
        # Registry-only — no config marker proves ownership. Never delete by
        # URL alone, even when suppression is already on.
        _LOGGER.warning(
            "System dashboard: registration without config — ambiguous "
            "ownership, removal fails closed"
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION
    if version is None:
        _LOGGER.warning(
            "System dashboard: registration with invalid/absent marker — "
            "removal fails closed"
        )
        return SYSTEM_DASHBOARD_UNMANAGED_COLLISION

    # B. One registration + valid positive managed marker (current/older/
    # newer): the marker proves PVAutonomy ownership for explicit removal.
    #
    # [WP6B-A1 amendment] Removal is collection-backed ONLY: HA's awaited
    # change listener removes the panel, drops the runtime LovelaceStorage
    # and deletes its config store — persisted and runtime state stay in
    # agreement without a restart. A direct registry-store delete would
    # leave the running collection stale (panel still served; a later
    # collection mutation could resurrect the record) — not fail-closed.
    # Availability is therefore proven BEFORE any mutation, including the
    # suppression write: an unavailable collection aborts with NO change.
    item_id = matching[0].get("id")
    if (
        runtime_collection is None
        or not item_id
        or item_id not in runtime_collection.data
    ):
        _LOGGER.warning(
            "System dashboard: live lovelace dashboards collection "
            "unavailable or record unknown to it — removal aborted "
            "fail-closed (no change; retry via Options Flow)"
        )
        return SYSTEM_DASHBOARD_ERROR

    # Persist suppression FIRST (so a concurrent/later ensure cannot
    # recreate mid-removal), then delete through the collection.
    if not already_suppressed:
        await _write_suppression_state(hass, True)
    try:
        await runtime_collection.async_delete_item(item_id)
    except Exception:  # noqa: BLE001 — listener-phase partial state
        # HA pops the item BEFORE notifying listeners, so an exception here
        # means the registration is gone while runtime object/panel may
        # linger. Suppression is persisted (ensure cannot recreate) and the
        # existing remnant-cleanup paths converge the rest on retry.
        _LOGGER.warning(
            "System dashboard: runtime removal failed after registration "
            "delete — partial removal (suppressed; retry via Disable or "
            "Enable remnant cleanup)",
            exc_info=True,
        )
        return SYSTEM_DASHBOARD_PARTIAL_REMOVAL
    _LOGGER.info("System dashboard removed and suppressed (OCD-3, runtime)")
    return SYSTEM_DASHBOARD_REMOVED


async def async_enable_system_dashboard(hass) -> str:
    """Clear suppression and regenerate the System Dashboard (OCD-3).

    Serialised by the shared lock. Clears suppression persistently, then
    runs the normal (lock-free) ensure implementation so the dashboard is
    (re)created at the current schema. If ensure fails, suppression stays
    cleared and the error is surfaced (a later setup/reload retries).
    """
    try:
        async with _SYSTEM_DASHBOARD_LOCK:
            try:
                suppressed = await _read_suppression_state(hass)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "System dashboard: suppression state unreadable — cannot "
                    "enable",
                    exc_info=True,
                )
                return SYSTEM_DASHBOARD_READ_ERROR
            if suppressed:
                # [WP3 review B1] Pre-clear classification with the SHARED
                # store reader/parser: Enable must never convert an
                # operation-created resumable removal remnant into a wedged
                # enabled collision, and must never clear suppression while
                # ownership is ambiguous.
                try:
                    matching, stored = await _read_system_dashboard_stores(
                        hass, _get_runtime_dashboards_collection(hass)
                    )
                except Exception:  # noqa: BLE001 — fail closed, keep suppressed
                    _LOGGER.warning(
                        "System dashboard: stores unreadable — cannot enable "
                        "(suppression retained)",
                        exc_info=True,
                    )
                    return SYSTEM_DASHBOARD_READ_ERROR
                stored_config = (
                    stored.get("config") if isinstance(stored, dict) else None
                )
                version = get_managed_schema_version(stored_config)
                if len(matching) > 1:
                    _LOGGER.warning(
                        "System dashboard: duplicate registrations — enable "
                        "fails closed (suppression retained)"
                    )
                    return SYSTEM_DASHBOARD_UNMANAGED_COLLISION
                if stored is not None and version is None:
                    _LOGGER.warning(
                        "System dashboard: stored config without a valid "
                        "managed marker — enable fails closed (suppression "
                        "retained)"
                    )
                    return SYSTEM_DASHBOARD_UNMANAGED_COLLISION
                if len(matching) == 1 and stored is None:
                    _LOGGER.warning(
                        "System dashboard: registration without config — "
                        "enable fails closed (suppression retained)"
                    )
                    return SYSTEM_DASHBOARD_UNMANAGED_COLLISION
                if len(matching) == 0 and stored is not None:
                    # Operation-created resumable removal remnant (valid
                    # positive marker proves ownership; suppression is true).
                    # Complete the persistent cleanup BEFORE clearing
                    # suppression; any cleanup failure keeps suppression true
                    # and stays retryable via Enable or Disable.
                    try:
                        await _remove_system_dashboard_config(hass)
                    except Exception:  # noqa: BLE001 — keep suppressed
                        _LOGGER.warning(
                            "System dashboard: removal-remnant cleanup "
                            "failed — enable aborted (suppression retained)",
                            exc_info=True,
                        )
                        return SYSTEM_DASHBOARD_ERROR
                    if not await _remove_system_dashboard_panel(hass):
                        _LOGGER.warning(
                            "System dashboard: removal-remnant panel cleanup "
                            "failed — enable aborted (suppression retained)"
                        )
                        return SYSTEM_DASHBOARD_PARTIAL_REMOVAL
                # Remaining states: fully absent, or a complete valid managed
                # dashboard (failed removal that never got past suppression).
                # Both are safe: clear suppression and run normal Ensure.
                await _write_suppression_state(hass, False)
            # Lock-free impl (we already hold the lock — no recursion).
            outcome = await _ensure_system_dashboard_impl(hass)
            if outcome in (
                SYSTEM_DASHBOARD_CREATED,
                SYSTEM_DASHBOARD_CURRENT,
                SYSTEM_DASHBOARD_REGENERATED,
            ):
                return SYSTEM_DASHBOARD_ENABLED
            return outcome  # collision / read_error / error surfaced as-is
    except Exception:  # noqa: BLE001 — never raise into Options Flow
        _LOGGER.warning(
            "System dashboard enable failed unexpectedly", exc_info=True
        )
        return SYSTEM_DASHBOARD_ERROR


def build_dashboard_config(
    device_name: str,
    display_title: str,
    registry: dict[str, Any],
    *,
    existing_entity_ids: set[str] | None = None,
    live_entity_ids: set[str] | None = None,
    mac_suffix: str | None = None,
    entry_id: str | None = None,
    selected_tier: str | None = None,
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
        selected_tier=selected_tier,
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

    # [M2/#168 WP1] Per-device dashboards stay single-view and marker-free;
    # the shared constructor keeps their payload identical to the pre-WP1
    # output.
    return build_lovelace_payload([view])


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
    and ``model_slug`` are accepted to keep observed callsites compatible and are
    reserved for future dashboard policy. ``selected_tier`` (from
    ``entry.options[CONF_SELECTED_TIER]``) IS used: on a Standard-tier device the
    builder drops extended-only controls and suppresses the Export-Limit card
    (issues #67/#68); Extended tier is unchanged (#55 preserved).

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
                selected_tier=selected_tier,
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
    selected_tier: str | None = None,
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
    # [WP6B-A1 amendment] The registry is read from (and mutated through)
    # the live dashboards collection, exactly like the System Dashboard:
    # the collection's in-memory data is the in-process truth (its store
    # write is delayed), and a direct registry write is invisible to the
    # running boot AND clobbered by the next collection flush — live-proven
    # during WP6B-A1 E4, where a directly-registered device dashboard was
    # erased by the system dashboard's collection-backed removal.
    runtime_collection = _get_runtime_dashboards_collection(hass)
    if runtime_collection is not None:
        matching_records = [
            dict(item)
            for item in runtime_collection.data.values()
            if item.get("url_path") == url_path
        ]
    else:
        dashboards_store = Store(
            hass, version=1, key="lovelace_dashboards", minor_version=1
        )
        dashboards_data = await dashboards_store.async_load()
        existing_items = (dashboards_data or {}).get("items", [])
        matching_records = [
            item for item in existing_items if item.get("url_path") == url_path
        ]
    already_registered = len(matching_records) > 0
    registered_item_id = (
        matching_records[0].get("id") if matching_records else None
    )

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
        selected_tier=selected_tier,
    )

    # --- Step 1: Add registry entry only if not yet present ---
    # [WP6B-A1 amendment] Creation is collection-backed ONLY (no direct
    # registry-store write). Unavailable collection -> fail-closed skip:
    # nothing is written (a config store without a servable registration
    # would be an orphan); the next refresh/setup retries.
    created_item = None
    if not already_registered:
        if runtime_collection is None:
            _LOGGER.warning(
                "Device dashboard %s not created: live lovelace dashboards "
                "collection unavailable — fail-closed (no write; retry via "
                "Refresh or the next setup)",
                url_path,
            )
            return False
        created_item = await runtime_collection.async_create_item(
            {
                "url_path": url_path,
                "title": sidebar_title,
                "icon": "mdi:solar-power",
                "show_in_sidebar": True,
                "require_admin": False,
            }
        )
        registered_item_id = created_item.get("id")
        _LOGGER.info(
            "Dashboard entry created (runtime-registered): %s (%s)",
            url_path,
            sidebar_title,
        )
    else:
        _LOGGER.info(
            "Dashboard entry '%s' already registered — refreshing config only",
            url_path,
        )

    # --- Step 2: Write view/card config ---
    # [issue #67] Prefer writing THROUGH Home Assistant's in-memory
    # LovelaceStorage object: that updates HA's cached config AND fires the
    # ``lovelace_updated`` event, so open frontends reload the dashboard
    # automatically — no HA restart and no manual browser reload after a
    # "Dashboard aktualisieren" / tier re-flash. A direct Store write (the
    # historical path) leaves HA's cached config stale until a restart, so the
    # corrected dashboard only appears after a restart. This uses HA-internal
    # lovelace data structures, so it is fail-safe: ANY problem falls back to the
    # direct Store write — i.e. exactly the previous behaviour, never worse.
    lovelace_config = config["data"]["config"]
    num_cards = len(lovelace_config["views"][0]["cards"])
    saved_via_lovelace = False
    try:
        lovelace_data = hass.data.get("lovelace")
        dashboards = getattr(lovelace_data, "dashboards", None)
        if dashboards is None and isinstance(lovelace_data, dict):
            dashboards = lovelace_data.get("dashboards")
        dashboard_obj = dashboards.get(url_path) if dashboards else None
        if dashboard_obj is not None and hasattr(dashboard_obj, "async_save"):
            await dashboard_obj.async_save(lovelace_config)
            saved_via_lovelace = True
            _LOGGER.info(
                "Dashboard config saved via LovelaceStorage for %s (%d cards) "
                "— open clients auto-reload (no restart needed)",
                url_path,
                num_cards,
            )
    except Exception:  # noqa: BLE001 — HA-internal API; fall back below
        _LOGGER.debug(
            "LovelaceStorage save path unavailable for %s; falling back to "
            "direct store write",
            url_path,
            exc_info=True,
        )
        saved_via_lovelace = False

    if not saved_via_lovelace:
        if created_item is not None:
            # [WP6B-A1 amendment] Fresh collection-backed creation whose
            # runtime config save is impossible: roll the registration back
            # instead of writing a config the running boot cannot serve.
            _LOGGER.warning(
                "Device dashboard %s: runtime config save unavailable — "
                "rolling back the fresh registration (fail-closed)",
                url_path,
            )
            try:
                await runtime_collection.async_delete_item(created_item["id"])
            except Exception:  # noqa: BLE001 — surfaced via False
                _LOGGER.warning(
                    "Device dashboard %s: rollback of the runtime "
                    "registration failed",
                    url_path,
                    exc_info=True,
                )
            return False
        # Already-registered dashboard: content-only refresh of an existing
        # registration (registry untouched); key follows the item ID.
        config_store = Store(
            hass,
            version=1,
            key=f"lovelace.{registered_item_id or url_path}",
            minor_version=1,
        )
        await config_store.async_save(config["data"])
        _LOGGER.info(
            "Dashboard config written (direct store) for %s (%d cards) "
            "— reload the page to see changes",
            url_path,
            num_cards,
        )

    # --- Step 3: Panel visibility ---
    # Fresh creations are panel-registered by the collection listener; only
    # an already-registered dashboard may need the best-effort registration
    # (e.g. panel dropped manually in the same boot).
    if created_item is None:
        try:
            from homeassistant.components.frontend import (
                async_register_built_in_panel,
            )

            async_register_built_in_panel(
                hass,
                component_name="lovelace",
                frontend_url_path=url_path,
                sidebar_title=sidebar_title,
                sidebar_icon="mdi:solar-power",
                config={"mode": "storage"},
                require_admin=False,
            )
            _LOGGER.info(
                "Panel registered for immediate sidebar visibility: %s", url_path
            )
        except Exception:  # noqa: BLE001 — panel already present is fine
            _LOGGER.debug(
                "Panel registration skipped for %s (already present)",
                url_path,
                exc_info=True,
            )

    return True
