"""Entity Registry Reset — Unload → Remove → Reload for clean entity IDs.

After a Reconfigure/rename, ESPHome entities keep their old HA entity IDs
because the Entity Registry matches by unique_id (MAC-based). This module
forces fresh registration by:

1. Collecting all entity registry entries for the target config_entry
2. Unloading the config entry
3. Removing those entries from the entity registry
4. Purging deleted_entities tombstones (prevents HA from restoring old entity_ids)
5. Reloading the config entry → HA creates entities with current friendly_name prefix

The device_id remains stable (Device Registry is NOT touched).

Root cause: HA Core's async_remove() creates a "tombstone" in deleted_entities
keyed by (domain, platform, unique_id). When the platform re-discovers the same
unique_id, async_get_or_create() restores the OLD entity_id from this tombstone.
Tombstones with config_entry_id set have orphaned_timestamp=None, meaning they
live FOREVER (never auto-purged). Our hard reset must explicitly delete them.

See: WORKER-PROMPT-ENTITY-REGISTRY-HARD-RESET-RENAME-ISSUE-PR-TEMPLATE.md
Ref: BLOCKER-REPORT-entity-naming-convention-violation.md
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Legacy / double-prefix indicators
_VIOLATION_PREFIXES = ("pvautonomy_",)


@dataclass
class ResetReport:
    """Structured report from hard_reset_entities_for_config_entry().

    Fields match the Worker Prompt specification:
    - deleted: entity_ids successfully removed + tombstones purged
    - skipped: entity_ids that were skipped (wrong config_entry, etc.)
    - errors: entity_ids that failed with reasons
    """

    config_entry_id: str = ""
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    tombstones_purged: int = 0
    tombstones_remaining: int = 0
    dry_run: bool = False
    duration_s: float = 0.0

    @property
    def success(self) -> bool:
        """True if at least one entity was deleted and no errors occurred."""
        return len(self.deleted) > 0 and len(self.errors) == 0

    def summary(self) -> dict[str, Any]:
        return {
            "config_entry_id": self.config_entry_id,
            "deleted": len(self.deleted),
            "skipped": len(self.skipped),
            "errors": len(self.errors),
            "tombstones_purged": self.tombstones_purged,
            "tombstones_remaining": self.tombstones_remaining,
            "dry_run": self.dry_run,
            "duration_s": round(self.duration_s, 2),
        }


@dataclass
class ResetResult:
    """Result of an entity registry reset operation (legacy wrapper)."""

    success: bool = False
    mode: str = "full_reset"            # "full_reset" | "violations_only"
    dry_run: bool = False
    removed_count: int = 0
    re_registered_count: int = 0
    violations_found: int = 0
    device_id_before: str | None = None
    device_id_after: str | None = None
    device_id_stable: bool = True
    old_entity_ids: list[str] = field(default_factory=list)
    new_entity_ids: list[str] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "removed": self.removed_count,
            "re_registered": self.re_registered_count,
            "violations_found": self.violations_found,
            "device_id_stable": self.device_id_stable,
            "error": self.error,
            "duration_s": round(self.duration_s, 1),
        }


def _collect_entries_for_config_entry(
    hass: HomeAssistant,
    config_entry_id: str,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    """Collect entity registry entries for a config entry.

    Args:
        hass: Home Assistant instance.
        config_entry_id: ESPHome config entry ID.
        device_id: Optional guard — only entries matching this device_id.

    Returns:
        List of dicts with entity_id and unique_id.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries = []

    for entry in registry.entities.values():
        if entry.config_entry_id != config_entry_id:
            continue
        if device_id and entry.device_id != device_id:
            continue
        entries.append({
            "entity_id": entry.entity_id,
            "unique_id": entry.unique_id,
            "platform": entry.platform,
            "device_id": entry.device_id,
        })

    return entries


def _has_naming_violation(entity_id: str, expected_prefix: str) -> bool:
    """Check if an entity_id violates the naming convention.

    Detects:
    - Legacy prefix (pvautonomy_*)
    - Double-prefix (expected_prefix appearing twice)
    - Missing expected prefix entirely
    """
    # Strip domain (sensor. / switch. etc.)
    _, _, object_id = entity_id.partition(".")

    # Legacy prefix
    for bad in _VIOLATION_PREFIXES:
        if object_id.startswith(bad):
            return True

    # Double-prefix
    if expected_prefix and f"{expected_prefix}{expected_prefix}" in object_id:
        return True

    return False


def _resolve_esphome_config_entry(
    hass: HomeAssistant,
    device_id: str,
) -> str | None:
    """Find the ESPHome config_entry_id for a device.

    Walks the device registry to find a config entry with domain='esphome'
    linked to the given device_id.
    """
    from homeassistant.helpers import device_registry as dr

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return None

    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == "esphome":
            return entry_id

    return None


