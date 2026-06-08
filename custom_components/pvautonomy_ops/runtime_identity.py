"""Runtime identity helpers for legacy device metadata.

Some early metadata-store entries were auto-created from the device name and
therefore have an empty ``mac_suffix``. Build/install flows still need the
suffix to resolve per-device ESPHome secrets. This module derives it from the
matching ESPHome config entry without mutating Home Assistant storage.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from .mac_utils import InvalidMACError, canonical_mac_last6

_LOGGER = logging.getLogger(__name__)


def _normalize_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _extract_suffix(value: Any) -> str:
    try:
        return canonical_mac_last6(str(value or ""))
    except InvalidMACError:
        return ""


def _metadata_names(metadata: Any) -> set[str]:
    names = {
        _normalize_name(getattr(metadata, "device_id", "")),
        _normalize_name(getattr(metadata, "device_slug", "")),
    }
    # Defensive for foreign objects (e.g. metadata mocks without ensure_slug)
    with contextlib.suppress(Exception):  # pragma: no cover
        names.add(_normalize_name(metadata.ensure_slug()))
    return {name for name in names if name}


async def async_resolve_metadata_mac_suffix(
    hass: HomeAssistant,
    metadata: Any,
) -> str:
    """Return metadata MAC suffix, deriving it from ESPHome if missing.

    The function is read-only. It never updates ``pvautonomy_ops_devices``.
    """
    direct = _extract_suffix(getattr(metadata, "mac_suffix", ""))
    if direct:
        return direct

    target_names = _metadata_names(metadata)
    if not target_names:
        return ""

    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if async_entries is None:
        return ""

    try:
        entries = async_entries("esphome")
    except TypeError:
        entries = async_entries()
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Failed to inspect ESPHome config entries", exc_info=True)
        return ""

    for entry in entries or []:
        data = getattr(entry, "data", {}) or {}
        entry_names = {
            _normalize_name(data.get("device_name")),
            _normalize_name(data.get("name")),
            _normalize_name(getattr(entry, "title", "")),
        }
        if not (target_names & {name for name in entry_names if name}):
            continue

        for candidate in (
            getattr(entry, "unique_id", ""),
            data.get("mac"),
            data.get("mac_address"),
            getattr(entry, "title", ""),
            data.get("device_name"),
        ):
            suffix = _extract_suffix(candidate)
            if suffix:
                _LOGGER.info(
                    "Resolved missing metadata mac_suffix for %s from ESPHome "
                    "entry %s",
                    getattr(metadata, "device_id", "<unknown>"),
                    getattr(entry, "entry_id", "")[:8],
                )
                return suffix

    return ""


def entry_owner_ha_device_id(entry: Any) -> str:
    """Return the HA device UUID this ConfigEntry is bound to (read-only).

    Reads ``options.ha_device_id`` (promoted into root options during
    ``async_setup_entry``) and falls back to
    ``options._initial_device.ha_device_id`` for the bootstrap window before
    promotion. Returns ``""`` when the entry carries no owner binding (legacy
    entries), so callers can treat empty as "unknown / do not assert".
    """
    opts = getattr(entry, "options", None) or {}
    if not isinstance(opts, Mapping):
        return ""
    val = str(opts.get("ha_device_id") or "").strip()
    if val:
        return val
    initial = opts.get("_initial_device")
    if isinstance(initial, Mapping):
        return str(initial.get("ha_device_id") or "").strip()
    return ""


def device_metadata_matches_esphome(
    metadata: Any,
    *,
    esphome_name: str = "",
    esphome_title: str = "",
    mac_suffix: str = "",
    ha_device_id: str = "",
) -> bool:
    """Return ``True`` iff ``metadata`` belongs to the same physical
    device as the supplied ESPHome attributes.

    EPIC-004 follow-up: the Config Flow wizard uses this to mark legacy
    devices as ``— re-flash`` even when the persisted metadata has empty
    ``mac_suffix`` / ``ha_device_id`` fields (e.g. metadata seeded via
    the name-parsing fallback in :meth:`PVAutonomyMetadataStore.resolve`,
    which leaves ``mac_suffix=""``).

    Match precedence (any single hit is sufficient):

    1. ``ha_device_id`` matches ``metadata.ha_device_id`` — the strongest
       signal for modern wizard-created metadata.
    2. ``mac_suffix`` matches ``metadata.mac_suffix`` — works as soon as
       MAC has been observed once (e.g. via runtime_identity backfill).
    3. ``esphome_name`` or ``esphome_title`` normalizes to one of
       ``metadata.device_id``, ``metadata.device_slug``, or
       ``metadata.ensure_slug()`` — covers legacy metadata with empty
       MAC where only the canonical device slug ties the two together.

    The function is read-only: it never touches the metadata store, the
    config entry registry, or the device registry.
    """
    meta_ha_device_id = str(getattr(metadata, "ha_device_id", "") or "").strip()
    if ha_device_id and meta_ha_device_id and ha_device_id == meta_ha_device_id:
        return True

    meta_suffix = _extract_suffix(getattr(metadata, "mac_suffix", ""))
    incoming_suffix = _extract_suffix(mac_suffix)
    if meta_suffix and incoming_suffix and meta_suffix == incoming_suffix:
        return True

    target_names = {
        _normalize_name(esphome_name),
        _normalize_name(esphome_title),
    }
    target_names = {name for name in target_names if name}
    if target_names and (target_names & _metadata_names(metadata)):
        return True

    return False


async def async_resolve_metadata_esphome_host(
    hass: HomeAssistant,
    metadata: Any,
) -> str:
    """Return the ESPHome config-entry host for ``metadata`` if known."""
    for entry in _matching_esphome_entries(hass, metadata):
        host = str((getattr(entry, "data", {}) or {}).get("host") or "").strip()
        if host:
            return host
    return ""


def _matching_esphome_entries(hass: HomeAssistant, metadata: Any) -> list[Any]:
    """Return ESPHome config entries matching metadata names, read-only."""
    target_names = _metadata_names(metadata)
    if not target_names:
        return []

    config_entries = getattr(hass, "config_entries", None)
    async_entries = getattr(config_entries, "async_entries", None)
    if async_entries is None:
        return []

    try:
        entries = async_entries("esphome")
    except TypeError:
        entries = async_entries()
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Failed to inspect ESPHome config entries", exc_info=True)
        return []

    matches = []
    for entry in entries or []:
        data = getattr(entry, "data", {}) or {}
        entry_names = {
            _normalize_name(data.get("device_name")),
            _normalize_name(data.get("name")),
            _normalize_name(getattr(entry, "title", "")),
        }
        if target_names & {name for name in entry_names if name}:
            matches.append(entry)
    return matches
