"""PVAutonomy Ops Integration.

Phase 2: READ Inputs A-F + WRITE Outputs G-H (COMPLETE)
Phase 3: EXECUTE Actions A-G + Buttons I-L (IN PROGRESS)
EPIC-006 WP3: Config Flow Wizard + Flash Pipeline Integration

Contract: ops-contract-v1.md (v1.0.0)
Directive: D-ADDON-002, D-ADDON-BASELINE-SEC-001, EPIC-006-WP3
"""
import logging
from collections.abc import Mapping
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    BUILD_BACKEND_ESPHOME_DASHBOARD,
    BUILD_BACKEND_MANUAL,
    CONF_ARTIFACT_CHANNEL,
    CONF_ARTIFACT_HW_FAMILY,
    CONF_ARTIFACT_OWNER,
    CONF_ARTIFACT_REPO,
    CONF_BUILD_BACKEND,
    CONF_ENVELOPE_MODE_ENABLED,
    CONF_FLASH_MIN_SIZE_KB,
    CONF_GATES_FRESHNESS_MIN,
    CONF_MAP_CONFIRMED,
    CONF_MODBUS_VERSION,
    CONF_POLL_INTERVAL,
    CONF_PROXY_API_KEY,
    CONF_PROXY_BASE_URL,
    CONF_PROXY_CUSTOMER_ID,
    CONF_SELECTED_DEVICE,
    CONF_SELECTED_TIER,
    CONF_SIMULATED_FAILURE_MODE,
    CONF_STRICT_GATES,
    CONFIG_ENTRY_VERSION,
    CONTRACT_VERSION,
    DEFAULT_ARTIFACT_CHANNEL,
    DEFAULT_ARTIFACT_HW_FAMILY,
    DEFAULT_ARTIFACT_OWNER,
    DEFAULT_ARTIFACT_REPO,
    DEFAULT_BUILD_BACKEND,
    DEFAULT_ENVELOPE_MODE_ENABLED,
    DEFAULT_FLASH_MIN_SIZE_KB,
    DEFAULT_GATES_FRESHNESS_MIN,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROXY_API_KEY,
    DEFAULT_PROXY_BASE_URL,
    DEFAULT_PROXY_CUSTOMER_ID,
    DEFAULT_SELECTED_TIER,
    DEFAULT_SIMULATED_FAILURE_MODE,
    DEFAULT_STRICT_GATES,
    DOMAIN,
    SETUP_STATE_ADOPTED,
    VERSION,
)
from .discovery import ContractInputReader
from .operations import OperationLock, OperationRunner, OperationTracker
from .stepper import WizardEngine

_LOGGER = logging.getLogger(__name__)

# Platforms to forward via ConfigEntry
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.TIME,
]


def get_integration_data(hass: HomeAssistant, entry_id: str | None = None) -> dict:
    """Get the runtime data dict for a pvautonomy_ops config entry.

    WP3: Multiple entries possible (one per device).

    EPIC-015 P2-01 — fail-closed semantics:
    - If entry_id is given and found → return that entry's data.
    - If entry_id is given but NOT found → return {} (fail closed).
      Active runtime code must not silently fall through to another entry.
    - If entry_id is None → legacy first-entry fallback (backward compat
      ONLY for callers that have not yet been threaded with entry_id;
      see P2-02 for remaining migration).

    Usage:
        from . import get_integration_data
        # Active runtime (always pass entry_id):
        config = get_integration_data(hass, entry_id).get("config", {})
        # Legacy (no entry_id — backward compat only):
        config = get_integration_data(hass).get("config", {})
    """
    domain_data = hass.data.get(DOMAIN, {})
    if entry_id:
        if entry_id in domain_data:
            return domain_data[entry_id]
        # P2-01: Fail closed — do NOT fall through to first-entry
        _LOGGER.warning(
            "get_integration_data: entry_id '%s' not found in domain data "
            "(fail closed — returning empty dict)",
            entry_id[:8] if entry_id else "",
        )
        return {}
    # Legacy no-entry_id path: return first entry with "config" key.
    # WARNING: This branch exists for legacy callers without entry context
    # (factory_installer, lifecycle). Active runtime code must pass entry_id.
    for value in domain_data.values():
        if isinstance(value, dict) and "config" in value:
            return value
    return {}


