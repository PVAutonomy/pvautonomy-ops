"""Entity cleanup for prefix-split detection and resolution (EPIC-011 Phase 2).

When an ESPHome device is renamed (slug change), Home Assistant creates new
entities with the new prefix but keeps old ones as orphans.  This module
detects the split and provides disable/delete operations scoped to a single
PVAutonomy-managed device.

Also provides registry-driven guardrail pass (TASK-20260327-SPH10K-HA-ENTITY-GUARDRAILS):
automatically disable entities that the inverter registry marks as
``enabled_by_default: false`` for the current device after initial setup/reflash.

Safety stance: disable-first, delete only with explicit confirmation.
Ref: WORKER-PROMPT-EPIC-011-PHASE-2-CLEANUP-ENTITIES.md
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .defs_paths import DefsNotFoundError, resolve_registry_root

_LOGGER = logging.getLogger(__name__)

# Regex to extract device prefix from entity object_id.
# Matches the slug portion: {model}_{site}_{nn} (2-digit number is anchor).
# Lazy match ensures the first *_\d{2} wins.
_PREFIX_RE = re.compile(r"^(.*?_\d{2})_")

# PVAutonomy entities end with _device (Ops Contract v1 naming convention).
_PVA_SUFFIX = "_device"

# Maximum entities shown in persistent notification report.
REPORT_MAX_ENTITIES = 20


# ── Data classes ──────────────────────────────────────────────────


@dataclass
class CleanupCandidate:
    """A single entity eligible for cleanup."""

    entity_id: str
    domain: str
    unique_id: str | None
    prefix: str
    disabled_by: str | None  # None, "user", "integration", …


@dataclass
class CleanupPlan:
    """Detection result for a prefix-split cleanup."""

    config_entry_id: str
    ha_device_id: str
    current_prefix: str
    old_prefixes: list[str] = field(default_factory=list)
    candidates: list[CleanupCandidate] = field(default_factory=list)

    @property
    def has_split(self) -> bool:
        return len(self.candidates) > 0

    def counts_by_prefix(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.candidates:
            counts[c.prefix] = counts.get(c.prefix, 0) + 1
        return counts


@dataclass
class CleanupResult:
    """Result of a cleanup execution (disable, enable, and/or delete)."""

    disabled_count: int = 0
    enabled_count: int = 0
    deleted_count: int = 0
    skipped_count: int = 0  # already disabled by user → not touched
    disabled_entities: list[str] = field(default_factory=list)
    enabled_entities: list[str] = field(default_factory=list)
    deleted_entities: list[str] = field(default_factory=list)
    skipped_entities: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────


def extract_prefix(entity_id: str) -> str | None:
    """Extract the device prefix from an entity_id.

    ``sensor.sph10k_haus_03_battery_soc_device`` → ``sph10k_haus_03``

    Returns None if the pattern does not match.
    """
    parts = entity_id.split(".", 1)
    if len(parts) != 2:
        return None
    match = _PREFIX_RE.match(parts[1])
    return match.group(1) if match else None


def is_pva_entity(entity_id: str) -> bool:
    """Return True if *entity_id* follows the PVAutonomy ``*_device`` convention."""
    return entity_id.endswith(_PVA_SUFFIX)


# ── Detection (R1) ───────────────────────────────────────────────


def detect_prefix_split(
    hass: HomeAssistant,
    config_entry_id: str,
    current_slug: str,
    ha_device_id: str | None = None,
) -> CleanupPlan | None:
    """Detect prefix split for a PVAutonomy-managed device.

    Args:
        hass: Home Assistant instance.
        config_entry_id: PVAutonomy config entry ID.
        current_slug: Current immutable device slug (e.g. ``sph10k-haus-02``).
        ha_device_id: HA device registry ID (optional, resolved from entry if missing).

    Returns:
        ``CleanupPlan`` (may have ``candidates=[]`` if no split), or
        ``None`` if the HA device cannot be resolved (fail-closed).
    """
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    # Resolve HA device_id if not provided.
    if not ha_device_id:
        for device in device_reg.devices.values():
            if config_entry_id in device.config_entries:
                ha_device_id = device.id
                break

    if not ha_device_id:
        _LOGGER.info(
            "EPIC-011 cleanup: cannot resolve device for config_entry %s",
            config_entry_id,
        )
        return None

    current_prefix = current_slug.replace("-", "_")
    device_entities = er.async_entries_for_device(entity_reg, ha_device_id)

    old_prefixes: set[str] = set()
    candidates: list[CleanupCandidate] = []

    for entry in device_entities:
        if not is_pva_entity(entry.entity_id):
            continue

        prefix = extract_prefix(entry.entity_id)
        if not prefix:
            continue

        # Case-insensitive comparison (FU-EPIC011-1).
        if prefix.lower() == current_prefix.lower():
            continue  # belongs to current generation

        old_prefixes.add(prefix)
        candidates.append(
            CleanupCandidate(
                entity_id=entry.entity_id,
                domain=entry.domain,
                unique_id=entry.unique_id,
                prefix=prefix,
                disabled_by=(
                    entry.disabled_by.value
                    if entry.disabled_by is not None
                    else None
                ),
            )
        )

    plan = CleanupPlan(
        config_entry_id=config_entry_id,
        ha_device_id=ha_device_id,
        current_prefix=current_prefix,
        old_prefixes=sorted(old_prefixes),
        candidates=candidates,
    )

    _LOGGER.info(
        "EPIC-011 cleanup detection: device=%s, current=%s, "
        "old_prefixes=%s, candidates=%d",
        ha_device_id,
        current_prefix,
        plan.old_prefixes,
        len(candidates),
    )
    return plan


# ── Execution (R3) ───────────────────────────────────────────────


def disable_entities(
    hass: HomeAssistant,
    entity_ids: list[str],
) -> CleanupResult:
    """Disable entities via entity registry (``disabled_by=INTEGRATION``).

    Only touches entities whose ``disabled_by`` is ``None``.
    User-disabled entities are skipped (Planner constraint).
    """
    entity_reg = er.async_get(hass)
    result = CleanupResult()

    for entity_id in entity_ids:
        entry = entity_reg.async_get(entity_id)
        if not entry:
            result.errors.append(f"Not found: {entity_id}")
            continue

        if entry.disabled_by is not None:
            result.skipped_count += 1
            result.skipped_entities.append(entity_id)
            continue

        try:
            entity_reg.async_update_entity(
                entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            result.disabled_count += 1
            result.disabled_entities.append(entity_id)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"Disable failed ({entity_id}): {exc}")

    _LOGGER.info(
        "EPIC-011 cleanup disable: disabled=%d, skipped=%d, errors=%d",
        result.disabled_count,
        result.skipped_count,
        len(result.errors),
    )
    return result


def delete_entities(
    hass: HomeAssistant,
    entity_ids: list[str],
) -> CleanupResult:
    """Delete entities from entity registry (irreversible).

    Call only after explicit user confirmation.
    """
    entity_reg = er.async_get(hass)
    result = CleanupResult()

    for entity_id in entity_ids:
        entry = entity_reg.async_get(entity_id)
        if not entry:
            result.errors.append(f"Not found: {entity_id}")
            continue

        try:
            entity_reg.async_remove(entity_id)
            result.deleted_count += 1
            result.deleted_entities.append(entity_id)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"Delete failed ({entity_id}): {exc}")

    _LOGGER.info(
        "EPIC-011 cleanup delete: deleted=%d, errors=%d",
        result.deleted_count,
        len(result.errors),
    )
    return result


def _iter_entity_registry_entries(entity_reg) -> list[object]:
    """Return entity registry entries across HA versions and test doubles."""
    entities = getattr(entity_reg, "entities", None)
    if isinstance(entities, dict):
        return list(entities.values())

    entries = getattr(entity_reg, "_entries", None)
    if isinstance(entries, dict):
        return list(entries.values())

    return []


def find_stale_grid_first_time_entities(
    hass: HomeAssistant,
    device_name: str,
    config_entry_id: str,
) -> list[str]:
    """Find stale draft time entities left by earlier Grid First experiments."""
    entity_reg = er.async_get(hass)
    stale_ids: list[str] = []
    canonical_ids = {
        f"time.{device_name}_grid_first_timeslot_1_start_time",
        f"time.{device_name}_grid_first_timeslot_1_stop_time",
    }
    generic_ids = {
        "time.grid_first_slot_1_start",
        "time.grid_first_slot_1_stop",
    }

    for entry in _iter_entity_registry_entries(entity_reg):
        entity_id = getattr(entry, "entity_id", "")
        if not entity_id.startswith("time."):
            continue
        if getattr(entry, "platform", "") != "pvautonomy_ops":
            continue
        if entity_id in canonical_ids:
            continue
        if entity_id in generic_ids or (
            entity_id.startswith(f"time.{device_name}_grid_first_timeslot_1_")
            and entity_id.endswith("_time_2")
        ):
            stale_ids.append(entity_id)

    return stale_ids


def heal_grid_first_time_entities(
    hass: HomeAssistant,
    device_name: str,
    config_entry_id: str,
) -> CleanupResult:
    """Delete stale Grid First time entity rows before canonical entities are added."""
    stale_ids = find_stale_grid_first_time_entities(hass, device_name, config_entry_id)
    if not stale_ids:
        return CleanupResult()
    return delete_entities(hass, stale_ids)


# ── Non-battery wrapper cleanup (Issue #108) ─────────────────────


def find_unsupported_wrapper_entities(
    hass: HomeAssistant,
    device_name: str,
    config_entry_id: str | None = None,
) -> list[str]:
    """Find unsupported priority/grid-first wrapper entities for non-battery devices.

    Checks for stale or newly-blocked control wrapper entities that must not
    exist for devices where ``features.battery_storage`` is False (e.g. MIC600).
    Also includes the retired ``export_limit_toggle_device`` orphan.

    Only entities with ``platform == 'pvautonomy_ops'`` are considered;
    entities from other integrations with coincidentally matching IDs are left
    untouched.

    Idempotent and safe to call even when no stale entities are present.
    """
    entity_reg = er.async_get(hass)
    candidate_ids = [
        f"switch.{device_name}_load_first_activate_device",
        f"switch.{device_name}_battery_first_activate_device",
        f"switch.{device_name}_grid_first_schedule_enabled_draft",
        f"time.{device_name}_grid_first_timeslot_1_start_time",
        f"time.{device_name}_grid_first_timeslot_1_stop_time",
        f"switch.{device_name}_export_limit_toggle_device",  # retired orphan
    ]
    found: list[str] = []
    for entity_id in candidate_ids:
        entry = entity_reg.async_get(entity_id)
        if entry is None:
            continue
        if entry.platform != "pvautonomy_ops":
            continue
        found.append(entity_id)
    return found


def cleanup_unsupported_wrapper_entities(
    hass: HomeAssistant,
    device_name: str,
    config_entry_id: str | None = None,
) -> CleanupResult:
    """Delete stale unsupported wrapper entities for non-battery devices.

    Removes priority-mode and grid-first control wrapper entities that must
    not exist when ``features.battery_storage`` is False (e.g. MIC600).
    Also removes the retired ``export_limit_toggle_device`` orphan if present.

    Idempotent: safe to call multiple times; entities already removed
    produce no error.
    """
    stale_ids = find_unsupported_wrapper_entities(hass, device_name, config_entry_id)
    if not stale_ids:
        return CleanupResult()
    result = delete_entities(hass, stale_ids)
    _LOGGER.info(
        "Non-battery wrapper cleanup: deleted=%d, errors=%d (device=%s)",
        result.deleted_count,
        len(result.errors),
        device_name,
    )
    return result


# ── Report (R4) ──────────────────────────────────────────────────


def generate_report(
    plan: CleanupPlan,
    result: CleanupResult,
    max_entities: int = REPORT_MAX_ENTITIES,
) -> str:
    """Generate a human-readable markdown cleanup report.

    Counts and old prefixes come first; entity lists are bounded to
    *max_entities* to avoid oversized persistent notifications.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "## PVAutonomy Entity Cleanup Report",
        f"**Date:** {now}",
        f"**Device:** `{plan.ha_device_id}`",
        f"**Current prefix:** `{plan.current_prefix}`",
        f"**Old prefixes:** {', '.join(f'`{p}`' for p in plan.old_prefixes)}",
        "",
        "### Summary",
        f"- Disabled: **{result.disabled_count}**",
        f"- Deleted: **{result.deleted_count}**",
        f"- Skipped (already disabled): **{result.skipped_count}**",
    ]

    if result.errors:
        lines.append(f"- Errors: **{len(result.errors)}**")

    def _bounded_list(title: str, items: list[str]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"### {title}")
        for item in items[:max_entities]:
            lines.append(f"- `{item}`")
        overflow = len(items) - max_entities
        if overflow > 0:
            lines.append(f"- \u2026 and {overflow} more")

    _bounded_list("Disabled entities", result.disabled_entities)
    _bounded_list("Deleted entities", result.deleted_entities)

    if result.errors:
        lines.append("")
        lines.append("### Errors")
        for err in result.errors[:10]:
            lines.append(f"- {err}")
        if len(result.errors) > 10:
            lines.append(f"- \u2026 and {len(result.errors) - 10} more")

    lines.append("")
    lines.append(
        "**Next steps:** Check dashboards and automations "
        "for references to old entity IDs."
    )
    return "\n".join(lines)


