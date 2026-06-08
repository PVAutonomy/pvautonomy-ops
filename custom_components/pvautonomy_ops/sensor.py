"""PVAutonomy Ops Sensors (Contract Outputs G, H).

Publishes add-on status and device count to Home Assistant.
"""
import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONTRACT_VERSION,
    DOMAIN,
    ENTITY_DEVICE_COUNT_SENSOR,
    ENTITY_STATUS_SENSOR,
    STATE_DEGRADED,
    STATE_ERROR,
    STATE_INITIALIZING,
    STATE_OK,
    STATE_WARN,
    VERSION,
)
from .discovery import ContractInputReader
from .stepper import WizardEngine

ENTITY_WIZARD_SENSOR = "sensor.pvautonomy_ops_wizard"

_STATUS_HISTORY_STORE_VERSION = 1
_STATUS_HISTORY_STORE_PREFIX = f"{DOMAIN}_status_history"

_BUILD_METADATA_KEYS = (
    "last_build_cache_hit",
    "last_build_id",
    "last_build_artifact_path",
    "last_build_firmware_size",
    "last_build_target_device",
    "last_build_registry_file",
    "last_build_backend",
    "last_build_force_rebuild",
)

_INSTALL_STATE_KEYS = (
    "last_install_stage",
    "last_install_progress",
    "last_install_target_device",
    "last_install_firmware_size",
    "last_install_success_at",
    # EPIC-004 firmware-status fix: the build_id that was actually installed,
    # so the UI can tell a freshly prepared (but not yet installed) build apart
    # from one that is genuinely installed. Display-safe (a CI build id, not a
    # secret). Absent on history written before this key existed → the card
    # then treats the device as "not currently installed" (fail-safe).
    "last_install_build_id",
    "last_install_error",
    "last_install_error_at",
)

_FIRMWARE_HISTORY_BY_DEVICE_ATTR = "firmware_history_by_device"
_FIRMWARE_HISTORY_KEYS = tuple(dict.fromkeys((*_BUILD_METADATA_KEYS, *_INSTALL_STATE_KEYS)))

_LOGGER = logging.getLogger(__name__)


def _normalize_firmware_history_device_id(value: Any) -> str | None:
    """Return the stable per-device firmware-history key for display status."""
    if value is None:
        return None

    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if normalized.startswith("edge101_"):
        normalized = normalized[8:]
    if normalized in ("", "none", "unknown", "unavailable"):
        return None
    return normalized


