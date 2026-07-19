"""Grid Power Capability foundation (M3A / #169 WP2).

Authority: Ops Contract v1.1.2 §9.4.1–§9.4.5 (Grid Power Capability) and §9.1
(ownership model). This module implements the **installation-global** Grid
Power capability that the installation anchor (§9.4.2, WP1) owns:

* an explicit capability **ownership scope** (§9.1) — ``INSTALLATION_GLOBAL`` /
  ``DEVICE`` — that is stated, not inferred from Config Entry type;
* the **source mapping** model persisted in the anchor Config Entry *options*
  (§9.4.3): primary identity is the ``(domain, platform, unique_id)`` tuple,
  with an explicit fail-closed degraded (entity-id-only) path;
* deterministic **normalization** (§9.4.4): signed-net and split import/export,
  canonical output watts, positive = import / negative = export;
* the capability **state machine** ``not_configured | validating | ready |
  not_ready`` and the measurement availability table (§9.4.5).

Scope boundary (WP2): this is the runtime *model*. The customer onboarding
flow (Detect → Map → Guide, Type B/C UI) is WP3; the Type-A guided adapter
(SHRDZM/P1) is WP4/AC-v1.1-3 (§9.4.8) and is deliberately **not** implemented
here. Mapping is settable only through the internal API below
(``GridPowerManager.async_set_mapping`` + the mapping builders). No new
service, no config-flow step, no MQTT, no dashboard.

The manager is event-driven (state-change + entity-registry + a staleness
timer). It deliberately does **not** use a ``DataUpdateCoordinator`` — this
repo does not use one, and the capability is push-shaped, not poll-shaped.
The two Grid Power sensor entities (``sensor.py``) are thin views over the
manager's computed state.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    GRID_POWER_DEFAULT_FRESHNESS_S,
    GRID_POWER_DEFAULT_PAIR_SKEW_S,
    GRID_POWER_MODE_NONE,
    GRID_POWER_MODE_SIGNED_NET,
    GRID_POWER_MODE_SPLIT,
    GRID_POWER_NORMALIZED_UNIT,
    GRID_POWER_OPTION_AUTHORITATIVE_SIGN,
    GRID_POWER_OPTION_FRESHNESS_S,
    GRID_POWER_OPTION_MODE,
    GRID_POWER_OPTION_PAIR_SKEW_S,
    GRID_POWER_OPTION_SCHEMA_VERSION,
    GRID_POWER_OPTION_SIGN_CONFIRMED,
    GRID_POWER_OPTION_SOURCES,
    GRID_POWER_OPTIONS_KEY,
    GRID_POWER_ROLE_EXPORT,
    GRID_POWER_ROLE_IMPORT,
    GRID_POWER_ROLE_NET,
    GRID_POWER_SCHEMA_VERSION,
    GRID_POWER_SIGN_CONVENTION,
    GRID_POWER_SOURCE_TYPE_MAPPED_ENTITY,
    GRID_POWER_SOURCE_TYPE_NONE,
    GRID_POWER_SRC_DEGRADED,
    GRID_POWER_SRC_DOMAIN,
    GRID_POWER_SRC_ENTITY_ID,
    GRID_POWER_SRC_PLATFORM,
    GRID_POWER_SRC_UNIQUE_ID,
    GRID_POWER_STALENESS_TICK_S,
    GRID_POWER_STATE_NOT_CONFIGURED,
    GRID_POWER_STATE_NOT_READY,
    GRID_POWER_STATE_READY,
    GRID_POWER_STATE_VALIDATING,
)

_LOGGER = logging.getLogger(__name__)

# Runtime-slot key under hass.data[DOMAIN][anchor_entry_id]. Deliberately NOT
# "config" — the anchor slot must stay invisible to device service-target
# resolution / legacy fallback / unload accounting (WP1 invariant).
GRID_POWER_MANAGER_KEY = "grid_power_manager"

# Roles that must be present for each mode (mapping-schema validation, §9.4.3).
_REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    GRID_POWER_MODE_SIGNED_NET: (GRID_POWER_ROLE_NET,),
    GRID_POWER_MODE_SPLIT: (GRID_POWER_ROLE_IMPORT, GRID_POWER_ROLE_EXPORT),
}


# ---------------------------------------------------------------------------
# Mapping model (§9.4.3) — internal API. Lives in anchor Config Entry options.
# ---------------------------------------------------------------------------


def source_ref(
    domain: str,
    platform: str,
    *,
    unique_id: str | None = None,
    entity_id: str | None = None,
    degraded: bool = False,
) -> dict:
    """Build one source reference for a mapping.

    Primary identity is the ``(domain, platform, unique_id)`` tuple. A source
    that lacks an external ``unique_id`` MAY be mapped as an explicit
    ``degraded`` (entity-id-only) Type-B source; such a mapping is validated on
    every evaluation and fails closed if its Entity ID disappears — it is never
    auto-rebound (§9.4.3).
    """
    if not degraded and not unique_id:
        raise ValueError(
            "a non-degraded source requires a stable unique_id; map without a "
            "unique_id only as an explicit degraded (entity-id-only) source"
        )
    if degraded and not entity_id:
        raise ValueError("a degraded source requires an entity_id")
    return {
        GRID_POWER_SRC_DOMAIN: domain,
        GRID_POWER_SRC_PLATFORM: platform,
        GRID_POWER_SRC_UNIQUE_ID: None if degraded else unique_id,
        GRID_POWER_SRC_ENTITY_ID: entity_id,
        GRID_POWER_SRC_DEGRADED: bool(degraded),
    }


def none_mapping() -> dict:
    """A Type-C ``none`` mapping (§9.4.7) — healthy, not a failure."""
    return {
        GRID_POWER_OPTION_SCHEMA_VERSION: GRID_POWER_SCHEMA_VERSION,
        GRID_POWER_OPTION_MODE: GRID_POWER_MODE_NONE,
    }


def signed_net_mapping(
    net: dict,
    *,
    sign_confirmed: bool = False,
    authoritative_sign: bool = False,
    freshness_s: int = GRID_POWER_DEFAULT_FRESHNESS_S,
) -> dict:
    """Build a signed-net mapping (§9.4.4).

    For a *generic* signed-net source (``authoritative_sign`` False) the
    customer sign confirmation is required before the capability can ever be
    ``ready``; a heuristic MUST NOT, alone, release ``ready`` (§9.4.4).
    """
    return {
        GRID_POWER_OPTION_SCHEMA_VERSION: GRID_POWER_SCHEMA_VERSION,
        GRID_POWER_OPTION_MODE: GRID_POWER_MODE_SIGNED_NET,
        GRID_POWER_OPTION_SOURCES: {GRID_POWER_ROLE_NET: net},
        GRID_POWER_OPTION_SIGN_CONFIRMED: bool(sign_confirmed),
        GRID_POWER_OPTION_AUTHORITATIVE_SIGN: bool(authoritative_sign),
        GRID_POWER_OPTION_FRESHNESS_S: int(freshness_s),
    }


def split_mapping(
    import_source: dict,
    export_source: dict,
    *,
    freshness_s: int = GRID_POWER_DEFAULT_FRESHNESS_S,
    pair_skew_s: int = GRID_POWER_DEFAULT_PAIR_SKEW_S,
) -> dict:
    """Build a split import/export mapping (§9.4.4)."""
    return {
        GRID_POWER_OPTION_SCHEMA_VERSION: GRID_POWER_SCHEMA_VERSION,
        GRID_POWER_OPTION_MODE: GRID_POWER_MODE_SPLIT,
        GRID_POWER_OPTION_SOURCES: {
            GRID_POWER_ROLE_IMPORT: import_source,
            GRID_POWER_ROLE_EXPORT: export_source,
        },
        GRID_POWER_OPTION_FRESHNESS_S: int(freshness_s),
        GRID_POWER_OPTION_PAIR_SKEW_S: int(pair_skew_s),
    }


def validate_mapping_dict(mapping: dict | None) -> dict | None:
    """Validate + normalize a mapping dict for persistence (§9.4.3).

    Returns a clean dict (with ``schema_version`` stamped) or ``None`` for an
    unconfigured / explicit ``none`` mapping. Raises ``ValueError`` on a
    structurally invalid mapping (unknown mode, missing required source role,
    malformed source ref) — fail closed rather than persist garbage.
    """
    if mapping is None:
        return None
    if not isinstance(mapping, dict):
        raise ValueError(f"mapping must be a dict, got {type(mapping).__name__}")

    mode = mapping.get(GRID_POWER_OPTION_MODE, GRID_POWER_MODE_NONE)
    if mode == GRID_POWER_MODE_NONE:
        return None
    if mode not in _REQUIRED_ROLES:
        raise ValueError(f"unsupported grid-power mode: {mode!r}")

    sources = mapping.get(GRID_POWER_OPTION_SOURCES) or {}
    if not isinstance(sources, dict):
        raise ValueError("mapping sources must be a dict")
    clean_sources: dict[str, dict] = {}
    for role in _REQUIRED_ROLES[mode]:
        src = sources.get(role)
        if not isinstance(src, dict):
            raise ValueError(f"mode {mode!r} requires source role {role!r}")
        clean_sources[role] = _validate_source_ref(src)

    out: dict = {
        GRID_POWER_OPTION_SCHEMA_VERSION: GRID_POWER_SCHEMA_VERSION,
        GRID_POWER_OPTION_MODE: mode,
        GRID_POWER_OPTION_SOURCES: clean_sources,
        GRID_POWER_OPTION_FRESHNESS_S: int(
            mapping.get(GRID_POWER_OPTION_FRESHNESS_S, GRID_POWER_DEFAULT_FRESHNESS_S)
        ),
    }
    if mode == GRID_POWER_MODE_SIGNED_NET:
        out[GRID_POWER_OPTION_SIGN_CONFIRMED] = bool(
            mapping.get(GRID_POWER_OPTION_SIGN_CONFIRMED, False)
        )
        out[GRID_POWER_OPTION_AUTHORITATIVE_SIGN] = bool(
            mapping.get(GRID_POWER_OPTION_AUTHORITATIVE_SIGN, False)
        )
    if mode == GRID_POWER_MODE_SPLIT:
        out[GRID_POWER_OPTION_PAIR_SKEW_S] = int(
            mapping.get(GRID_POWER_OPTION_PAIR_SKEW_S, GRID_POWER_DEFAULT_PAIR_SKEW_S)
        )
    return out


def _validate_source_ref(src: dict) -> dict:
    domain = src.get(GRID_POWER_SRC_DOMAIN)
    platform = src.get(GRID_POWER_SRC_PLATFORM)
    unique_id = src.get(GRID_POWER_SRC_UNIQUE_ID)
    entity_id = src.get(GRID_POWER_SRC_ENTITY_ID)
    degraded = bool(src.get(GRID_POWER_SRC_DEGRADED))
    if not domain or not platform:
        raise ValueError("a source ref requires domain and platform")
    if degraded:
        if not entity_id:
            raise ValueError("a degraded source requires an entity_id")
        unique_id = None
    elif not unique_id:
        raise ValueError(
            "a non-degraded source requires a stable unique_id (map without one "
            "only as an explicit degraded source)"
        )
    return {
        GRID_POWER_SRC_DOMAIN: domain,
        GRID_POWER_SRC_PLATFORM: platform,
        GRID_POWER_SRC_UNIQUE_ID: unique_id,
        GRID_POWER_SRC_ENTITY_ID: entity_id,
        GRID_POWER_SRC_DEGRADED: degraded,
    }


def entity_to_source_ref(hass: HomeAssistant, entity_id: str) -> dict:
    """Resolve a customer-selected Entity ID to a persisted source ref (§9.4.3).

    A registry-stable source (has an external ``unique_id``) is persisted by
    its ``(domain, platform, unique_id)`` tuple. A source without a registry
    unique_id is persisted as an explicit **degraded** (entity-id-only) Type-B
    mapping — reduced durability, fail-closed on rename/disappear, never
    auto-rebound. This is the single mapping-construction path shared by the
    onboarding flow so the flow never invents its own identity model.
    """
    domain = entity_id.split(".", 1)[0]
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is not None and reg_entry.unique_id:
        return source_ref(domain, reg_entry.platform, unique_id=reg_entry.unique_id)
    platform = reg_entry.platform if reg_entry is not None else "manual"
    return source_ref(domain, platform, entity_id=entity_id, degraded=True)


@dataclass(frozen=True)
class _SourceSpec:
    """A resolved-at-parse-time source reference."""

    role: str
    domain: str
    platform: str
    unique_id: str | None
    entity_id: str | None
    degraded: bool


@dataclass(frozen=True)
class _Mapping:
    mode: str
    sources: tuple[_SourceSpec, ...]
    freshness_s: int
    pair_skew_s: int
    sign_confirmed: bool
    authoritative_sign: bool


def _parse_mapping(options: dict) -> _Mapping | None:
    return parse_mapping_value(options.get(GRID_POWER_OPTIONS_KEY))


def parse_mapping_value(raw: dict | None) -> _Mapping | None:
    """Parse a raw grid-power mapping dict (the options value) into a _Mapping.

    Returns None for none / unknown / malformed → the healthy unconfigured
    (Type-C) state. Shared by the manager (persisted options) and the dry-run
    validator (candidate mappings from the onboarding flow) so both interpret a
    mapping identically — no parsing duplication.
    """
    if not isinstance(raw, dict):
        return None
    mode = raw.get(GRID_POWER_OPTION_MODE, GRID_POWER_MODE_NONE)
    if mode not in _REQUIRED_ROLES:
        return None  # none / unknown → unconfigured (healthy Type-C)
    sources_raw = raw.get(GRID_POWER_OPTION_SOURCES) or {}
    specs: list[_SourceSpec] = []
    for role in _REQUIRED_ROLES[mode]:
        src = sources_raw.get(role)
        if not isinstance(src, dict):
            return None  # malformed persisted mapping → treat as unconfigured
        specs.append(
            _SourceSpec(
                role=role,
                domain=src.get(GRID_POWER_SRC_DOMAIN),
                platform=src.get(GRID_POWER_SRC_PLATFORM),
                unique_id=src.get(GRID_POWER_SRC_UNIQUE_ID),
                entity_id=src.get(GRID_POWER_SRC_ENTITY_ID),
                degraded=bool(src.get(GRID_POWER_SRC_DEGRADED)),
            )
        )
    return _Mapping(
        mode=mode,
        sources=tuple(specs),
        freshness_s=int(raw.get(GRID_POWER_OPTION_FRESHNESS_S, GRID_POWER_DEFAULT_FRESHNESS_S)),
        pair_skew_s=int(raw.get(GRID_POWER_OPTION_PAIR_SKEW_S, GRID_POWER_DEFAULT_PAIR_SKEW_S)),
        sign_confirmed=bool(raw.get(GRID_POWER_OPTION_SIGN_CONFIRMED, False)),
        authoritative_sign=bool(raw.get(GRID_POWER_OPTION_AUTHORITATIVE_SIGN, False)),
    )


# ---------------------------------------------------------------------------
# Sample reading / normalization (§9.4.4)
# ---------------------------------------------------------------------------


@dataclass
class _Sample:
    kind: str  # "ok" | "pending" | "invalid"
    value_w: float | None = None
    unit: str | None = None
    # Authoritative observation-age basis for freshness + pair coherence
    # (§9.4.4). This is the source's last *report* time, not its last *change*
    # time: a live meter reporting an unchanged value (e.g. a steady 0 W net)
    # is fresh evidence of liveness. HA's ``last_updated`` freezes on an
    # identical state+attributes report, so using it would falsely mark a
    # steady source stale; ``last_reported`` advances on every report.
    observed_at: datetime | None = None
    reason: str | None = None


def _redact_identity(value: str | None) -> str | None:
    """Opaque, non-reversible correlation id for diagnostics/logs (WP5).

    A truncated SHA-256 of a source identifier — stable enough to correlate
    across a diagnostics capture, but never the raw unique_id / entity_id /
    device token. Returns None for an empty value.
    """
    if not value:
        return None
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _to_watts(raw: float, unit: str | None) -> float | None:
    if unit in (None, "", "W", "w"):
        return raw
    if unit in ("kW", "kw"):
        return raw * 1000.0
    if unit in ("mW", "mw"):
        return raw / 1000.0
    return None  # unsupported unit → invalid


def _read_sample(hass: HomeAssistant, entity_id: str, allow_pending: bool) -> _Sample:
    """Read one source entity into a normalized watts sample.

    ``allow_pending`` is True for stable (registry-resolved) sources: a missing
    state object is a transient (entity registered but not yet producing, or
    MQTT-discovery republish) and maps to *validating*. It is False for a
    degraded (entity-id-only) source, where a missing entity is a fail-closed
    *not_ready* (§9.4.3).
    """
    state = hass.states.get(entity_id)
    if state is None:
        if allow_pending:
            return _Sample(kind="pending")
        return _Sample(kind="invalid", reason="source_missing")
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return _Sample(kind="invalid", reason=f"source_{state.state}")
    try:
        raw = float(state.state)
    except (ValueError, TypeError):
        return _Sample(kind="invalid", reason="non_numeric")
    if not math.isfinite(raw):
        return _Sample(kind="invalid", reason="non_finite")
    unit = state.attributes.get("unit_of_measurement")
    value_w = _to_watts(raw, unit)
    if value_w is None:
        return _Sample(kind="invalid", reason="unsupported_unit")
    observed_at = getattr(state, "last_reported", None) or state.last_updated
    return _Sample(
        kind="ok", value_w=value_w, unit=unit, observed_at=observed_at
    )


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------


@dataclass
class _Evaluation:
    state: str = GRID_POWER_STATE_NOT_CONFIGURED
    power_w: float | None = None
    reason: str | None = None
    source_entities: list[str] = field(default_factory=list)
    source_units: list[str | None] = field(default_factory=list)

    @property
    def available(self) -> bool:
        # Measurement availability table (§9.4.5): ready → numeric (available);
        # validating → unknown (available, value None); everything else →
        # unavailable.
        return self.state in (GRID_POWER_STATE_READY, GRID_POWER_STATE_VALIDATING)


@dataclass(frozen=True)
class CandidateValidation:
    """Result of a dry-run mapping validation (WP3 onboarding feedback)."""

    state: str
    reason: str | None
    power_w: float | None
    degraded: bool
    source_entities: list[str]

    @property
    def is_ready(self) -> bool:
        return self.state == GRID_POWER_STATE_READY


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class GridPowerManager:
    """Installation-global Grid Power capability owner (anchor-scoped).

    Owns the mapping (anchor options), computes the capability/measurement
    state (§9.4.5), and pushes changes to the two sensor views. Event-driven:
    source state changes, entity-registry updates (rename/replace/remove), and
    a staleness timer.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._mapping: _Mapping | None = None
        self._eval = _Evaluation()
        self._last_valid_value: float | None = None
        self._last_update: datetime | None = None
        self._listeners: list[Callable[[], None]] = []
        self._tracked_entity_ids: tuple[str, ...] = ()
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_registry: Callable[[], None] | None = None
        self._unsub_timer: Callable[[], None] | None = None
        self._unsub_entry_update: Callable[[], None] | None = None
        self._started = False

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        """Wire subscriptions and perform the first evaluation.

        Subscriptions are stored on the manager and torn down deterministically
        in :meth:`shutdown` (called from the anchor's ``async_unload_entry``),
        so the manager leaks nothing across unload / reload / restart.
        """
        self._mapping = _parse_mapping(dict(self._entry.options))
        self._resubscribe_sources()

        self._unsub_registry = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_event
        )
        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._handle_timer,
            timedelta(seconds=GRID_POWER_STALENESS_TICK_S),
        )
        self._unsub_entry_update = self._entry.add_update_listener(
            self._handle_entry_updated
        )

        self._started = True
        self._evaluate()

    @callback
    def shutdown(self) -> None:
        """Tear down every subscription. Idempotent."""
        self._started = False
        self._async_teardown_state_tracker()
        for attr in ("_unsub_registry", "_unsub_timer", "_unsub_entry_update"):
            unsub = getattr(self, attr)
            if unsub is not None:
                unsub()
                setattr(self, attr, None)
        self._listeners.clear()

    @callback
    def _async_teardown_state_tracker(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None

    # -- view wiring --------------------------------------------------------

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a sensor view's write callback; returns an unsubscribe."""
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # -- internal mapping API (§9.4.3; customer flow = WP3) -----------------

    async def async_set_mapping(self, mapping: dict | None) -> None:
        """Persist a new source mapping atomically into the anchor options.

        Validates first (fail closed on a structurally invalid mapping), then
        writes via ``async_update_entry`` and re-evaluates. Deleting a mapping
        (``None`` / ``none``) removes the options key — Grid Power returns to
        the healthy ``not_configured`` state; a deleted mapping is never
        silently restored (§9.4.2/§9.4.3).
        """
        validated = validate_mapping_dict(mapping)
        new_options = dict(self._entry.options)
        if validated is None:
            new_options.pop(GRID_POWER_OPTIONS_KEY, None)
        else:
            new_options[GRID_POWER_OPTIONS_KEY] = validated
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        # async_update_entry fires the update listener as a task; also refresh
        # synchronously so callers (tests / internal API) see the effect now.
        self._reload_from_options()

    @callback
    def validate_candidate(self, mapping: dict | None) -> "CandidateValidation":
        """Dry-run a candidate mapping WITHOUT persisting it (§9.4.5).

        Used by the WP3 onboarding flow to give the customer validation
        feedback before committing. Runs the exact same state machine as live
        evaluation against current source states, so the flow never
        re-implements normalization/validation. Does not mutate manager state.
        """
        validated = validate_mapping_dict(mapping)  # raises on structural error
        parsed = parse_mapping_value(validated)
        result = self._compute(parsed)
        degraded = bool(parsed and any(s.degraded for s in parsed.sources))
        return CandidateValidation(
            state=result.state,
            reason=result.reason,
            power_w=result.power_w,
            degraded=degraded,
            source_entities=list(result.source_entities),
        )

    @callback
    def _reload_from_options(self) -> None:
        self._mapping = _parse_mapping(dict(self._entry.options))
        self._resubscribe_sources()
        self._evaluate()

    async def _handle_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        # External options writes (e.g. a future WP3 reconfigure flow) land
        # here. Re-read in place — the anchor must NOT do a full entry reload.
        self._reload_from_options()

    # -- source resolution + subscription ----------------------------------

    def _resolve_entity_id(self, spec: _SourceSpec) -> str | None:
        """Resolve a source spec to its current Entity ID (§9.4.1/§9.4.3).

        Stable identity: resolve the ``(domain, platform, unique_id)`` tuple
        dynamically through the Entity Registry — never a stored literal
        Entity ID. Degraded: the configured Entity ID is the identity.
        """
        if spec.degraded or not spec.unique_id:
            return spec.entity_id
        ent_reg = er.async_get(self.hass)
        return ent_reg.async_get_entity_id(spec.domain, spec.platform, spec.unique_id)

    def _resolved_entity_ids(self) -> list[str | None]:
        if self._mapping is None:
            return []
        return [self._resolve_entity_id(s) for s in self._mapping.sources]

    @callback
    def _resubscribe_sources(self) -> None:
        entity_ids = tuple(e for e in self._resolved_entity_ids() if e)
        if entity_ids == self._tracked_entity_ids:
            return
        self._async_teardown_state_tracker()
        self._tracked_entity_ids = entity_ids
        if entity_ids:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(entity_ids), self._handle_source_event
            )

    # -- event handlers -----------------------------------------------------

    @callback
    def _handle_source_event(self, event: Event) -> None:
        self._evaluate()

    @callback
    def _handle_timer(self, now: datetime) -> None:
        # Re-evaluate on a cadence so staleness (a source that stops updating)
        # is caught even though no state-change event fires.
        self._evaluate()

    @callback
    def _handle_registry_event(self, event: Event) -> None:
        # A source may have been renamed (unique_id stable → new Entity ID) or
        # removed (tuple no longer resolves → fail closed). Re-resolve, adjust
        # subscriptions, and re-evaluate.
        if event.data.get("action") not in ("create", "update", "remove"):
            return
        self._resubscribe_sources()
        self._evaluate()

    # -- the state machine (§9.4.5) ----------------------------------------

    @callback
    def _evaluate(self) -> None:
        if not self._started:
            return
        result = self._compute(self._mapping)
        if result.state == GRID_POWER_STATE_READY:
            self._last_valid_value = result.power_w
        self._last_update = dt_util.utcnow()

        changed = (
            result.state != self._eval.state
            or result.power_w != self._eval.power_w
            or result.reason != self._eval.reason
        )
        self._eval = result
        if changed:
            self._notify()

    def _compute(self, mapping: _Mapping | None) -> _Evaluation:
        if mapping is None:
            return _Evaluation(state=GRID_POWER_STATE_NOT_CONFIGURED)

        # Generic signed-net: a heuristic MUST NOT alone release ready — sign
        # confirmation is required first (§9.4.4).
        if (
            mapping.mode == GRID_POWER_MODE_SIGNED_NET
            and not mapping.authoritative_sign
            and not mapping.sign_confirmed
        ):
            return _Evaluation(
                state=GRID_POWER_STATE_NOT_READY, reason="sign_unconfirmed"
            )

        # Resolve every source (§9.4.3). An unresolvable stable tuple fails
        # closed → not_ready, never an auto-rebind to a different source.
        resolved: dict[str, str] = {}
        for spec in mapping.sources:
            entity_id = self._resolve_entity_id(spec)
            if not entity_id:
                return _Evaluation(
                    state=GRID_POWER_STATE_NOT_READY, reason="source_unresolved"
                )
            resolved[spec.role] = entity_id

        samples: dict[str, _Sample] = {}
        for spec in mapping.sources:
            samples[spec.role] = _read_sample(
                self.hass, resolved[spec.role], allow_pending=not spec.degraded
            )

        source_entities = [resolved[s.role] for s in mapping.sources]
        source_units = [samples[s.role].unit for s in mapping.sources]

        # Validation / recovery in progress → unknown (§9.4.5).
        if any(s.kind == "pending" for s in samples.values()):
            return _Evaluation(
                state=GRID_POWER_STATE_VALIDATING,
                reason="validating",
                source_entities=source_entities,
                source_units=source_units,
            )
        # Invalid / missing / ambiguous → unavailable.
        for spec in mapping.sources:
            sample = samples[spec.role]
            if sample.kind == "invalid":
                return _Evaluation(
                    state=GRID_POWER_STATE_NOT_READY,
                    reason=f"{spec.role}:{sample.reason}",
                    source_entities=source_entities,
                    source_units=source_units,
                )

        now = dt_util.utcnow()
        for spec in mapping.sources:
            sample = samples[spec.role]
            age = (now - sample.observed_at).total_seconds()
            if age > mapping.freshness_s:
                return _Evaluation(
                    state=GRID_POWER_STATE_NOT_READY,
                    reason=f"{spec.role}:stale",
                    source_entities=source_entities,
                    source_units=source_units,
                )

        if mapping.mode == GRID_POWER_MODE_SIGNED_NET:
            power = samples[GRID_POWER_ROLE_NET].value_w
        else:  # split
            import_w = samples[GRID_POWER_ROLE_IMPORT].value_w
            export_w = samples[GRID_POWER_ROLE_EXPORT].value_w
            # Both channels are non-negative power magnitudes (§9.4.4).
            if import_w < 0 or export_w < 0:
                return _Evaluation(
                    state=GRID_POWER_STATE_NOT_READY,
                    reason="negative_channel",
                    source_entities=source_entities,
                    source_units=source_units,
                )
            # Pair coherence is separate from individual freshness (§9.4.4).
            # Skew is measured on the observation (report) times of the two
            # samples, consistent with the freshness basis above.
            skew = abs(
                (
                    samples[GRID_POWER_ROLE_IMPORT].observed_at
                    - samples[GRID_POWER_ROLE_EXPORT].observed_at
                ).total_seconds()
            )
            if skew > mapping.pair_skew_s:
                return _Evaluation(
                    state=GRID_POWER_STATE_NOT_READY,
                    reason="pair_skew",
                    source_entities=source_entities,
                    source_units=source_units,
                )
            # Deterministic subtraction, no tolerance blending (§9.4.4).
            power = import_w - export_w

        return _Evaluation(
            state=GRID_POWER_STATE_READY,
            power_w=power,
            source_entities=source_entities,
            source_units=source_units,
        )

    # -- read surface for the sensor views ---------------------------------

    @property
    def capability_state(self) -> str:
        return self._eval.state

    @property
    def is_ready(self) -> bool:
        return self._eval.state == GRID_POWER_STATE_READY

    @property
    def power_w(self) -> float | None:
        # Only the current, validated value is ever exposed as the measurement;
        # a stale/last-valid value MUST NOT be emitted as current (§9.4.5).
        return self._eval.power_w if self.is_ready else None

    @property
    def measurement_available(self) -> bool:
        return self._eval.available

    @property
    def source_entities(self) -> list[str]:
        return list(self._eval.source_entities)

    @property
    def capability_attributes(self) -> dict:
        """Attributes for the capability sensor (§9.4)."""
        mapping = self._mapping
        source_type = (
            GRID_POWER_SOURCE_TYPE_MAPPED_ENTITY
            if mapping is not None
            else GRID_POWER_SOURCE_TYPE_NONE
        )
        units = [u for u in self._eval.source_units if u]
        return {
            "source_entities": list(self._eval.source_entities),
            "source_type": source_type,
            "source_unit": units[0] if len(units) == 1 else (units or None),
            "normalized_unit": GRID_POWER_NORMALIZED_UNIT,
            "sign_convention": GRID_POWER_SIGN_CONVENTION,
            "import_export_interpretation": "positive=import, negative=export",
            "available": self._eval.available,
            "freshness_threshold_s": (
                mapping.freshness_s if mapping else GRID_POWER_DEFAULT_FRESHNESS_S
            ),
            "last_update": self._last_update,
            "last_valid_value": self._last_valid_value,
            "validation_state": self._eval.state,
            "validation_reason": self._eval.reason,
            "ready": self.is_ready,
            "stale_behavior": "neutral_safe_state",
        }

    @callback
    def diagnostics(self) -> dict:
        """Source-neutral, redacted capability diagnostics (WP5).

        Distinguishes the operational conditions an operator needs (healthy,
        stale, unavailable, source missing, invalid metadata/unit, invalid
        combination, unconfigured) via categorical state + reason, WITHOUT
        leaking any customer-specific identifier. Source identity is reduced to
        an opaque truncated hash; no raw unique_id, entity_id, device_id,
        config_entry_id, MQTT topic, or device token is exposed. This is the
        capability surface — it carries no SHRDZM/MQTT/OBIS-specific terms.
        """
        mapping = self._mapping
        sources: list[dict] = []
        if mapping is not None:
            for spec in mapping.sources:
                entity_id = self._resolve_entity_id(spec)
                if entity_id:
                    sample = _read_sample(
                        self.hass, entity_id, allow_pending=not spec.degraded
                    )
                    sample_status = (
                        sample.kind
                        if sample.reason is None
                        else f"{sample.kind}:{sample.reason}"
                    )
                else:
                    sample_status = "unresolved"
                sources.append(
                    {
                        "role": spec.role,
                        "domain": spec.domain,
                        "platform": spec.platform,
                        "degraded": spec.degraded,
                        # Opaque, non-reversible correlation id — never the raw
                        # unique_id/entity_id (which may embed a device token).
                        "identity_ref": _redact_identity(
                            spec.unique_id or spec.entity_id
                        ),
                        "resolved": entity_id is not None,
                        "sample": sample_status,
                    }
                )
        return {
            "configured": mapping is not None,
            "capability_state": self._eval.state,
            "validation_reason": self._eval.reason,
            "measurement_available": self._eval.available,
            "power_w": self.power_w,
            "last_valid_value": self._last_valid_value,
            "mode": mapping.mode if mapping else None,
            "sign_confirmed": bool(mapping.sign_confirmed) if mapping else None,
            "authoritative_sign": (
                bool(mapping.authoritative_sign) if mapping else None
            ),
            "freshness_threshold_s": (
                mapping.freshness_s if mapping else GRID_POWER_DEFAULT_FRESHNESS_S
            ),
            "pair_skew_max_s": (
                mapping.pair_skew_s
                if (mapping and mapping.mode == GRID_POWER_MODE_SPLIT)
                else None
            ),
            "sources": sources,
        }
