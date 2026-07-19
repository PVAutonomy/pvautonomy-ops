"""SHRDZM Type-A Grid Power source adapter (M3A / #169 WP4).

Authority: Ops Contract v1.1.2 §9.4.4 / §9.4.7 / §9.4.8. This is the **one**
concrete Grid Power source adapter in M3A. It discovers an already-integrated
SHRDZM/P1 smart meter through existing Home Assistant **MQTT Discovery** entities
and produces a *canonical, source-neutral* Grid Power mapping (the WP2 schema).
Everything downstream — validation, normalization, persistence, capability
state — remains the WP2 `GridPowerManager` runtime; this adapter never
duplicates it.

**Source neutrality (the boundary).** All SHRDZM / MQTT / OBIS / entity-registry
specifics live in THIS module. The adapter's only output is a canonical Grid
Power mapping (`(domain, platform, unique_id)` source refs) plus an opaque
identity and a non-secret label. Code outside this module must not depend on the
manufacturer string, model string, MQTT platform internals, config-entry id,
topic, device-identifier shape, or OBIS suffixes.

**Detection is registry-stable and deterministic (§9.4.8).** Device gate
(manufacturer == SHRDZM, model == SMARTMETER, shared HA device_id), entity gate
(platform == mqtt, power metadata), and channel classification by the **exact**
Entity Registry `unique_id` OBIS suffix. Never entity_id, friendly/original
name, dashboard label, MQTT topic, the renamed device name, or the underscore-
normalized entity-id suffix. The device-specific identifier prefix (the embedded
MAC/token) is treated as opaque and is never hardcoded, interpreted, or leaked.

**Production-verified registry shape (redacted):** device manufacturer
"SHRDZM" / model "SMARTMETER"; identifiers ``("mqtt", "SHRDZM-<opaque-token>")``;
entities on one device_id, platform "mqtt", `original_device_class` "power",
unit "W", `capabilities.state_class` "measurement", `unique_id` retaining the
verbatim MQTT-Discovery id with hyphenated OBIS suffixes `_1-7-0` / `_2-7-0` /
`_16-7-0`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .grid_power import signed_net_mapping, source_ref, split_mapping

_LOGGER = logging.getLogger(__name__)

# --- Device gate (registry-stable, exact) ---
SHRDZM_MANUFACTURER = "SHRDZM"
SHRDZM_MODEL = "SMARTMETER"
_MQTT_PLATFORM = "mqtt"

# --- OBIS live-power channels (§9.4.8). Classified by the EXACT hyphenated
# Entity Registry unique_id suffix — NOT substring/contains, NOT the
# underscore-normalized entity-id. Energy counters (1.8.0/2.8.0) are outside
# #169 and deliberately unclassified. ---
_OBIS_IMPORT_SUFFIX = "_1-7-0"   # momentary grid import (W), non-negative
_OBIS_EXPORT_SUFFIX = "_2-7-0"   # momentary grid export (W), non-negative
_OBIS_NET_SUFFIX = "_16-7-0"     # momentary signed net power (W)

_ROLE_IMPORT = "import"
_ROLE_EXPORT = "export"
_ROLE_NET = "net"

# --- Power metadata gate. SHRDZM emits W; accept the watt-family units WP2
# normalizes. state_class is validated only where the registry exposes it. ---
_POWER_DEVICE_CLASS = "power"
_MEASUREMENT_STATE_CLASS = "measurement"
_WATT_UNITS = ("W", "kW", "mW")

# Neutral mapping-mode labels (source-neutral, not SHRDZM/OBIS terms).
MODE_SPLIT = "split"
MODE_SIGNED_NET = "signed_net"


@dataclass(frozen=True)
class GridPowerSourceCandidate:
    """Source-neutral discovered Grid Power source — the adapter's output boundary.

    ``mapping`` is a canonical WP2 Grid Power mapping. ``source_identity`` is an
    opaque, non-secret HA identity (the registry device_id, never the customer
    MAC/token). ``display_label`` is non-secret (manufacturer/model or the user's
    own device name) and is safe to show/log. No SHRDZM/MQTT/OBIS value beyond
    this boundary is exposed to Grid Power consumers.
    """

    source_identity: str
    display_label: str
    mode: str
    mapping: dict
    channels: tuple[str, ...]


@callback
def _classify_channel(unique_id: str | None) -> str | None:
    """Classify a source by the EXACT OBIS suffix of its registry unique_id.

    Exact suffix (``str.endswith``) — never substring/contains — so `_11-7-0`,
    `_1-7-0-x`, energy `_1-8-0`, etc. never accidentally match.
    """
    if not unique_id:
        return None
    if unique_id.endswith(_OBIS_NET_SUFFIX):
        return _ROLE_NET
    if unique_id.endswith(_OBIS_IMPORT_SUFFIX):
        return _ROLE_IMPORT
    if unique_id.endswith(_OBIS_EXPORT_SUFFIX):
        return _ROLE_EXPORT
    return None


@callback
def _is_instantaneous_power(entry: er.RegistryEntry) -> bool:
    """Registry-metadata power gate (§9.4.8): device_class power, watt-family
    unit, and state_class measurement WHERE the registry exposes it."""
    if entry.original_device_class != _POWER_DEVICE_CLASS:
        return False
    if entry.unit_of_measurement not in _WATT_UNITS:
        return False
    state_class = (entry.capabilities or {}).get("state_class")
    if state_class is not None and state_class != _MEASUREMENT_STATE_CLASS:
        return False
    return True


@callback
def _is_shrdzm_device(device: dr.DeviceEntry) -> bool:
    # Exact manufacturer + model; renamed name_by_user does not affect this.
    return (
        device.manufacturer == SHRDZM_MANUFACTURER
        and device.model == SHRDZM_MODEL
    )


def _non_secret_label(device: dr.DeviceEntry) -> str:
    """A redacted, non-secret label. Prefers the user's own device name;
    otherwise manufacturer + model. NEVER the device identifiers / MAC token
    (which may appear in the discovery name/identifiers)."""
    if device.name_by_user:
        return device.name_by_user
    return f"{SHRDZM_MANUFACTURER} {SHRDZM_MODEL}"


def _build_candidate(
    hass: HomeAssistant, device: dr.DeviceEntry
) -> GridPowerSourceCandidate | None:
    """Build a validated candidate for one SHRDZM device, or None if the device
    is incomplete/ambiguous/inconsistent (fail closed)."""
    ent_reg = er.async_get(hass)
    by_role: dict[str, list[er.RegistryEntry]] = {}
    for entry in er.async_entries_for_device(
        ent_reg, device.id, include_disabled_entities=False
    ):
        if entry.platform != _MQTT_PLATFORM:
            continue
        role = _classify_channel(entry.unique_id)
        if role is None:
            continue
        if not _is_instantaneous_power(entry):
            continue
        by_role.setdefault(role, []).append(entry)

    # Ambiguous within the device (two entities classify to the same OBIS role).
    if any(len(entries) > 1 for entries in by_role.values()):
        _LOGGER.debug(
            "SHRDZM device %s has ambiguous duplicate OBIS channels; skipped",
            device.id,
        )
        return None

    imp = by_role.get(_ROLE_IMPORT, [None])[0]
    exp = by_role.get(_ROLE_EXPORT, [None])[0]
    net = by_role.get(_ROLE_NET, [None])[0]

    # Valid combinations (§9.4.4 modes + §9.4.8 OBIS mapping): prefer split when
    # BOTH import and export are present; else signed-net when the net channel
    # is present; otherwise incomplete → fail closed. Not all three channels are
    # mandatory (§9.4.8 uses MAY).
    if imp is not None and exp is not None:
        mapping = split_mapping(
            source_ref(imp.domain, imp.platform, unique_id=imp.unique_id),
            source_ref(exp.domain, exp.platform, unique_id=exp.unique_id),
        )
        mode = MODE_SPLIT
        channels = (_ROLE_IMPORT, _ROLE_EXPORT) + (
            (_ROLE_NET,) if net is not None else ()
        )
    elif net is not None:
        # SHRDZM is a known Type-A adapter whose signed-net sign convention is
        # authoritative (positive = import, negative = export; §9.4.4), so no
        # customer sign confirmation is required.
        mapping = signed_net_mapping(
            source_ref(net.domain, net.platform, unique_id=net.unique_id),
            authoritative_sign=True,
        )
        mode = MODE_SIGNED_NET
        channels = (_ROLE_NET,)
    else:
        return None

    return GridPowerSourceCandidate(
        source_identity=device.id,
        display_label=_non_secret_label(device),
        mode=mode,
        mapping=mapping,
        channels=channels,
    )


@callback
def discover_shrdzm_candidates(
    hass: HomeAssistant,
) -> list[GridPowerSourceCandidate]:
    """Deterministically discover valid SHRDZM Grid Power candidates.

    Registry-stable evidence only. Returns validated candidates sorted by their
    opaque identity (deterministic order for multi-candidate selection). Present
    but incomplete/ambiguous SHRDZM devices are excluded (fail closed); use
    :func:`count_shrdzm_devices` for guidance about their presence.
    """
    dev_reg = dr.async_get(hass)
    candidates = [
        candidate
        for device in dev_reg.devices.values()
        if _is_shrdzm_device(device)
        if (candidate := _build_candidate(hass, device)) is not None
    ]
    candidates.sort(key=lambda c: c.source_identity)
    return candidates


@callback
def count_shrdzm_devices(hass: HomeAssistant) -> int:
    """Total SHRDZM devices present (valid or not) — for actionable guidance."""
    dev_reg = dr.async_get(hass)
    return sum(1 for device in dev_reg.devices.values() if _is_shrdzm_device(device))