# ── Registry-driven guardrails (TASK-20260327) ──────────────────


# Registry section → HA domain mapping.
_REGISTRY_DOMAIN_MAP: dict[str, str] = {
    "sensors": "sensor",
    "numbers": "number",
    "switches": "switch",
    "selects": "select",
}

# Registry root is resolved by the shared resolver (bundle-only, fail-closed)
# — see defs_paths.py.


def load_guardrail_candidates(
    registry_file: str,
    device_prefix: str,
    *,
    registry_root: Path | None = None,
) -> list[str]:
    """Derive HA entity IDs that should be disabled by default in HA.

    Pure function — reads the registry JSON and computes expected entity IDs
    for a specific device prefix.  No HA runtime needed.

    Args:
        registry_file: Relative path inside inverter-registry/
            (e.g. ``growatt/sph/sph10k.json``).
        device_prefix: Underscore-form slug (e.g. ``sph10k_haus_03``).
        registry_root: Override for the registry base directory.

    Returns:
        Sorted list of entity IDs (e.g.
        ``["number.sph10k_haus_03_battery_stop_discharge_soc_device", …]``).
        Empty list on any load error (fail-closed).
    """
    path = _resolve_registry_path(registry_file, registry_root)
    if path is None:
        _LOGGER.warning(
            "Guardrail: registry file not found: %s (fail-closed)", registry_file,
        )
        return []

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning("Guardrail: cannot load registry %s: %s", path, exc)
        return []

    registers = data.get("registers")
    if not isinstance(registers, dict):
        return []

    candidates: list[str] = []
    for section_key, ha_domain in _REGISTRY_DOMAIN_MAP.items():
        entries = registers.get(section_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            should_disable = (
                entry.get("disabled_by_default") is True
                or entry.get("enabled_by_default") is False
            )
            if not should_disable:
                continue
            reg_id = entry.get("id")
            if reg_id:
                entity_id = f"{ha_domain}.{device_prefix}_{reg_id}_device"
                candidates.append(entity_id)

    candidates.sort()
    return candidates


def load_guardrail_states(
    registry_file: str,
    device_prefix: str,
    *,
    registry_root: Path | None = None,
) -> dict[str, bool]:
    """Return desired default enable-state for one device's registry entities.

    The returned mapping is ``entity_id -> should_disable`` and is used to
    reconcile integration-disabled entities on repeated setup/reflash. This
    lets PVAutonomy automatically re-enable entities that were previously
    guarded behind ``enabled_by_default: false`` once the registry promotes
    them into the customer-facing contract.
    """
    path = _resolve_registry_path(registry_file, registry_root)
    if path is None:
        _LOGGER.warning(
            "Guardrail: registry file not found: %s (fail-closed)", registry_file,
        )
        return {}

    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning("Guardrail: cannot load registry %s: %s", path, exc)
        return {}

    registers = data.get("registers")
    if not isinstance(registers, dict):
        return {}

    states: dict[str, bool] = {}
    for section_key, ha_domain in _REGISTRY_DOMAIN_MAP.items():
        entries = registers.get(section_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            reg_id = entry.get("id")
            if not reg_id:
                continue
            entity_id = f"{ha_domain}.{device_prefix}_{reg_id}_device"
            states[entity_id] = (
                entry.get("disabled_by_default") is True
                or entry.get("enabled_by_default") is False
            )

    return states


def apply_entity_guardrails(
    hass: HomeAssistant,
    candidates: list[str] | dict[str, bool],
    ha_device_id: str,
) -> CleanupResult:
    """Reconcile guardrail candidates scoped to one HA device.

    Only entities that:
    1. exist in the entity registry,
    2. belong to *ha_device_id*, and
    3. are currently enabled (``disabled_by is None``)

    are disabled (``disabled_by=INTEGRATION``) when the registry still marks
    them as gated. Integration-disabled entities that are no longer gated are
    re-enabled automatically. User-disabled entities are never re-enabled.

    Args:
        hass: Home Assistant instance.
        candidates: Either a legacy entity-id list from
            :func:`load_guardrail_candidates` (disable-only) or a mapping of
            ``entity_id -> should_disable`` from :func:`load_guardrail_states`.
        ha_device_id: HA device registry ID (scope guard).

    Returns:
        :class:`CleanupResult` with counts and entity lists.
    """
    entity_reg = er.async_get(hass)
    result = CleanupResult()

    if isinstance(candidates, dict):
        desired_states = candidates
    else:
        desired_states = {entity_id: True for entity_id in candidates}

    for entity_id, should_disable in desired_states.items():
        entry = entity_reg.async_get(entity_id)
        if not entry:
            # Entity doesn't exist in HA yet — skip silently.
            continue

        # Scope guard: only touch entities belonging to this device.
        if entry.device_id != ha_device_id:
            continue

        try:
            if should_disable:
                if entry.disabled_by is not None:
                    result.skipped_count += 1
                    result.skipped_entities.append(entity_id)
                    continue

                entity_reg.async_update_entity(
                    entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
                result.disabled_count += 1
                result.disabled_entities.append(entity_id)
            elif entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
                entity_reg.async_update_entity(entity_id, disabled_by=None)
                result.enabled_count += 1
                result.enabled_entities.append(entity_id)
        except Exception as exc:  # noqa: BLE001
            action = "disable" if should_disable else "enable"
            result.errors.append(f"Guardrail {action} failed ({entity_id}): {exc}")

    _LOGGER.info(
        "Guardrail pass: disabled=%d, enabled=%d, skipped=%d, errors=%d (device=%s)",
        result.disabled_count,
        result.enabled_count,
        result.skipped_count,
        len(result.errors),
        ha_device_id[:8] if ha_device_id else "?",
    )
    return result


def _resolve_registry_path(
    registry_file: str,
    registry_root: Path | None = None,
) -> Path | None:
    """Resolve the absolute path to a registry JSON file.

    Returns None if the file does not exist (fail-closed).
    """
    if registry_root is not None:
        p = registry_root / registry_file
        return p if p.is_file() else None

    try:
        root = resolve_registry_root()
    except DefsNotFoundError:
        return None
    p = root / registry_file
    return p if p.is_file() else None
