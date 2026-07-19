"""Installation Anchor lifecycle (M3A / #169 WP1).

Authority: Ops Contract v1.1.2 §9.4.2 (installation anchor) and OCD-6
(installation-global anchoring mechanism, DECIDED 2026-07-17).

The installation anchor is a dedicated **non-device, single-instance**
PVAutonomy Config Entry that represents the installation itself. It is the
canonical installation identity that later Grid Power onboarding (WP2+) will
use to own its installation-global entities. WP1 implements only the anchor's
lifecycle — creation, discovery, lookup, singleton enforcement, removal, and
re-onboarding. It creates **no** entities, services, dashboards, or Grid Power
functionality.

Normative behavior implemented here (§9.4.2):

- fixed domain-scoped identity, **at most one** anchor per installation;
- identity/ownership never derived from a device Config Entry or load order;
- idempotent, concurrency-safe, same-boot creation with no HA restart;
- deliberate deletion is respected and **suppressed** against automatic
  recreation (no zombie anchor on a later device setup);
- suppression is the only residual persisted separately (a small store);
- re-onboarding is explicit: clears suppression and creates a fresh anchor.

Storage keys and the concrete flow source are implementation detail
(intentionally not fixed by the Contract); they live in const.py.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    ENTRY_KIND,
    ENTRY_KIND_INSTALLATION_ANCHOR,
    INSTALLATION_ANCHOR_SOURCE,
    INSTALLATION_ANCHOR_STORE_KEY,
    INSTALLATION_ANCHOR_STORE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Serialises the ensure path so concurrent device setups / boots converge on
# exactly one anchor (belt-and-suspenders alongside the flow manager's
# unique-id dedup + _abort_if_unique_id_configured). The lock is stored per
# hass (in hass.data) rather than at module level: a module-level asyncio.Lock
# binds to the first event loop that touches it and then raises
# "bound to a different event loop" when reused across loops (each test, and
# each HA run, has its own loop). Not a dict, so it is invisible to the
# entry-slot filters in __init__.async_unload_entry.
_ENSURE_LOCK_KEY = "_installation_anchor_ensure_lock"


def _get_ensure_lock(hass: HomeAssistant) -> asyncio.Lock:
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get(_ENSURE_LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        domain_data[_ENSURE_LOCK_KEY] = lock
    return lock

# Suppression store schema: strict {"suppressed": bool}. Anything else is
# treated as malformed and fails closed (auto-creation is blocked) so a
# corrupt store never silently resurrects a deliberately-deleted anchor.
_SUPPRESSED_FIELD = "suppressed"

# Ensure outcomes (returned for observability/tests; not customer-facing).
ENSURE_CREATED = "created"
ENSURE_EXISTS = "exists"
ENSURE_IN_PROGRESS = "in_progress"
ENSURE_SUPPRESSED = "suppressed"
ENSURE_ABORTED = "aborted"


def is_installation_anchor(entry: ConfigEntry) -> bool:
    """Return True iff ``entry`` is the installation anchor.

    The discriminator is the immutable ``entry.data[ENTRY_KIND]`` value, set at
    creation. This is the single authoritative predicate — do NOT re-derive it
    from unique_id or title elsewhere.
    """
    try:
        return entry.data.get(ENTRY_KIND) == ENTRY_KIND_INSTALLATION_ANCHOR
    except AttributeError:  # defensive: malformed entry object
        return False


def is_device_entry(entry: ConfigEntry) -> bool:
    """Return True for ordinary (non-anchor) PVAutonomy Config Entries.

    Device and legacy/yaml-import entries are all "not the anchor" here; finer
    device-vs-legacy classification stays in __init__._is_legacy_entry.
    """
    return not is_installation_anchor(entry)


def async_find_anchor_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """All anchor Config Entries currently registered (expected: 0 or 1)."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if is_installation_anchor(entry)
    ]


def async_get_anchor_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """The single installation anchor entry, or None if none exists.

    Includes disabled anchors: a disabled anchor still *exists* (§9.4.2 —
    disable retains the entry and its options; it is not recreated).
    """
    anchors = async_find_anchor_entries(hass)
    return anchors[0] if anchors else None


def _suppression_store(hass: HomeAssistant) -> Store:
    return Store(
        hass,
        INSTALLATION_ANCHOR_STORE_VERSION,
        INSTALLATION_ANCHOR_STORE_KEY,
    )


async def async_is_suppressed(hass: HomeAssistant) -> bool:
    """Return True iff automatic anchor recreation is suppressed.

    Absent store → not suppressed. Malformed store → fail closed (True), so a
    corrupt residual never resurrects a deliberately-deleted anchor.
    """
    data = await _suppression_store(hass).async_load()
    if data is None:
        return False
    if not isinstance(data, dict) or not isinstance(
        data.get(_SUPPRESSED_FIELD), bool
    ):
        _LOGGER.warning(
            "Installation-anchor suppression store is malformed (%r); "
            "failing closed (treating as suppressed)",
            data,
        )
        return True
    return data[_SUPPRESSED_FIELD]


async def async_set_suppressed(hass: HomeAssistant, suppressed: bool) -> None:
    """Persist the suppression flag atomically."""
    await _suppression_store(hass).async_save({_SUPPRESSED_FIELD: suppressed})


async def async_clear_suppression(hass: HomeAssistant) -> None:
    """Remove residual suppression state entirely (no orphaned state)."""
    await _suppression_store(hass).async_remove()


def _anchor_flow_in_progress(hass: HomeAssistant) -> bool:
    """True if an anchor-creation flow is already running (race guard)."""
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if flow.get("context", {}).get("source") == INSTALLATION_ANCHOR_SOURCE:
            return True
    return False


async def async_ensure_installation_anchor(hass: HomeAssistant) -> str:
    """Ensure exactly one installation anchor exists (idempotent).

    Suppression-aware, concurrency-safe, same-boot, no restart. Returns one of
    the ENSURE_* outcome strings. Never raises for the ordinary "already
    exists / suppressed / racing" cases.
    """
    async with _get_ensure_lock(hass):
        # Already present (including a disabled anchor) → nothing to do.
        if async_get_anchor_entry(hass) is not None:
            return ENSURE_EXISTS
        # Another ensure won the race and its flow is mid-flight.
        if _anchor_flow_in_progress(hass):
            return ENSURE_IN_PROGRESS
        # Deliberately deleted → do not resurrect (no zombie anchor).
        if await async_is_suppressed(hass):
            return ENSURE_SUPPRESSED

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": INSTALLATION_ANCHOR_SOURCE}
        )

    # FlowResultType is a StrEnum, so its members equal their string value.
    if result.get("type") == FlowResultType.CREATE_ENTRY:
        _LOGGER.info("Installation anchor created")
        return ENSURE_CREATED
    # Anything else (abort/already_configured from a concurrent flow) is a
    # benign convergence on the single anchor, not an error.
    _LOGGER.debug(
        "Installation anchor ensure did not create a new entry (type=%s, "
        "reason=%s)",
        result.get("type"),
        result.get("reason"),
    )
    return ENSURE_ABORTED


async def async_reonboard_installation_anchor(hass: HomeAssistant) -> str:
    """Explicit re-onboarding primitive (§9.4.2).

    Clears deletion suppression and creates a fresh singleton anchor with the
    same constant identity. Does NOT restore any previously-deleted Grid Power
    mapping (that is WP2 and requires explicit re-selection). Internal only —
    no new service is introduced; a customer-visible entry point is WP3.
    """
    await async_clear_suppression(hass)
    return await async_ensure_installation_anchor(hass)