def _count_tombstones_for_unique_ids(
    hass: HomeAssistant,
    unique_ids: set[str],
) -> int:
    """Count how many deleted_entities tombstones match the given unique_ids."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    if not hasattr(registry, "deleted_entities"):
        return 0

    count = 0
    for _key, deleted_entry in registry.deleted_entities.items():
        uid = getattr(deleted_entry, "unique_id", None)
        if uid and uid in unique_ids:
            count += 1
    return count


async def hard_reset_entities_for_config_entry(
    hass: HomeAssistant,
    config_entry_id: str,
    *,
    dry_run: bool = False,
) -> ResetReport:
    """Hard-reset all entities for a config entry — remove + purge tombstones.

    This is the strict, scoped reset that ensures HA cannot restore old
    entity_ids after a device rename. Unlike reset_entity_registry(), this
    function:
    - Requires config_entry_id (no device_id resolution)
    - Removes ALL entities for the config entry (no device_id filter)
    - Explicitly purges ALL tombstones and verifies none remain
    - Returns a structured ResetReport with deleted/skipped/errors

    The caller is responsible for unloading before and reloading after.

    Args:
        hass: Home Assistant instance.
        config_entry_id: ESPHome config entry ID (required).
        dry_run: If True, preview only — no modifications.

    Returns:
        ResetReport with deleted, skipped, errors, tombstone counts.
    """
    report = ResetReport(config_entry_id=config_entry_id, dry_run=dry_run)
    start_time = time.monotonic()

    _LOGGER.info(
        "entity_reset: starting hard reset for config_entry_id=%s",
        config_entry_id,
    )

    # ── Validate config entry exists ──
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if not entry:
        report.errors.append({
            "entity_id": "(config_entry)",
            "reason": f"Config entry {config_entry_id} not found",
        })
        report.duration_s = time.monotonic() - start_time
        return report

    # ── Collect ALL entities for this config entry (no device_id filter) ──
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries_to_remove = []
    for reg_entry in registry.entities.values():
        if reg_entry.config_entry_id != config_entry_id:
            continue
        entries_to_remove.append({
            "entity_id": reg_entry.entity_id,
            "unique_id": reg_entry.unique_id,
            "platform": reg_entry.platform,
            "device_id": reg_entry.device_id,
        })

    total = len(entries_to_remove)
    _LOGGER.info(
        "entity_reset: starting hard reset for config_entry_id=%s total_entities=%d",
        config_entry_id,
        total,
    )

    if total == 0:
        _LOGGER.info(
            "entity_reset: no entities found for config_entry_id=%s — nothing to reset",
            config_entry_id,
        )
        report.duration_s = time.monotonic() - start_time
        return report

    # ── Dry-run: preview only ──
    if dry_run:
        report.deleted = [e["entity_id"] for e in entries_to_remove]
        report.duration_s = time.monotonic() - start_time
        _LOGGER.info(
            "entity_reset: dry_run completed config_entry_id=%s would_delete=%d",
            config_entry_id,
            total,
        )
        return report

    # ── Remove entities from registry ──
    unique_ids_removed: set[str] = set()
    for entry_data in entries_to_remove:
        eid = entry_data["entity_id"]
        uid = entry_data["unique_id"]
        try:
            registry.async_remove(eid)
            report.deleted.append(eid)
            unique_ids_removed.add(uid)
            _LOGGER.debug(
                "entity_reset: deleting entity_id=%s unique_id=%s",
                eid,
                uid,
            )
        except KeyError:
            # Entity already removed (race condition or prior cleanup)
            report.skipped.append(eid)
            unique_ids_removed.add(uid)  # still need to purge tombstone
            _LOGGER.debug("entity_reset: entity already removed: %s", eid)
        except Exception as exc:
            report.errors.append({"entity_id": eid, "reason": str(exc)})
            _LOGGER.warning("entity_reset: failed to remove %s: %s", eid, exc)

    # ── Purge deleted_entities tombstones ──
    # async_remove() creates tombstones with orphaned_timestamp=None (eternal).
    # We must delete them so async_get_or_create() generates fresh entity_ids.
    purged = 0
    if hasattr(registry, "deleted_entities"):
        keys_to_delete = []
        for key, deleted_entry in registry.deleted_entities.items():
            uid = getattr(deleted_entry, "unique_id", None)
            if uid and uid in unique_ids_removed:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            try:
                del registry.deleted_entities[key]
                purged += 1
            except Exception as exc:
                _LOGGER.warning(
                    "entity_reset: failed to purge tombstone %s: %s", key, exc
                )

        if purged:
            registry.async_schedule_save()
    else:
        _LOGGER.warning(
            "entity_reset: deleted_entities not available on this HA version — "
            "tombstones cannot be purged, old entity_ids may return on reload"
        )

    report.tombstones_purged = purged

    # ── Verify: no tombstones remain for our unique_ids ──
    remaining = _count_tombstones_for_unique_ids(hass, unique_ids_removed)
    report.tombstones_remaining = remaining
    if remaining > 0:
        _LOGGER.warning(
            "entity_reset: %d tombstones still present after purge for "
            "config_entry_id=%s — old entity_ids may return",
            remaining,
            config_entry_id,
        )

    report.duration_s = time.monotonic() - start_time
    _LOGGER.info(
        "entity_reset: completed hard reset config_entry_id=%s "
        "deleted=%d skipped=%d errors=%d tombstones_purged=%d "
        "tombstones_remaining=%d (%.2fs)",
        config_entry_id,
        len(report.deleted),
        len(report.skipped),
        len(report.errors),
        purged,
        remaining,
        report.duration_s,
    )

    return report


async def reset_entity_registry(
    hass: HomeAssistant,
    *,
    config_entry_id: str | None = None,
    device_id: str | None = None,
    expected_prefix: str = "",
    mode: str = "full_reset",
    dry_run: bool = False,
    warmup_timeout_s: int = 60,
    min_entities: int = 5,
    progress_cb: Any = None,
) -> ResetResult:
    """Reset entity registry for an ESPHome config entry.

    Sequence: Unload → Remove registry entries → Reload → Verify.

    Args:
        hass: Home Assistant instance.
        config_entry_id: ESPHome config entry to reset. If None, resolved from device_id.
        device_id: HA device_id (for guard + config entry resolution).
        expected_prefix: Expected entity_id prefix slug (e.g. "sph10k_haus_02_").
        mode: "full_reset" (remove all) or "violations_only" (only bad prefixes).
        dry_run: If True, preview only — no unload/remove/reload.
        warmup_timeout_s: Max seconds to wait for entities after reload.
        min_entities: Minimum entities expected after reload.
        progress_cb: Optional async callback(stage, pct, detail).

    Returns:
        ResetResult with before/after entity lists and verification.
    """
    result = ResetResult(mode=mode, dry_run=dry_run)
    start_time = time.monotonic()

    async def _progress(stage: str, pct: int, detail: str = "") -> None:
        if progress_cb:
            try:
                await progress_cb(stage, pct, detail)
            except Exception:
                _LOGGER.debug("progress callback failed", exc_info=True)

    # ── Resolve config_entry_id if not provided ──
    if not config_entry_id and device_id:
        config_entry_id = _resolve_esphome_config_entry(hass, device_id)

    if not config_entry_id:
        result.error = "Cannot resolve ESPHome config_entry_id"
        result.duration_s = time.monotonic() - start_time
        return result

    entry = hass.config_entries.async_get_entry(config_entry_id)
    if not entry:
        result.error = f"Config entry {config_entry_id} not found"
        result.duration_s = time.monotonic() - start_time
        return result

    # ── Record device_id for stability check ──
    if device_id:
        result.device_id_before = device_id
    else:
        # Try to infer device_id from entity entries
        entries = _collect_entries_for_config_entry(hass, config_entry_id)
        if entries:
            result.device_id_before = entries[0].get("device_id")

    # ── Step 1: Snapshot current entities ──
    await _progress("snapshot", 5, "Collecting current entities…")
    old_entries = _collect_entries_for_config_entry(
        hass, config_entry_id, device_id=device_id
    )
    result.old_entity_ids = [e["entity_id"] for e in old_entries]

    if not old_entries:
        _LOGGER.info("No entities found for config_entry %s — nothing to reset", config_entry_id)
        result.success = True
        result.duration_s = time.monotonic() - start_time
        return result

    # ── Determine which entries to remove ──
    if mode == "violations_only":
        to_remove = [
            e for e in old_entries
            if _has_naming_violation(e["entity_id"], expected_prefix)
        ]
        result.violations_found = len(to_remove)
    else:
        to_remove = old_entries
        result.violations_found = sum(
            1 for e in old_entries
            if _has_naming_violation(e["entity_id"], expected_prefix)
        )

    _LOGGER.info(
        "Entity reset [%s]: %d entries found, %d to remove (dry_run=%s)",
        mode, len(old_entries), len(to_remove), dry_run,
    )

    # ── Dry-run: preview only ──
    if dry_run:
        result.removed_count = len(to_remove)
        result.success = True
        result.duration_s = time.monotonic() - start_time
        return result

    # ── Step 2: Unload config entry ──
    await _progress("unload", 20, "Unloading ESPHome integration…")
    _LOGGER.info("Unloading config entry %s (%s)", config_entry_id, entry.title)

    try:
        unload_ok = await hass.config_entries.async_unload(config_entry_id)
    except Exception as exc:
        result.error = f"Failed to unload config entry: {exc}"
        result.duration_s = time.monotonic() - start_time
        _LOGGER.error(result.error)
        return result

    if not unload_ok:
        result.error = "Config entry unload returned False"
        result.duration_s = time.monotonic() - start_time
        _LOGGER.error(result.error)
        return result

    # ── Step 3: Remove entities + purge tombstones ──
    await _progress("remove", 40, f"Removing {len(to_remove)} entity registry entries…")

    if mode == "full_reset":
        # Delegate to hard_reset for comprehensive removal + tombstone purge
        hard_report = await hard_reset_entities_for_config_entry(
            hass, config_entry_id, dry_run=False,
        )
        removed = len(hard_report.deleted)
    else:
        # violations_only: selectively remove only bad entities + purge their tombstones
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        removed = 0
        unique_ids_to_purge: set[str] = set()

        for entry_data in to_remove:
            eid = entry_data["entity_id"]
            try:
                registry.async_remove(eid)
                removed += 1
                unique_ids_to_purge.add(entry_data["unique_id"])
            except Exception as exc:
                _LOGGER.warning("Failed to remove %s: %s", eid, exc)

        # Purge tombstones for removed entities
        if unique_ids_to_purge and hasattr(registry, "deleted_entities"):
            keys_to_delete = []
            for key, deleted_entry in registry.deleted_entities.items():
                uid = getattr(deleted_entry, "unique_id", None)
                if uid and uid in unique_ids_to_purge:
                    keys_to_delete.append(key)
            for key in keys_to_delete:
                try:
                    del registry.deleted_entities[key]
                except Exception as exc:
                    _LOGGER.debug("Failed to purge tombstone %s: %s", key, exc)
            if keys_to_delete:
                registry.async_schedule_save()

    result.removed_count = removed

    # ── Step 4: Reload config entry ──
    await _progress("reload", 60, "Reloading ESPHome integration…")
    _LOGGER.info("Reloading config entry %s", config_entry_id)

    try:
        reload_ok = await hass.config_entries.async_setup(config_entry_id)
    except Exception as exc:
        result.error = f"Failed to reload config entry: {exc}"
        result.duration_s = time.monotonic() - start_time
        _LOGGER.error(result.error)
        return result

    if not reload_ok:
        _LOGGER.warning("Config entry setup returned False — may still work")

    # ── Step 5: Warmup — wait for entities to re-register ──
    await _progress("warmup", 70, "Waiting for entities to re-register…")
    _LOGGER.info("Warmup: waiting up to %ds for ≥%d entities", warmup_timeout_s, min_entities)

    elapsed = 0
    poll_interval = 3
    while elapsed < warmup_timeout_s:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        new_entries = _collect_entries_for_config_entry(
            hass, config_entry_id, device_id=device_id
        )
        count = len(new_entries)
        pct = 70 + int((elapsed / warmup_timeout_s) * 20)
        await _progress("warmup", min(pct, 90), f"Re-registered: {count}/{min_entities}")

        if count >= min_entities:
            _LOGGER.info("Warmup: %d entities registered after %ds", count, elapsed)
            break
    else:
        new_entries = _collect_entries_for_config_entry(
            hass, config_entry_id, device_id=device_id
        )
        _LOGGER.warning(
            "Warmup timeout: only %d entities after %ds (expected ≥%d)",
            len(new_entries), warmup_timeout_s, min_entities,
        )

    result.new_entity_ids = [e["entity_id"] for e in new_entries]
    result.re_registered_count = len(new_entries)

    # ── Step 6: Verify naming ──
    await _progress("verify", 92, "Verifying entity naming…")

    violations = []
    for eid in result.new_entity_ids:
        if _has_naming_violation(eid, expected_prefix):
            violations.append(eid)

    if violations:
        _LOGGER.warning(
            "Still %d naming violations after reset: %s",
            len(violations), violations[:5],
        )

    # ── Step 7: Verify device_id stability ──
    if result.device_id_before:
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(result.device_id_before)
        result.device_id_after = device.id if device else None
        result.device_id_stable = result.device_id_before == result.device_id_after

    # ── Complete ──
    result.success = True
    result.duration_s = time.monotonic() - start_time

    await _progress("complete", 100, f"Reset complete: {removed} removed, {result.re_registered_count} re-registered")

    _LOGGER.info(
        "Entity reset COMPLETE [%s]: removed=%d, re_registered=%d, "
        "violations=%d, device_id_stable=%s (%.1fs)",
        mode, removed, result.re_registered_count,
        len(violations), result.device_id_stable, result.duration_s,
    )

    return result