def _display_safe_firmware_history(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only customer-visible firmware status metadata."""
    return {key: data.get(key) for key in _FIRMWARE_HISTORY_KEYS if key in data}


async def _compute_owned_device_keys(
    hass: Any, entry_id: str | None
) -> set[str]:
    """Return the set of normalized ``device_key`` values this entry owns.

    Used by ``PVAutonomyOpsStatusSensor`` to keep its
    ``firmware_history_by_device`` strictly scoped to the device(s) bound to
    its own ConfigEntry — preventing cross-entry pollution where, for example,
    a pre-Adopt build that ran under the SPH entry left an ``mic600_garage_01``
    entry in the SPH sensor's history and the Lovelace "Firmware" card then
    rendered the SPH sensor's stale MIC values instead of the MIC sensor's
    fresh ones (first-match-wins iteration over ``states.sensor``).

    Ownership signals (any non-empty wins, union of all):

    1. ``entry.options.selected_device`` (Adopt/wizard write this on creation).
    2. ``entry.options._initial_device.device_id`` / ``device_slug``
       (bootstrap window before ``async_setup_entry`` promotes / removes it).
    3. ``entry.options.ha_device_id`` (or ``_initial_device.ha_device_id``)
       resolved against the domain-global metadata store
       (``DeviceMetadata.ha_device_id``) — covers entries whose
       ``selected_device`` was never persisted (e.g. legacy SPH where only
       ``ha_device_id`` remains after promotion).

    Empty result means "no owner signal known" — callers MUST treat it as
    "accept all" so this guard never breaks legacy single-entry installs.
    Read-only; never mutates entry options or the metadata store.
    """
    keys: set[str] = set()
    if not entry_id:
        return keys
    domain_data = getattr(hass, "data", None) or {}
    entry_data = domain_data.get(DOMAIN, {}).get(entry_id, {}) if isinstance(
        domain_data, dict
    ) else {}
    if not isinstance(entry_data, dict):
        return keys
    entry = entry_data.get("entry")
    opts = getattr(entry, "options", None) or {}
    if not isinstance(opts, Mapping):
        return keys

    # Signal 1: selected_device
    sel = _normalize_firmware_history_device_id(opts.get("selected_device"))
    if sel:
        keys.add(sel)

    # Signal 2: _initial_device.device_id / device_slug (bootstrap window)
    initial = opts.get("_initial_device")
    if isinstance(initial, Mapping):
        for key in ("device_id", "device_slug"):
            normalized = _normalize_firmware_history_device_id(initial.get(key))
            if normalized:
                keys.add(normalized)

    # Signal 3: ha_device_id → metadata-store lookup
    ha_id = str(opts.get("ha_device_id") or "").strip()
    if not ha_id and isinstance(initial, Mapping):
        ha_id = str(initial.get("ha_device_id") or "").strip()
    if ha_id:
        store = entry_data.get("metadata_store")
        if store is not None:
            try:
                metas = await store.get_all()
            except Exception:  # noqa: BLE001 - defensive
                metas = []
            for meta in metas or []:
                meta_ha = str(getattr(meta, "ha_device_id", "") or "").strip()
                if meta_ha and meta_ha == ha_id:
                    normalized = _normalize_firmware_history_device_id(
                        getattr(meta, "device_id", "")
                    )
                    if normalized:
                        keys.add(normalized)

    return keys


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PVAutonomy Ops sensors from a ConfigEntry."""
    _LOGGER.info("Setting up PVAutonomy Ops sensors (ConfigEntry)")

    # PN-2: per entry_id keying
    entry_data = hass.data[DOMAIN][entry.entry_id]
    input_reader: ContractInputReader = entry_data["input_reader"]
    operation_tracker = entry_data["operation_tracker"]
    wizard_engine: WizardEngine = entry_data["wizard_engine"]
    is_legacy = entry_data.get("is_legacy", False)  # EPIC-006-STAB Phase 2

    async_add_entities(
        [
            PVAutonomyOpsStatusSensor(input_reader, operation_tracker, entry.entry_id, is_legacy=is_legacy),
            PVAutonomyOpsDevicesCountSensor(input_reader, entry.entry_id, is_legacy=is_legacy),
            PVAutonomyOpsWizardSensor(wizard_engine, entry.entry_id),
        ],
        True,
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up PVAutonomy Ops sensors (legacy YAML, kept for backward compat)."""
    _LOGGER.info("Setting up PVAutonomy Ops sensors (YAML platform)")

    # PN-2: find first entry's data (legacy path has no entry object)
    entry_data = next(iter(hass.data.get(DOMAIN, {}).values()), {})
    entry_id = entry_data.get("entry", None)
    entry_id = entry_id.entry_id if entry_id else None

    input_reader: ContractInputReader = entry_data["input_reader"]
    operation_tracker = entry_data["operation_tracker"]
    wizard_engine: WizardEngine = entry_data["wizard_engine"]

    async_add_entities(
        [
            PVAutonomyOpsStatusSensor(input_reader, operation_tracker, entry_id),
            PVAutonomyOpsDevicesCountSensor(input_reader, entry_id),
            PVAutonomyOpsWizardSensor(wizard_engine, entry_id),
        ],
        True,
    )


class PVAutonomyOpsStatusSensor(SensorEntity):
    """Output G: sensor.pvautonomy_ops_status."""

    _attr_name = "PVAutonomy Status"

    def __init__(self, input_reader: ContractInputReader, operation_tracker, entry_id: str | None = None, *, is_legacy: bool = False) -> None:
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_STATUS_SENSOR}" if entry_id else ENTITY_STATUS_SENSOR
        self._attr_suggested_object_id = ENTITY_STATUS_SENSOR
        self.input_reader = input_reader
        self.operation_tracker = operation_tracker  # Phase 3: lifecycle tracking
        self._entry_id = entry_id  # PN-2: for per-entry selection lookup
        # EPIC-006-STAB Phase 2: Legacy entities disabled by default
        if is_legacy:
            self._attr_entity_registry_enabled_default = False
        self._attr_native_value = STATE_INITIALIZING
        self._attr_extra_state_attributes = {
            "version": VERSION,
            "contract_version": CONTRACT_VERSION,
            # Gates attributes (initialized as null)
            "gates_last_run": None,
            "gates_overall": None,
            "gates_passed_count": None,
            "gates_failed_count": None,
            "gates_warn_count": None,
            "gates_fail": None,
            "gates_warn": None,
            "gates_details": None,
            # Flash attributes (Phase 3.3)
            "flash_stage": None,
            "flash_version": None,
            "flash_target_device": None,
            "flash_last_success": None,
            "flash_last_error": None,
            "flash_last_error_time": None,
            # EPIC-006-STAB Phase 3: IP + build observability
            "device_ip": None,
            "ip_resolution_method": None,
            "ip_resolution_duration_ms": None,
            "flash_build_id": None,
            # EPIC-007: Build cache observability
            "last_build_cache_hit": None,
            "last_build_id": None,
            # EPIC-004: build_firmware (build-only) metadata
            "last_build_artifact_path": None,
            "last_build_firmware_size": None,
            "last_build_target_device": None,
            "last_build_registry_file": None,
            "last_build_backend": None,
            "last_build_force_rebuild": None,
            # EPIC-004 SPEC-20260514: install_prepared_firmware (install-only)
            # metadata. Surfaces the latest customer-initiated install state
            # for the Firmware card without exposing secret material.
            "last_install_stage": None,
            "last_install_progress": None,
            "last_install_target_device": None,
            "last_install_firmware_size": None,
            "last_install_success_at": None,
            "last_install_build_id": None,
            "last_install_error": None,
            "last_install_error_at": None,
            _FIRMWARE_HISTORY_BY_DEVICE_ATTR: {},
        }
        # Store latest gate results (updated via event)
        self._gate_results = None
        # Store latest flash state (updated via event)
        self._flash_state = None
        # EPIC-007: Build cache metadata (updated via build_stage event)
        self._build_meta: dict[str, Any] | None = None
        # EPIC-004 SPEC-20260514: Install state (updated via install_stage event)
        self._install_state: dict[str, Any] | None = None
        self._firmware_history_by_device: dict[str, dict[str, Any]] = {}
        self._status_history_store: Store | None = None

    async def async_added_to_hass(self) -> None:
        """Register event listeners when sensor is added to HA."""
        await super().async_added_to_hass()

        store_key = f"{_STATUS_HISTORY_STORE_PREFIX}_{self._entry_id or 'legacy'}"
        self._status_history_store = Store(
            self.hass, _STATUS_HISTORY_STORE_VERSION, store_key
        )
        await self._async_restore_status_history()

        # EPIC-006-STAB Phase 1: Register entity_id string in hass.data
        # for entry-scoped lookup (reload-safe — stores string, not object)
        if self._entry_id:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
            if isinstance(entry_data, dict):
                entry_data["status_entity_id"] = self.entity_id
                _LOGGER.info(
                    "Registered status_entity_id=%s for entry=%s",
                    self.entity_id, self._entry_id[:8],
                )

        # EPIC-015 P2-03: Use async_on_remove to auto-unsubscribe on entity
        # removal (reload/unload). Previous code leaked listeners on every reload.
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_gates_completed",
                self._handle_gates_completed,
            )
        )

        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_flash_stage",
                self._handle_flash_stage,
            )
        )

        # EPIC-007: Build pipeline stage events (cache_hit observability)
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_build_stage",
                self._handle_build_stage,
            )
        )

        # EPIC-004 SPEC-20260514-epic004-install-prepared-firmware-service:
        # install pipeline stage events (customer-initiated install of the
        # prepared firmware artifact).
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_install_stage",
                self._handle_install_stage,
            )
        )

        # EPIC-004 SPEC-20260513 (operation tracker): listen to operation
        # lifecycle events so op_state / op_name / op_progress flip promptly
        # in the UI while build_firmware (or any future op-wrapped service)
        # runs. Entry-id filtered in each handler.
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_operation_started",
                self._handle_operation_started,
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_operation_progress",
                self._handle_operation_progress,
            )
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{DOMAIN}_operation_completed",
                self._handle_operation_completed,
            )
        )

        _LOGGER.debug(
            "Registered gates_completed + flash_stage + build_stage + "
            "operation_started/progress/completed event listeners"
        )

    def _merge_firmware_history_for_device(
        self,
        device_id: Any,
        updates: dict[str, Any],
    ) -> None:
        """Merge display-safe firmware status into the per-device history map."""
        device_key = _normalize_firmware_history_device_id(device_id)
        if not device_key:
            return

        safe_updates = _display_safe_firmware_history(updates)
        if not safe_updates:
            return

        existing = dict(self._firmware_history_by_device.get(device_key, {}))
        existing.update(safe_updates)
        if any(value is not None for value in existing.values()):
            self._firmware_history_by_device[device_key] = existing
            self._attr_extra_state_attributes[_FIRMWARE_HISTORY_BY_DEVICE_ATTR] = (
                self._firmware_history_by_device
            )

    def _firmware_history_for_device(self, device_id: Any) -> dict[str, Any]:
        """Return a copy of the stored firmware history for one device."""
        device_key = _normalize_firmware_history_device_id(device_id)
        if not device_key:
            return {}

        history = self._firmware_history_by_device.get(device_key)
        return dict(history) if isinstance(history, dict) else {}

    async def _async_restore_status_history(self) -> None:
        """Restore persisted build/install metadata after reloads.

        Firmware status is customer-facing history, not only live operation
        state. Keep it entry-scoped and device-keyed so one device cannot
        overwrite another device's build/install telemetry after a second
        build or install runs in the same integration entry.
        """
        if self._status_history_store is None:
            return

        try:
            stored = await self._status_history_store.async_load()
        except Exception:  # pragma: no cover - defensive storage failure
            _LOGGER.warning(
                "Failed to restore PVAutonomy status history",
                exc_info=True,
            )
            return

        if not isinstance(stored, dict):
            return

        raw_by_device = stored.get(_FIRMWARE_HISTORY_BY_DEVICE_ATTR)
        if isinstance(raw_by_device, dict):
            for raw_device_id, raw_history in raw_by_device.items():
                device_key = _normalize_firmware_history_device_id(raw_device_id)
                if not device_key or not isinstance(raw_history, dict):
                    continue

                history = _display_safe_firmware_history(raw_history)
                if any(value is not None for value in history.values()):
                    self._firmware_history_by_device[device_key] = history

        build_meta = {
            key: stored.get(key)
            for key in _BUILD_METADATA_KEYS
            if key in stored
        }
        install_state = {
            key: stored.get(key)
            for key in _INSTALL_STATE_KEYS
            if key in stored
        }

        if any(value is not None for value in build_meta.values()):
            self._build_meta = build_meta
            self._attr_extra_state_attributes.update(build_meta)
        if any(value is not None for value in install_state.values()):
            self._install_state = install_state
            self._attr_extra_state_attributes.update(install_state)

        latest_target_device = (
            install_state.get("last_install_target_device")
            or build_meta.get("last_build_target_device")
        )
        if latest_target_device:
            self._merge_firmware_history_for_device(
                latest_target_device,
                {**build_meta, **install_state},
            )

        self._attr_extra_state_attributes[_FIRMWARE_HISTORY_BY_DEVICE_ATTR] = (
            self._firmware_history_by_device
        )

        # Ownership eviction: cross-entry pollution may have left foreign
        # device_keys in the restored history (e.g. SPH entry's sensor
        # carrying mic600_garage_01 from a pre-Adopt build that ran under
        # SPH's Phase-2 single-entry fallback). Drop any device_key this
        # entry does not own and persist the cleaned state once. Skip when
        # no owner signal is known (legacy compat — see helper docstring).
        owned = await _compute_owned_device_keys(self.hass, self._entry_id)
        if owned:
            foreign = set(self._firmware_history_by_device) - owned
            if foreign:
                for device_key in foreign:
                    self._firmware_history_by_device.pop(device_key, None)
                _LOGGER.info(
                    "Status history: evicted %d foreign device_key(s) on "
                    "restore (entry=%s owns=%s, dropped=%s)",
                    len(foreign),
                    (self._entry_id or "")[:8],
                    sorted(owned),
                    sorted(foreign),
                )
                self._attr_extra_state_attributes[
                    _FIRMWARE_HISTORY_BY_DEVICE_ATTR
                ] = self._firmware_history_by_device
                await self._async_save_status_history()

    async def _async_save_status_history(self) -> None:
        """Persist customer-visible build/install metadata."""
        if self._status_history_store is None:
            return

        attrs = self._attr_extra_state_attributes
        build_meta = self._build_meta or {
            key: attrs.get(key) for key in _BUILD_METADATA_KEYS
        }
        install_state = self._install_state or {
            key: attrs.get(key) for key in _INSTALL_STATE_KEYS
        }
        latest = {**build_meta, **install_state}
        history_by_device = {
            device_key: history
            for device_key, raw_history in self._firmware_history_by_device.items()
            if isinstance(raw_history, dict)
            for history in [_display_safe_firmware_history(raw_history)]
            if any(value is not None for value in history.values())
        }
        self._firmware_history_by_device = history_by_device
        self._attr_extra_state_attributes[_FIRMWARE_HISTORY_BY_DEVICE_ATTR] = (
            history_by_device
        )
        data = {
            **latest,
            _FIRMWARE_HISTORY_BY_DEVICE_ATTR: history_by_device,
        }

        if not any(value is not None for value in latest.values()) and not history_by_device:
            return

        try:
            await self._status_history_store.async_save(data)
        except Exception:  # pragma: no cover - defensive storage failure
            _LOGGER.warning(
                "Failed to persist PVAutonomy status history",
                exc_info=True,
            )

    async def _handle_gates_completed(self, event) -> None:
        """Handle gate completion event and update attributes.

        Args:
            event: Event containing gate results
        """
        data = event.data

        # EPIC-006-STAB Phase 1: strict entry_id filtering
        event_entry_id = data.get("entry_id")
        if event_entry_id and self._entry_id and event_entry_id != self._entry_id:
            return  # Event belongs to another entry — ignore

        self._gate_results = {
            "gates_last_run": data.get("checked_at"),
            "gates_overall": data.get("overall"),
            "gates_passed_count": data.get("gates_passed"),
            "gates_failed_count": data.get("gates_failed"),
            "gates_warn_count": data.get("gates_warned"),
            "gates_fail": data.get("failed_gates"),
            "gates_warn": data.get("warned_gates"),
            "gates_details": data.get("details"),
        }

        _LOGGER.info(
            "Gates completed event received: %s (passed=%d, warned=%d, failed=%d)",
            data.get("overall"),
            data.get("gates_passed", 0),
            data.get("gates_warned", 0),
            data.get("gates_failed", 0),
        )

        # Trigger sensor update to refresh attributes
        self.async_schedule_update_ha_state(True)

    async def _handle_flash_stage(self, event) -> None:
        """Handle flash stage event and update attributes.

        Args:
            event: Event containing flash stage info
        """
        data = event.data

        # EPIC-006-STAB Phase 1: strict entry_id filtering
        event_entry_id = data.get("entry_id")
        if event_entry_id and self._entry_id and event_entry_id != self._entry_id:
            return  # Event belongs to another entry — ignore

        stage = data.get("stage")

        # Initialize flash state if not exists (preserve existing values)
        if self._flash_state is None:
            self._flash_state = {
                "flash_stage": None,
                "flash_version": None,
                "flash_target_device": None,
                "flash_last_success": None,
                "flash_last_error": None,
                "flash_last_error_time": None,
            }

        # Update current stage info
        self._flash_state["flash_stage"] = stage
        self._flash_state["flash_version"] = data.get("version")
        self._flash_state["flash_target_device"] = data.get("target_device")
        # EPIC-006-STAB Phase 3: IP + build observability
        if data.get("device_ip") is not None:
            self._flash_state["device_ip"] = data["device_ip"]
            self._flash_state["ip_resolution_method"] = data.get("ip_method")
            self._flash_state["ip_resolution_duration_ms"] = data.get("ip_duration_ms")
        if data.get("build_job_id") is not None:
            self._flash_state["flash_build_id"] = data["build_job_id"]

        # Update success/error tracking based on final stage
        if stage == "complete":
            self._flash_state["flash_last_success"] = datetime.now(timezone.utc).isoformat()
            # Clear error fields on success
            self._flash_state["flash_last_error"] = None
            self._flash_state["flash_last_error_time"] = None
        elif stage == "failed":
            error_msg = data.get("error", f"Flash failed at stage: {stage}")
            self._flash_state["flash_last_error"] = error_msg
            self._flash_state["flash_last_error_time"] = datetime.now(timezone.utc).isoformat()

        _LOGGER.info(
            "Flash stage event received: %s (version=%s, device=%s)",
            stage,
            data.get("version"),
            data.get("target_device"),
        )

        # UX Pack: Persistent notification for setup progress (non-blocking)
        from .notifications import FLASH_STAGE_TO_NOTIFICATION, notify_setup_progress

        notification_stage = FLASH_STAGE_TO_NOTIFICATION.get(stage)
        _LOGGER.debug(
            "Notification check: stage=%s → notification_stage=%s, entry_id=%s",
            stage, notification_stage, self._entry_id,
        )
        if notification_stage and self._entry_id:
            device_label = data.get("target_device") or "Unknown Device"
            # EPIC-015 P2-05: Resolve label from the event's target_device
            # via read-only lookup — not get_all()[0] (wrong in multi-device).
            entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
            metadata_store = entry_data.get("metadata_store")
            target_device = data.get("target_device")
            if metadata_store and target_device:
                try:
                    meta = await metadata_store.lookup(target_device)
                    if meta:
                        from .const import MODEL_REGISTRY_MAP
                        display_name = MODEL_REGISTRY_MAP.get(
                            meta.model_slug, {}
                        ).get("display_name", meta.model_slug.upper())
                        device_label = (
                            f"{display_name} "
                            f"{meta.site.title()} {str(meta.number).zfill(2)}"
                        )
                except Exception:
                    # Fall back to target_device from event
                    _LOGGER.debug("metadata lookup failed for notification display", exc_info=True)

            error_msg = data.get("error") if stage == "failed" else None
            self.hass.async_create_task(
                notify_setup_progress(
                    self.hass,
                    entry_id=self._entry_id,
                    device_label=device_label,
                    stage=notification_stage,
                    error=error_msg,
                )
            )

        # Trigger sensor update to refresh attributes
        self.async_schedule_update_ha_state(True)

    async def _handle_build_stage(self, event) -> None:
        """Handle build pipeline stage events (EPIC-007: cache observability)."""
        data = event.data

        # Entry-scoped filtering (same pattern as gates/flash handlers)
        event_entry_id = data.get("entry_id")
        if event_entry_id and self._entry_id and event_entry_id != self._entry_id:
            return  # Event belongs to another entry — ignore

        stage = data.get("stage")

        # Ownership filter: refuse to merge build metadata for a device this
        # entry does not own. Prevents the cross-entry pollution that caused
        # the Lovelace Firmware card to show stale values from another
        # entry's sensor (see _compute_owned_device_keys docstring). When
        # ownership is unknown (legacy single-entry installs), the filter is
        # a no-op.
        if stage in ("complete", "failed"):
            device_key = _normalize_firmware_history_device_id(
                data.get("device_id")
            )
            if device_key:
                owned = await _compute_owned_device_keys(
                    self.hass, self._entry_id
                )
                if owned and device_key not in owned:
                    _LOGGER.debug(
                        "Status history: dropping build_stage(%s) for "
                        "non-owned device %s (entry=%s owns=%s)",
                        stage, device_key,
                        (self._entry_id or "")[:8], sorted(owned),
                    )
                    return

        # Only update on terminal events that carry build metadata
        if stage == "complete":
            # EPIC-004: build-only firmware metadata (build_firmware service +
            # any pipeline run). target_device is the deterministic device_id
            # carried by every build_stage event; force_rebuild/registry_file
            # are added to the base event payload.
            self._build_meta = {
                "last_build_cache_hit": data.get("cache_hit", False),
                "last_build_id": data.get("job_id"),
                "last_build_artifact_path": data.get("artifact_path"),
                "last_build_firmware_size": data.get("firmware_size"),
                "last_build_target_device": data.get("device_id"),
                "last_build_registry_file": data.get("registry_file"),
                "last_build_backend": data.get("build_backend"),
                "last_build_force_rebuild": data.get("force_rebuild"),
            }
            self._merge_firmware_history_for_device(
                data.get("device_id"),
                self._build_meta,
            )
            await self._async_save_status_history()
            self.async_schedule_update_ha_state(True)
        elif stage == "failed":
            self._build_meta = {
                "last_build_cache_hit": None,
                "last_build_id": data.get("job_id"),
                "last_build_artifact_path": None,
                "last_build_firmware_size": None,
                "last_build_target_device": data.get("device_id"),
                "last_build_registry_file": data.get("registry_file"),
                "last_build_backend": data.get("build_backend"),
                "last_build_force_rebuild": data.get("force_rebuild"),
            }
            self._merge_firmware_history_for_device(
                data.get("device_id"),
                self._build_meta,
            )
            await self._async_save_status_history()
            self.async_schedule_update_ha_state(True)

    async def _handle_install_stage(self, event) -> None:
        """Handle install pipeline stage events (EPIC-004 install service).

        Records the latest install state for the Firmware card without
        exposing any secret material (OTA password, etc.).
        """
        data = event.data

        # Entry-scoped filtering (same pattern as build/flash handlers)
        event_entry_id = data.get("entry_id")
        if event_entry_id and self._entry_id and event_entry_id != self._entry_id:
            return  # Event belongs to another entry — ignore

        stage = data.get("stage")
        target_device = data.get("target_device")
        firmware_size = data.get("firmware_size")
        progress = data.get("progress")

        # Ownership filter: refuse to merge install metadata for a device
        # this entry does not own (same rationale as _handle_build_stage —
        # see _compute_owned_device_keys docstring). When ownership is
        # unknown, the filter is a no-op.
        device_key = _normalize_firmware_history_device_id(target_device)
        if device_key:
            owned = await _compute_owned_device_keys(self.hass, self._entry_id)
            if owned and device_key not in owned:
                _LOGGER.debug(
                    "Status history: dropping install_stage(%s) for "
                    "non-owned device %s (entry=%s owns=%s)",
                    stage, device_key,
                    (self._entry_id or "")[:8], sorted(owned),
                )
                return

        # Preserve previous success/error timestamps so the card can still
        # show "letzte Installation am ..." once a new install starts.
        prev = self._firmware_history_for_device(target_device)
        if not prev and self._install_state:
            previous_target = self._install_state.get("last_install_target_device")
            if (
                _normalize_firmware_history_device_id(previous_target)
                == _normalize_firmware_history_device_id(target_device)
            ):
                prev = self._install_state
        new_state: dict[str, Any] = {
            "last_install_stage": stage,
            "last_install_progress": progress,
            "last_install_target_device": target_device,
            "last_install_firmware_size": firmware_size,
            "last_install_success_at": prev.get("last_install_success_at"),
            "last_install_build_id": prev.get("last_install_build_id"),
            "last_install_error": prev.get("last_install_error"),
            "last_install_error_at": prev.get("last_install_error_at"),
        }

        if stage == "complete":
            new_state["last_install_success_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
            # Record which prepared build was installed so the UI can
            # distinguish "installed" from a newer build that is only prepared.
            # install_prepared_firmware installs the on-disk prepared artifact,
            # whose id is the device's current last_build_id.
            new_state["last_install_build_id"] = prev.get("last_build_id")
            # Clear error fields once an install succeeds.
            new_state["last_install_error"] = None
            new_state["last_install_error_at"] = None
        elif stage == "failed":
            new_state["last_install_error"] = (
                data.get("error") or "Installation failed"
            )
            new_state["last_install_error_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

        self._install_state = new_state
        self._merge_firmware_history_for_device(target_device, new_state)
        await self._async_save_status_history()

        _LOGGER.info(
            "Install stage event received: stage=%s progress=%s device=%s",
            stage, progress, target_device,
        )
        self.async_schedule_update_ha_state(True)

    def _event_is_for_this_entry(self, data) -> bool:
        """Strict entry-id filter for operation lifecycle events.

        Permissive when either side is None (single-entry / legacy path),
        strict when both sides are set (multi-entry deployments).
        """
        ev_entry_id = data.get("entry_id")
        if ev_entry_id and self._entry_id and ev_entry_id != self._entry_id:
            return False
        return True

    async def _handle_operation_started(self, event) -> None:
        """Refresh status sensor when a tracked operation starts.

        The OperationTracker already mutated its in-memory state before
        firing the event; we only need to schedule a sensor update so the
        new op_state / op_name / op_started values surface in HA promptly.
        Entry-id filtered.
        """
        if not self._event_is_for_this_entry(event.data):
            return
        _LOGGER.debug(
            "operation_started event received: %s (entry=%s)",
            event.data.get("operation"),
            (event.data.get("entry_id") or "")[:8],
        )
        self.async_schedule_update_ha_state(True)

    async def _handle_operation_progress(self, event) -> None:
        """Refresh status sensor on operation progress updates."""
        if not self._event_is_for_this_entry(event.data):
            return
        _LOGGER.debug(
            "operation_progress event received: %s%% (entry=%s)",
            event.data.get("progress"),
            (event.data.get("entry_id") or "")[:8],
        )
        self.async_schedule_update_ha_state(True)

    async def _handle_operation_completed(self, event) -> None:
        """Refresh status sensor when a tracked operation completes."""
        if not self._event_is_for_this_entry(event.data):
            return
        _LOGGER.debug(
            "operation_completed event received: %s success=%s (entry=%s)",
            event.data.get("operation"),
            event.data.get("success"),
            (event.data.get("entry_id") or "")[:8],
        )
        self.async_schedule_update_ha_state(True)

    async def _sync_device_dropdown(self) -> None:
        """Auto-populate input_select.edge101_selected_production_device from discovered devices.

        Called during each periodic update to keep the dropdown in sync
        with the Device Registry. Only updates when options actually change.
        """
        from .const import ENTITY_DEVICE_SELECTOR

        try:
            dropdown_items = await self.input_reader.get_all_devices_for_dropdown()
            new_options = ["none"] + [item["value"] for item in dropdown_items]

            # Read current options to avoid unnecessary service calls
            selector_state = self.hass.states.get(ENTITY_DEVICE_SELECTOR)
            if selector_state is None:
                _LOGGER.debug("input_select %s not found, skipping dropdown sync", ENTITY_DEVICE_SELECTOR)
                return

            current_options = selector_state.attributes.get("options", [])
            current = selector_state.state
            current_is_real = current not in ("unknown", "unavailable", "", "none")
            current_key = _normalize_firmware_history_device_id(current)
            canonical_current = None
            if current_key:
                canonical_current = next(
                    (
                        option for option in new_options
                        if _normalize_firmware_history_device_id(option) == current_key
                    ),
                    None,
                )

            # HA logs "Current option ... no longer valid" when set_options
            # removes the current state. During the slug -> display-name
            # migration, keep the legacy value temporarily, select the
            # canonical display name, then prune the compatibility option.
            staged_options = list(new_options)
            if current_is_real and current not in staged_options:
                staged_options.append(current)

            if set(current_options) == set(new_options):
                if canonical_current and current != canonical_current:
                    await self.hass.services.async_call(
                        "input_select",
                        "select_option",
                        {
                            "entity_id": ENTITY_DEVICE_SELECTOR,
                            "option": canonical_current,
                        },
                        blocking=True,
                    )
                return  # No option change needed

            await self.hass.services.async_call(
                "input_select",
                "set_options",
                {
                    "entity_id": ENTITY_DEVICE_SELECTOR,
                    "options": staged_options,
                },
                blocking=True,
            )

            if canonical_current and current != canonical_current:
                await self.hass.services.async_call(
                    "input_select",
                    "select_option",
                    {
                        "entity_id": ENTITY_DEVICE_SELECTOR,
                        "option": canonical_current,
                    },
                    blocking=True,
                )
            elif current_is_real and current not in new_options:
                await self.hass.services.async_call(
                    "input_select",
                    "select_option",
                    {
                        "entity_id": ENTITY_DEVICE_SELECTOR,
                        "option": "none",
                    },
                    blocking=True,
                )

            if staged_options != new_options:
                await self.hass.services.async_call(
                    "input_select",
                    "set_options",
                    {
                        "entity_id": ENTITY_DEVICE_SELECTOR,
                        "options": new_options,
                    },
                    blocking=True,
                )

            _LOGGER.info(
                "Dropdown auto-synced with %d devices: %s",
                len(dropdown_items),
                new_options,
            )
        except Exception as err:
            _LOGGER.warning("Failed to auto-sync dropdown: %s", err)

    async def async_update(self) -> None:
        try:
            from .discovery import (
                CONNECTIVITY_OFFLINE,
                CONNECTIVITY_ONLINE,
                DEVICE_KIND_FACTORY,
                DEVICE_KIND_PRODUCTION,
            )

            validation = await self.input_reader.validate_inputs(entry_id=self._entry_id)
            selected = await self.input_reader.get_selected_device_from_storage(
                self._entry_id
            )
            if not selected:
                selected = await self.input_reader.get_selected_device(
                    entry_id=self._entry_id
                )
            selected_kind = await self.input_reader.get_selected_device_kind(
                device_name=selected, entry_id=self._entry_id
            )

            # EPIC-005-A1: Single call replaces legacy+registry+dedup logic
            all_devices = await self.input_reader.get_all_discovered_devices()

            # Derive counts from DiscoveredDevice fields
            devices_online = sum(
                1 for d in all_devices if d.connectivity == CONNECTIVITY_ONLINE
            )
            devices_offline = sum(
                1 for d in all_devices if d.connectivity == CONNECTIVITY_OFFLINE
            )
            total_devices = len(all_devices)

            factory_devices = [d for d in all_devices if d.state == DEVICE_KIND_FACTORY]
            production_devices = [d for d in all_devices if d.state == DEVICE_KIND_PRODUCTION]

            # Auto-populate legacy input_select dropdown (only if it exists)
            await self._sync_device_dropdown()

            last_error = None
            last_error_time = None

            if not validation.get("valid", False):
                self._attr_native_value = STATE_DEGRADED
                missing = validation.get("missing_inputs", [])
                last_error = f"Missing inputs: {', '.join(missing)}"
                last_error_time = datetime.now(timezone.utc).isoformat()
            elif total_devices == 0:
                self._attr_native_value = STATE_WARN
                last_error = "No devices discovered"
                last_error_time = datetime.now(timezone.utc).isoformat()
            elif devices_offline > 0:
                self._attr_native_value = STATE_WARN
            else:
                self._attr_native_value = STATE_OK

            # Merge Phase 3 operation tracking with Phase 2 attributes
            op_attrs = self.operation_tracker.to_dict()

            # Smart last_discovery: Only update when Discover action succeeds
            if op_attrs.get("op_name") == "discover" and op_attrs.get("op_state") == "success":
                last_discovery = op_attrs.get("op_finished")
            else:
                last_discovery = self._attr_extra_state_attributes.get(
                    "last_discovery",
                    datetime.now(timezone.utc).isoformat(),
                )

            # Merge gate results if available (from event)
            gate_attrs = {}
            if self._gate_results:
                gate_attrs = self._gate_results
            else:
                gate_attrs = {
                    "gates_last_run": self._attr_extra_state_attributes.get("gates_last_run"),
                    "gates_overall": self._attr_extra_state_attributes.get("gates_overall"),
                    "gates_passed_count": self._attr_extra_state_attributes.get("gates_passed_count"),
                    "gates_failed_count": self._attr_extra_state_attributes.get("gates_failed_count"),
                    "gates_warn_count": self._attr_extra_state_attributes.get("gates_warn_count"),
                    "gates_fail": self._attr_extra_state_attributes.get("gates_fail"),
                    "gates_warn": self._attr_extra_state_attributes.get("gates_warn"),
                    "gates_details": self._attr_extra_state_attributes.get("gates_details"),
                }

            # Merge flash state if available (from event) - Phase 3.3
            flash_attrs = {}
            if self._flash_state:
                flash_attrs = self._flash_state
            else:
                flash_attrs = {
                    "flash_stage": self._attr_extra_state_attributes.get("flash_stage"),
                    "flash_version": self._attr_extra_state_attributes.get("flash_version"),
                    "flash_target_device": self._attr_extra_state_attributes.get("flash_target_device"),
                    "flash_last_success": self._attr_extra_state_attributes.get("flash_last_success"),
                    "flash_last_error": self._attr_extra_state_attributes.get("flash_last_error"),
                    "flash_last_error_time": self._attr_extra_state_attributes.get("flash_last_error_time"),
                    # EPIC-006-STAB Phase 3
                    "device_ip": self._attr_extra_state_attributes.get("device_ip"),
                    "ip_resolution_method": self._attr_extra_state_attributes.get("ip_resolution_method"),
                    "ip_resolution_duration_ms": self._attr_extra_state_attributes.get("ip_resolution_duration_ms"),
                    "flash_build_id": self._attr_extra_state_attributes.get("flash_build_id"),
                }

            # EPIC-007: Merge build cache metadata (from build_stage event)
            build_meta_attrs = {}
            if self._build_meta:
                build_meta_attrs = self._build_meta
            else:
                build_meta_attrs = {
                    "last_build_cache_hit": self._attr_extra_state_attributes.get("last_build_cache_hit"),
                    "last_build_id": self._attr_extra_state_attributes.get("last_build_id"),
                    "last_build_artifact_path": self._attr_extra_state_attributes.get("last_build_artifact_path"),
                    "last_build_firmware_size": self._attr_extra_state_attributes.get("last_build_firmware_size"),
                    "last_build_target_device": self._attr_extra_state_attributes.get("last_build_target_device"),
                    "last_build_registry_file": self._attr_extra_state_attributes.get("last_build_registry_file"),
                    "last_build_backend": self._attr_extra_state_attributes.get("last_build_backend"),
                    "last_build_force_rebuild": self._attr_extra_state_attributes.get("last_build_force_rebuild"),
                }

            # EPIC-004 SPEC-20260514: install_prepared_firmware metadata
            # (from install_stage events). Customer-facing only — never
            # carries OTA password / secret values.
            install_state_attrs: dict[str, Any]
            if self._install_state:
                install_state_attrs = self._install_state
            else:
                install_state_attrs = {
                    "last_install_stage": self._attr_extra_state_attributes.get("last_install_stage"),
                    "last_install_progress": self._attr_extra_state_attributes.get("last_install_progress"),
                    "last_install_target_device": self._attr_extra_state_attributes.get("last_install_target_device"),
                    "last_install_firmware_size": self._attr_extra_state_attributes.get("last_install_firmware_size"),
                    "last_install_success_at": self._attr_extra_state_attributes.get("last_install_success_at"),
                    "last_install_error": self._attr_extra_state_attributes.get("last_install_error"),
                    "last_install_error_at": self._attr_extra_state_attributes.get("last_install_error_at"),
                }

            self._attr_extra_state_attributes = {
                # Contract v1.0.0 attributes
                "version": VERSION,
                "contract_version": CONTRACT_VERSION,
                "last_discovery": last_discovery,
                "devices_total": total_devices,
                "devices_online": devices_online,
                "devices_offline": devices_offline,
                "active_device": selected,
                "active_device_kind": selected_kind,
                "last_error": last_error or op_attrs.get("last_error"),
                "last_error_time": last_error_time or op_attrs.get("last_error_time"),

                # EPIC-005-A1: Factory + Production device lists
                "factory_devices": [d.name for d in factory_devices],
                "production_devices": [d.name for d in production_devices],
                "factory_count": len(factory_devices),
                "production_count": len(production_devices),

                # Phase 3 extensions (operation lifecycle)
                "op_state": op_attrs.get("op_state"),
                "op_name": op_attrs.get("op_name"),
                "op_started": op_attrs.get("op_started"),
                "op_finished": op_attrs.get("op_finished"),
                "op_progress": op_attrs.get("op_progress"),
                "op_duration_ms": op_attrs.get("op_duration_ms"),
                "last_action": op_attrs.get("last_action"),
                "last_action_time": op_attrs.get("last_action_time"),

                # Gates attributes (Phase 3 + User Directive)
                **gate_attrs,

                # Flash attributes (Phase 3.3)
                **flash_attrs,

                # EPIC-007: Build cache observability
                **build_meta_attrs,

                # EPIC-004 SPEC-20260514: install state for the Firmware card
                **install_state_attrs,

                # Issue #7: per-device firmware/update display history.
                _FIRMWARE_HISTORY_BY_DEVICE_ATTR: self._firmware_history_by_device,
            }

        except Exception as e:
            _LOGGER.error("Error updating status sensor: %s", e, exc_info=True)
            self._attr_native_value = STATE_ERROR
            self._attr_extra_state_attributes = {
                "version": VERSION,
                "contract_version": CONTRACT_VERSION,
                "last_error": str(e),
                "last_error_time": datetime.now(timezone.utc).isoformat(),
            }


class PVAutonomyOpsDevicesCountSensor(SensorEntity):
    """Output H: sensor.pvautonomy_ops_devices_count."""

    _attr_name = "PVAutonomy Devices"

    def __init__(self, input_reader: ContractInputReader, entry_id: str | None = None, *, is_legacy: bool = False) -> None:
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_DEVICE_COUNT_SENSOR}" if entry_id else ENTITY_DEVICE_COUNT_SENSOR
        self._attr_suggested_object_id = ENTITY_DEVICE_COUNT_SENSOR
        self.input_reader = input_reader
        # EPIC-006-STAB Phase 2: Legacy entities disabled by default
        if is_legacy:
            self._attr_entity_registry_enabled_default = False
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {
            "online": 0,
            "offline": 0,
            "unknown": 0,
            "factory": 0,
            "production": 0,
        }

    async def async_update(self) -> None:
        try:
            from .discovery import (
                CONNECTIVITY_OFFLINE,
                CONNECTIVITY_ONLINE,
                CONNECTIVITY_UNKNOWN,
                DEVICE_KIND_FACTORY,
                DEVICE_KIND_PRODUCTION,
            )

            # EPIC-005-A1: Single call replaces legacy+registry+dedup logic
            all_devices = await self.input_reader.get_all_discovered_devices()

            online = sum(1 for d in all_devices if d.connectivity == CONNECTIVITY_ONLINE)
            offline = sum(1 for d in all_devices if d.connectivity == CONNECTIVITY_OFFLINE)
            unknown = sum(1 for d in all_devices if d.connectivity == CONNECTIVITY_UNKNOWN)
            factory_count = sum(1 for d in all_devices if d.state == DEVICE_KIND_FACTORY)
            production_count = sum(1 for d in all_devices if d.state == DEVICE_KIND_PRODUCTION)

            self._attr_native_value = len(all_devices)
            self._attr_extra_state_attributes = {
                "online": online,
                "offline": offline,
                "unknown": unknown,
                "factory": factory_count,
                "production": production_count,
            }

        except Exception as e:
            _LOGGER.error("Error updating device count sensor: %s", e, exc_info=True)
            self._attr_native_value = 0
            self._attr_extra_state_attributes = {"online": 0, "offline": 0, "unknown": 0}


class PVAutonomyOpsWizardSensor(SensorEntity):
    """Wizard stepper state sensor (P3-13-001).

    Exposes the entire wizard state as sensor attributes so Lovelace
    cards can render the stepper UI without tab switching.
    """

    _attr_name = "PVAutonomy Wizard"
    _attr_icon = "mdi:wizard-hat"
    _attr_should_poll = False  # Event-driven updates only

    def __init__(self, wizard_engine: WizardEngine, entry_id: str | None = None) -> None:
        # WP3-Hotfix: per-entry unique_id to prevent collisions
        self._attr_unique_id = f"{entry_id}_{ENTITY_WIZARD_SENSOR}" if entry_id else ENTITY_WIZARD_SENSOR
        self._attr_suggested_object_id = ENTITY_WIZARD_SENSOR
        self._engine = wizard_engine
        self._unsub: Any = None
        self._update_attrs()

    def _update_attrs(self) -> None:
        """Sync sensor state from wizard engine."""
        ws = self._engine.state
        self._attr_native_value = ws.stage.value
        self._attr_extra_state_attributes = ws.to_dict()

    async def async_added_to_hass(self) -> None:
        """Register wizard state listener."""
        await super().async_added_to_hass()

        def _on_wizard_change() -> None:
            self._update_attrs()
            # Thread-safe for HA 2026.2+: async_write_ha_state must be
            # called from the event loop thread.
            try:
                asyncio.get_running_loop()
                self.async_write_ha_state()
            except RuntimeError:
                self.hass.loop.call_soon_threadsafe(
                    self.async_write_ha_state
                )

        self._unsub = self._engine.add_listener(_on_wizard_change)
        _LOGGER.debug("Wizard sensor registered listener")

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe wizard listener."""
        if self._unsub:
            self._unsub()
            self._unsub = None
