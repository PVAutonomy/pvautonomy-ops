"""Config and Options Flow for PVAutonomy Ops.

EPIC-006 WP3: Multi-step Config Flow Wizard for new device setup.
OptionsFlow with menu: Settings / Add Device / Relocate.

WP3-Hotfix: Wizard binds to physical HA device (MAC last6) +
per-device unique_ids.

Contract: ops-contract-v1.md (v1.0.0)
Directive: EPIC-006-WP3
"""
import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
# Optional: selector helpers (unavailable in local test env without HA).
with contextlib.suppress(ImportError):  # pragma: no cover
    from homeassistant.helpers.selector import (
        SelectOptionDict,
        SelectSelector,
        SelectSelectorConfig,
        SelectSelectorMode,
    )

from .const import (
    BUILD_BACKEND_MANUAL,
    BUILD_BACKEND_PROXY_REMOTE,
    BUILD_SERVICE_LOCAL_ESPHOME,
    BUILD_SERVICE_MANAGED,
    BUILD_SERVICE_SELF_HOSTED,
    CONF_ARTIFACT_CHANNEL,
    CONF_BUILD_BACKEND,
    CONF_BUILD_SERVICE_MODE,
    CONF_CACHE_KEEP_BUILDS,
    CONF_DEVICE_SLUG,
    CONF_MANUFACTURER,
    CONF_MAP_CONFIRMED,
    CONF_MODBUS_VERSION,
    CONF_MODEL_SLUG,
    CONF_NUMBER,
    CONF_OTA_RETRIES,
    CONF_OTA_RETRY_DELAYS,
    CONF_POLL_INTERVAL,
    CONF_PROXY_API_KEY,
    CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT,
    CONF_PROXY_BASE_URL,
    CONF_PROXY_CUSTOMER_ID,
    CONF_SELECTED_DEVICE,
    CONF_SELECTED_TIER,
    CONF_SIMULATED_FAILURE_MODE,
    CONF_SITE,
    CONF_STRICT_GATES,
    CONFIG_ENTRY_VERSION,
    DEFAULT_ARTIFACT_CHANNEL,
    DEFAULT_BUILD_BACKEND,
    DEFAULT_CACHE_KEEP_BUILDS,
    DEFAULT_OTA_RETRIES,
    DEFAULT_OTA_RETRY_DELAYS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PROXY_API_KEY,
    DEFAULT_PROXY_AUTO_REFRESH,
    DEFAULT_PROXY_BASE_URL,
    DEFAULT_PROXY_CUSTOMER_ID,
    DEFAULT_SELECTED_TIER,
    DEFAULT_SIMULATED_FAILURE_MODE,
    DOMAIN,
    LOCATION_PRESETS,
    MANUFACTURER_MAP,
    MENU_OPTION_ADOPT_DIRECT,
    MENU_OPTION_ADVANCED_PROXY,
    MENU_OPTION_LOCAL_ESPHOME,
    MENU_OPTION_MANAGED_BUILD,
    MODEL_REGISTRY_MAP,
    SETUP_STATE_ADOPTED,
    TIER_STANDARD,
    TIER_EXTENDED,
    TIER_UNSAFE,
    UNSAFE_CONSENT_PHRASE,
)
from .device_id import compute_device_id, compute_node_name
from .const import GRID_POWER_OPTIONS_KEY
from .grid_power import (
    GRID_POWER_MANAGER_KEY,
    GridPowerManager,
    entity_to_source_ref,
    signed_net_mapping,
    split_mapping,
    validate_mapping_dict,
)
from .grid_power_shrdzm import (
    count_shrdzm_devices,
    discover_shrdzm_candidates,
)

_LOGGER = logging.getLogger(__name__)

# Localized feature-level display labels for customer-facing and legacy tier
# strings. The wizard intentionally exposes only standard + extended; unsafe
# remains an internal/legacy value and is fail-closed if submitted.
_TIER_LABELS: dict[str, dict[str, str]] = {
    "en": {
        TIER_STANDARD: "Standard",
        TIER_EXTENDED: "Extended",
        TIER_UNSAFE: "Unsafe",
    },
    "de": {
        TIER_STANDARD: "Standard",
        TIER_EXTENDED: "Erweitert",
        TIER_UNSAFE: "Experte",
    },
}


def _tier_display_label(hass, tier_value: str) -> str:
    """Return the localized short label for a tier value."""
    lang = getattr(getattr(hass, "config", None), "language", "en")
    labels = _TIER_LABELS.get(lang, _TIER_LABELS["en"])
    return labels.get(tier_value, tier_value.title())


def get_supported_tiers(registry_file: str) -> list[str]:
    """Derive the supported customer-facing tier choices from the registry.

    Returns a list of tier values (e.g. ["standard"] or
    ["standard", "extended"]).

    Fail-closed: if the registry cannot be read or lacks evidence for
    higher tiers, only "standard" is returned. Unsafe entries may still exist
    in the registry for internal/guardrail purposes, but they are never
    surfaced as a wizard choice.
    """
    try:
        from .dashboard_builder import load_registry

        registry = load_registry(registry_file)
    except Exception:
        _LOGGER.debug(
            "Cannot load registry %s for tier check — defaulting to standard-only",
            registry_file,
        )
        return [TIER_STANDARD]

    registers = registry.get("registers", {})
    numbers = registers.get("numbers", [])
    switches = registers.get("switches", [])
    selects = registers.get("selects", [])
    all_controls = numbers + switches + selects

    # Extended: requires controls explicitly tagged tier="extended" in the registry.
    # Entries with no tier tag default to standard and must not promote to extended.
    # (Previous entity_category+enabled_by_default heuristic false-positived on
    # internal MIC600 Modbus helpers — TASK-014T fix.)
    has_extended = any(
        e.get("tier") == TIER_EXTENDED
        for e in all_controls
    )

    tiers = [TIER_STANDARD]
    if has_extended:
        tiers.append(TIER_EXTENDED)
    return tiers


# Legacy defaults kept for backward compat (imported by other modules)
DEFAULT_NAME = "PVAutonomy"