def get_runtime_config(entry: ConfigEntry) -> dict:
    """Build runtime config dict from ConfigEntry options with defaults.

    Args:
        entry: The config entry to read options from.

    Returns:
        Dict with all runtime config values (guaranteed complete with defaults).
    """
    opts = entry.options
    return {
        CONF_POLL_INTERVAL: opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        CONF_ARTIFACT_CHANNEL: opts.get(CONF_ARTIFACT_CHANNEL, DEFAULT_ARTIFACT_CHANNEL),
        CONF_ARTIFACT_HW_FAMILY: opts.get(CONF_ARTIFACT_HW_FAMILY, DEFAULT_ARTIFACT_HW_FAMILY),
        CONF_ARTIFACT_OWNER: opts.get(CONF_ARTIFACT_OWNER, DEFAULT_ARTIFACT_OWNER),
        CONF_ARTIFACT_REPO: opts.get(CONF_ARTIFACT_REPO, DEFAULT_ARTIFACT_REPO),
        CONF_FLASH_MIN_SIZE_KB: opts.get(CONF_FLASH_MIN_SIZE_KB, DEFAULT_FLASH_MIN_SIZE_KB),
        CONF_GATES_FRESHNESS_MIN: opts.get(CONF_GATES_FRESHNESS_MIN, DEFAULT_GATES_FRESHNESS_MIN),
        CONF_STRICT_GATES: opts.get(CONF_STRICT_GATES, DEFAULT_STRICT_GATES),
        CONF_BUILD_BACKEND: opts.get(CONF_BUILD_BACKEND, DEFAULT_BUILD_BACKEND),
        CONF_SIMULATED_FAILURE_MODE: opts.get(CONF_SIMULATED_FAILURE_MODE, DEFAULT_SIMULATED_FAILURE_MODE),
        # G6 (ADR-0003 D-E): hidden envelope force-disable. Options win;
        # entry.data is honored so an operator storage-edit works either way.
        CONF_ENVELOPE_MODE_ENABLED: opts.get(
            CONF_ENVELOPE_MODE_ENABLED,
            entry.data.get(CONF_ENVELOPE_MODE_ENABLED, DEFAULT_ENVELOPE_MODE_ENABLED),
        ),
        # Proxy Remote Build Backend (EPIC-005-D1)
        CONF_PROXY_BASE_URL: opts.get(CONF_PROXY_BASE_URL, DEFAULT_PROXY_BASE_URL),
        CONF_PROXY_API_KEY: opts.get(CONF_PROXY_API_KEY, DEFAULT_PROXY_API_KEY),
        CONF_PROXY_CUSTOMER_ID: opts.get(CONF_PROXY_CUSTOMER_ID, DEFAULT_PROXY_CUSTOMER_ID),
        # EPIC-015 P1-02: Build intent (re-flash relevant)
        CONF_SELECTED_TIER: opts.get(CONF_SELECTED_TIER, DEFAULT_SELECTED_TIER),
        CONF_MODBUS_VERSION: opts.get(CONF_MODBUS_VERSION, None),
        CONF_MAP_CONFIRMED: opts.get(CONF_MAP_CONFIRMED, False),
        # Internal: entry_id for auto-derive (not user-facing)
        "_entry_id": entry.entry_id,
    }


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate a PVAutonomy Ops config entry to the current version.

    V1 → V2: Config Flow changes from 1-step to multi-step wizard.
    Existing entries keep their options intact. No data schema change needed.

    Critical: V1 entries used unique_id = DOMAIN which blocks new per-device
    entries ("already_configured"). Migration changes unique_id to a
    legacy-prefixed value so new entries with unique_id = pvautonomy_ops_{device_id}
    can be created.

    EPIC-006-WP3.
    """
    if config_entry.version < CONFIG_ENTRY_VERSION:
        _LOGGER.info(
            "Migrating config entry from V%d to V%d (entry_id=%s)",
            config_entry.version,
            CONFIG_ENTRY_VERSION,
            config_entry.entry_id,
        )

        # Fix unique_id: V1 used DOMAIN as unique_id which blocks new entries.
        # Change to legacy-prefixed value to free up the DOMAIN namespace.
        if config_entry.unique_id == DOMAIN:
            new_unique_id = f"{DOMAIN}_legacy_{config_entry.entry_id}"
            _LOGGER.info(
                "Migrating unique_id: '%s' → '%s' (frees per-device entries)",
                config_entry.unique_id,
                new_unique_id,
            )
            hass.config_entries.async_update_entry(
                config_entry,
                unique_id=new_unique_id,
                version=CONFIG_ENTRY_VERSION,
            )
        else:
            hass.config_entries.async_update_entry(
                config_entry, version=CONFIG_ENTRY_VERSION
            )

        _LOGGER.info("Migration to V%d complete", CONFIG_ENTRY_VERSION)
    return True


def _is_legacy_entry(entry: ConfigEntry) -> bool:
    """Detect legacy V1 entries (no physical device binding).

    WP3 entries always have ha_device_id in options (persisted from _initial_device
    during bootstrap). Legacy entries were created before the Config Flow Wizard
    and never had a per-device binding.

    EPIC-006-STAB Phase 2.
    """
    has_device_binding = bool(
        entry.options.get("ha_device_id")
        or entry.data.get("ha_device_id")
    )
    return not has_device_binding


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up PVAutonomy Ops from YAML (triggers import to ConfigEntry).

    If ``pvautonomy_ops:`` is in configuration.yaml, this creates a
    ConfigEntry via the import flow so the integration runs under
    the modern ConfigEntry lifecycle.
    """
    if DOMAIN in config:
        # Only trigger import if no config entries exist yet.
        # After V1→V2 migration the legacy entry already exists —
        # re-importing would create a duplicate.
        existing = hass.config_entries.async_entries(DOMAIN)
        if existing:
            _LOGGER.debug(
                "YAML config detected for %s but %d entries already exist — skipping import",
                DOMAIN,
                len(existing),
            )
        else:
            _LOGGER.info(
                "YAML config detected for %s — triggering ConfigEntry import", DOMAIN
            )
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "import"},
                    data=config.get(DOMAIN) or {},
                )
            )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PVAutonomy Ops from a ConfigEntry.

    This is the modern lifecycle entry point — called both for UI-created
    entries and YAML-imported entries.
    """
    _LOGGER.info(
        "Setting up PVAutonomy Ops entry (version %s, contract %s)",
        VERSION,
        CONTRACT_VERSION,
    )

    # EPIC-005-A2: Migrate config entry title from legacy "Ops" names
    _OLD_TITLES = {"PVAutonomy Ops", "PVAutonomy Production Operations"}
    if entry.title in _OLD_TITLES:
        hass.config_entries.async_update_entry(entry, title="PVAutonomy")
        _LOGGER.info("Migrated config entry title: '%s' → 'PVAutonomy'", entry.title)

    # Build runtime config from options
    runtime_config = get_runtime_config(entry)
    poll_interval = runtime_config[CONF_POLL_INTERVAL]

    # Initialize core components
    input_reader = ContractInputReader(hass)
    operation_lock = OperationLock()
    # EPIC-004 build-firmware operation tracker (SPEC-20260513): pass entry.entry_id
    # so operation_started / progress / completed events are entry-scoped and the
    # status sensor in this entry filters them correctly.
    operation_tracker = OperationTracker(hass, entry.entry_id)
    operation_runner = OperationRunner(hass, operation_tracker, operation_lock)
    wizard_engine = WizardEngine(hass, entry_id=entry.entry_id)  # EPIC-015 P1-05

    # Initialize persistent keyring (D-OPS-KEYRING-STRATEGY-001)
    from .keyring import PVAutonomyKeyring

    keyring = PVAutonomyKeyring(hass)
    await keyring.async_load()

    # EPIC-006-WP3: Initialize persistent metadata store
    # EPIC-015 P3-03: domain-global singleton — all entries share one instance
    from .metadata import async_get_metadata_store

    metadata_store = await async_get_metadata_store(hass)

    # EPIC-006-WP3: Bootstrap initial device from Config Flow wizard
    initial_device = entry.options.get("_initial_device")
    if initial_device:
        from datetime import datetime, timezone
        from .metadata import DeviceMetadata

        now = datetime.now(timezone.utc).isoformat()
        metadata = DeviceMetadata(
            device_id=initial_device["device_id"],
            mac_suffix=initial_device.get("mac_suffix", ""),
            manufacturer=initial_device["manufacturer"],
            model_slug=initial_device["model_slug"],
            site=initial_device["site"],
            number=initial_device["number"],
            registry_file=initial_device["registry_file"],
            created_at=now,
            updated_at=now,
            ha_device_id=initial_device.get("ha_device_id", ""),
            esphome_yaml_filename=initial_device.get("esphome_yaml_filename", ""),
        )
        await metadata_store.put(metadata)
        _LOGGER.info(
            "Bootstrapped device metadata from Config Flow: %s",
            metadata.device_id,
        )

        # Remove _initial_device from options (one-time bootstrap)
        # Persist ha_device_id at top level for legacy detection (Phase 2)
        # EPIC-015 P1-02: Promote build-intent keys before _initial_device removal.
        # These keys may already exist at root level (set during entry creation);
        # only backfill from _initial_device if missing (legacy bootstrap path).
        new_options = dict(entry.options)
        new_options["ha_device_id"] = initial_device.get("ha_device_id", "")
        if CONF_MODBUS_VERSION not in new_options:
            new_options[CONF_MODBUS_VERSION] = initial_device.get("modbus_version")
        if CONF_MAP_CONFIRMED not in new_options:
            new_options[CONF_MAP_CONFIRMED] = initial_device.get("map_confirmed", False)
        if CONF_SELECTED_TIER not in new_options:
            new_options[CONF_SELECTED_TIER] = DEFAULT_SELECTED_TIER
        del new_options["_initial_device"]
        hass.config_entries.async_update_entry(entry, options=new_options)

        # Check _setup_state: if Config Flow already did build+flash, skip background build
        setup_state = initial_device.get("_setup_state")
        if setup_state == "complete":
            _trigger_initial_build = False
            _LOGGER.info(
                "Config flow completed build+flash for %s — skipping background build",
                metadata.device_id,
            )
        elif setup_state == "partial":
            _trigger_initial_build = False
            _LOGGER.info(
                "Config flow built firmware for %s but OTA skipped (device offline) "
                "— use Flash button when device is online",
                metadata.device_id,
            )
        elif setup_state == SETUP_STATE_ADOPTED:
            # Adopt flow: device already runs production firmware. Register
            # ownership only — never build, install, or reflash on setup.
            _trigger_initial_build = False
            _LOGGER.info(
                "Adopted already-running device %s — registered ownership, "
                "no build/install/reflash",
                metadata.device_id,
            )
        else:
            # Legacy path (pre-refactor entries without _setup_state)
            _trigger_initial_build = True
    else:
        _trigger_initial_build = False

    # Auto-migrate: ensure ha_device_id is in options for device-bound entries.
    # WP3 entries created via Config Flow encode the HA device UUID in unique_id
    # (pattern: pvautonomy_ops_{ha_device_id}). If ha_device_id was lost from
    # options (e.g. manual entry creation, interrupted bootstrap), recover it
    # so _is_legacy_entry() returns False and the entry is treated as WP3.
    if not entry.options.get("ha_device_id"):
        _recovered_device_id = ""
        uid = entry.unique_id or ""
        prefix = f"{DOMAIN}_"

        # Source 1: Extract from unique_id (pvautonomy_ops_{ha_device_id})
        if uid.startswith(prefix) and not uid.startswith(f"{DOMAIN}_legacy_"):
            candidate = uid[len(prefix):]
            if len(candidate) >= 16:  # HA device UUIDs are 32 hex chars
                _recovered_device_id = candidate

        # Source 2: Fallback to metadata store
        if not _recovered_device_id:
            all_devices = await metadata_store.get_all()
            for dev in all_devices:
                if dev.ha_device_id:
                    _recovered_device_id = dev.ha_device_id
                    break

        if _recovered_device_id:
            new_options = dict(entry.options)
            new_options["ha_device_id"] = _recovered_device_id
            hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info(
                "Auto-migrated ha_device_id=%s for entry %s",
                _recovered_device_id[:8],
                entry.entry_id[:8],
            )

    # EPIC-006-A5: Clean up stale partial downloads on startup
    from pathlib import Path
    from .cache import cleanup_partials
    from .const import PROXY_ARTIFACT_CACHE_DIR
    cache_base = Path("/config") / PROXY_ARTIFACT_CACHE_DIR
    try:
        cleaned = await hass.async_add_executor_job(cleanup_partials, cache_base)
        if cleaned:
            _LOGGER.info("Startup cache cleanup: removed %d stale partial(s)", cleaned)
    except Exception as exc:
        _LOGGER.warning("Startup cache cleanup failed (non-fatal): %s", exc)

    # EPIC-006-A5: Propagate OTA retry config to hass.data for call sites
    from .const import (
        CONF_CACHE_KEEP_BUILDS,
        CONF_OTA_RETRIES,
        CONF_OTA_RETRY_DELAYS,
        DEFAULT_CACHE_KEEP_BUILDS,
        DEFAULT_OTA_RETRIES,
        DEFAULT_OTA_RETRY_DELAYS,
    )
    ota_retries = entry.options.get(CONF_OTA_RETRIES, DEFAULT_OTA_RETRIES)
    ota_delays_str = entry.options.get(CONF_OTA_RETRY_DELAYS, DEFAULT_OTA_RETRY_DELAYS)
    try:
        ota_retry_delays = tuple(float(x.strip()) for x in ota_delays_str.split(","))
    except (ValueError, AttributeError):
        ota_retry_delays = (0, 10, 30)

    # EPIC-006-STAB Phase 2: Legacy detection
    is_legacy = _is_legacy_entry(entry)
    if is_legacy:
        _LOGGER.warning(
            "Legacy config entry %s loaded (no device binding). "
            "Consider removing it if WP3 entries exist.",
            entry.entry_id[:8],
        )

    cache_keep_builds = entry.options.get(CONF_CACHE_KEEP_BUILDS, DEFAULT_CACHE_KEEP_BUILDS)

    # Store in hass.data for platforms to access (PN-2: per entry_id keying)
    hass.data.setdefault(DOMAIN, {})
    # EPIC-015 P2-01: Domain-root OTA/cache values kept ONLY for legacy callers
    # (factory_installer, lifecycle) that do not yet have entry_id threading.
    # Active runtime code (button.py, _async_initial_build) must read from
    # per-entry data below. P2-02 will thread entry_id into legacy callers.
    hass.data[DOMAIN]["ota_retries"] = ota_retries
    hass.data[DOMAIN]["ota_retry_delays"] = ota_retry_delays
    hass.data[DOMAIN]["cache_keep_builds"] = cache_keep_builds
    hass.data[DOMAIN][entry.entry_id] = {
        "input_reader": input_reader,
        "operation_lock": operation_lock,
        "operation_tracker": operation_tracker,
        "operation_runner": operation_runner,
        "wizard_engine": wizard_engine,
        "keyring": keyring,
        "metadata_store": metadata_store,
        "config": runtime_config,
        "entry": entry,
        "selected_device": entry.options.get(CONF_SELECTED_DEVICE),
        "is_legacy": is_legacy,  # EPIC-006-STAB Phase 2
        # EPIC-015 P2-01: Entry-scoped OTA/cache config for active runtime callers
        "ota_retries": ota_retries,
        "ota_retry_delays": ota_retry_delays,
        "cache_keep_builds": cache_keep_builds,
    }

    # Listen for options updates (live reload without restart)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Defer platform setup until HA is fully started (templates ready)
    async def start_integration(_event):
        """Initialize platforms and periodic updates after HA fully started."""
        _LOGGER.info(
            "HA fully started, forwarding platforms (version %s, contract %s)",
            VERSION,
            CONTRACT_VERSION,
        )

        # Forward platform setup via ConfigEntry (modern pattern)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _LOGGER.info("Platform forwarding initiated")

        # EPIC-015 P1-06: Register services once at domain scope
        if not hass.data[DOMAIN].get("_services_registered"):
            await _async_register_services(hass)
            hass.data[DOMAIN]["_services_registered"] = True
            _LOGGER.info("Domain-scope services registered (P1-06)")

        # Schedule periodic update
        async def periodic_update(_now=None):
            """Periodic update handler."""
            _LOGGER.debug("Periodic update triggered")
            hass.bus.async_fire(f"{DOMAIN}_update")

        _LOGGER.info(
            "Starting periodic updates (interval=%s seconds)", poll_interval
        )
        cancel_timer = async_track_time_interval(
            hass, periodic_update, timedelta(seconds=poll_interval)
        )
        # Store cancel handle for unload
        hass.data[DOMAIN][entry.entry_id]["cancel_timer"] = cancel_timer

        # Run initial update
        await periodic_update()

        # EPIC-006-WP3 Phase 7: Async build kickoff for newly created entries
        if _trigger_initial_build:
            hass.async_create_task(
                _async_initial_build(hass, metadata_store, runtime_config)
            )

        # Reconcile registry-driven guardrails on every setup/reload so
        # entities previously disabled by the integration are re-enabled
        # automatically when the registry flips them back on.
        await _async_apply_guardrails(hass, metadata_store, entry)

    # If HA is already running (e.g. after options-update reload), start
    # immediately. Otherwise wait for HOMEASSISTANT_STARTED (first boot).
    if hass.is_running:
        _LOGGER.info("HA already running — starting platforms immediately")
        await start_integration(None)
    else:
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, start_integration
        )
        _LOGGER.info(
            "PVAutonomy Ops entry setup complete. Waiting for HOMEASSISTANT_STARTED."
        )

    return True


async def _async_apply_guardrails(
    hass: HomeAssistant,
    metadata_store,
    entry: ConfigEntry,
) -> None:
    """Reconcile registry-driven guardrails for one device.

    Applies the current registry state for all guardrail-managed entities of
    the current device. Integration-disabled entities are re-enabled when the
    registry now marks them enabled by default; user-disabled entities remain
    untouched.

    Fail-closed: if registry, device, or slug cannot be resolved,
    nothing is disabled.
    """
    from .entity_cleanup import apply_entity_guardrails, load_guardrail_states

    ha_device_id = entry.options.get("ha_device_id", "")
    if not ha_device_id:
        _LOGGER.info("Guardrail: no ha_device_id on entry — skipping")
        return

    # Find device metadata to get registry_file and slug.
    all_devices = await metadata_store.get_all()
    metadata = None
    for dev in all_devices:
        if dev.ha_device_id == ha_device_id:
            metadata = dev
            break

    if metadata is None:
        _LOGGER.info("Guardrail: no metadata for device %s — skipping", ha_device_id[:8])
        return

    if not metadata.registry_file:
        _LOGGER.info("Guardrail: no registry_file for %s — skipping", metadata.device_id)
        return

    # Build the device prefix from metadata (same convention as ESPHome slug).
    device_prefix = f"{metadata.model_slug}_{metadata.site}_{metadata.number:02d}"

    desired_states = await hass.async_add_executor_job(
        load_guardrail_states, metadata.registry_file, device_prefix,
    )

    if not desired_states:
        _LOGGER.info("Guardrail: no managed entities for %s", metadata.device_id)
        return

    result = apply_entity_guardrails(hass, desired_states, ha_device_id)
    _LOGGER.info(
        "Guardrail complete for %s: %d disabled, %d enabled, %d skipped, %d errors",
        metadata.device_id,
        result.disabled_count,
        result.enabled_count,
        result.skipped_count,
        len(result.errors),
    )


async def _async_initial_build(
    hass: HomeAssistant,
    metadata_store,
    runtime_config: dict,
) -> None:
    """Run initial build pipeline for a newly created Config Flow entry.

    Non-blocking background task: compiles device-specific firmware via the
    build proxy. OTA upload only if the device IP is resolvable (otherwise
    graceful skip with log hint).

    UX Pack: Fires pvautonomy_ops_flash_stage events so the status sensor
    and persistent notifications track progress. These are separate from
    the build_stage events fired by the pipeline itself (no double-events).

    EPIC-006-WP3, Phase 7.
    """
    from .pipeline import run_build_pipeline
    from .flash_uploader import resolve_device_ip, get_ota_password, ota_upload_with_retry, OTA_DEFAULT_PORT

    # EPIC-015 P2-05: Find the device metadata for this entry.
    # Prefer ha_device_id match over get_all()[0] to avoid wrong-device risk.
    entry_id = runtime_config.get("_entry_id", "")
    metadata = None

    # Try targeted lookup via ha_device_id from entry options
    if entry_id:
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
        ha_device_id = ""
        _entry_obj = entry_data.get("entry")
        if _entry_obj:
            ha_device_id = _entry_obj.options.get("ha_device_id", "")
        if ha_device_id:
            all_devices = await metadata_store.get_all()
            for dev in all_devices:
                if dev.ha_device_id == ha_device_id:
                    metadata = dev
                    break

    # Fallback: single-device entry (bootstrap path)
    if metadata is None:
        all_devices = await metadata_store.get_all()
        if not all_devices:
            _LOGGER.warning("Initial build: no devices in metadata store — skipping")
            return
        metadata = all_devices[0]
        if len(all_devices) > 1:
            _LOGGER.warning(
                "Initial build: %d devices in store, using first (%s) — "
                "consider matching by ha_device_id",
                len(all_devices), metadata.device_id,
            )

    # UX Pack: flash_stage event emitter for notification + sensor updates
    def fire_flash_stage(stage: str, progress: int, **extra: str | None) -> None:
        hass.bus.async_fire(
            f"{DOMAIN}_flash_stage",
            {
                "entry_id": entry_id,
                "stage": stage,
                "progress": progress,
                "version": extra.get("version"),
                "target_device": metadata.device_id,
                "error": extra.get("error"),
            },
        )

    _LOGGER.info(
        "Initial build kickoff: %s (model=%s, site=%s, number=%d)",
        metadata.device_id,
        metadata.model_slug,
        metadata.site,
        metadata.number,
    )

    fire_flash_stage("init", 0)

    try:
        fire_flash_stage("build", 15)
        result = await run_build_pipeline(
            hass,
            model=metadata.model_slug,
            site=metadata.site,
            number=metadata.number,
            registry_file=metadata.registry_file,
            mac_suffix=metadata.mac_suffix or None,
            channel=runtime_config.get("artifact_channel", "stable"),
            build_backend=runtime_config.get("build_backend"),
            simulated_failure_mode=runtime_config.get("simulated_failure_mode", "none"),
            entry_id=entry_id,  # EPIC-015 P2-02
        )

        if not result.success or not result.artifact_path:
            fire_flash_stage("failed", 0, error=result.error or "no artifact")
            _LOGGER.warning(
                "Initial build failed for %s: %s",
                metadata.device_id,
                result.error or "no artifact",
            )
            return

        _LOGGER.info(
            "Initial build succeeded: %s (%d bytes, backend=%s)",
            metadata.device_id,
            result.firmware_size,
            result.build_backend,
        )

        # Attempt OTA upload only if device IP is resolvable
        device_ip, _ip_method, _ip_dur = resolve_device_ip(hass, metadata.device_id, ha_device_id=metadata.ha_device_id)
        if not device_ip:
            fire_flash_stage("complete", 100)
            _LOGGER.info(
                "Initial build: device IP not resolvable for %s — "
                "firmware cached, use Flash button when device is online",
                metadata.device_id,
            )
            return

        fire_flash_stage("upload", 60)
        ota_pw_result = await hass.async_add_executor_job(
            get_ota_password, hass, metadata.device_id
        )
        ota_password = ota_pw_result.password if ota_pw_result else None

        # EPIC-015 P2-01: Read OTA config from entry-scoped data, not domain root
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
        await ota_upload_with_retry(
            hass,
            host=device_ip,
            port=OTA_DEFAULT_PORT,
            password=ota_password,
            firmware_path=result.artifact_path,
            timeout_s=120.0,
            retries=entry_data.get("ota_retries", 3),
            delays=entry_data.get("ota_retry_delays", (0, 10, 30)),
        )

        fire_flash_stage("postcheck", 85)

        fire_flash_stage("complete", 100)
        _LOGGER.info(
            "Initial build + OTA complete for %s", metadata.device_id
        )

    except Exception as exc:
        fire_flash_stage("failed", 0, error=str(exc))
        _LOGGER.warning(
            "Initial build/OTA failed for %s (non-fatal, device can be flashed manually)",
            metadata.device_id,
            exc_info=True,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PVAutonomy Ops config entry."""
    _LOGGER.info("Unloading PVAutonomy Ops entry %s", entry.entry_id[:8])

    # Cancel periodic timer (PN-2: per entry_id)
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    cancel_timer = entry_data.get("cancel_timer")
    if cancel_timer:
        cancel_timer()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)

        # EPIC-015 P1-06: Check if any config entries remain
        remaining_entries = {
            k: v for k, v in domain_data.items()
            if isinstance(v, dict) and "config" in v
        }
        if not remaining_entries:
            # Last entry unloaded — remove domain services + clean up
            if domain_data.get("_services_registered"):
                _async_remove_services(hass)
            hass.data.pop(DOMAIN, None)

    return unload_ok


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update (live reload)."""
    _LOGGER.info("Options updated — reloading PVAutonomy Ops entry")
    await hass.config_entries.async_reload(entry.entry_id)


def _resolve_target_entry_data(
    hass: HomeAssistant, call
) -> tuple[str, dict]:
    """Resolve the target config entry runtime data for a service call.

    EPIC-015 P1-06 / ADR-20260323-entry-scoped-service-contract:
    - If entry_id provided in call data: return that entry's data
    - If exactly one entry loaded: return it (backward compat)
    - If zero or multiple entries: fail closed with clear error
    """
    from homeassistant.exceptions import HomeAssistantError

    domain_data = hass.data.get(DOMAIN, {})
    requested_entry_id = call.data.get("entry_id")

    if requested_entry_id:
        entry_data = domain_data.get(requested_entry_id)
        if not entry_data or not isinstance(entry_data, dict) or "config" not in entry_data:
            raise HomeAssistantError(
                f"pvautonomy_ops: entry_id '{requested_entry_id}' not loaded"
            )
        return requested_entry_id, entry_data

    # No entry_id supplied — check loaded entries
    loaded_entries = {
        k: v for k, v in domain_data.items()
        if isinstance(v, dict) and "config" in v
    }

    if len(loaded_entries) == 0:
        raise HomeAssistantError(
            "pvautonomy_ops: no config entries loaded"
        )
    if len(loaded_entries) == 1:
        entry_id, entry_data = next(iter(loaded_entries.items()))
        return entry_id, entry_data

    raise HomeAssistantError(
        f"pvautonomy_ops: {len(loaded_entries)} entries loaded — "
        f"specify entry_id to avoid ambiguity "
        f"(available: {', '.join(loaded_entries.keys())})"
    )


# Customer-facing message when a build/install is requested for a device that
# no ConfigEntry owns. English fallback only — the localized text lives in
# strings.json / translations/{en,de}.json under exceptions.device_not_set_up
# and is selected via the exception's translation_key (P1.3-GO-A). Technical
# ownership detail is logged separately.
_DEVICE_NOT_SET_UP_MSG = (
    "Device is not yet set up in PVAutonomy. Open Settings > Devices & "
    "Services > PVAutonomy Ops and either adopt the running device or run "
    "setup."
)
# translation_key values for the resolver's customer-facing errors; the
# placeholder names must match exceptions.* in strings.json/translations.
_TK_DEVICE_NOT_SET_UP = "device_not_set_up"
_TK_DEVICE_OWNERSHIP_AMBIGUOUS = "device_ownership_ambiguous"


def _normalize_device_identifier(name: str) -> str:
    """Slug-tolerant normalization mirroring ``metadata.lookup``.

    Lowercases, replaces spaces/dashes with underscores, strips a leading
    ``edge101_`` hardware prefix so callers can pass either ``mic600_garage_01``
    or ``edge101_mic600_garage_01`` and get the same canonical key.
    """
    s = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    prefix = "edge101_"
    if s.startswith(prefix):
        s = s[len(prefix):]
    return s


def _entry_owns_device(
    entry_data: dict, normalized_name: str, metadata_payload
) -> bool:
    """Return ``True`` iff this entry's options claim ownership of the
    device with canonical name ``normalized_name``.

    Issue #6 — owner-binding lives on the ConfigEntry, not in the
    domain-global metadata store singleton. Three signals are accepted
    (any one is sufficient):

    1. ``options.selected_device`` matches the requested device name
       (slug-tolerant on both sides).
    2. ``options.ha_device_id`` matches the ``ha_device_id`` of the
       device's metadata record (only when both are non-empty).
    3. ``options._initial_device`` — defensive bootstrap window before
       the wizard has promoted ``ha_device_id`` / ``selected_device``
       into the root options dict (see ``async_setup_entry``).
    """
    entry = entry_data.get("entry")
    opts = getattr(entry, "options", None) or {}
    if not isinstance(opts, Mapping):
        return False

    # Signal 1: options.selected_device
    selected = opts.get(CONF_SELECTED_DEVICE) or opts.get("selected_device")
    if selected and _normalize_device_identifier(selected) == normalized_name:
        return True

    # Helper to extract ha_device_id from either a dataclass-like payload
    # (the real ``DeviceMetadata``) or a dict (test fakes).
    def _payload_ha(payload):
        if payload is None:
            return ""
        val = getattr(payload, "ha_device_id", None)
        if val is None and isinstance(payload, dict):
            val = payload.get("ha_device_id")
        return val or ""

    # Signal 2: options.ha_device_id vs metadata.ha_device_id
    meta_ha = _payload_ha(metadata_payload)
    opt_ha = opts.get("ha_device_id") or ""
    if meta_ha and opt_ha and meta_ha == opt_ha:
        return True

    # Signal 3: _initial_device (during bootstrap before promotion)
    initial = opts.get("_initial_device")
    if isinstance(initial, dict):
        for key in ("device_id", "device_slug"):
            cand = initial.get(key) or ""
            if cand and _normalize_device_identifier(cand) == normalized_name:
                return True
        init_ha = initial.get("ha_device_id") or ""
        if meta_ha and init_ha and meta_ha == init_ha:
            return True

    return False


async def _resolve_entry_for_device(
    hass: HomeAssistant, device_name: str
) -> tuple[str, dict]:
    """Resolve the config entry that owns ``device_name`` across all entries.

    Issue #6: the metadata store is a domain-global singleton (EPIC-015
    P3-03), so a per-entry ``metadata_store.lookup(device_name)`` returns
    a hit for *every* loaded entry as soon as any wizard has registered
    that device. This made the previous resolver rely entirely on a
    ``source != "import"`` tiebreaker, which silently routed a build for
    a new device (e.g. MIC600 with only legacy metadata) to an unrelated
    user entry (e.g. SPH10K).

    The resolver now runs in two phases:

    * **Phase 1 — entry-bound ownership.** Each loaded entry is checked
      against :func:`_entry_owns_device`, which reads owner signals from
      ``entry.options`` (selected_device, ha_device_id, _initial_device).
      One owner → return. Multiple owners → import-stub tiebreaker
      (kept for the live SPH10K+yaml-import-stub case). Two non-import
      owners for the same device → fail closed.

    * **Phase 2 — legacy single-entry fallback.** With exactly one
      loaded entry and a metadata-store hit, the entry is returned for
      backward compatibility with installs that pre-date owner-binding.
      With two or more entries loaded and no Phase-1 owner, the resolver
      fails closed and does *not* tiebreak by source — there is no
      principled way to attribute the device without an owner signal.
    """
    from homeassistant.exceptions import HomeAssistantError

    name = (device_name or "").strip()
    if not name:
        raise HomeAssistantError(
            "pvautonomy_ops: device_name is required to resolve the "
            "target entry without an explicit entry_id"
        )

    domain_data = hass.data.get(DOMAIN, {})
    loaded_entries = {
        k: v for k, v in domain_data.items()
        if isinstance(v, dict) and "config" in v
    }

    if not loaded_entries:
        raise HomeAssistantError("pvautonomy_ops: no config entries loaded")

    normalized = _normalize_device_identifier(name)

    # One-time metadata lookup against the domain-global singleton
    # (EPIC-015 P3-03). Reached via any loaded entry's ``metadata_store``
    # reference — they all alias the same instance, so we stop after the
    # first successful lookup and ignore raising stores defensively.
    #
    # The setup dashboard may pass a customer-friendly display name such as
    # "Sph10K Home 02". Try the same normalized identifiers that
    # metadata.lookup() accepts so multi-entry service calls can still bind to
    # the device-bound entry.
    lookup_candidates = [name]
    if normalized != name:
        lookup_candidates.append(normalized)
    prefixed_normalized = f"edge101_{normalized}"
    if prefixed_normalized not in lookup_candidates:
        lookup_candidates.append(prefixed_normalized)

    metadata_payload = None
    for _data in loaded_entries.values():
        store = _data.get("metadata_store")
        if store is None:
            continue
        for candidate in lookup_candidates:
            try:
                metadata_payload = await store.lookup(candidate)
            except Exception:  # noqa: BLE001 - defensive
                continue
            if metadata_payload is not None:
                break
        if metadata_payload is not None:
            break

    # ── Phase 1: entry-bound ownership ────────────────────────────
    owners = [
        (eid, data) for eid, data in loaded_entries.items()
        if _entry_owns_device(data, normalized, metadata_payload)
    ]

    if len(owners) == 1:
        chosen_eid, chosen_data = owners[0]
        _LOGGER.info(
            "pvautonomy_ops: device %r resolved to entry %s "
            "(path=ownership)",
            name, chosen_eid[:8] if chosen_eid else "",
        )
        return chosen_eid, chosen_data

    if len(owners) >= 2:
        # Multiple entry-bound owners — keep the legacy import-stub
        # tiebreaker (live SPH10K case: user-entry + yaml-import stub
        # both point at the same ha_device_id). Any other constellation
        # is genuinely ambiguous and must fail closed.
        non_import = [
            (eid, data) for eid, data in owners
            if getattr(data.get("entry"), "source", "") != "import"
        ]
        if len(non_import) == 1:
            chosen_eid, chosen_data = non_import[0]
            discarded = [eid for eid, _ in owners if eid != chosen_eid]
            _LOGGER.info(
                "pvautonomy_ops: device %r resolved to entry %s "
                "(path=ownership+import-tiebreak); ignored import "
                "entries: %s",
                name, chosen_eid[:8] if chosen_eid else "",
                ", ".join(d[:8] for d in discarded),
            )
            return chosen_eid, chosen_data
        ambiguous = ", ".join(eid for eid, _ in owners)
        _LOGGER.warning(
            "pvautonomy_ops: device %r is owned by multiple "
            "loaded entries (%s) — ambiguous, failing closed",
            name, ambiguous,
        )
        raise HomeAssistantError(
            f"Device {name!r} is claimed by more than one PVAutonomy "
            f"entry ({ambiguous}). Call the service again with an "
            f"explicit entry_id, or remove the duplicate entry.",
            translation_domain=DOMAIN,
            translation_key=_TK_DEVICE_OWNERSHIP_AMBIGUOUS,
            translation_placeholders={
                "device_name": name,
                "entries": ambiguous,
            },
        )

    # ── Phase 2: legacy fallback (single-entry only) ──────────────
    if len(loaded_entries) == 1:
        only_eid, only_data = next(iter(loaded_entries.items()))
        if metadata_payload is not None:
            _LOGGER.info(
                "pvautonomy_ops: device %r resolved to entry %s "
                "(path=legacy-metadata-fallback, single-entry)",
                name, only_eid[:8] if only_eid else "",
            )
            return only_eid, only_data
        # Single entry, but the device is unknown to the metadata store
        # as well. Surface a customer-actionable message; technical detail
        # stays in the log.
        _LOGGER.warning(
            "pvautonomy_ops: device %r not registered in any loaded entry "
            "(available entries: %s)",
            name, ", ".join(sorted(loaded_entries)),
        )
        raise HomeAssistantError(
            _DEVICE_NOT_SET_UP_MSG,
            translation_domain=DOMAIN,
            translation_key=_TK_DEVICE_NOT_SET_UP,
            translation_placeholders={"device_name": name},
        )

    # Multi-entry with no Phase-1 owner → fail closed. We intentionally
    # do *not* fall back to a source-based tiebreaker across all loaded
    # entries: without an entry-bound owner signal, attributing the
    # device would be a guess (Issue #6). Surface a customer-actionable
    # message; the technical ownership detail stays in the log.
    _LOGGER.warning(
        "pvautonomy_ops: device %r is not owned by any loaded entry; "
        "adopt the device or run setup (available entries: %s)",
        name, ", ".join(sorted(loaded_entries)),
    )
    raise HomeAssistantError(
        _DEVICE_NOT_SET_UP_MSG,
        translation_domain=DOMAIN,
        translation_key=_TK_DEVICE_NOT_SET_UP,
        translation_placeholders={"device_name": name},
    )


# Service names registered at domain scope (EPIC-015 P1-06)
_SERVICE_NAMES = (
    "start_initial_setup",
    "start_reconfigure",
    "start_factory_reset",
    "wizard_advance",
    "wizard_abort",
    "confirm_key_saved",
    "apply_noise_psk",
    "set_selected_device",
    # EPIC-009 / TASK-014G: explicit dashboard rebuild trigger (no Wizard,
    # no Options reload, no ESPHome action). Reuses
    # dashboard_builder.async_create_dashboard().
    "refresh_customer_dashboard",
    # EPIC-012 / TASK-014Q: explicit Grid First draft commit trigger.
    # Reads draft helpers + live source values, validates fail-closed,
    # then writes rate, stop SoC, slot 1 start/stop, schedule enable,
    # priority_control = "Grid First" in deterministic order.
    "activate_grid_first_draft",
    # EPIC-004 / SPEC-20260512-epic004-build-firmware-service: build-only
    # firmware service. Builds production firmware via the normal proxy/GHA
    # path, stores the artifact, exposes safe build metadata. NEVER flashes.
    "build_firmware",
    # EPIC-004 / SPEC-20260514-epic004-install-prepared-firmware-service:
    # install-only firmware service. Uploads the already-prepared artifact
    # to the selected production device via OTA. NEVER builds, NEVER calls
    # the wizard/reconfigure/factory-reset path, and NEVER writes inverter
    # registers. Requires confirmed: true.
    "install_prepared_firmware",
    # TASK-20260520 Phase 2b: guarded operator entrypoint to provision the
    # repo-wide AES-256 compile_secret_key into PVAutonomyKeyring. The key
    # MUST match the GitHub Actions repo secret COMPILE_SECRET_KEY in
    # PVAutonomy/inverter-registry. Never logs key material (fingerprint
    # only); status never returns raw key bytes.
    "set_compile_secret_key",
    "clear_compile_secret_key",
    "compile_secret_key_status",
)


def _async_remove_services(hass: HomeAssistant) -> None:
    """Remove all pvautonomy_ops services (last entry unloaded). EPIC-015 P1-06."""
    for service_name in _SERVICE_NAMES:
        hass.services.async_remove(DOMAIN, service_name)
    _LOGGER.info("Removed all %s services (last entry unloaded)", DOMAIN)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HA services at domain scope (EPIC-015 P1-06).

    Services resolve the target entry at call time via _resolve_target_entry_data.
    ADR-20260323-entry-scoped-service-contract: no per-entry closure capture.
    """
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    # Optional entry_id added to all service schemas (P1-06)
    _ENTRY_ID_FIELD = {vol.Optional("entry_id"): cv.string}

    WIZARD_CONTEXT_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): cv.string,
            vol.Optional("device_kind"): cv.string,
            vol.Optional("model"): cv.string,
            vol.Optional("site"): cv.string,
            vol.Optional("number"): cv.string,
            vol.Optional("registry_file"): cv.string,
            vol.Optional("mac_suffix"): cv.string,
            vol.Optional("encryption_key"): cv.string,
            vol.Optional("confirmed"): cv.boolean,
            **_ENTRY_ID_FIELD,
        },
        extra=vol.ALLOW_EXTRA,
    )

    async def handle_start_initial_setup(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        device_id = call.data.get("device_id", "")
        await wizard.start_initial_setup(device_id=device_id)

    async def handle_start_reconfigure(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        device_id = call.data.get("device_id", "")
        await wizard.start_reconfigure(device_id=device_id)

    async def handle_start_factory_reset(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        device_id = call.data.get("device_id", "")
        await wizard.start_factory_reset(device_id=device_id)

    def _is_unrendered_template(value: str) -> bool:
        """Check if a value is a raw Jinja2 template that wasn't rendered."""
        return "{{" in value or "{%" in value

    # Entity IDs for dashboard input helpers (used to auto-fill wizard context)
    _INPUT_ENTITY_MAP = {
        "device_id": "input_select.edge101_selected_production_device",
        "model": "input_select.inverter_model_select",
        "site": "input_text.inverter_location",
        "number": "input_text.inverter_number",
    }

    # Map human-readable dropdown labels → (model_slug, registry_file)
    # Dropdown labels come from input_select.inverter_model_select options
    _MODEL_LABEL_MAP: dict[str, tuple[str, str]] = {
        "Growatt SPH10K (Batterie)": ("sph10k", "growatt/sph/sph10k.json"),
        "Growatt MIC600 (Micro)": ("mic600", "growatt/mic/mic600.json"),
        "Growatt MID15K (3-Phasen)": ("mid15k", "growatt/mid/mid15k.json"),
    }

    def _resolve_model_label(label: str) -> tuple[str, str]:
        """Resolve a dropdown label to (model_slug, registry_file).

        Falls back to slug inference for unknown labels.
        """
        if label in _MODEL_LABEL_MAP:
            return _MODEL_LABEL_MAP[label]
        # Fallback: try to extract model slug from label
        # e.g. "Growatt SPH10K (Batterie)" → extract "sph10k"
        import re
        match = re.search(r"(sph\d+k?|mic\d+|mid\d+k?|mod\d+k?)", label, re.IGNORECASE)
        if match:
            slug = match.group(1).lower()
            # Infer registry path from slug prefix
            for prefix in ("sph", "mic", "mid", "mod"):
                if slug.startswith(prefix):
                    return (slug, f"growatt/{prefix}/{slug}.json")
        # Last resort: slugify the whole label
        slug = re.sub(r"[^a-z0-9]", "", label.lower())
        return (slug, f"{slug}.json")

    async def handle_wizard_advance(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        context = {k: v for k, v in call.data.items() if k != "entry_id"}

        # Auto-fill from HA entity state when Lovelace doesn't render templates
        for key, entity_id in _INPUT_ENTITY_MAP.items():
            val = context.get(key, "")
            if not val or (isinstance(val, str) and _is_unrendered_template(val)):
                state_val = hass.states.get(entity_id)
                if state_val and state_val.state not in ("unknown", "unavailable", ""):
                    context[key] = state_val.state

        # Resolve human-readable model label → slug + registry_file
        model_val = context.get("model", "")
        if model_val and model_val in _MODEL_LABEL_MAP or (
            model_val and not model_val.islower()
        ):
            slug, reg_file = _resolve_model_label(model_val)
            context["model"] = slug
            # Only override registry_file if not explicitly provided
            rf = context.get("registry_file", "")
            if not rf or (isinstance(rf, str) and _is_unrendered_template(rf)):
                context["registry_file"] = reg_file
        else:
            # Model is already a slug — still auto-derive registry_file if needed
            rf = context.get("registry_file", "")
            if (not rf or (isinstance(rf, str) and _is_unrendered_template(rf))) and model_val:
                _, reg_file = _resolve_model_label(model_val)
                context["registry_file"] = reg_file

        await wizard.advance(context=context)

    async def handle_wizard_abort(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        await wizard.abort()

    async def handle_confirm_key_saved(call) -> None:
        _eid, entry_data = _resolve_target_entry_data(hass, call)
        wizard: WizardEngine = entry_data["wizard_engine"]
        await wizard.confirm_key_saved()

    hass.services.async_register(
        DOMAIN, "start_initial_setup", handle_start_initial_setup,
        schema=vol.Schema({vol.Optional("device_id"): cv.string, **_ENTRY_ID_FIELD}),
    )
    hass.services.async_register(
        DOMAIN, "start_reconfigure", handle_start_reconfigure,
        schema=vol.Schema({vol.Optional("device_id"): cv.string, **_ENTRY_ID_FIELD}),
    )
    hass.services.async_register(
        DOMAIN, "start_factory_reset", handle_start_factory_reset,
        schema=vol.Schema({vol.Optional("device_id"): cv.string, **_ENTRY_ID_FIELD}),
    )
    hass.services.async_register(
        DOMAIN, "wizard_advance", handle_wizard_advance,
        schema=WIZARD_CONTEXT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "wizard_abort", handle_wizard_abort,
        schema=vol.Schema({**_ENTRY_ID_FIELD}),
    )
    hass.services.async_register(
        DOMAIN, "confirm_key_saved", handle_confirm_key_saved,
        schema=vol.Schema({**_ENTRY_ID_FIELD}),
    )

    # D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001: Deterministic noise_psk recovery
    async def handle_apply_noise_psk(call) -> None:
        """Resolve noise_psk from secrets.yaml and apply to ESPHome config entry.

        No plaintext key parameter — key is resolved server-side only via
        edge101_api_key_{mac_suffix} from /config/esphome/secrets.yaml.

        Inputs:
          device_uid: 6-char hex MAC suffix (e.g. "2eb1e4")
          device_id:  HA Device Registry UUID (alternative to device_uid)
          entry_id:   Target config entry (optional, EPIC-015 P1-06)
        """
        _entry_id, entry_data = _resolve_target_entry_data(hass, call)

        from homeassistant.helpers import device_registry as dr
        from .keyring import (
            apply_noise_psk_to_esphome_entry,
            mask_key,
            resolve_noise_psk_from_secrets,
        )
        from .mac_utils import InvalidMACError, canonical_mac_last6

        device_uid = call.data.get("device_uid", "")
        device_id = call.data.get("device_id", "")

        # ── Step 1: Resolve mac_suffix via canonical_mac_last6 ──
        mac_suffix = ""
        device_name = ""

        if device_uid:
            try:
                mac_suffix = canonical_mac_last6(device_uid)
            except InvalidMACError:
                _LOGGER.error(
                    "apply_noise_psk: invalid device_uid '%s'", device_uid
                )
                return
        elif device_id:
            dev_reg = dr.async_get(hass)
            device_entry = dev_reg.async_get(device_id)
            if not device_entry:
                _LOGGER.error(
                    "apply_noise_psk: device_id '%s' not found in registry",
                    device_id[:8] if device_id else "",
                )
                return
            device_name = device_entry.name or ""
            for conn_type, conn_id in device_entry.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac_suffix = canonical_mac_last6(conn_id)
                    break
            if not mac_suffix:
                _LOGGER.error(
                    "apply_noise_psk: no MAC found for device_id '%s'",
                    device_id[:8],
                )
                return
        else:
            _LOGGER.error("apply_noise_psk: either device_uid or device_id is required")
            return

        _LOGGER.info(
            "apply_noise_psk: resolving key for mac_suffix=%s", mac_suffix
        )

        # ── Step 2: Resolve key from secrets.yaml (per-device only) ──
        noise_psk = await hass.async_add_executor_job(
            resolve_noise_psk_from_secrets, hass, mac_suffix
        )
        if not noise_psk:
            _LOGGER.error(
                "apply_noise_psk: edge101_api_key_%s not found in "
                "/config/esphome/secrets.yaml — create the secret and retry",
                mac_suffix,
            )
            return

        _LOGGER.info(
            "apply_noise_psk: key resolved (key=%s)", mask_key(noise_psk)
        )

        # ── Step 3: Persist in keyring (resolved at call time, P1-06) ──
        keyring = entry_data.get("keyring")
        if keyring:
            await keyring.set_production_noise_psk(mac_suffix, noise_psk)

        # ── Step 4: Resolve full MAC for robust entry lookup ──
        dev_reg = dr.async_get(hass)
        full_mac = ""
        ha_dev_id = ""
        for device_entry in dev_reg.devices.values():
            for conn_type, conn_id in device_entry.connections:
                if (
                    conn_type == dr.CONNECTION_NETWORK_MAC
                    and conn_id.replace(":", "").lower().endswith(mac_suffix)
                ):
                    full_mac = conn_id
                    ha_dev_id = device_entry.id
                    if not device_name:
                        device_name = device_entry.name or ""
                    break
            if full_mac:
                break

        _LOGGER.info(
            "apply_noise_psk: lookup mac=%s, ha_dev_id=%s, device_name=%s",
            mask_key(full_mac) if full_mac else "<none>",
            ha_dev_id[:8] if ha_dev_id else "<none>",
            device_name or "<none>",
        )

        # ── Step 5: Apply to ESPHome config entry + reload + verify ──
        applied = await apply_noise_psk_to_esphome_entry(
            hass,
            noise_psk,
            device_mac=full_mac,
            ha_device_id=ha_dev_id,
            device_names=[device_name] if device_name else None,
        )

        _LOGGER.info(
            "apply_noise_psk: applied=%s for mac_suffix=%s",
            applied, mac_suffix,
        )

    hass.services.async_register(
        DOMAIN, "apply_noise_psk", handle_apply_noise_psk,
        schema=vol.Schema({
            vol.Optional("device_uid"): cv.string,
            vol.Optional("device_id"): cv.string,
            **_ENTRY_ID_FIELD,
        }),
    )

    # EPIC-005-A1: set_selected_device service (Contract Action D)
    async def handle_set_selected_device(call) -> None:
        """Set the active device for operations.

        Persists to config entry options + syncs legacy input_select.
        Resolved at call time (EPIC-015 P1-06).
        """
        entry_id, entry_data = _resolve_target_entry_data(hass, call)
        device_name = call.data.get("device_name")
        input_reader: ContractInputReader = entry_data["input_reader"]
        await input_reader.set_selected_device(entry_id, device_name)
        hass.bus.async_fire(f"{DOMAIN}_update")
        _LOGGER.info("set_selected_device: %s (entry=%s)", device_name, entry_id[:8])

    hass.services.async_register(
        DOMAIN, "set_selected_device", handle_set_selected_device,
        schema=vol.Schema({vol.Required("device_name"): cv.string, **_ENTRY_ID_FIELD}),
    )

    # EPIC-009 SPH-Ctrl-Regression / TASK-014G: explicit customer dashboard
    # rebuild trigger. Reuses dashboard_builder.async_create_dashboard()
    # which is idempotent (registry entry kept once) but always rewrites
    # .storage/lovelace.{url_path}. Does NOT reload the config entry, run
    # the Wizard, trigger ESPHome, or write any Modbus register.
    async def handle_refresh_customer_dashboard(call) -> None:
        """Rebuild the stored Lovelace dashboard from the current builder.

        Resolution order for the target entry + device:

          1. If ``entry_id`` is supplied → use ``_resolve_target_entry_data``
             (existing fail-closed contract). Within the resolved entry,
             prefer call ``device_name`` over ``CONF_SELECTED_DEVICE``,
             falling back to the metadata store's single-device case.

          2. If ``entry_id`` is omitted and exactly one PVAutonomy Ops
             entry is loaded → same as (1) against that entry.

          3. If ``entry_id`` is omitted and multiple entries are loaded:
             - ``device_name`` MUST be supplied.
             - Resolve the target metadata via ``metadata_store.lookup``
               in each loaded entry's store (the store is a
               domain-global singleton in production, but loop defensively
               so per-entry mocks in tests still work). The first entry
               whose store returns a match wins for metadata.
             - Choose the matching entry by, in order:
                 a. exact ``entry.options['ha_device_id']`` match against
                    ``metadata.ha_device_id``;
                 b. ``entry.options[CONF_SELECTED_DEVICE]`` matching the
                    requested identifier or ``metadata.device_id``.
             - Exactly one matching entry is required: zero matches or
               more than one match fail closed.

        Fail-closed on:
          * multi-entry calls without ``device_name`` (and without
            ``entry_id``);
          * ``device_name`` that resolves to no metadata in any store;
          * metadata that does not bind to exactly one loaded entry in
            multi-entry mode.
        """
        from homeassistant.exceptions import HomeAssistantError

        from .const import (
            DEFAULT_SELECTED_TIER as _DEFAULT_SELECTED_TIER,
            MODEL_REGISTRY_MAP as _MODEL_REGISTRY_MAP,
        )
        from .dashboard_builder import async_create_dashboard
        from .metadata import DeviceMetadata as _DeviceMetadata

        requested_device = (call.data.get("device_name") or "").strip()
        requested_entry_id = (call.data.get("entry_id") or "").strip()

        domain_data = hass.data.get(DOMAIN, {})
        loaded_entries = {
            k: v for k, v in domain_data.items()
            if isinstance(v, dict) and "config" in v
        }

        entry_id: str
        entry_data: dict
        entry: ConfigEntry
        metadata_store = None
        metadata: _DeviceMetadata | None = None

        # ── Single-entry / explicit-entry_id path ────────────────────
        if requested_entry_id or len(loaded_entries) <= 1:
            entry_id, entry_data = _resolve_target_entry_data(hass, call)
            entry = entry_data["entry"]
            metadata_store = entry_data["metadata_store"]

            selected_device = entry.options.get(CONF_SELECTED_DEVICE) or ""
            identifier = requested_device or selected_device

            if identifier:
                metadata = await metadata_store.lookup(identifier)
                if metadata is None:
                    raise HomeAssistantError(
                        f"pvautonomy_ops.refresh_customer_dashboard: "
                        f"no device metadata for identifier {identifier!r}"
                    )
            else:
                all_devices = await metadata_store.get_all()
                if len(all_devices) == 1:
                    metadata = all_devices[0]
                elif len(all_devices) == 0:
                    raise HomeAssistantError(
                        "pvautonomy_ops.refresh_customer_dashboard: "
                        "no devices registered — supply device_name or "
                        "select a device first"
                    )
                else:
                    raise HomeAssistantError(
                        "pvautonomy_ops.refresh_customer_dashboard: "
                        f"{len(all_devices)} devices registered — supply "
                        "device_name to disambiguate"
                    )
        else:
            # ── Multi-entry path: device_name is required ────────────
            if not requested_device:
                raise HomeAssistantError(
                    "pvautonomy_ops.refresh_customer_dashboard: "
                    f"{len(loaded_entries)} entries loaded — supply "
                    "device_name (or entry_id) to disambiguate"
                )

            # Find metadata in any loaded entry's store. Production uses
            # a domain-global singleton store; tests may inject per-entry
            # mocks, so iterate defensively.
            for _eid, _data in loaded_entries.items():
                _store = _data.get("metadata_store")
                if _store is None:
                    continue
                try:
                    found = await _store.lookup(requested_device)
                except Exception:  # noqa: BLE001 — defensive in multi-store loop
                    found = None
                if found is not None:
                    metadata = found
                    break

            if metadata is None:
                raise HomeAssistantError(
                    "pvautonomy_ops.refresh_customer_dashboard: "
                    f"no device metadata for identifier "
                    f"{requested_device!r} in any loaded entry"
                )

            # Bind metadata to exactly one loaded entry.
            target_ha_device_id = (metadata.ha_device_id or "").strip()
            target_device_id = (metadata.device_id or "").strip()

            matches: list[tuple[str, dict]] = []
            for _eid, _data in loaded_entries.items():
                _entry: ConfigEntry = _data["entry"]
                _opts = _entry.options
                _opt_ha_id = (_opts.get("ha_device_id") or "").strip()
                _opt_selected = (_opts.get(CONF_SELECTED_DEVICE) or "").strip()

                # (a) ha_device_id binding (preferred — set at entry creation)
                if (
                    target_ha_device_id
                    and _opt_ha_id
                    and _opt_ha_id == target_ha_device_id
                ):
                    matches.append((_eid, _data))
                    continue

                # (b) selected_device matches identifier or canonical device_id
                if _opt_selected and _opt_selected in (
                    requested_device,
                    target_device_id,
                ):
                    matches.append((_eid, _data))

            if len(matches) == 0:
                raise HomeAssistantError(
                    "pvautonomy_ops.refresh_customer_dashboard: "
                    f"device {requested_device!r} resolved to metadata "
                    f"(device_id={target_device_id!r}, ha_device_id="
                    f"{target_ha_device_id!r}) but no loaded entry "
                    "matches via ha_device_id or selected_device — "
                    "supply entry_id explicitly"
                )
            if len(matches) > 1:
                ambiguous = ", ".join(eid for eid, _ in matches)
                raise HomeAssistantError(
                    "pvautonomy_ops.refresh_customer_dashboard: "
                    f"device {requested_device!r} matches multiple loaded "
                    f"entries ({ambiguous}) — supply entry_id explicitly"
                )

            entry_id, entry_data = matches[0]
            entry = entry_data["entry"]

        # ── Compute dashboard parameters ─────────────────────────────
        # Dashboard slug uses underscores; ensure_slug() returns the
        # dash form per ADR-003 / EPIC-011.
        device_name = metadata.ensure_slug().replace("-", "_")
        registry_file = (
            (call.data.get("registry_file") or "").strip()
            or metadata.registry_file
        )
        model_info = _MODEL_REGISTRY_MAP.get(metadata.model_slug, {})
        display_title = (
            (call.data.get("display_title") or "").strip()
            or model_info.get("display_name")
            or metadata.model_slug.upper()
        )

        modbus_version = entry.options.get(CONF_MODBUS_VERSION)
        selected_tier = entry.options.get(
            CONF_SELECTED_TIER, _DEFAULT_SELECTED_TIER
        )

        _LOGGER.info(
            "refresh_customer_dashboard: rebuilding dashboard "
            "(entry=%s, device_name=%s, model=%s, registry=%s, "
            "selected_tier=%s)",
            entry_id[:8],
            device_name,
            metadata.model_slug,
            registry_file,
            selected_tier,
        )

        # EPIC-012 superset signature: pass observed kwargs even if the
        # current builder ignores them. Keeps the callsite stable across
        # future dashboard policy upgrades.
        created = await async_create_dashboard(
            hass,
            device_name=device_name,
            display_title=display_title,
            registry_file=registry_file,
            modbus_version=modbus_version,
            pv_strings=None,
            model_slug=metadata.model_slug,
            selected_tier=selected_tier,
            entry_id=entry_id,
        )

        if created:
            _LOGGER.info(
                "refresh_customer_dashboard: dashboard rebuilt for %s",
                device_name,
            )
        else:
            _LOGGER.warning(
                "refresh_customer_dashboard: dashboard rebuild returned "
                "False for %s (see prior log lines for cause)",
                device_name,
            )

    hass.services.async_register(
        DOMAIN, "refresh_customer_dashboard", handle_refresh_customer_dashboard,
        schema=vol.Schema({
            vol.Optional("device_name"): cv.string,
            vol.Optional("display_title"): cv.string,
            vol.Optional("registry_file"): cv.string,
            **_ENTRY_ID_FIELD,
        }),
    )

    # EPIC-012 / TASK-014Q: explicit Grid First draft commit.
    async def _resolve_grid_first_activation_device_name(call) -> str:
        """Resolve and validate the device for Grid First draft activation."""
        from homeassistant.exceptions import HomeAssistantError

        requested_device = (call.data.get("device_name") or "").strip()
        requested_entry_id = (call.data.get("entry_id") or "").strip()
        if not requested_device:
            raise HomeAssistantError(
                "pvautonomy_ops.activate_grid_first_draft: "
                "device_name is required"
            )

        domain_data = hass.data.get(DOMAIN, {})
        loaded_entries = {
            k: v for k, v in domain_data.items()
            if isinstance(v, dict) and "config" in v
        }

        def _metadata_names(metadata) -> set[str]:
            names = {requested_device}
            device_id = (getattr(metadata, "device_id", "") or "").strip()
            if device_id:
                names.add(device_id)
            try:
                slug = (metadata.ensure_slug() or "").strip()
            except Exception:  # noqa: BLE001 - fail-safe fallback below
                slug = ""
            if slug:
                names.add(slug)
                names.add(slug.replace("-", "_"))
            return names

        def _entry_matches_metadata(entry: ConfigEntry, metadata) -> bool:
            opts = entry.options
            opt_ha_id = (opts.get("ha_device_id") or "").strip()
            opt_selected = (opts.get(CONF_SELECTED_DEVICE) or "").strip()
            target_ha_id = (getattr(metadata, "ha_device_id", "") or "").strip()
            names = _metadata_names(metadata)

            matched = False
            checked = False
            if target_ha_id and opt_ha_id:
                checked = True
                matched = matched or opt_ha_id == target_ha_id
            if opt_selected:
                checked = True
                matched = matched or opt_selected in names
            if checked:
                return matched

            # [TASK-014Q/C3] Pre-WP3 single-entry installs were configured before
            # ha_device_id / CONF_SELECTED_DEVICE were written to options, so they
            # have no binding data.  In that specific case we match by default
            # (there is only one entry, so the match is unambiguous).
            # In a multi-entry install the same absence of binding data would be
            # ambiguous — returning False prevents a mis-match and forces the caller
            # to handle degraded state instead of silently picking the wrong entry.
            return len(loaded_entries) <= 1

        async def _lookup_metadata(entry_data: dict, identifier: str):
            store = entry_data.get("metadata_store")
            if store is None:
                return None
            return await store.lookup(identifier)

        metadata = None
        entry_data = None

        if requested_entry_id or len(loaded_entries) <= 1:
            _eid, entry_data = _resolve_target_entry_data(hass, call)
            metadata = await _lookup_metadata(entry_data, requested_device)
            if metadata is None:
                raise HomeAssistantError(
                    "pvautonomy_ops.activate_grid_first_draft: "
                    f"no device metadata for identifier {requested_device!r}"
                )
            entry = entry_data["entry"]
            if not _entry_matches_metadata(entry, metadata):
                raise HomeAssistantError(
                    "pvautonomy_ops.activate_grid_first_draft: "
                    f"device_name {requested_device!r} does not belong to "
                    f"entry_id {_eid!r}"
                )
        else:
            for _eid, _data in loaded_entries.items():
                try:
                    found = await _lookup_metadata(_data, requested_device)
                except Exception:  # noqa: BLE001 - defensive in multi-store loop
                    found = None
                if found is not None:
                    metadata = found
                    break

            if metadata is None:
                raise HomeAssistantError(
                    "pvautonomy_ops.activate_grid_first_draft: "
                    f"no device metadata for identifier {requested_device!r} "
                    "in any loaded entry"
                )

            matches: list[tuple[str, dict]] = []
            for _eid, _data in loaded_entries.items():
                if _entry_matches_metadata(_data["entry"], metadata):
                    matches.append((_eid, _data))

            if len(matches) == 0:
                raise HomeAssistantError(
                    "pvautonomy_ops.activate_grid_first_draft: "
                    f"device {requested_device!r} resolved to metadata but "
                    "no loaded entry matches via ha_device_id or selected_device"
                )
            if len(matches) > 1:
                ambiguous = ", ".join(eid for eid, _ in matches)
                raise HomeAssistantError(
                    "pvautonomy_ops.activate_grid_first_draft: "
                    f"device {requested_device!r} matches multiple loaded "
                    f"entries ({ambiguous}) — supply entry_id explicitly"
                )

        return metadata.ensure_slug().replace("-", "_")

    async def handle_activate_grid_first_draft(call) -> None:
        """Commit the current Grid First draft bundle to the inverter.

        Resolve metadata and bind it to exactly one config entry before
        delegating to ``async_commit_grid_first_draft``.
        """
        from .switch import async_commit_grid_first_draft

        device_name = await _resolve_grid_first_activation_device_name(call)
        await async_commit_grid_first_draft(hass, device_name=device_name)

    hass.services.async_register(
        DOMAIN, "activate_grid_first_draft", handle_activate_grid_first_draft,
        schema=vol.Schema({
            vol.Required("device_name"): cv.string,
            **_ENTRY_ID_FIELD,
        }),
    )

    # EPIC-004 / SPEC-20260512-epic004-build-firmware-service:
    # `build_firmware` — build-only firmware service. Resolves the target
    # entry/device deterministically (fail closed on ambiguity), runs the
    # normal production build pipeline (proxy/GHA with compile-secret
    # injection), stores the firmware artifact, and exposes safe build
    # metadata via build-stage events / status sensor attributes. It never
    # calls OTA / install / reconfigure. `force_rebuild=true` bypasses the
    # proxy artifact cache for registry-only update scenarios.
    async def handle_build_firmware(call) -> None:
        # EPIC-004 SPEC-20260513-epic004-build-firmware-operation-tracker:
        # Wrap the build-only helper in the per-entry OperationRunner so the
        # status sensor exposes op_state=running / op_name=build_firmware /
        # op_progress while the build runs. Bridge build-stage events into
        # OperationTracker.update_progress (entry-scoped) and clean the
        # listener up unconditionally. No OTA/install/reconfigure paths are
        # introduced — the underlying helper remains build-only.
        from homeassistant.exceptions import HomeAssistantError

        from .build_service import async_build_firmware_for_device

        device_name = call.data.get("device_name")
        # EPIC-004 follow-up: when no entry_id is supplied but device_name
        # is, prefer device-name-based entry resolution so multi-entry
        # customer installs don't have to hand-craft a ULID into the UI.
        # Falls back to the existing entry-or-single-entry resolver.
        requested_entry_id = (call.data.get("entry_id") or "").strip()
        if not requested_entry_id and device_name:
            entry_id, entry_data = await _resolve_entry_for_device(
                hass, device_name,
            )
        else:
            entry_id, entry_data = _resolve_target_entry_data(hass, call)
        force_rebuild = bool(call.data.get("force_rebuild", False))

        # fix/#128: Guard — local/manual build entries do not use the proxy build path.
        # Entries created via adopt_direct or local_esphome have CONF_BUILD_BACKEND=manual
        # and must not attempt a proxy build. Raise clearly before any pipeline setup.
        _backend_mode = entry_data.get(CONF_BUILD_BACKEND, DEFAULT_BUILD_BACKEND)
        if _backend_mode in (BUILD_BACKEND_MANUAL, BUILD_BACKEND_ESPHOME_DASHBOARD):
            raise HomeAssistantError(
                "This device uses a local build path. Build firmware with ESPHome, "
                "then register or update the device in PVAutonomy."
            )

        # fix/#120: preflight COMPILE_SECRET_KEY before the operation starts so
        # the status sensor never transitions to op_state=running for a pure
        # configuration gap. The backend fail-closed guard remains as defence-in-
        # depth; this check prevents the unnecessary YAML generation, health
        # check, and pipeline setup that would otherwise precede it.
        #
        # G7 (#122): envelope-mode builds seal per-device compile secrets into
        # the HPKE envelope and need no repo-wide COMPILE_SECRET_KEY — the
        # G6/G7 target state is exactly "no COMPILE_SECRET_KEY on the HA".
        # Require the legacy key only when the envelope path cannot activate
        # for this build (force-disabled via CONF_ENVELOPE_MODE_ENABLED, or no
        # pinned roots). The decision reuses the pipeline's own resolver with
        # the same entry runtime config, so preflight and build wiring can
        # never disagree. If an envelope build later degrades to the legacy
        # wire path (keyset 404/405), build_backend.start_build() still fails
        # closed before any plaintext leaves HA
        # (compile_secret_key_missing_or_invalid).
        from .keyring import COMPILE_SECRET_KEY_RE, PVAutonomyKeyring
        from .pipeline import _envelope_mode_enabled
        from .secret_envelope import ROOT_PUBKEYS_PINNED

        _envelope_possible = bool(ROOT_PUBKEYS_PINNED) and _envelope_mode_enabled(
            hass, entry_data.get("config") or {}
        )
        if not _envelope_possible:
            _keyring = entry_data.get("keyring")
            if _keyring is None:
                _keyring = PVAutonomyKeyring(hass)
                await _keyring.async_load()
            _csk = await _keyring.get_compile_secret_key()
            if not _csk or not COMPILE_SECRET_KEY_RE.match(_csk):
                raise HomeAssistantError(
                    "Build encryption is not configured. Please complete "
                    "PVAutonomy operator provisioning (COMPILE_SECRET_KEY) "
                    "before starting a firmware build."
                )

        operation_runner = entry_data["operation_runner"]
        operation_tracker = entry_data["operation_tracker"]

        # Build-stage → operation progress bridge (entry-scoped, listener
        # cleaned up in finally).
        @callback
        def _on_build_stage(event) -> None:
            data = event.data
            ev_entry = data.get("entry_id")
            if ev_entry and entry_id and ev_entry != entry_id:
                return  # foreign entry — ignore
            try:
                progress_val = data.get("progress")
                if progress_val is None:
                    return
                operation_tracker.update_progress(
                    int(progress_val),
                    data.get("stage") or data.get("detail"),
                )
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "build-firmware progress bridge: ignoring malformed event",
                    exc_info=True,
                )

        unsub = hass.bus.async_listen(
            f"{DOMAIN}_build_stage", _on_build_stage
        )

        try:
            result = await operation_runner.run(
                "build_firmware",
                async_build_firmware_for_device,
                hass,
                entry_id=entry_id,
                entry_data=entry_data,
                device_name=device_name,
                force_rebuild=force_rebuild,
            )
        finally:
            unsub()

        if not result.get("success"):
            raise HomeAssistantError(
                f"pvautonomy_ops.build_firmware failed: "
                f"{result.get('error') or 'unknown error'}"
            )

        meta = result.get("result") or {}
        _LOGGER.info(
            "build_firmware service done (entry=%s): build_id=%s, "
            "target_device=%s, cache_hit=%s, firmware_size=%s, backend=%s, "
            "force_rebuild=%s",
            entry_id[:8] if entry_id else "",
            meta.get("build_id"),
            meta.get("target_device"),
            meta.get("cache_hit"),
            meta.get("firmware_size"),
            meta.get("build_backend"),
            meta.get("force_rebuild"),
        )

    hass.services.async_register(
        DOMAIN, "build_firmware", handle_build_firmware,
        schema=vol.Schema({
            vol.Optional("device_name"): cv.string,
            vol.Optional("force_rebuild"): cv.boolean,
            **_ENTRY_ID_FIELD,
        }),
    )

    # EPIC-004 / SPEC-20260514-epic004-install-prepared-firmware-service:
    # `install_prepared_firmware` — install-only firmware service. Installs
    # the prepared firmware artifact produced earlier by `build_firmware`
    # onto the selected production Edge101 device via OTA. NEVER calls
    # `run_build_pipeline`, NEVER starts a new build, NEVER calls
    # wizard/reconfigure/factory-reset, NEVER writes inverter registers.
    # Requires `confirmed: true` and fails closed otherwise.
    async def handle_install_prepared_firmware(call) -> None:
        from homeassistant.exceptions import HomeAssistantError

        from .install_service import (
            async_install_prepared_firmware_for_device,
        )

        device_name = call.data.get("device_name")
        # EPIC-004 follow-up: when no entry_id is supplied but device_name
        # is, prefer device-name-based entry resolution so multi-entry
        # customer installs don't have to hand-craft a ULID into the UI.
        # Falls back to the existing entry-or-single-entry resolver.
        requested_entry_id = (call.data.get("entry_id") or "").strip()
        if not requested_entry_id and device_name:
            entry_id, entry_data = await _resolve_entry_for_device(
                hass, device_name,
            )
        else:
            entry_id, entry_data = _resolve_target_entry_data(hass, call)
        confirmed = bool(call.data.get("confirmed", False))

        operation_runner = entry_data["operation_runner"]
        operation_tracker = entry_data["operation_tracker"]

        # install_stage → operation progress bridge (entry-scoped).
        @callback
        def _on_install_stage(event) -> None:
            data = event.data
            ev_entry = data.get("entry_id")
            if ev_entry and entry_id and ev_entry != entry_id:
                return
            try:
                progress_val = data.get("progress")
                if progress_val is None:
                    return
                operation_tracker.update_progress(
                    int(progress_val),
                    data.get("stage"),
                )
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "install_prepared_firmware progress bridge: "
                    "ignoring malformed event",
                    exc_info=True,
                )

        unsub = hass.bus.async_listen(
            f"{DOMAIN}_install_stage", _on_install_stage
        )

        try:
            result = await operation_runner.run(
                "install_prepared_firmware",
                async_install_prepared_firmware_for_device,
                hass,
                entry_id=entry_id,
                entry_data=entry_data,
                device_name=device_name,
                confirmed=confirmed,
            )
        finally:
            unsub()

        if not result.get("success"):
            raise HomeAssistantError(
                f"pvautonomy_ops.install_prepared_firmware failed: "
                f"{result.get('error') or 'unknown error'}"
            )

        meta = result.get("result") or {}
        _LOGGER.info(
            "install_prepared_firmware service done (entry=%s): "
            "target_device=%s, firmware_size=%s, device_ip=%s, "
            "ip_method=%s",
            entry_id[:8] if entry_id else "",
            meta.get("target_device"),
            meta.get("firmware_size"),
            meta.get("device_ip"),
            meta.get("ip_method"),
        )

        # Customer Path First (P1 / fix/customer-path-dashboard-refresh-after-install):
        # A successful install changes the device's entity surface (new/renamed
        # entities appear, stale ones drop out). Rebuild the customer dashboard
        # automatically so the change is visible WITHOUT a manual refresh —
        # closing the proof/staging gap recorded in
        # docs/CUSTOMER-PATH-VALIDATION.md (Developer-Tools install + manual
        # refresh is staging-only; the supported customer path must surface the
        # result by itself). Reuses the existing idempotent
        # refresh_customer_dashboard service rather than re-implementing the
        # rebuild here. Best-effort: the install already succeeded, so a refresh
        # failure must NEVER fail this call — it is logged (no secrets) so the
        # customer can retry the refresh action.
        refresh_payload = {}
        refresh_device = meta.get("target_device") or device_name
        if refresh_device:
            refresh_payload["device_name"] = refresh_device
        if entry_id:
            refresh_payload["entry_id"] = entry_id
        try:
            await hass.services.async_call(
                DOMAIN,
                "refresh_customer_dashboard",
                refresh_payload,
                blocking=True,
            )
            _LOGGER.info(
                "install_prepared_firmware: customer dashboard refreshed "
                "after install (entry=%s, device=%s)",
                entry_id[:8] if entry_id else "",
                refresh_device,
            )
        except Exception as exc:  # pragma: no cover - best-effort, never fail install
            _LOGGER.warning(
                "install_prepared_firmware: post-install dashboard refresh "
                "failed (non-fatal; customer can refresh manually): %s",
                exc,
            )

    hass.services.async_register(
        DOMAIN, "install_prepared_firmware", handle_install_prepared_firmware,
        schema=vol.Schema({
            vol.Optional("device_name"): cv.string,
            vol.Required("confirmed"): cv.boolean,
            **_ENTRY_ID_FIELD,
        }),
    )

    # ------------------------------------------------------------------
    # TASK-20260520 Phase 2b: compile_secret_key provisioning entrypoint
    # ------------------------------------------------------------------
    # The repo-wide AES-256 key (64 hex / 32 bytes) HA uses to encrypt the
    # legacy `encrypted_secrets` compile payload. It MUST equal the GitHub
    # Actions repo secret COMPILE_SECRET_KEY in PVAutonomy/inverter-registry,
    # otherwise the build workflow cannot decrypt. The real value is
    # provisioned out-of-band by the operator via these services; it is
    # never logged (fingerprint only) and never returned as raw bytes.
    import hashlib as _hashlib
    from homeassistant.core import SupportsResponse as _SupportsResponse
    from homeassistant.exceptions import (
        HomeAssistantError as _HAError,
        ServiceValidationError as _SvcValidationError,
    )

    def _key_fingerprint(key: str) -> str:
        """Non-reversible sha256(key)[:8] correlation fingerprint."""
        return _hashlib.sha256(key.encode("ascii")).hexdigest()[:8]

    async def handle_set_compile_secret_key(call) -> None:
        """Store the repo-wide compile_secret_key in the keyring.

        The key value is never logged. On invalid format a
        ServiceValidationError is raised WITHOUT echoing the value.
        """
        entry_id, entry_data = _resolve_target_entry_data(hass, call)
        keyring = entry_data.get("keyring")
        if keyring is None:
            raise _HAError("pvautonomy_ops: keyring unavailable for this entry")
        key = call.data["compile_secret_key"]
        try:
            await keyring.set_compile_secret_key(key)
        except ValueError as exc:
            # Do not include the rejected value in the surfaced error.
            raise _SvcValidationError(
                "compile_secret_key must be 64 hex characters (AES-256). "
                "It must match the GitHub Actions repo secret "
                "COMPILE_SECRET_KEY in PVAutonomy/inverter-registry."
            ) from exc
        # keyring logs only a fingerprint; add an entry-scoped breadcrumb.
        _LOGGER.info(
            "set_compile_secret_key: stored for entry=%s (fingerprint=%s)",
            entry_id[:8], _key_fingerprint(key),
        )

    async def handle_clear_compile_secret_key(call) -> None:
        """Remove the stored compile_secret_key from the keyring."""
        entry_id, entry_data = _resolve_target_entry_data(hass, call)
        keyring = entry_data.get("keyring")
        if keyring is None:
            raise _HAError("pvautonomy_ops: keyring unavailable for this entry")
        await keyring.clear_compile_secret_key()
        _LOGGER.info(
            "clear_compile_secret_key: cleared for entry=%s", entry_id[:8]
        )

    async def handle_compile_secret_key_status(call) -> dict:
        """Report whether a compile_secret_key is provisioned.

        Returns only {present: bool, fingerprint: <sha256[:8]|None>} — never
        any raw key material.
        """
        entry_id, entry_data = _resolve_target_entry_data(hass, call)
        keyring = entry_data.get("keyring")
        if keyring is None:
            raise _HAError("pvautonomy_ops: keyring unavailable for this entry")
        key = await keyring.get_compile_secret_key()
        present = bool(key)
        fingerprint = _key_fingerprint(key) if present else None
        _LOGGER.info(
            "compile_secret_key_status: entry=%s present=%s fingerprint=%s",
            entry_id[:8], present, fingerprint,
        )
        return {"present": present, "fingerprint": fingerprint}

    hass.services.async_register(
        DOMAIN, "set_compile_secret_key", handle_set_compile_secret_key,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("compile_secret_key"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, "clear_compile_secret_key", handle_clear_compile_secret_key,
        schema=vol.Schema({vol.Required("entry_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, "compile_secret_key_status", handle_compile_secret_key_status,
        schema=vol.Schema({vol.Required("entry_id"): cv.string}),
        supports_response=_SupportsResponse.ONLY,
    )

    _LOGGER.info("Registered %d services for %s (domain-scope, P1-06)", len(_SERVICE_NAMES), DOMAIN)