def _resolve_slug_from_esphome(hass, ha_device_id: str) -> str | None:
    """Resolve device slug from ESPHome config entry (legacy fallback)."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(ha_device_id)
    if not device:
        return None
    for eid in device.config_entries:
        ce = hass.config_entries.async_get_entry(eid)
        if ce and ce.domain == "esphome":
            name = ce.data.get("device_name")
            if name:
                return name
    return None


def get_device_slug_from_entry(
    entry: config_entries.ConfigEntry,
    hass=None,
) -> str | None:
    """Extract device_slug from a config entry (R5 backward compat).

    Resolution order:
    1. _initial_device.device_slug (new format, EPIC-011)
    2. Compute from _initial_device.model_slug + site + number (legacy)
    3. _initial_device.device_name (if present, legacy migration)
    4. Root-level device_slug in options (legacy)
    5. Resolve from ESPHome config entry via ha_device_id (pre-EPIC-011)
    """
    initial = entry.options.get("_initial_device", {})
    # 1. Direct slug (new format)
    slug = initial.get(CONF_DEVICE_SLUG)
    if slug:
        return slug
    # 2. Compute from components (legacy entries)
    model = initial.get(CONF_MODEL_SLUG)
    site = initial.get(CONF_SITE)
    number = initial.get(CONF_NUMBER)
    if model and site and number is not None:
        return compute_node_name(model, site, int(number))
    # 3. Legacy device_name key
    if initial.get("device_name"):
        return initial["device_name"]
    # 4. Root-level device_slug (legacy)
    if entry.options.get(CONF_DEVICE_SLUG):
        return entry.options[CONF_DEVICE_SLUG]
    # 5. Resolve from ESPHome via ha_device_id (pre-EPIC-011 entries)
    ha_device_id = entry.options.get("ha_device_id")
    if hass and ha_device_id:
        return _resolve_slug_from_esphome(hass, ha_device_id)
    return None


# ============================================================================
# Config Flow: Multi-Step Setup Wizard (EPIC-006-WP3, Deliverable A)
# ============================================================================


class PVAutonomyOpsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step Config Flow for PVAutonomy Ops.

    Steps:
        user (menu) → proxy → manufacturer → model → location → target_device →
          • setup_new:  → summary → build + OTA flash
          • adopt:      → adopt_confirm (register only, NO build/install/flash)
    One Config Entry per physical device (unique_id = pvautonomy_ops_{ha_device_id}).
    """

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        """Initialize wizard state."""
        self._proxy_base_url: str = ""
        self._proxy_api_key: str = ""
        self._proxy_customer_id: str = ""
        self._manufacturer: str = ""
        self._model_slug: str = ""
        self._site: str = ""
        self._number: int = 1
        self._registry_file: str = ""
        # WP3-Hotfix: Physical device binding
        self._ha_device_id: str = ""
        self._mac_suffix: str = ""
        # In-wizard progress tracking
        self._device_id: str = ""
        self._summary_title: str = ""
        self._display_name: str = ""
        # async_show_progress task refs
        self._build_task: asyncio.Task | None = None
        self._flash_task: asyncio.Task | None = None
        self._build_result = None  # PipelineResult
        self._flash_error: str | None = None
        self._device_ip: str | None = None
        self._ota_password: str | None = None
        # Tier gating (EPIC-010 Tiering v1.1)
        self._selected_tier: str = DEFAULT_SELECTED_TIER
        # Version-aware registry (EPIC-010 vNext)
        # TASK-20260327: _map_status distinguishes three outcomes:
        #   "unknown"  — no pre-build evidence (HR73 missing, anchors unavailable)
        #   "confirmed"— anchors pass, HR73 parseable, map is plausible
        #   "failed"   — contradictory evidence (anchors out of range, HR73 unparseable)
        self._modbus_version: int | None = None
        self._map_status: str = "unknown"  # "unknown" | "confirmed" | "failed"
        # Adopt-running-device mode: bind an already-running ESPHome/Edge101
        # device WITHOUT any build/install/reflash. Set from the first-screen
        # menu; routes target_device → adopt_confirm instead of the build path.
        self._adopt_mode: bool = False
        # Local ESPHome YAML export mode: True only when local_esphome is
        # selected (not adopt_direct). Both paths share BUILD_SERVICE_LOCAL_ESPHOME
        # but only local_esphome should route location → local_yaml_ready.
        self._local_yaml_mode: bool = False
        # Build service mode (#128): records which UX path created this entry.
        # Default = managed (pva_ key + DEFAULT_PROXY_BASE_URL); overridden by
        # the mode-selector step before any proxy/key collection happens.
        self._build_service_mode: str = BUILD_SERVICE_MANAGED
        # MAC conflict detection (relocate from existing device)
        from .metadata import DeviceMetadata

        self._relocate_from: DeviceMetadata | None = None
        # Re-flash: existing config entry to replace
        self._replacing_entry: config_entries.ConfigEntry | None = None

    @property
    def _map_confirmed(self) -> bool:
        """Derive boolean map_confirmed from tri-state _map_status.

        TASK-20260327: Only contradictory evidence ("failed") blocks the
        build.  Missing evidence ("unknown") preserves customer tier choice.
        """
        return self._map_status != "failed"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Choose build service mode.

        Four paths:
        - managed_build: PVAutonomy Managed Build Service (pva_ key, wizard builds firmware)
        - adopt_direct: Register already-running device (no key, no build)
        - local_esphome: Build firmware yourself (no key, guidance only)
        - advanced_proxy: Self-hosted / custom proxy (existing proxy step)
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=[
                MENU_OPTION_MANAGED_BUILD,
                MENU_OPTION_ADOPT_DIRECT,
                MENU_OPTION_LOCAL_ESPHOME,
                MENU_OPTION_ADVANCED_PROXY,
            ],
        )

    async def async_step_setup_new(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Redirected to managed_build (backward compat for programmatic callers)."""
        return await self.async_step_managed_build()

    async def async_step_adopt_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Redirected to adopt_direct (backward compat for programmatic callers)."""
        return await self.async_step_adopt_direct()

    async def async_step_managed_build(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu target: PVAutonomy Managed Build Service."""
        self._adopt_mode = False
        self._build_service_mode = BUILD_SERVICE_MANAGED
        return await self.async_step_managed_key()

    async def async_step_adopt_direct(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu target: Register already-running device without proxy."""
        self._adopt_mode = True
        self._build_service_mode = BUILD_SERVICE_LOCAL_ESPHOME
        self._proxy_base_url = ""
        self._proxy_api_key = ""
        self._proxy_customer_id = ""
        return await self.async_step_manufacturer()

    async def async_step_local_esphome(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu target: Local ESPHome / build yourself."""
        self._adopt_mode = True
        self._local_yaml_mode = True
        self._build_service_mode = BUILD_SERVICE_LOCAL_ESPHOME
        self._proxy_base_url = ""
        self._proxy_api_key = ""
        self._proxy_customer_id = ""
        return await self.async_step_local_esphome_guide()

    async def async_step_advanced_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu target: Self-hosted / custom proxy (advanced)."""
        self._adopt_mode = False
        self._build_service_mode = BUILD_SERVICE_SELF_HOSTED
        return await self.async_step_proxy()

    async def async_step_managed_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Managed Build Service: collect PVAutonomy Build-Key only.

        proxy_base_url is fixed to DEFAULT_PROXY_BASE_URL and never shown.
        customer_id is auto-derived via /whoami.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._proxy_api_key = user_input[CONF_PROXY_API_KEY]
            self._proxy_base_url = DEFAULT_PROXY_BASE_URL
            self._proxy_customer_id = ""

            try:
                from .build_backend import ProxyRemoteBuildBackend

                backend = ProxyRemoteBuildBackend(
                    base_url=self._proxy_base_url,
                    api_key=self._proxy_api_key,
                    customer_id="",
                )
                try:
                    await backend.health_check()

                    try:
                        whoami = await backend.whoami()
                        if whoami and whoami.get("customer_id"):
                            self._proxy_customer_id = whoami["customer_id"]
                        else:
                            _LOGGER.warning(
                                "/whoami returned no customer_id — attempting fallback"
                            )
                    except Exception as whoami_exc:
                        exc_str = str(whoami_exc).lower()
                        if "401" in exc_str or "403" in exc_str:
                            errors["base"] = "build_key_rejected"
                            _LOGGER.warning("Build-Key auth failed: %s", whoami_exc)
                        else:
                            _LOGGER.warning(
                                "/whoami failed (trying fallback): %s", whoami_exc
                            )
                finally:
                    await backend.close()

                if not errors and not self._proxy_customer_id:
                    inherited = self._find_inherited_customer_id()
                    if inherited:
                        self._proxy_customer_id = inherited

                if not errors and not self._proxy_customer_id:
                    errors["base"] = "build_account_incomplete"
                    _LOGGER.warning("Managed: customer_id not derivable")

                if not errors:
                    return await self.async_step_manufacturer()

            except Exception as exc:
                errors["base"] = "build_service_unavailable"
                _LOGGER.warning("Managed Build Service unreachable: %s", exc)

        return self.async_show_form(
            step_id="managed_key",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROXY_API_KEY,
                        default="",
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_local_esphome_guide(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Local ESPHome: guidance step — no build, no key required.

        Shows instructions for building firmware with ESPHome locally.
        Routes to manufacturer/model selection, then ends in adopt_confirm.
        No managed Build-Key, no COMPILE_SECRET_KEY.
        """
        if user_input is not None:
            return await self.async_step_manufacturer()

        return self.async_show_form(
            step_id="local_esphome_guide",
            data_schema=vol.Schema({}),
        )

    async def _generate_and_save_local_yaml(self) -> tuple[str, str]:
        """Generate ESPHome YAML for local self-build and save to config dir.

        Returns (yaml_path_str, node_name) on success.
        Raises YamlGenerationError on failure.
        No managed-service context required — generates !secret placeholders.
        """
        from .yaml_generator import YamlGenerationError, generate_device_yaml  # noqa: F401

        node_name = compute_node_name(self._model_slug, self._site, self._number)
        yaml_content = generate_device_yaml(
            model=self._model_slug,
            site=self._site,
            number=self._number,
            registry_file=self._registry_file,
            mac_suffix=None,
            selected_tier=TIER_STANDARD,
            modbus_version=None,
            map_confirmed=True,
        )

        out_dir = Path(self.hass.config.path("pvautonomy", "generated"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{node_name}.yaml"
        out_path.write_text(yaml_content, encoding="utf-8")
        _LOGGER.info(
            "Local ESPHome YAML written: %s (%d lines)",
            out_path,
            yaml_content.count("\n"),
        )
        return str(out_path), node_name

    async def async_step_local_yaml_ready(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Local ESPHome: generate YAML, write to file, show path + instructions.

        Called after model/location are known.
        No Build-Key, no COMPILE_SECRET_KEY, no managed build.
        On submit, aborts with local_yaml_exported — user returns later to adopt.
        """
        if user_input is not None:
            return self.async_abort(reason="local_yaml_exported")

        yaml_path = ""
        node_name = compute_node_name(self._model_slug, self._site, self._number)
        try:
            yaml_path, node_name = await self._generate_and_save_local_yaml()
        except Exception as exc:
            _LOGGER.error("Local YAML generation failed: %s", exc)
            yaml_path = ""

        return self.async_show_form(
            step_id="local_yaml_ready",
            data_schema=vol.Schema({}),
            description_placeholders={
                "yaml_path": yaml_path or "(generation failed — check logs)",
                "node_name": node_name,
            },
        )

    async def _proceed_after_binding(self) -> FlowResult:
        """Branch after the physical device is bound in ``target_device``.

        Adopt mode skips tier selection and the in-wizard build/flash and
        goes straight to confirmation; normal setup continues to tiering.
        """
        if self._adopt_mode:
            return await self.async_step_adopt_confirm()
        return await self.async_step_tier_selection()

    async def async_step_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Proxy connection configuration + health check.

        UX Pack: customer_id is auto-derived via /whoami (hidden from user).
        Error handling differentiates 404/401/5xx for actionable messages.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._proxy_base_url = user_input[CONF_PROXY_BASE_URL]
            self._proxy_api_key = user_input[CONF_PROXY_API_KEY]
            # UX Pack: customer_id auto-derived, not in form
            self._proxy_customer_id = ""

            # Validate: proxy health check + auto-derive customer_id via /whoami
            try:
                from .build_backend import ProxyRemoteBuildBackend

                backend = ProxyRemoteBuildBackend(
                    base_url=self._proxy_base_url,
                    api_key=self._proxy_api_key,
                    customer_id="",
                )
                try:
                    await backend.health_check()

                    # Auto-derive customer_id via /whoami
                    try:
                        whoami = await backend.whoami()
                        if whoami and whoami.get("customer_id"):
                            self._proxy_customer_id = whoami["customer_id"]
                            _LOGGER.info(
                                "Auto-derived customer_id from /whoami: %s",
                                self._proxy_customer_id,
                            )
                        else:
                            _LOGGER.warning(
                                "/whoami returned no customer_id — attempting "
                                "fallback to existing pvautonomy_ops entry"
                            )
                    except Exception as whoami_exc:
                        # Differentiated /whoami error handling
                        exc_str = str(whoami_exc).lower()
                        if "404" in exc_str:
                            errors["base"] = "proxy_too_old"
                            _LOGGER.warning("/whoami 404 — proxy needs update")
                        elif "401" in exc_str or "403" in exc_str:
                            errors["base"] = "proxy_auth_failed"
                            _LOGGER.warning("/whoami auth failed: %s", whoami_exc)
                        else:
                            # Other transport/parse errors: try the fallback
                            # before failing the flow (B).
                            _LOGGER.warning(
                                "/whoami failed (will try fallback): %s",
                                whoami_exc,
                            )
                finally:
                    await backend.close()

                # (B) Inherit customer_id from a matching existing user entry
                # — same proxy_base_url + same proxy_api_key. Adopt-typical:
                # the second device of the same customer reuses the same
                # API key, so customer_id is identical.
                if not errors and not self._proxy_customer_id:
                    inherited = self._find_inherited_customer_id()
                    if inherited:
                        self._proxy_customer_id = inherited
                        _LOGGER.info(
                            "Inherited proxy_customer_id from existing "
                            "pvautonomy_ops entry (same proxy + API key)"
                        )

                # (A) Fail closed if customer_id still unknown — no silent
                # empty-customer_id entries (which produce HTTP 400 from the
                # proxy at the next build).
                if not errors and not self._proxy_customer_id:
                    errors["base"] = "customer_id_missing"
                    _LOGGER.warning(
                        "Proxy customer_id not derivable (/whoami empty and "
                        "no matching existing entry) — stopping setup"
                    )

                if not errors:
                    return await self.async_step_manufacturer()

            except Exception as exc:
                errors["base"] = "proxy_unreachable"
                _LOGGER.warning("Proxy health check failed: %s", exc)

        # Pre-fill from existing entries (for 2nd+ device)
        defaults = self._get_proxy_defaults()

        return self.async_show_form(
            step_id="proxy",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROXY_BASE_URL,
                        default=defaults.get(CONF_PROXY_BASE_URL, DEFAULT_PROXY_BASE_URL),
                    ): str,
                    vol.Required(
                        CONF_PROXY_API_KEY,
                        default=defaults.get(CONF_PROXY_API_KEY, DEFAULT_PROXY_API_KEY),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_manufacturer(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Select manufacturer from registry.

        UX Pack: Auto-skip when only one manufacturer exists.
        """
        if user_input is not None:
            self._manufacturer = user_input[CONF_MANUFACTURER]
            return await self.async_step_model()

        manufacturer_options = {k: k.title() for k in MANUFACTURER_MAP}

        # UX Pack: auto-skip if only one manufacturer
        if len(manufacturer_options) == 1:
            self._manufacturer = next(iter(MANUFACTURER_MAP))
            return await self.async_step_model()

        return self.async_show_form(
            step_id="manufacturer",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MANUFACTURER): vol.In(manufacturer_options),
                }
            ),
        )

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: Select model filtered by manufacturer.

        UX Pack: Auto-skip when only one model exists for this manufacturer.
        """
        if user_input is not None:
            self._model_slug = user_input[CONF_MODEL_SLUG]
            self._registry_file = MODEL_REGISTRY_MAP[self._model_slug]["registry_file"]
            return await self.async_step_location()

        model_slugs = MANUFACTURER_MAP.get(self._manufacturer, [])

        # UX Pack: auto-skip if only one model for this manufacturer
        if len(model_slugs) == 1:
            self._model_slug = model_slugs[0]
            self._registry_file = MODEL_REGISTRY_MAP[self._model_slug]["registry_file"]
            return await self.async_step_location()

        model_options = {
            slug: MODEL_REGISTRY_MAP[slug]["display_name"]
            for slug in model_slugs
        }

        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODEL_SLUG): vol.In(model_options),
                }
            ),
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 5: Device location (site preset + number).

        UX Pack: Dropdown presets for common locations + custom text field.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            site_preset = user_input.get("site_preset", "custom")
            custom_site = user_input.get(CONF_SITE, "").strip().lower()
            number = user_input[CONF_NUMBER]

            # Resolve site from preset or custom input
            if site_preset == "custom":
                site = custom_site
            else:
                site = site_preset

            if not site or len(site) < 2:
                errors[CONF_SITE] = "site_too_short"
            elif number < 1 or number > 10:
                errors[CONF_NUMBER] = "number_out_of_range"
            else:
                self._site = site
                self._number = number
                # LOCAL_ESPHOME mode is shared by local_esphome (YAML export) and
                # adopt_direct (register running device). Only route to YAML export
                # when _local_yaml_mode is True (set by local_esphome, not adopt_direct).
                if getattr(self, "_local_yaml_mode", False):
                    return await self.async_step_local_yaml_ready()
                return await self.async_step_target_device()

        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Required("site_preset", default="home"): vol.In(
                        LOCATION_PRESETS
                    ),
                    vol.Optional(CONF_SITE, default=""): str,
                    vol.Required(CONF_NUMBER, default=1): vol.All(
                        int, vol.Range(min=1, max=10)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_target_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 6: Bind to a physical HA device (MAC derivation).

        Scans the Device Registry for ESPHome devices with MAC connections.
        User selects the physical device to bind this config entry to.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            ha_device_id = user_input.get("ha_device_id", "")

            if not ha_device_id:
                errors["ha_device_id"] = "no_device_selected"
            else:
                # Derive MAC suffix from HA Device Registry
                dev_reg = dr.async_get(self.hass)
                device_entry = dev_reg.async_get(ha_device_id)

                if not device_entry:
                    errors["ha_device_id"] = "device_not_found"
                else:
                    mac = None
                    for conn_type, conn_id in device_entry.connections:
                        if conn_type == dr.CONNECTION_NETWORK_MAC:
                            mac = conn_id
                            break

                    if not mac:
                        errors["ha_device_id"] = "missing_mac_identifier"
                    else:
                        from .mac_utils import InvalidMACError, canonical_mac_last6

                        try:
                            self._mac_suffix = canonical_mac_last6(mac)
                            self._ha_device_id = ha_device_id
                        except InvalidMACError:
                            errors["ha_device_id"] = "missing_mac_identifier"

            if not errors:
                # Check if this physical device already has a config entry
                new_uid = f"{DOMAIN}_{self._ha_device_id}"
                await self.async_set_unique_id(new_uid)
                # Find existing entry for re-flash (instead of aborting)
                self._replacing_entry: config_entries.ConfigEntry | None = None
                for entry in self._async_current_entries():
                    if entry.unique_id == new_uid:
                        self._replacing_entry = entry
                        _LOGGER.info(
                            "Re-flash: replacing existing entry %s (%s)",
                            entry.entry_id, entry.title,
                        )
                        break

                # --- EPIC-011: Slug immutability + uniqueness (ADR-003) ---
                candidate_slug = compute_node_name(
                    self._model_slug, self._site, self._number
                )

                # R2: Immutability — re-flash must keep stored slug
                if self._replacing_entry is not None:
                    stored_slug = get_device_slug_from_entry(
                        self._replacing_entry
                    )
                    # FU-EPIC011-1: normalize for case-insensitive compare
                    if (
                        stored_slug
                        and stored_slug.lower() != candidate_slug.lower()
                    ):
                        _LOGGER.info(
                            "EPIC-011 slug mismatch blocked: "
                            "stored_slug=%s, attempted_slug=%s, "
                            "device=%s",
                            stored_slug,
                            candidate_slug,
                            self._ha_device_id,
                        )
                        errors["ha_device_id"] = "slug_immutable"

                # R3: Uniqueness — no two entries may share same slug
                if not errors:
                    for entry in self._async_current_entries():
                        if (
                            self._replacing_entry
                            and entry.entry_id
                            == self._replacing_entry.entry_id
                        ):
                            continue  # skip self
                        existing_slug = get_device_slug_from_entry(entry)
                        if (
                            existing_slug
                            and existing_slug.lower() == candidate_slug.lower()
                        ):
                            _LOGGER.info(
                                "EPIC-011 duplicate slug blocked: "
                                "slug=%s already used by entry %s",
                                candidate_slug,
                                entry.entry_id,
                            )
                            errors["ha_device_id"] = "slug_already_in_use"
                            break

                if errors:
                    # Re-show target_device form with errors
                    device_options = await self._get_esphome_devices_with_mac()
                    if not device_options:
                        return self.async_abort(reason="no_devices_found")
                    return self.async_show_form(
                        step_id="target_device",
                        data_schema=vol.Schema(
                            {
                                vol.Required("ha_device_id"): vol.In(
                                    device_options
                                ),
                            }
                        ),
                        errors=errors,
                    )

                # MAC conflict detection: check if MAC is already in metadata store
                try:
                    from .metadata import async_get_metadata_store

                    # EPIC-015 P3-03: use singleton — no stale snapshot
                    store = await async_get_metadata_store(self.hass)
                    existing = await store.get_by_mac_suffix(self._mac_suffix)
                    if existing and (
                        existing.site != self._site
                        or existing.number != self._number
                        or existing.model_slug != self._model_slug
                    ):
                        self._relocate_from = existing
                        return await self.async_step_confirm_relocate()
                except Exception:
                    _LOGGER.warning(
                        "MAC conflict check failed (non-fatal, continuing)",
                        exc_info=True,
                    )

                return await self._proceed_after_binding()

        # Build device dropdown from Device Registry (ESPHome devices with MAC)
        device_options = await self._get_esphome_devices_with_mac()

        if not device_options:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="target_device",
            data_schema=vol.Schema(
                {
                    vol.Required("ha_device_id"): vol.In(device_options),
                }
            ),
            errors=errors,
        )

    async def _get_esphome_devices_with_mac(self) -> dict[str, str]:
        """Get ESPHome devices from Device Registry that have MAC connections.

        Returns:
            Dict of {ha_device_id: "Device Name (mac_suffix)"}.
            Already-bound devices are shown with a marker suffix for re-flash.

        EPIC-004 follow-up: a device is treated as "already bound" when
        EITHER a ``pvautonomy_ops`` config entry's ``unique_id`` carries
        its ``ha_device_id`` (the modern wizard-created shape), OR the
        persisted metadata store already owns it (legacy entries whose
        metadata has empty ``mac_suffix`` / ``ha_device_id``). The
        metadata fallback uses :func:`runtime_identity.
        device_metadata_matches_esphome`, which matches by MAC suffix,
        ha_device_id, or normalized device name \u2014 all read-only.
        """
        dev_reg = dr.async_get(self.hass)
        from .mac_utils import InvalidMACError, canonical_mac_last6
        from .runtime_identity import device_metadata_matches_esphome

        # Collect ha_device_ids already bound to pvautonomy_ops entries
        # via the modern unique_id shape (``pvautonomy_ops_<ha_device_id>``).
        bound_device_ids: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            uid = entry.unique_id or ""
            if uid.startswith(f"{DOMAIN}_"):
                bound_id = uid[len(f"{DOMAIN}_"):]
                bound_device_ids.add(bound_id)

        # Legacy fallback: also consult the metadata store. Wizard entries
        # produced before EPIC-011 (and entries seeded via the name-parsing
        # fallback in ``PVAutonomyMetadataStore.resolve``) may have no
        # matching unique_id but still represent a managed device.
        managed_metadatas: list[Any] = []
        try:
            from .metadata import async_get_metadata_store

            store = await async_get_metadata_store(self.hass)
            managed_metadatas = await store.get_all()
        except Exception:  # noqa: BLE001 \u2014 best-effort, non-fatal
            _LOGGER.debug(
                "Wizard: metadata store unavailable; legacy re-flash "
                "detection will rely solely on config-entry unique_ids",
                exc_info=True,
            )

        device_options: dict[str, str] = {}
        for device_entry in dev_reg.devices.values():
            # Only ESPHome devices
            is_esphome = any(
                self.hass.config_entries.async_get_entry(eid)
                and self.hass.config_entries.async_get_entry(eid).domain == "esphome"
                for eid in device_entry.config_entries
            )
            if not is_esphome:
                continue

            # Must have a MAC connection
            mac = None
            for conn_type, conn_id in device_entry.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    mac = conn_id
                    break
            if not mac:
                continue

            try:
                suffix = canonical_mac_last6(mac)
            except InvalidMACError:
                suffix = mac[-6:]

            is_bound = device_entry.id in bound_device_ids
            if not is_bound and managed_metadatas:
                esphome_name = getattr(device_entry, "name", "") or ""
                esphome_title = (
                    getattr(device_entry, "name_by_user", "")
                    or esphome_name
                )
                for meta in managed_metadatas:
                    if device_metadata_matches_esphome(
                        meta,
                        esphome_name=esphome_name,
                        esphome_title=esphome_title,
                        mac_suffix=suffix,
                        ha_device_id=device_entry.id,
                    ):
                        is_bound = True
                        break

            label = f"{device_entry.name or device_entry.id} ({suffix})"
            if is_bound:
                label += " \u2014 re-flash"
            device_options[device_entry.id] = label

        return device_options

    async def async_step_confirm_relocate(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """MAC conflict: device already known under different name. Relocate?"""
        if user_input is not None:
            action = user_input.get("action", "cancel")
            if action == "relocate":
                # Archive old ESPHome YAML (best effort)
                old_fn = getattr(self._relocate_from, "esphome_yaml_filename", "")
                if old_fn:
                    try:
                        from .esphome_sync import async_archive_esphome_yaml

                        await async_archive_esphome_yaml(
                            self.hass, filename=old_fn, reason="relocate_via_wizard"
                        )
                    except Exception:
                        _LOGGER.warning(
                            "Failed to archive old YAML %s (non-fatal)", old_fn,
                            exc_info=True,
                        )
                # Update metadata store with new location
                try:
                    from .metadata import async_get_metadata_store

                    # EPIC-015 P3-03: use singleton — write visible to runtime immediately
                    store = await async_get_metadata_store(self.hass)
                    await store.update_location(
                        self._relocate_from.device_id, self._site, self._number
                    )
                except Exception:
                    _LOGGER.warning(
                        "Failed to update metadata for relocate (non-fatal)",
                        exc_info=True,
                    )
                self._relocate_from = None
                return await self._proceed_after_binding()
            # Cancel
            self._relocate_from = None
            return self.async_abort(reason="device_already_bound")

        old_label = (
            f"{self._relocate_from.model_slug} "
            f"{self._relocate_from.site} "
            f"{str(self._relocate_from.number).zfill(2)}"
        )
        new_label = (
            f"{self._model_slug} {self._site} {str(self._number).zfill(2)}"
        )
        return self.async_show_form(
            step_id="confirm_relocate",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): vol.In(
                        {"relocate": "Relocate", "cancel": "Cancel"}
                    ),
                }
            ),
            description_placeholders={
                "old_label": old_label,
                "new_label": new_label,
            },
        )

    def _run_modbus_map_check(self) -> None:
        """Read HR73 + plausibility anchors from HA states (EPIC-010 vNext).

        Sets self._modbus_version and self._map_status.

        TASK-20260327: Three outcomes instead of the previous binary:
          "unknown"   — no evidence (HR73 missing, no anchors available).
                        Non-standard tiers are allowed (customer choice preserved).
          "confirmed" — HR73 readable AND anchors pass plausibility.
                        Non-standard tiers are allowed.
          "failed"    — contradictory evidence (HR73 unparseable, anchors out of range).
                        Non-standard tiers are blocked (fail-closed).
        """
        from .device_id import compute_node_name

        node_name = compute_node_name(self._model_slug, self._site, self._number)
        prefix = node_name.replace("-", "_")

        # Step 1: Read HR73 (sensor.{prefix}_modbus_version_device)
        hr73_state = self.hass.states.get(f"sensor.{prefix}_modbus_version_device")
        if hr73_state is None or hr73_state.state in ("unavailable", "unknown", ""):
            _LOGGER.info(
                "Map check: HR73 not available for %s (fresh install or factory fw). "
                "Status: unknown (tier choice preserved).",
                node_name,
            )
            self._modbus_version = None
            self._map_status = "unknown"
            return

        try:
            self._modbus_version = int(float(hr73_state.state))
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Map check: HR73 not parseable: '%s'. Status: failed (contradictory).",
                hr73_state.state,
            )
            self._modbus_version = None
            self._map_status = "failed"
            return

        # Step 2: Plausibility anchors (always-on, no PV-dependent checks).
        # TASK-20260327-ANCHOR-FIX: Removed ac_voltage_l1 — its 100..280V range
        # is wrong for 3-phase SPH devices where line-to-line voltage is ~400V.
        # Keeping only robust, topology-independent anchors.
        anchors = [
            (f"sensor.{prefix}_ac_frequency_device", 45.0, 55.0),
            (f"sensor.{prefix}_battery_soc_device", 0.0, 100.0),
        ]
        passed = 0
        checked = 0
        for entity_id, lo, hi in anchors:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown", ""):
                continue  # skip missing — don't penalise offline sensors
            checked += 1
            with contextlib.suppress(ValueError, TypeError):
                if lo <= float(state.state) <= hi:
                    passed += 1

        if checked == 0:
            _LOGGER.info(
                "Map check: No anchor entities available for %s. "
                "Status: unknown (tier choice preserved).",
                node_name,
            )
            self._map_status = "unknown"
            return

        if passed < checked:
            _LOGGER.warning(
                "Map check: Anchor FAIL for %s: %d/%d passed. "
                "Status: failed (contradictory evidence).",
                node_name, passed, checked,
            )
            self._map_status = "failed"
            return

        candidate = (
            "SPH_MAP_V020" if self._modbus_version == 20
            else "SPH_MAP_V124" if self._modbus_version == 124
            else f"UNKNOWN(HR73={self._modbus_version})"
        )
        _LOGGER.info(
            "Map check: PASS for %s (HR73=%d, candidate=%s, anchors %d/%d).",
            node_name, self._modbus_version, candidate, passed, checked,
        )
        self._map_status = "confirmed"

    async def async_step_tier_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Tier selection step: filtered by registry capability.

        EPIC-010 Tiering v1.1. Standard is the safe default.
        Unsafe remains an internal/legacy tier and is not offered in the
        customer-facing wizard.
        EPIC-010 vNext: reads HR73 + anchors to set map_confirmed.

        TASK-20260318-WIZARD-TIER-FILTERING:
        Only tiers that the selected registry actually supports are shown.
        If only standard is supported, the step is skipped automatically.
        """
        # get_supported_tiers() reads the registry JSON from disk (sync
        # open + json.load) — run it in the executor so we never block the
        # event loop. See dashboard_builder.load_registry().
        supported = await self.hass.async_add_executor_job(
            get_supported_tiers, self._registry_file
        )

        if user_input is not None:
            tier = user_input.get(CONF_SELECTED_TIER, TIER_STANDARD)
            # Fail-closed: reject tier not in supported set
            if tier not in supported:
                tier = TIER_STANDARD
            self._selected_tier = tier
            # Run map verification on submission (reads HA states — fast, synchronous)
            self._run_modbus_map_check()

            # TASK-20260327: Fail-closed when non-standard tier + contradictory
            # map evidence.  "unknown" (no evidence) preserves the customer's
            # tier choice; only "failed" (contradictory anchors / unparseable
            # HR73) blocks non-standard tiers.
            if tier != TIER_STANDARD and self._map_status == "failed":
                return await self.async_step_tier_map_blocked()

            if tier == TIER_UNSAFE:
                return await self.async_step_unsafe_consent()
            return await self.async_step_summary()

        # Auto-skip: if only standard is supported, no choice needed
        if supported == [TIER_STANDARD]:
            _LOGGER.info(
                "Registry %s supports only standard tier — skipping selection",
                self._registry_file,
            )
            self._selected_tier = TIER_STANDARD
            self._run_modbus_map_check()
            return await self.async_step_summary()

        # Build filtered option list from the customer-facing tier set.
        options = [
            SelectOptionDict(value=t, label=t)
            for t in supported
        ]

        return self.async_show_form(
            step_id="tier_selection",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SELECTED_TIER, default=TIER_STANDARD
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            translation_key="tier_option",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_tier_map_blocked(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fail-closed blocker: non-standard tier requested but map not confirmed.

        TASK-20260327-SPH10K-MAP-CONFIRMED-FAIL-CLOSED.
        The user selected extended/unsafe but the register map plausibility
        check did not pass.  Instead of silently downgrading, we show an
        explicit blocker and let the user choose:
        - continue_standard: proceed with standard tier (safe)
        - cancel: abort the setup flow
        """
        if user_input is not None:
            action = user_input.get("action", "cancel")
            if action == "continue_standard":
                self._selected_tier = TIER_STANDARD
                return await self.async_step_summary()
            # Any other action (including "cancel") → abort
            return self.async_abort(reason="setup_cancelled")

        return self.async_show_form(
            step_id="tier_map_blocked",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value="continue_standard",
                                    label="continue_standard",
                                ),
                                SelectOptionDict(
                                    value="cancel",
                                    label="cancel",
                                ),
                            ],
                            translation_key="tier_map_blocked_action",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={
                "requested_tier": _tier_display_label(self.hass, self._selected_tier),
            },
        )

    async def async_step_unsafe_consent(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Unsafe consent gate: checkbox + confirmation phrase required.

        EPIC-010 Tiering v1.1.
        Without explicit consent, the build is blocked (fails closed).
        Required phrase: 'I UNDERSTAND' (case-sensitive).
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            confirmed = user_input.get("consent_checkbox", False)
            phrase = user_input.get("consent_phrase", "").strip()

            if not confirmed:
                errors["consent_checkbox"] = "unsafe_consent_required"
            elif phrase != UNSAFE_CONSENT_PHRASE:
                errors["consent_phrase"] = "unsafe_consent_phrase_mismatch"
            else:
                # Both gates passed → proceed to summary
                return await self.async_step_summary()

        return self.async_show_form(
            step_id="unsafe_consent",
            data_schema=vol.Schema(
                {
                    vol.Required("consent_checkbox", default=False): bool,
                    vol.Required("consent_phrase", default=""): str,
                }
            ),
            description_placeholders={
                "consent_phrase": UNSAFE_CONSENT_PHRASE,
            },
            errors=errors,
        )

    async def async_step_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 7: Summary + confirm → start in-wizard build."""
        device_id = compute_device_id(self._model_slug, self._site, self._number)
        display_name = MODEL_REGISTRY_MAP[self._model_slug]["display_name"]
        summary = f"{display_name} — {self._site.title()} {str(self._number).zfill(2)}"

        if user_input is not None:
            self._device_id = device_id
            self._summary_title = f"PVAutonomy ({summary})"
            self._display_name = display_name
            return await self.async_step_progress_build()

        return self.async_show_form(
            step_id="summary",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": summary,
                "device_id": device_id,
                "registry_file": self._registry_file,
                "manufacturer": self._manufacturer.title(),
                "model": display_name,
                "mac_suffix": self._mac_suffix or "(not bound)",
                # [issue #57 follow-up] Show the chosen feature level
                # (Funktionsumfang) on the pre-build confirm screen, not just on
                # the completion screen — the operator confirms the tier BEFORE
                # the build starts.
                "selected_tier": _tier_display_label(self.hass, self._selected_tier),
            },
        )

    async def _validate_entity_surface(self) -> list[str] | None:
        """Check the adopted device exposes required PVAutonomy entity names.

        Uses the HA entity registry (works when the device is offline).
        Matches by ``RegistryEntry.original_name`` — the name set in the
        generated ESPHome YAML (e.g. ``battery_soc_device``), not the
        HA-slugified ``entity_id``.

        Returns:
            ``list[str]`` (non-empty) — missing required entity names → block.
            ``list[str]`` (empty) — surface complete → allow.
            ``None`` — validation could not be performed (broken installation,
            registry unavailable) → block with ``entity_surface_validation_failed``.
        """
        from homeassistant.helpers import entity_registry as er

        from .yaml_generator import derive_required_entity_names

        if not self._ha_device_id:
            _LOGGER.debug("Surface validation: no ha_device_id — skipping")
            return []

        try:
            required_names = derive_required_entity_names(
                self._registry_file,
                selected_tier=self._selected_tier,
            )
        except Exception:
            _LOGGER.error(
                "Surface validation: unexpected error deriving required entity names "
                "from registry %s — blocking adoption (fail-closed)",
                self._registry_file,
                exc_info=True,
            )
            return None  # fail-closed: unexpected error → block

        if required_names is None:
            # derive_required_entity_names signals registry load failure.
            # For known models the bundled registry must always be present.
            _LOGGER.error(
                "Surface validation: cannot load registry %s — "
                "blocking adoption (fail-closed, bundled defs unavailable)",
                self._registry_file,
            )
            return None  # fail-closed: registry unavailable → block

        if not required_names:
            # Registry loaded but has no standard-tier required entities.
            # This is technically valid for future models — skip validation.
            _LOGGER.debug(
                "Surface validation: registry %s yielded no required entities "
                "— skipping (empty contract, not a load failure)",
                self._registry_file,
            )
            return []

        ent_reg = er.async_get(self.hass)
        device_entities = er.async_entries_for_device(ent_reg, self._ha_device_id)

        actual_names: set[str] = set()
        for ent in device_entities:
            if getattr(ent, "disabled_by", None) is not None:
                continue
            original = getattr(ent, "original_name", None)
            if original:
                actual_names.add(original)

        missing = [name for name in required_names if name not in actual_names]
        if missing:
            _LOGGER.info(
                "Surface validation: device %s missing %d required entities: %s",
                self._ha_device_id,
                len(missing),
                missing[:5],
            )
        else:
            _LOGGER.debug(
                "Surface validation: device %s entity surface complete (%d checked)",
                self._ha_device_id,
                len(required_names),
            )
        return missing

    async def async_step_adopt_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Adopt step: register an already-running device — no build/flash.

        Creates a device-bound ConfigEntry whose options encode ownership
        (``selected_device`` + ``_initial_device`` with ``ha_device_id`` /
        ``mac_suffix``) and a build-skipping ``_setup_state = "adopted"``.
        ``async_setup_entry`` seeds the metadata store from ``_initial_device``
        and skips the post-setup background build, so adoption never triggers
        a build, install, or reflash.
        """
        device_id = compute_device_id(self._model_slug, self._site, self._number)
        device_slug = compute_node_name(
            self._model_slug, self._site, self._number
        )
        display_name = MODEL_REGISTRY_MAP[self._model_slug]["display_name"]
        summary = f"{display_name} — {self._site.title()} {str(self._number).zfill(2)}"

        # The physical device is already owned (target_device found a matching
        # entry) → adoption would duplicate it. Abort cleanly instead.
        if self._replacing_entry is not None:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            self._device_id = device_id
            self._summary_title = f"PVAutonomy ({summary})"
            self._display_name = display_name

            # #134: Entity-surface validator — inspect the selected ESPHome
            # device's entity registry before accepting it.  Block adoption
            # when required PVAutonomy entities are absent so that a renamed/
            # incomplete firmware surface fails early with actionable guidance.
            # None = validation could not be performed (broken installation).
            missing = await self._validate_entity_surface()

            if missing is None:
                # Registry/contract unavailable: block adoption so a broken
                # installation cannot silently produce an unvalidated entry.
                return self.async_show_form(
                    step_id="adopt_confirm",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": summary,
                        "device_id": device_id,
                        "registry_file": self._registry_file,
                        "manufacturer": self._manufacturer.title(),
                        "model": display_name,
                        "mac_suffix": self._mac_suffix or "(not bound)",
                        "missing_entities": "",
                    },
                    errors={"base": "entity_surface_validation_failed"},
                )

            if missing:
                truncated = missing[:5]
                suffix = f" … (+{len(missing) - 5} more)" if len(missing) > 5 else ""
                missing_str = ", ".join(truncated) + suffix
                return self.async_show_form(
                    step_id="adopt_confirm",
                    data_schema=vol.Schema({}),
                    description_placeholders={
                        "device_name": summary,
                        "device_id": device_id,
                        "registry_file": self._registry_file,
                        "manufacturer": self._manufacturer.title(),
                        "model": display_name,
                        "mac_suffix": self._mac_suffix or "(not bound)",
                        "missing_entities": missing_str,
                    },
                    errors={"base": "missing_required_entities"},
                )

            # UI-001 / #170 F1: adopted (running) devices receive their
            # customer dashboard at commissioning time — same best-effort
            # background task as the build path below. The build path gates
            # on _setup_state == "complete", which an adoption never reaches
            # ("adopted"), so without this the dashboard only exists after a
            # manual Maintenance "Refresh device dashboard" and the Help
            # navigation targets a not-yet-existing dashboard.
            if self._registry_file:
                self.hass.async_create_task(
                    self._create_customer_dashboard(
                        device_slug.replace("-", "_")
                    )
                )

            return self.async_create_entry(
                title=self._summary_title,
                data={},
                options={
                    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                    CONF_PROXY_BASE_URL: self._proxy_base_url,
                    CONF_PROXY_API_KEY: self._proxy_api_key,
                    CONF_PROXY_CUSTOMER_ID: self._proxy_customer_id,
                    CONF_BUILD_BACKEND: (
                        BUILD_BACKEND_MANUAL
                        if self._build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME
                        else BUILD_BACKEND_PROXY_REMOTE
                    ),
                    CONF_BUILD_SERVICE_MODE: self._build_service_mode,
                    CONF_ARTIFACT_CHANNEL: DEFAULT_ARTIFACT_CHANNEL,
                    CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT: DEFAULT_PROXY_AUTO_REFRESH,
                    CONF_OTA_RETRIES: DEFAULT_OTA_RETRIES,
                    CONF_OTA_RETRY_DELAYS: DEFAULT_OTA_RETRY_DELAYS,
                    CONF_CACHE_KEEP_BUILDS: DEFAULT_CACHE_KEEP_BUILDS,
                    CONF_STRICT_GATES: False,
                    CONF_SELECTED_TIER: self._selected_tier,
                    CONF_MODBUS_VERSION: self._modbus_version,
                    CONF_MAP_CONFIRMED: self._map_confirmed,
                    # Ownership signal 1: bind this entry to the device now,
                    # before async_setup_entry promotes ha_device_id.
                    CONF_SELECTED_DEVICE: device_id,
                    "_initial_device": {
                        "device_id": device_id,
                        CONF_DEVICE_SLUG: device_slug,
                        "ha_device_id": self._ha_device_id,
                        "mac_suffix": self._mac_suffix,
                        "manufacturer": self._manufacturer,
                        "model_slug": self._model_slug,
                        "site": self._site,
                        "number": self._number,
                        "registry_file": self._registry_file,
                        "esphome_yaml_filename": "",
                        "_setup_state": SETUP_STATE_ADOPTED,
                        "_firmware_size_kb": 0,
                        "modbus_version": self._modbus_version,
                        "map_confirmed": self._map_confirmed,
                    },
                },
            )

        return self.async_show_form(
            step_id="adopt_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": summary,
                "device_id": device_id,
                "registry_file": self._registry_file,
                "manufacturer": self._manufacturer.title(),
                "model": display_name,
                "mac_suffix": self._mac_suffix or "(not bound)",
                "missing_entities": "",
            },
        )

    # ------------------------------------------------------------------
    # In-wizard build+flash progress (async_show_progress pattern)
    # ------------------------------------------------------------------

    async def async_step_progress_build(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show spinner while firmware is being built."""
        if self._build_task is None:
            # fix/#113/#116/#128: preflight — verify COMPILE_SECRET_KEY before build.
            # Managed mode uses its own abort reason; other modes keep the original.
            if not await self._preflight_compile_secret_key():
                if self._build_service_mode == BUILD_SERVICE_MANAGED:
                    return self.async_abort(reason="managed_build_not_configured")
                return self.async_abort(reason="compile_secret_missing_or_invalid")
            self._build_task = self.hass.async_create_task(
                self._do_build_firmware()
            )

        task: asyncio.Task = self._build_task  # type: ignore[assignment]
        if not task.done():
            return self.async_show_progress(
                step_id="progress_build",
                progress_action="building_firmware",
                progress_task=task,
                description_placeholders={
                    "device_name": self._display_name,
                },
            )

        # Task finished — check result
        try:
            task.result()
        except asyncio.CancelledError:
            self._build_task = None
            self._build_result = None
            return self.async_abort(reason="setup_cancelled")
        except Exception as exc:
            _LOGGER.exception(
                "Wizard build failed (step=progress_build, model=%s, "
                "registry=%s, mac=%s, proxy=%s)",
                self._model_slug,
                self._registry_file,
                self._mac_suffix,
                self._proxy_base_url.split("/")[2] if self._proxy_base_url and "/" in self._proxy_base_url else "(none)",
            )
            # Map raw exceptions to user-friendly messages (DE/EN)
            raw = str(exc)
            # P2-c (ADR-0001): firmware definitions ship WITH the integration —
            # never instruct the customer to provision/check a /config file.
            # Definition-related failures point to update/reinstall instead.
            defs_hint = (
                "The firmware definitions shipped with the PVAutonomy "
                "integration appear to be incomplete or invalid. Update or "
                "reinstall the integration (HACS or installer add-on) and try "
                "again. No manual file under /config is required.\n"
                "Die mit der PVAutonomy-Integration gelieferten "
                "Firmware-Definitionen sind unvollständig oder ungültig. "
                "Aktualisieren oder installieren Sie die Integration (HACS oder "
                "Installer-Add-on) neu. Eine manuelle Datei unter /config ist "
                "nicht erforderlich."
            )
            if "NoneType" in raw and ("subscriptable" in raw or "attribute" in raw):
                self._flash_error = defs_hint
            elif "YAML" in raw and "parse" in raw.lower():
                self._flash_error = defs_hint
            elif "Build already in progress" in raw:
                self._flash_error = (
                    "A firmware build is already running for this device. "
                    "Please wait about 15 minutes and try again once. "
                    "Do not start the process multiple times. If this message "
                    "continues, contact support.\n"
                    "Für dieses Gerät läuft bereits eine Firmware-Erstellung. "
                    "Bitte warten Sie etwa 15 Minuten und versuchen Sie es "
                    "dann einmal erneut. Starten Sie den Vorgang nicht "
                    "mehrfach neu. Wenn die Meldung weiter erscheint, "
                    "kontaktieren Sie den Support."
                )
            elif (
                "production base not found" in raw.lower()
                or "registry file not found" in raw.lower()
                or "firmware-definition" in raw.lower()
            ):
                # A missing/unresolved bundled definition — never surface the
                # raw /config path (incl. the resolver's DefsNotFoundError which
                # lists legacy /config locations); point to update/reinstall.
                self._flash_error = defs_hint
            elif "not found" in raw.lower():
                self._flash_error = raw
            elif "Invalid mac_suffix" in raw:
                self._flash_error = (
                    f"MAC address error: {raw}\n"
                    "The device MAC suffix is invalid. Re-discover the device."
                )
            else:
                self._flash_error = f"Build failed: {raw}"
            self._build_task = None
            return self.async_show_progress_done(
                next_step_id="error_build_failed"
            )

        if not self._build_result or not self._build_result.success:
            self._flash_error = (
                self._build_result.error if self._build_result else "Unknown build error"
            )
            self._build_task = None
            return self.async_show_progress_done(
                next_step_id="error_build_failed"
            )

        # Build succeeded → proceed to flash
        self._build_task = None
        return self.async_show_progress_done(next_step_id="progress_flash")

    async def _do_build_firmware(self) -> None:
        """Background task: run build pipeline without a config entry."""
        from .pipeline import run_build_pipeline

        self._build_result = await run_build_pipeline(
            self.hass,
            model=self._model_slug,
            site=self._site,
            number=self._number,
            registry_file=self._registry_file,
            mac_suffix=self._mac_suffix or None,
            channel=DEFAULT_ARTIFACT_CHANNEL,
            build_backend=BUILD_BACKEND_PROXY_REMOTE,
            selected_tier=self._selected_tier,
            modbus_version=self._modbus_version,
            map_confirmed=self._map_confirmed,
            proxy_config={
                CONF_PROXY_BASE_URL: self._proxy_base_url,
                CONF_PROXY_API_KEY: self._proxy_api_key,
                CONF_PROXY_CUSTOMER_ID: self._proxy_customer_id,
            },
        )

    async def _get_compile_secret_key(self) -> str | None:
        """Load the global keyring and return the compile secret key if valid.

        Returns the raw 64-hex string when provisioned and format-valid, or
        None when absent, invalid, or the keyring is unavailable.  Isolated as
        a method so tests can mock it without patching sys.modules.
        """
        import re as _re

        _valid_key_re = _re.compile(r"^[0-9a-fA-F]{64}$")
        try:
            from .keyring import PVAutonomyKeyring

            keyring = PVAutonomyKeyring(self.hass)
            await keyring.async_load()
            key = await keyring.get_compile_secret_key()
        except Exception:
            return None
        return key if (key and _valid_key_re.match(key)) else None

    async def _preflight_compile_secret_key(self) -> bool:
        """Return True if the build can start: envelope mode active, or a
        valid COMPILE_SECRET_KEY provisioned in the keyring.

        G7 (#122): envelope-mode builds seal per-device compile secrets into
        the HPKE envelope and need no repo-wide COMPILE_SECRET_KEY. The wizard
        path is entry-free, so resolve exactly like the pipeline's wizard
        branch: pinned roots plus the CONF_ENVELOPE_MODE_ENABLED entry-scan
        (an explicit False on any entry force-disables). If an envelope build
        later degrades to the legacy wire path (keyset 404/405),
        build_backend.start_build() still fails closed before any plaintext
        leaves HA.

        Legacy path delegates validation to _get_compile_secret_key (mockable
        seam). Does not log or expose the key value.
        """
        from .pipeline import _envelope_mode_enabled
        from .secret_envelope import ROOT_PUBKEYS_PINNED

        if ROOT_PUBKEYS_PINNED and _envelope_mode_enabled(self.hass, {}):
            return True
        key = await self._get_compile_secret_key()
        if not key:
            _LOGGER.warning(
                "Wizard preflight: COMPILE_SECRET_KEY missing or invalid — "
                "aborting build before pipeline start"
            )
            return False
        return True

    async def async_step_progress_flash(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show spinner while firmware is being flashed via OTA."""
        if self._flash_task is None:
            # Resolve device IP + OTA password before starting flash
            from .flash_uploader import resolve_device_ip, get_ota_password

            device_ip, _, _ = resolve_device_ip(
                self.hass, self._device_id, ha_device_id=self._ha_device_id
            )
            if not device_ip:
                return self.async_show_progress_done(
                    next_step_id="setup_complete_partial"
                )
            self._device_ip = device_ip

            self._ota_pw_result = await self.hass.async_add_executor_job(
                get_ota_password, self.hass, self._mac_suffix
            )
            if self._ota_pw_result:
                self._ota_password = self._ota_pw_result.password
            else:
                self._ota_password = None
                self._flash_error = (
                    f"OTA password not found for MAC suffix {self._mac_suffix}. "
                    "Check esphome/secrets.yaml."
                )
                return self.async_show_progress_done(
                    next_step_id="error_flash_failed"
                )

            self._flash_task = self.hass.async_create_task(
                self._do_flash_firmware()
            )

        task: asyncio.Task = self._flash_task  # type: ignore[assignment]
        if not task.done():
            return self.async_show_progress(
                step_id="progress_flash",
                progress_action="flashing_device",
                progress_task=task,
                description_placeholders={
                    "device_name": self._display_name,
                    "device_ip": self._device_ip or "",
                },
            )

        # Task finished — check result
        try:
            task.result()
        except asyncio.CancelledError:
            self._flash_task = None
            return self.async_abort(reason="setup_cancelled")
        except Exception as exc:
            _LOGGER.exception(
                "Wizard flash failed (step=progress_flash, device=%s, "
                "ip=%s, build_id=%s, mac=%s)",
                self._device_id,
                self._device_ip,
                self._build_result.build_job_id if self._build_result else None,
                self._mac_suffix,
            )
            raw_err = str(exc)
            pw = getattr(self, "_ota_pw_result", None)
            if "Authentication invalid" in raw_err and pw:
                _LOGGER.warning(
                    "OTA_AUTH_INVALID: key=%s, source=%s, device=%s",
                    pw.key_name, pw.source_file, self._device_id,
                )
                mac = self._mac_suffix or "unknown"
                per_device_key = f"edge101_ota_password_{mac}"
                also_checked = (
                    f"Also checked: '{per_device_key}' (not found)."
                    if pw.scope == "site-wide" and mac != "unknown"
                    else ""
                )
                self._flash_error = (
                    f"OTA password mismatch for device {mac}.\n"
                    f"Used key: '{pw.key_name}' from "
                    f"{pw.source_file.rsplit('/', 1)[-1]} ({pw.scope}).\n"
                    f"{also_checked}\n"
                    "Verify the password matches the device firmware."
                ).strip()
            else:
                self._flash_error = f"Flash failed: {exc}"
            self._flash_task = None
            return self.async_show_progress_done(
                next_step_id="error_flash_failed"
            )

        # Flash succeeded — reload ESPHome entry so it reconnects with
        # the new firmware (prevents stale backoff from pre-flash failures).
        self._flash_task = None
        if self._mac_suffix:
            from .keyring import schedule_post_flash_reload
            self.hass.async_create_task(
                schedule_post_flash_reload(self.hass, self._mac_suffix)
            )
        if self._build_result and self._build_result.cache_hit:
            return self.async_show_progress_done(
                next_step_id="setup_complete_cached"
            )
        return self.async_show_progress_done(next_step_id="setup_complete")

    async def _do_flash_firmware(self) -> None:
        """Background task: OTA upload firmware to device."""
        from .flash_uploader import OTA_DEFAULT_PORT, ota_upload_with_retry

        assert self._device_ip is not None  # resolved before task creation
        assert self._build_result is not None and self._build_result.artifact_path is not None

        await ota_upload_with_retry(
            self.hass,
            host=self._device_ip,
            port=OTA_DEFAULT_PORT,
            password=self._ota_password,
            firmware_path=self._build_result.artifact_path,
            timeout_s=120.0,
            retries=3,
            delays=(0, 10, 30),
        )

    # ------------------------------------------------------------------
    # Success / partial / error screens
    # ------------------------------------------------------------------

    def _create_entry_with_state(self, setup_state: str) -> FlowResult:
        """Create the config entry with _setup_state flag.

        unique_id is already set in async_step_target_device.
        If re-flashing, removes the old entry first (clean replacement).
        Also schedules ESPHome YAML sync and customer dashboard creation
        (both fire-and-forget, best-effort).
        """
        # Re-flash: remove old config entry before creating replacement
        if self._replacing_entry is not None:
            _LOGGER.info(
                "Re-flash: removing old entry %s (%s) before creating replacement",
                self._replacing_entry.entry_id, self._replacing_entry.title,
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_remove(
                    self._replacing_entry.entry_id
                )
            )
        fw_size_kb = 0
        if self._build_result and self._build_result.firmware_size:
            fw_size_kb = self._build_result.firmware_size // 1024

        # ESPHome Builder sync: compute filename and schedule write
        esphome_yaml_filename = ""
        if self._build_result and self._build_result.generated_yaml:
            from .device_id import compute_esphome_device_yaml_name

            esphome_yaml_filename = compute_esphome_device_yaml_name(
                self._model_slug, self._site, self._number
            )
            self.hass.async_create_task(
                self._sync_esphome_yaml(
                    esphome_yaml_filename,
                    self._build_result.generated_yaml,
                )
            )

        # UI-001: Schedule customer dashboard creation (best-effort)
        if setup_state == "complete" and self._registry_file:
            device_slug = compute_node_name(
                self._model_slug, self._site, self._number
            ).replace("-", "_")
            self.hass.async_create_task(
                self._create_customer_dashboard(device_slug)
            )

        # EPIC-015 P1-02: Carry forward strict-gate intent from replaced entry.
        # New entries default to False; replacement entries preserve prior setting.
        prior_strict_gates = False
        if self._replacing_entry is not None:
            prior_strict_gates = self._replacing_entry.options.get(
                CONF_STRICT_GATES, False
            )

        return self.async_create_entry(
            title=self._summary_title,
            data={},
            options={
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                CONF_PROXY_BASE_URL: self._proxy_base_url,
                CONF_PROXY_API_KEY: self._proxy_api_key,
                CONF_PROXY_CUSTOMER_ID: self._proxy_customer_id,
                CONF_BUILD_BACKEND: BUILD_BACKEND_PROXY_REMOTE,
                CONF_BUILD_SERVICE_MODE: self._build_service_mode,
                CONF_ARTIFACT_CHANNEL: DEFAULT_ARTIFACT_CHANNEL,
                CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT: DEFAULT_PROXY_AUTO_REFRESH,
                CONF_OTA_RETRIES: DEFAULT_OTA_RETRIES,
                CONF_OTA_RETRY_DELAYS: DEFAULT_OTA_RETRY_DELAYS,
                CONF_CACHE_KEEP_BUILDS: DEFAULT_CACHE_KEEP_BUILDS,
                # EPIC-015 P1-02: Build intent persisted at root level
                CONF_STRICT_GATES: prior_strict_gates,
                CONF_SELECTED_TIER: self._selected_tier,
                CONF_MODBUS_VERSION: self._modbus_version,
                CONF_MAP_CONFIRMED: self._map_confirmed,
                "_initial_device": {
                    "device_id": self._device_id,
                    CONF_DEVICE_SLUG: compute_node_name(
                        self._model_slug, self._site, self._number
                    ),
                    "ha_device_id": self._ha_device_id,
                    "mac_suffix": self._mac_suffix,
                    "manufacturer": self._manufacturer,
                    "model_slug": self._model_slug,
                    "site": self._site,
                    "number": self._number,
                    "registry_file": self._registry_file,
                    "esphome_yaml_filename": esphome_yaml_filename,
                    "_setup_state": setup_state,
                    "_firmware_size_kb": fw_size_kb,
                    "modbus_version": self._modbus_version,
                    "map_confirmed": self._map_confirmed,
                },
            },
        )

    async def _create_customer_dashboard(self, device_name: str) -> None:
        """Create customer dashboard after successful setup (best effort).

        EPIC-009 UI-001: Idempotent, fail-safe. Never blocks entry creation.
        """
        try:
            from .dashboard_builder import async_create_dashboard

            created = await async_create_dashboard(
                self.hass,
                device_name=device_name,
                display_title=self._display_name,
                registry_file=self._registry_file,
            )
            if created:
                _LOGGER.info("Customer dashboard created for %s", device_name)
            else:
                _LOGGER.debug(
                    "Customer dashboard skipped for %s (already exists or not ready)",
                    device_name,
                )
        except Exception:
            _LOGGER.warning(
                "Customer dashboard creation failed for %s (non-fatal)",
                device_name,
                exc_info=True,
            )

    async def _sync_esphome_yaml(self, filename: str, yaml_text: str) -> None:
        """Write generated YAML to ESPHome config dir (best effort)."""
        try:
            from .esphome_sync import async_write_esphome_yaml

            path = await async_write_esphome_yaml(
                self.hass, filename=filename, yaml_text=yaml_text
            )
            _LOGGER.info("ESPHome YAML synced: %s", path)
        except Exception:
            _LOGGER.warning(
                "ESPHome YAML sync failed for %s (non-fatal, device setup continues)",
                filename,
                exc_info=True,
            )

    def _build_id_short(self) -> str:
        """Return first 8 chars of build_job_id for display, or fallback."""
        if self._build_result and self._build_result.build_job_id:
            return self._build_result.build_job_id[:8]
        return "\u2014"

    def _firmware_size_display(self) -> str:
        """Return firmware size in KB as string, or fallback."""
        if self._build_result and self._build_result.firmware_size:
            return str(self._build_result.firmware_size // 1024)
        return "\u2014"

    async def async_step_setup_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Success screen: build + flash completed."""
        if user_input is not None:
            return self._create_entry_with_state("complete")

        return self.async_show_form(
            step_id="setup_complete",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._display_name,
                "firmware_size": self._firmware_size_display(),
                "build_id_short": self._build_id_short(),
                "selected_tier": _tier_display_label(self.hass, self._selected_tier),
            },
        )

    async def async_step_setup_complete_cached(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Success screen: build served from proxy cache + flash completed."""
        if user_input is not None:
            return self._create_entry_with_state("complete")

        return self.async_show_form(
            step_id="setup_complete_cached",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._display_name,
                "firmware_size": self._firmware_size_display(),
                "build_id_short": self._build_id_short(),
                "selected_tier": _tier_display_label(self.hass, self._selected_tier),
            },
        )

    async def async_step_setup_complete_partial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Partial success: firmware built but device offline."""
        if user_input is not None:
            return self._create_entry_with_state("partial")

        return self.async_show_form(
            step_id="setup_complete_partial",
            data_schema=vol.Schema({}),
            description_placeholders={
                "device_name": self._display_name,
            },
        )

    async def async_step_error_build_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Error screen: build failed with retry option."""
        if user_input is not None:
            action = user_input.get("action", "cancel")
            if action == "retry":
                self._build_task = None
                self._build_result = None
                self._flash_error = None
                return await self.async_step_progress_build()
            return self.async_abort(reason="setup_cancelled")

        return self.async_show_form(
            step_id="error_build_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): vol.In(
                        {"retry": "Retry", "cancel": "Cancel"}
                    ),
                }
            ),
            description_placeholders={
                "error_message": self._flash_error or "Unknown error",
            },
        )

    async def async_step_error_flash_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Error screen: flash failed with retry/skip/cancel options."""
        if user_input is not None:
            action = user_input.get("action", "cancel")
            if action == "retry":
                self._flash_task = None
                self._flash_error = None
                self._device_ip = None
                self._ota_password = None
                return await self.async_step_progress_flash()
            if action == "skip":
                return self._create_entry_with_state("partial")
            return self.async_abort(reason="setup_cancelled")

        return self.async_show_form(
            step_id="error_flash_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): vol.In(
                        {"retry": "Retry", "skip": "Skip flash", "cancel": "Cancel"}
                    ),
                }
            ),
            description_placeholders={
                "error_message": self._flash_error or "Unknown error",
            },
        )

    async def async_step_import(
        self, import_config: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle YAML import (one-time migration, backward compat).

        Creates a basic config entry without device metadata.
        """
        await self.async_set_unique_id(f"{DOMAIN}_yaml_import")
        self._abort_if_unique_id_configured()

        _LOGGER.info(
            "Importing PVAutonomy Ops from YAML configuration (one-time migration)"
        )

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={},
            options={
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            },
        )

    async def async_step_installation_anchor(
        self, discovery_info: Any | None = None
    ) -> FlowResult:
        """Create the installation anchor (M3A / #169 WP1).

        Ops Contract v1.1.2 §9.4.2 / OCD-6. Internal source only (dispatched
        from installation_anchor.async_ensure_installation_anchor); not
        customer-routable. Single-instance is enforced by the fixed unique_id
        plus _abort_if_unique_id_configured — the same precedent as the
        yaml_import singleton above. Creates a non-device entry with an
        immutable entry_kind discriminator and NO device metadata; it owns no
        entities, services, or dashboard (WP1 is lifecycle only).
        """
        from .const import (
            ENTRY_KIND,
            ENTRY_KIND_INSTALLATION_ANCHOR,
            INSTALLATION_ANCHOR_TITLE,
            INSTALLATION_ANCHOR_UNIQUE_ID,
        )

        await self.async_set_unique_id(INSTALLATION_ANCHOR_UNIQUE_ID)
        self._abort_if_unique_id_configured()

        _LOGGER.info("Creating PVAutonomy installation anchor (§9.4.2)")

        return self.async_create_entry(
            title=INSTALLATION_ANCHOR_TITLE,
            data={ENTRY_KIND: ENTRY_KIND_INSTALLATION_ANCHOR},
            options={},
        )

    def _get_proxy_defaults(self) -> dict[str, str]:
        """Get proxy config from existing entries (pre-fill for 2nd+ device)."""
        entries = self.hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            opts = entry.options
            if opts.get(CONF_PROXY_BASE_URL):
                return {
                    CONF_PROXY_BASE_URL: opts.get(CONF_PROXY_BASE_URL, DEFAULT_PROXY_BASE_URL),
                    CONF_PROXY_API_KEY: opts.get(CONF_PROXY_API_KEY, DEFAULT_PROXY_API_KEY),
                    CONF_PROXY_CUSTOMER_ID: opts.get(CONF_PROXY_CUSTOMER_ID, DEFAULT_PROXY_CUSTOMER_ID),
                }
        return {}

    def _find_inherited_customer_id(self) -> str:
        """Return a ``proxy_customer_id`` from a matching existing entry.

        Adopt-typical recovery (B): when ``/whoami`` does not yield a
        ``customer_id`` (or fails non-fatally), inherit the value from an
        existing ``pvautonomy_ops`` entry that targets the **same proxy
        URL** with the **same API key** — i.e. the same customer. Only
        non-empty values from non-``import`` entries are honored; empty
        entries and the legacy yaml-import stub are skipped. Read-only.
        """
        base = (self._proxy_base_url or "").strip()
        key = self._proxy_api_key or ""
        if not base or not key:
            return ""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if getattr(entry, "source", "") == "import":
                continue
            opts = entry.options or {}
            if (opts.get(CONF_PROXY_BASE_URL) or "").strip() != base:
                continue
            if (opts.get(CONF_PROXY_API_KEY) or "") != key:
                continue
            cid = (opts.get(CONF_PROXY_CUSTOMER_ID) or "").strip()
            if cid:
                return cid
        return ""

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow handler.

        M3A/#169 WP3: the installation anchor gets a dedicated Grid Power
        onboarding flow (§9.4.3/§9.4.7); device entries keep the existing
        settings/relocate/cleanup menu.
        """
        from .installation_anchor import is_installation_anchor

        if is_installation_anchor(config_entry):
            return GridPowerOptionsFlow()
        return PVAutonomyOpsOptionsFlow()


# ============================================================================
# Options Flow: Menu with Settings / Relocate (EPIC-006-WP3, Deliverable D)
# ============================================================================


def _build_settings_schema(
    mode: str | None,
    options: dict[str, Any],
    device_options: dict[str, str],
) -> vol.Schema:
    """Build the mode-aware Options-flow "settings" schema (#138).

    ``mode`` is the persisted ``build_service_mode`` from the config entry
    (``managed`` / ``local_esphome`` / ``self_hosted``). Field visibility hides
    mode-internal fields from normal Managed/Local users while preserving
    advanced/self-hosted configurability:

    - generic runtime options (device, poll interval, channel, OTA, cache,
      proxy_auto_refresh) — visible in all modes;
    - ``proxy_api_key`` — visible for Managed (Build-Key rotation) + Self-hosted
      (and legacy-safe); hidden for Local/Adopt;
    - ``proxy_base_url`` / ``proxy_customer_id`` / ``build_backend`` — self-hosted
      internals; visible only for Self-hosted (and legacy-safe);
    - ``simulated_failure_mode`` — dev/diagnostic; hidden in ALL modes.

    An unknown/missing mode (legacy/imported entries) is treated legacy-safe:
    all proxy-relevant fields are shown so an old config never becomes
    uneditable. Hidden fields are never wiped — the caller merges on submit.
    """
    is_known_mode = mode in (
        BUILD_SERVICE_MANAGED,
        BUILD_SERVICE_LOCAL_ESPHOME,
        BUILD_SERVICE_SELF_HOSTED,
    )
    is_legacy = not is_known_mode
    show_api_key = (
        mode == BUILD_SERVICE_MANAGED
        or mode == BUILD_SERVICE_SELF_HOSTED
        or is_legacy
    )
    show_proxy_internals = mode == BUILD_SERVICE_SELF_HOSTED or is_legacy

    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_SELECTED_DEVICE,
            default=options.get(CONF_SELECTED_DEVICE, ""),
        ): vol.In(device_options),
        vol.Optional(
            CONF_POLL_INTERVAL,
            default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        ): vol.All(int, vol.Range(min=10, max=300)),
        vol.Optional(
            CONF_ARTIFACT_CHANNEL,
            default=options.get(CONF_ARTIFACT_CHANNEL, DEFAULT_ARTIFACT_CHANNEL),
        ): vol.In(["stable", "beta"]),
    }

    if show_api_key:
        schema[
            vol.Optional(
                CONF_PROXY_API_KEY,
                default=options.get(CONF_PROXY_API_KEY, DEFAULT_PROXY_API_KEY),
            )
        ] = str
    if show_proxy_internals:
        schema[
            vol.Optional(
                CONF_PROXY_BASE_URL,
                default=options.get(CONF_PROXY_BASE_URL, DEFAULT_PROXY_BASE_URL),
            )
        ] = str
        schema[
            vol.Optional(
                CONF_PROXY_CUSTOMER_ID,
                default=options.get(
                    CONF_PROXY_CUSTOMER_ID, DEFAULT_PROXY_CUSTOMER_ID
                ),
            )
        ] = str

    schema[
        vol.Optional(
            CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT,
            default=options.get(
                CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT,
                DEFAULT_PROXY_AUTO_REFRESH,
            ),
        )
    ] = bool
    schema[
        vol.Optional(
            CONF_OTA_RETRIES,
            default=options.get(CONF_OTA_RETRIES, DEFAULT_OTA_RETRIES),
        )
    ] = vol.All(int, vol.Range(min=0, max=10))
    schema[
        vol.Optional(
            CONF_OTA_RETRY_DELAYS,
            default=options.get(CONF_OTA_RETRY_DELAYS, DEFAULT_OTA_RETRY_DELAYS),
        )
    ] = str
    schema[
        vol.Optional(
            CONF_CACHE_KEEP_BUILDS,
            default=options.get(CONF_CACHE_KEEP_BUILDS, DEFAULT_CACHE_KEEP_BUILDS),
        )
    ] = vol.All(int, vol.Range(min=1, max=100))

    if show_proxy_internals:
        schema[
            vol.Optional(
                CONF_BUILD_BACKEND,
                default=options.get(CONF_BUILD_BACKEND, DEFAULT_BUILD_BACKEND),
            )
        ] = vol.In([
            "proxy_remote", "simulated", "builder_addon",
            "esphome_dashboard", "manual",
        ])

    # CONF_SIMULATED_FAILURE_MODE is intentionally NOT exposed in any mode
    # (dev/diagnostic only); existing stored values are preserved on submit.

    return vol.Schema(schema)


class PVAutonomyOpsOptionsFlow(config_entries.OptionsFlow):
    """Options flow with structured menu.

    Note: self.config_entry is a read-only property provided by the HA
    OptionsFlow base class — do NOT set it in __init__.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "settings",
                "system_dashboard",
                "relocate",
                "cleanup_entities",
            ],
        )

    # ------------------------------------------------------------------
    # System Dashboard: OCD-3 explicit removal / re-enable (WP3).
    # Integration-global suppression state (not a per-entry option).
    # ------------------------------------------------------------------

    async def async_step_system_dashboard(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Route to the enabled or disabled management form by current state."""
        from .dashboard_builder import _read_suppression_state

        try:
            suppressed = await _read_suppression_state(self.hass)
        except Exception:  # noqa: BLE001 — fail closed, actionable error
            return self.async_abort(reason="system_dashboard_error")
        if suppressed:
            return await self.async_step_sysdash_disabled()
        return await self.async_step_sysdash_enabled()

    async def async_step_sysdash_enabled(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enabled state: offer confirmation-gated disable-and-remove."""
        from .dashboard_builder import (
            SYSTEM_DASHBOARD_ALREADY_REMOVED,
            SYSTEM_DASHBOARD_PARTIAL_REMOVAL,
            SYSTEM_DASHBOARD_REMOVED,
            SYSTEM_DASHBOARD_SUPPRESSED_ABSENT,
            SYSTEM_DASHBOARD_UNMANAGED_COLLISION,
            async_disable_and_remove_system_dashboard,
        )

        if user_input is not None:
            if not user_input.get("confirm_disable"):
                return self.async_abort(reason="system_dashboard_no_change")
            outcome = await async_disable_and_remove_system_dashboard(self.hass)
            if outcome in (
                SYSTEM_DASHBOARD_REMOVED,
                SYSTEM_DASHBOARD_SUPPRESSED_ABSENT,
                SYSTEM_DASHBOARD_ALREADY_REMOVED,
            ):
                return self.async_abort(reason="system_dashboard_removed")
            if outcome == SYSTEM_DASHBOARD_PARTIAL_REMOVAL:
                return self.async_abort(reason="system_dashboard_partial")
            if outcome == SYSTEM_DASHBOARD_UNMANAGED_COLLISION:
                return self.async_abort(reason="system_dashboard_ambiguous")
            return self.async_abort(reason="system_dashboard_error")

        return self.async_show_form(
            step_id="sysdash_enabled",
            data_schema=vol.Schema(
                {vol.Optional("confirm_disable", default=False): bool}
            ),
        )

    async def async_step_sysdash_disabled(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Disabled state: offer confirmation-gated enable-and-regenerate."""
        from .dashboard_builder import (
            SYSTEM_DASHBOARD_ENABLED,
            SYSTEM_DASHBOARD_PARTIAL_REMOVAL,
            SYSTEM_DASHBOARD_UNMANAGED_COLLISION,
            async_enable_system_dashboard,
        )

        if user_input is not None:
            if not user_input.get("confirm_enable"):
                return self.async_abort(reason="system_dashboard_no_change")
            outcome = await async_enable_system_dashboard(self.hass)
            if outcome == SYSTEM_DASHBOARD_ENABLED:
                return self.async_abort(reason="system_dashboard_enabled")
            if outcome == SYSTEM_DASHBOARD_UNMANAGED_COLLISION:
                return self.async_abort(reason="system_dashboard_ambiguous")
            if outcome == SYSTEM_DASHBOARD_PARTIAL_REMOVAL:
                return self.async_abort(reason="system_dashboard_partial")
            return self.async_abort(reason="system_dashboard_error")

        return self.async_show_form(
            step_id="sysdash_disabled",
            data_schema=vol.Schema(
                {vol.Optional("confirm_enable", default=False): bool}
            ),
        )

    # ------------------------------------------------------------------
    # Settings: Proxy + Build + OTA config
    # ------------------------------------------------------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Core settings: proxy, build, OTA config."""
        if user_input is not None:
            # Merge with existing options (preserve keys not in this form)
            new_options = dict(self.config_entry.options)
            new_options.update(user_input)
            return self.async_create_entry(title="", data=new_options)

        options = self.config_entry.options

        # Build device list for target device selector
        device_options: dict[str, str] = {"": "(no device selected)"}
        try:
            from .discovery import ContractInputReader

            reader = ContractInputReader(self.hass)
            dropdown_items = await reader.get_all_devices_for_dropdown()
            for item in dropdown_items:
                device_options[item["value"]] = item.get("label", item["value"])
        except Exception:
            _LOGGER.warning(
                "Could not load device list for options flow", exc_info=True
            )

        # #138: mode-aware Options-flow — see _build_settings_schema. Hidden
        # fields are NOT wiped (async_step_settings merges on submit above).
        mode = self.config_entry.data.get(CONF_BUILD_SERVICE_MODE)
        return self.async_show_form(
            step_id="settings",
            data_schema=_build_settings_schema(mode, options, device_options),
        )

    # ------------------------------------------------------------------
    # Relocate: Change device site/number in metadata store
    # ------------------------------------------------------------------

    async def async_step_relocate(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Relocate device: change site and/or number."""
        errors: dict[str, str] = {}

        if user_input is not None:
            site = user_input[CONF_SITE].strip().lower()
            number = user_input[CONF_NUMBER]

            if not site or len(site) < 2:
                errors[CONF_SITE] = "site_too_short"
            elif number < 1 or number > 10:
                errors[CONF_NUMBER] = "number_out_of_range"
            else:
                # EPIC-011 safe continuity: verify and preserve ESPHome
                # config-entry + Noise-PSK before allowing relocate.
                # See: BLOCKER-REPORT-RELOCATE-NOISE-PSK-LOSS.md

                # ── Gate 1: Resolve stable device identity ──
                initial = self.config_entry.options.get(
                    "_initial_device", {}
                )
                ha_device_id = (
                    initial.get("ha_device_id")
                    or self.config_entry.options.get("ha_device_id")
                )
                if not ha_device_id:
                    _LOGGER.warning(
                        "Relocate blocked: no ha_device_id in config entry"
                    )
                    return self.async_abort(reason="relocate_blocked")

                # ── Gate 2: Resolve metadata for old/new node names ──
                try:
                    from . import get_integration_data

                    int_data = get_integration_data(
                        self.hass, self.config_entry.entry_id
                    )
                    metadata_store = int_data.get("metadata_store")
                except Exception:
                    metadata_store = None

                if not metadata_store:
                    _LOGGER.warning(
                        "Relocate blocked: metadata store unavailable"
                    )
                    return self.async_abort(reason="relocate_blocked")

                all_devices = await metadata_store.get_all()
                device = next(
                    (d for d in all_devices
                     if d.ha_device_id == ha_device_id),
                    None,
                )
                if not device:
                    _LOGGER.warning(
                        "Relocate blocked: no metadata for "
                        "ha_device_id=%s (store has %d device(s))",
                        ha_device_id[:8],
                        len(all_devices),
                    )
                    return self.async_abort(reason="relocate_blocked")
                old_node_name = compute_node_name(
                    device.model_slug, device.site, device.number
                )
                new_node_name = compute_node_name(
                    device.model_slug, site, number
                )

                # ── Gate 3: Get full MAC for robust ESPHome lookup ──
                full_mac = ""
                dev_reg = dr.async_get(self.hass)
                dev_entry = dev_reg.async_get(ha_device_id)
                if dev_entry:
                    for conn_type, conn_id in dev_entry.connections:
                        if conn_type == dr.CONNECTION_NETWORK_MAC:
                            full_mac = conn_id
                            break

                # ── Gate 4: ESPHome continuity (fail-closed) ──
                from .keyring import update_esphome_entry_for_relocate

                continuity_ok = await update_esphome_entry_for_relocate(
                    self.hass,
                    new_node_name=new_node_name,
                    device_mac=full_mac,
                    ha_device_id=ha_device_id,
                    old_device_names=[old_node_name],
                )

                if not continuity_ok:
                    _LOGGER.warning(
                        "Relocate blocked (EPIC-011 fail-closed): "
                        "ESPHome continuity could not be verified. "
                        "site=%s number=%d",
                        site,
                        number,
                    )
                    return self.async_abort(reason="relocate_blocked")

                # ── Continuity verified — proceed with relocate ──
                try:
                    # Archive old ESPHome YAML (best effort)
                    old_filename = device.esphome_yaml_filename
                    if old_filename:
                        try:
                            from .esphome_sync import (
                                async_archive_esphome_yaml,
                            )

                            await async_archive_esphome_yaml(
                                self.hass,
                                filename=old_filename,
                                reason="relocate",
                            )
                        except Exception:
                            _LOGGER.warning(
                                "Failed to archive old YAML %s (non-fatal)",
                                old_filename,
                                exc_info=True,
                            )

                    updated = await metadata_store.update_location(
                        device.device_id, site, number
                    )
                    if updated:
                        _LOGGER.info(
                            "Device relocated: %s → site=%s, number=%d",
                            device.device_id,
                            site,
                            number,
                        )
                        # Regenerate + write new ESPHome YAML (best effort)
                        try:
                            from .device_id import (
                                compute_esphome_device_yaml_name,
                            )
                            from .esphome_sync import (
                                async_write_esphome_yaml,
                            )
                            from .yaml_generator import generate_device_yaml

                            new_filename = (
                                compute_esphome_device_yaml_name(
                                    device.model_slug, site, number
                                )
                            )
                            yaml_text = (
                                await self.hass.async_add_executor_job(
                                    lambda: generate_device_yaml(
                                        model=device.model_slug,
                                        site=site,
                                        number=number,
                                        registry_file=device.registry_file,
                                        mac_suffix=(
                                            device.mac_suffix or None
                                        ),
                                    )
                                )
                            )
                            await async_write_esphome_yaml(
                                self.hass,
                                filename=new_filename,
                                yaml_text=yaml_text,
                            )
                            # Update filename in metadata
                            updated.esphome_yaml_filename = new_filename
                            await metadata_store.put(updated)
                        except Exception:
                            _LOGGER.warning(
                                "Failed to regenerate ESPHome YAML "
                                "after relocate (non-fatal)",
                                exc_info=True,
                            )
                except Exception:
                    _LOGGER.warning(
                        "Failed to update metadata store",
                        exc_info=True,
                    )

                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )

        # Pre-fill with current values from metadata (filtered by ha_device_id)
        current_site = ""
        current_number = 1
        try:
            from . import get_integration_data

            initial = self.config_entry.options.get(
                "_initial_device", {}
            )
            prefill_device_id = (
                initial.get("ha_device_id")
                or self.config_entry.options.get("ha_device_id")
            )
            data = get_integration_data(self.hass, self.config_entry.entry_id)
            metadata_store = data.get("metadata_store")
            if metadata_store and prefill_device_id:
                all_devices = await metadata_store.get_all()
                device = next(
                    (d for d in all_devices
                     if d.ha_device_id == prefill_device_id),
                    None,
                )
                if device:
                    current_site = device.site
                    current_number = device.number
        except Exception:
            _LOGGER.debug("relocate prefill failed", exc_info=True)

        return self.async_show_form(
            step_id="relocate",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE, default=current_site): str,
                    vol.Required(CONF_NUMBER, default=current_number): vol.All(
                        int, vol.Range(min=1, max=10)
                    ),
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Cleanup: Detect and disable/delete old-prefix entities (EPIC-011 P2)
    # ------------------------------------------------------------------

    _cleanup_plan: object | None = None  # type: ignore[assignment]

    async def async_step_cleanup_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Detect prefix split and show cleanup options."""
        from .entity_cleanup import detect_prefix_split

        slug = get_device_slug_from_entry(self.config_entry, hass=self.hass)
        if not slug:
            return self.async_show_form(
                step_id="cleanup_no_split",
                description_placeholders={"status": "no_slug"},
            )

        initial = self.config_entry.options.get("_initial_device", {})
        ha_device_id = (
            initial.get("ha_device_id")
            or self.config_entry.options.get("ha_device_id")
        )

        plan = detect_prefix_split(
            self.hass,
            self.config_entry.entry_id,
            slug,
            ha_device_id=ha_device_id,
        )

        if plan is None or not plan.has_split:
            return self.async_show_form(
                step_id="cleanup_no_split",
                description_placeholders={
                    "current_prefix": slug.replace("-", "_"),
                },
            )

        # Store plan for subsequent steps.
        self._cleanup_plan = plan

        # Build description with counts per old prefix.
        prefix_summary = ", ".join(
            f"`{p}` ({n})"
            for p, n in plan.counts_by_prefix().items()
        )
        return self.async_show_form(
            step_id="cleanup_review",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="disable"): vol.In(
                        {
                            "disable": "Disable old entities",
                            "delete": "Delete old entities (irreversible)",
                            "cancel": "Cancel",
                        }
                    ),
                }
            ),
            description_placeholders={
                "current_prefix": plan.current_prefix,
                "old_prefixes": prefix_summary,
                "candidate_count": str(len(plan.candidates)),
            },
        )

    async def async_step_cleanup_no_split(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """No cleanup needed — return to options."""
        return self.async_create_entry(
            title="", data=dict(self.config_entry.options)
        )

    async def async_step_cleanup_review(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Process user choice from cleanup review."""
        if user_input is None:
            return await self.async_step_cleanup_entities()

        action = user_input.get("action", "cancel")
        if action == "cancel":
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        if action == "delete":
            return await self.async_step_cleanup_confirm_delete()

        # action == "disable"
        return await self._execute_cleanup(delete=False)

    async def async_step_cleanup_confirm_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Require DELETE confirmation phrase before deletion."""
        errors: dict[str, str] = {}

        if user_input is not None:
            phrase = (user_input.get("confirm_phrase") or "").strip()
            if phrase == "DELETE":
                return await self._execute_cleanup(delete=True)
            errors["confirm_phrase"] = "cleanup_phrase_mismatch"

        return self.async_show_form(
            step_id="cleanup_confirm_delete",
            data_schema=vol.Schema(
                {
                    vol.Required("confirm_phrase"): str,
                }
            ),
            errors=errors,
        )

    async def _execute_cleanup(self, *, delete: bool) -> FlowResult:
        """Run disable (and optionally delete) on old-prefix entities."""
        from .entity_cleanup import (
            CleanupPlan,
            delete_entities,
            disable_entities,
            generate_report,
        )

        plan: CleanupPlan = self._cleanup_plan  # type: ignore[assignment]
        entity_ids = [c.entity_id for c in plan.candidates]

        if delete:
            result = delete_entities(self.hass, entity_ids)
        else:
            result = disable_entities(self.hass, entity_ids)

        report = generate_report(plan, result)

        # Fire persistent notification (best-effort).
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "PVAutonomy Entity Cleanup",
                    "message": report,
                    "notification_id": f"pva_cleanup_{plan.config_entry_id}",
                },
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to create cleanup notification", exc_info=True)

        # Store result summary for completion step.
        self._cleanup_report = {
            "disabled": result.disabled_count,
            "deleted": result.deleted_count,
            "skipped": result.skipped_count,
            "errors": len(result.errors),
            "old_prefixes": ", ".join(f"`{p}`" for p in plan.old_prefixes),
        }
        return await self.async_step_cleanup_complete()

    async def async_step_cleanup_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show cleanup results and return to options."""
        if user_input is not None:
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        rpt = getattr(self, "_cleanup_report", {})
        return self.async_show_form(
            step_id="cleanup_complete",
            description_placeholders={
                "disabled": str(rpt.get("disabled", 0)),
                "deleted": str(rpt.get("deleted", 0)),
                "skipped": str(rpt.get("skipped", 0)),
                "errors": str(rpt.get("errors", 0)),
                "old_prefixes": rpt.get("old_prefixes", "—"),
            },
        )


# ============================================================================
# Grid Power onboarding Options Flow (M3A / #169 WP3)
# Authority: Ops Contract v1.1.2 §9.4.3 / §9.4.6 / §9.4.7. Detect → Map →
# Validate → Confirm → Guide, over the WP2 runtime (GridPowerManager +
# Installation Anchor). Supports Type B (mapped existing HA entity, incl.
# degraded entity-id-only) and Type C (none — healthy). Type A (SHRDZM/P1
# adapter) is out of scope here (§9.4.8 = WP4).
#
# No runtime duplication: the flow validates via the manager's dry-run
# (GridPowerManager.validate_candidate) and builds mappings via the canonical
# schema (entity_to_source_ref + the mapping builders + validate_mapping_dict).
# Persistence is the standard OptionsFlow create_entry write to the anchor
# options; the manager reacts through its own update listener. No MQTT
# credentials are read/requested/stored — only non-secret MQTT presence may be
# displayed (§9.4.6).
# ============================================================================


class GridPowerOptionsFlow(config_entries.OptionsFlow):
    """Installation-global Grid Power onboarding for the anchor (§9.4.3/§9.4.7).

    Note: self.config_entry is a read-only property from the OptionsFlow base
    class (the anchor entry) — do NOT set it in __init__.
    """

    def __init__(self) -> None:
        # The in-progress candidate mapping (raw grid_power dict) or None.
        self._candidate: dict | None = None
        # Type-A (SHRDZM) selection context — non-secret label + neutral mode.
        self._shrdzm_label: str = ""
        self._shrdzm_mode: str = ""

    # -- helpers ------------------------------------------------------------

    def _manager(self) -> "GridPowerManager | None":
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self.config_entry.entry_id, {})
            .get(GRID_POWER_MANAGER_KEY)
        )

    def _detect(self) -> tuple[int, bool]:
        """Detect (non-secret): count candidate power sensors + MQTT presence.

        MQTT presence is read only from HA's own component/entry registry — no
        broker credentials are touched (§9.4.6).
        """
        candidates = sum(
            1
            for state in self.hass.states.async_all("sensor")
            if state.attributes.get("device_class") == "power"
        )
        mqtt_present = "mqtt" in self.hass.config.components or bool(
            self.hass.config_entries.async_entries("mqtt")
        )
        return candidates, mqtt_present

    def _state_placeholders(self) -> dict[str, str]:
        manager = self._manager()
        state = manager.capability_state if manager is not None else "unknown"
        reason = ""
        if manager is not None:
            reason = manager.capability_attributes.get("validation_reason") or ""
        candidates, mqtt_present = self._detect()
        shrdzm_total = count_shrdzm_devices(self.hass)
        shrdzm_usable = len(discover_shrdzm_candidates(self.hass))
        return {
            "capability_state": state,
            "reason": reason or "—",
            "candidate_count": str(candidates),
            "mqtt": "yes" if mqtt_present else "no",
            # Type-A guidance: total detected vs. usable (complete-evidence) meters.
            "shrdzm_detected": str(shrdzm_total),
            "shrdzm_usable": str(shrdzm_usable),
        }

    def _persist(self, candidate: dict | None) -> FlowResult:
        """Persist a validated candidate into the anchor options (§9.4.3).

        Uses the standard OptionsFlow write; the GridPowerManager observes the
        change via its update listener and re-evaluates. No direct manager
        mutation here — single write path, no duplication.
        """
        new_options = dict(self.config_entry.options)
        validated = validate_mapping_dict(candidate)
        if validated is None:
            new_options.pop(GRID_POWER_OPTIONS_KEY, None)
        else:
            new_options[GRID_POWER_OPTIONS_KEY] = validated
        return self.async_create_entry(title="", data=new_options)

    def _power_entity_selector(self):
        from homeassistant.helpers import selector

        return selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="power")
        )

    # -- menu ---------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_grid_menu()

    async def async_step_grid_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Detect + present capability state and the onboarding choices."""
        configured = bool(self.config_entry.options.get(GRID_POWER_OPTIONS_KEY))
        menu_options: list[str] = []
        # Type A (§9.4.7/§9.4.8): offer the detected supported adapter FIRST, and
        # only when a complete, valid SHRDZM candidate exists (fail closed).
        if discover_shrdzm_candidates(self.hass):
            menu_options.append("configure_shrdzm")
        menu_options += ["configure_signed_net", "configure_split"]
        if configured:
            menu_options.append("remove")
        menu_options.append("guide")
        return self.async_show_menu(
            step_id="grid_menu",
            menu_options=menu_options,
            description_placeholders=self._state_placeholders(),
        )

    # -- Type A: guided SHRDZM/P1 adapter (§9.4.7/§9.4.8) -------------------

    async def async_step_configure_shrdzm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Detect supported SHRDZM candidates and route to select/confirm.

        Never silently falls back to a Type B mapping — if detection is
        incomplete/ambiguous the customer is routed to Guide with actionable
        context; the manual Type B/C paths stay available in the menu.
        """
        candidates = discover_shrdzm_candidates(self.hass)
        if not candidates:
            # Present-but-unusable SHRDZM devices → actionable guidance.
            return await self.async_step_guide()
        if len(candidates) == 1:
            self._candidate = candidates[0].mapping
            self._shrdzm_label = candidates[0].display_label
            self._shrdzm_mode = candidates[0].mode
            return await self.async_step_shrdzm_confirm()
        return await self.async_step_shrdzm_select()

    async def async_step_shrdzm_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Deterministic selection when multiple SHRDZM meters are present."""
        candidates = discover_shrdzm_candidates(self.hass)
        if not candidates:
            return await self.async_step_guide()
        # Deterministic, non-secret labels; opaque identity suffix disambiguates
        # identically-named devices without leaking the MAC/token.
        options = {
            c.source_identity: f"{c.display_label} ({c.source_identity[:8]})"
            for c in candidates
        }
        if user_input is not None:
            chosen = next(
                (c for c in candidates if c.source_identity == user_input["device"]),
                None,
            )
            if chosen is not None:
                self._candidate = chosen.mapping
                self._shrdzm_label = chosen.display_label
                self._shrdzm_mode = chosen.mode
                return await self.async_step_shrdzm_confirm()
        return self.async_show_form(
            step_id="shrdzm_select",
            data_schema=vol.Schema({vol.Required("device"): vol.In(options)}),
        )

    async def async_step_shrdzm_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the non-secret identity + dry-run projection, then persist."""
        manager = self._manager()
        if manager is None or self._candidate is None:
            return self.async_abort(reason="manager_unavailable")
        result = manager.validate_candidate(self._candidate)
        if user_input is not None:
            return self._persist(self._candidate)
        return self.async_show_form(
            step_id="shrdzm_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "label": self._shrdzm_label,
                "mode": self._shrdzm_mode,
                "projected_state": result.state,
                "reason": result.reason or "—",
            },
        )

    # -- Map: signed-net (Type B) ------------------------------------------

    async def async_step_configure_signed_net(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm_sign", False):
                # Generic signed-net: a heuristic never releases ready — the
                # customer must confirm the sign convention first (§9.4.4).
                errors["confirm_sign"] = "sign_not_confirmed"
            else:
                src = entity_to_source_ref(self.hass, user_input["source_entity"])
                self._candidate = signed_net_mapping(src, sign_confirmed=True)
                return await self.async_step_grid_validate()
        schema = vol.Schema(
            {
                vol.Required("source_entity"): self._power_entity_selector(),
                vol.Required("confirm_sign", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="configure_signed_net",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._state_placeholders(),
        )

    # -- Map: split import/export (Type B) ---------------------------------

    async def async_step_configure_split(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            import_entity = user_input["import_entity"]
            export_entity = user_input["export_entity"]
            if import_entity == export_entity:
                errors["export_entity"] = "same_entity"
            else:
                self._candidate = split_mapping(
                    entity_to_source_ref(self.hass, import_entity),
                    entity_to_source_ref(self.hass, export_entity),
                )
                return await self.async_step_grid_validate()
        schema = vol.Schema(
            {
                vol.Required("import_entity"): self._power_entity_selector(),
                vol.Required("export_entity"): self._power_entity_selector(),
            }
        )
        return self.async_show_form(
            step_id="configure_split",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._state_placeholders(),
        )

    # -- Validate + Confirm -------------------------------------------------

    async def async_step_grid_validate(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dry-run the candidate (no persistence), show feedback, then confirm."""
        manager = self._manager()
        if manager is None or self._candidate is None:
            return self.async_abort(reason="manager_unavailable")
        try:
            result = manager.validate_candidate(self._candidate)
        except ValueError:
            return self.async_abort(reason="invalid_mapping")

        if user_input is not None:
            # Confirm → persist. A not_ready-but-recoverable mapping (e.g. a
            # source that is not yet fresh) is still a legitimate configuration
            # and is persisted; the capability recovers when the source updates.
            return self._persist(self._candidate)

        return self.async_show_form(
            step_id="grid_validate",
            data_schema=vol.Schema({}),
            description_placeholders={
                "projected_state": result.state,
                "reason": result.reason or "—",
                "degraded": "yes" if result.degraded else "no",
                "sources": ", ".join(result.source_entities) or "—",
            },
        )

    # -- Remove (Type C — none, healthy) -----------------------------------

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # Deleting the mapping returns Grid Power to the healthy
            # not_configured state; it is never silently restored (§9.4.2).
            return self._persist(None)
        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema({}),
            description_placeholders=self._state_placeholders(),
        )

    # -- Guide (recovery guidance) -----------------------------------------

    async def async_step_guide(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return await self.async_step_grid_menu()
        return self.async_show_form(
            step_id="guide",
            data_schema=vol.Schema({}),
            description_placeholders=self._state_placeholders(),
        )
