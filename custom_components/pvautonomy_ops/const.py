"""Constants for PVAutonomy Ops integration.

Contract: ops-contract-v1.md (v1.0.0)
CI: verified by .github/workflows/ci-ops.yml
All entity IDs defined here - NO hardcodes elsewhere.
"""

# Integration metadata
DOMAIN = "pvautonomy_ops"
VERSION = "0.4.9"
CONTRACT_VERSION = "v1.0.0"

# Update interval (seconds)
UPDATE_INTERVAL = 60

# ============================================================================
# Contract Inputs (READ) - Source: ops-contract-v1.md Section 1
# ============================================================================

# Input A: Discovery & Device List
ENTITY_DISCOVERY_SENSOR = "sensor.edge101_production_devices"

# Input B: Selected Device (User Input)
ENTITY_DEVICE_SELECTOR = "input_select.edge101_selected_production_device"

# Input C: Device Health (per device) - Template pattern
# Actual: binary_sensor.{device_name}_health
ENTITY_HEALTH_PATTERN = "binary_sensor.{device}_health"

# Input D: Runtime Sensors (per device) - Template patterns
# Examples:
#   sensor.{device}_battery_soc_device
#   sensor.{device}_ac_output_power_device
#   sensor.{device}_local_load_power_device
#   sensor.{device}_uptime_device
#   sensor.{device}_wifi_signal_device
ENTITY_RUNTIME_SENSOR_PATTERN = "sensor.{device}_{metric}_device"

# Input E: Control Entities (per device) - Template patterns
# Examples:
#   number.{device}_active_power_rate_device
#   switch.{device}_grid_first_device
#   switch.{device}_battery_first_device
ENTITY_CONTROL_NUMBER_PATTERN = "number.{device}_{metric}_device"
ENTITY_CONTROL_SWITCH_PATTERN = "switch.{device}_{metric}_device"

# Input F: Hardware Family Marker (per device)
# Actual: sensor.{device_name}_hardware_family
ENTITY_HARDWARE_FAMILY_PATTERN = "sensor.{device}_hardware_family"

# ============================================================================
# Contract Outputs (WRITE) - Source: ops-contract-v1.md Section 2
# ============================================================================

# Output G: App Status Sensor (HA 2026.2: "Add-on" → "App")
ENTITY_STATUS_SENSOR = "sensor.pvautonomy_ops_status"

# Output H: Device Count Sensor
ENTITY_DEVICE_COUNT_SENSOR = "sensor.pvautonomy_ops_devices_count"

# Target Device Select (first-class UI control for device selection)
ENTITY_TARGET_DEVICE_SELECT = "pvautonomy_ops_target_device"

# ============================================================================
# Status States (for Output G)
# ============================================================================
STATE_OK = "ok"
STATE_WARN = "warn"
STATE_ERROR = "error"
STATE_DEGRADED = "degraded"
STATE_INITIALIZING = "initializing"

# ============================================================================
# Known Metrics (for validation - optional)
# ============================================================================
KNOWN_RUNTIME_METRICS = [
    "battery_soc",
    "ac_output_power",
    "local_load_power",
    "uptime",
    "wifi_signal",
]

KNOWN_CONTROL_METRICS = [
    "active_power_rate",
    "grid_first",
    "battery_first",
]

# ============================================================================
# Firmware Artifact Distribution (Phase 3.3)
# ============================================================================
# Canonical Source of Truth for OTA Firmware Artifacts
# Pattern: https://github.com/{owner}/{repo}/releases/download/v{version}/...

ARTIFACTS_OWNER = "PVAutonomy"
ARTIFACTS_REPO = "pvautonomy-firmware"
ARTIFACTS_BASE_URL = f"https://github.com/{ARTIFACTS_OWNER}/{ARTIFACTS_REPO}/releases/download"

# GitHub Pages URL for Factory Managed Pull (no redirects, ESP32-safe)
# ESP-Web-Tools manifest + firmware.ota.bin hosted here
ARTIFACTS_PAGES_BASE_URL = f"https://{ARTIFACTS_OWNER.lower()}.github.io/{ARTIFACTS_REPO}"

# Release channel priorities (for future auto-update logic)
ARTIFACTS_CHANNELS = ["stable", "beta", "dev"]

# ============================================================================
# Device-Specific Firmware Pipeline (P3-12-001)
# ============================================================================
# Hardware platform prefix for Edge101 devices
DEVICE_HW_PREFIX = "edge101"

# Per-device artifact path on GitHub Pages:
#   {ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}/{channel}/{device_id}/manifest.json
#   {ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}/{channel}/{device_id}/firmware.ota.bin
# Factory reset binary path:
#   {ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}/factory/firmware.ota.bin

# Builder App defaults (internal slug unchanged for backward compat)
BUILDER_ADDON_SLUG = "pvautonomy_builder"
BUILDER_ADDON_DEFAULT_PORT = 8099
BUILDER_ADDON_TIMEOUT = 600  # 10 min max compile time
BUILDER_POLL_INTERVAL = 3  # seconds between status polls

# Build states
BUILD_STATE_QUEUED = "queued"
BUILD_STATE_COMPILING = "compiling"
BUILD_STATE_SUCCESS = "success"
BUILD_STATE_FAILED = "failed"

# Build backend selection (WP1: SimulatedBuildBackend)
# Configurable via Options Flow → build_backend
BUILD_BACKEND_SIMULATED = "simulated"
BUILD_BACKEND_BUILDER_ADDON = "builder_addon"
BUILD_BACKEND_ESPHOME_DASHBOARD = "esphome_dashboard"  # WP2: future
BUILD_BACKEND_MANUAL = "manual"
BUILD_BACKEND_PROXY_REMOTE = "proxy_remote"  # EPIC-005-D1: Cloudflare Proxy
BUILD_BACKEND_CHOICES = [
    BUILD_BACKEND_SIMULATED,
    BUILD_BACKEND_BUILDER_ADDON,
    BUILD_BACKEND_ESPHOME_DASHBOARD,
    BUILD_BACKEND_MANUAL,
    BUILD_BACKEND_PROXY_REMOTE,
]
BUILD_BACKEND_DEFAULT = BUILD_BACKEND_SIMULATED  # Safe default until Builder App exists

# Hardware platform model (sent to Proxy as "model" field)
# Distinct from inverter model_slug (mic600/sph10k) which selects registry/YAML.
# The Proxy interprets "model" as the compile target / board family.
DEFAULT_HARDWARE_MODEL = "edge101"
SUPPORTED_HARDWARE_MODELS = [DEFAULT_HARDWARE_MODEL]

# Proxy Remote Build Backend (EPIC-005-D1)
PROXY_DEFAULT_BASE_URL = "https://pvautonomy-proxy.pvautonomy-proxy.workers.dev"
PROXY_DEFAULT_TIMEOUT = 900  # 15 min (matches proxy BUILD_TIMEOUT_MS)
PROXY_POLL_INTERVAL = 10  # seconds between proxy status polls
PROXY_API_TIMEOUT = 60  # seconds for API calls (start_build, get_status)
PROXY_DOWNLOAD_TIMEOUT = 120  # seconds for artifact download
PROXY_ARTIFACT_CACHE_DIR = "pvautonomy/cache"  # relative to /config/
PROXY_MAX_CACHED_BUILDS = 10  # retention: keep last N builds per device

# OTA Robustness + Cache (EPIC-006-A5)
CONF_OTA_RETRIES = "ota_retries"
CONF_OTA_RETRY_DELAYS = "ota_retry_delays"
CONF_CACHE_KEEP_BUILDS = "cache_keep_builds"

# Proxy refresh recovery (EPIC-006-D2)
CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT = "proxy_auto_refresh_on_timeout"
# Terminal states that warrant a refresh when artifact is missing.
# ISSUE-19: "success" included — a transiently poisoned proxy record can
# report success with artifact: null; the proxy's ?refresh=1 re-resolves it.
PROXY_TERMINAL_NO_ARTIFACT = {"timeout", "failed", "success"}

# Proxy status → BuildState mapping
PROXY_STATUS_MAP = {
    "queued": BUILD_STATE_QUEUED,
    "dispatched": BUILD_STATE_QUEUED,
    "running": BUILD_STATE_COMPILING,
    "success": BUILD_STATE_SUCCESS,
    "failed": BUILD_STATE_FAILED,
    "timeout": BUILD_STATE_FAILED,
    # cached is terminal-success by proxy contract (artifact always present)
    "cached": BUILD_STATE_SUCCESS,
}

# Simulated backend tunables
SIMULATED_BUILD_DURATION_S = 8.0  # Total simulated compile time
SIMULATED_ARTIFACT_SIZE = 150 * 1024  # 150 KB dummy firmware
SIMULATED_FAILURE_NONE = "none"
SIMULATED_FAILURE_COMPILE = "fail_compile"
SIMULATED_FAILURE_TIMEOUT = "timeout"
SIMULATED_FAILURE_MISSING_ARTIFACT = "missing_artifact"
SIMULATED_FAILURE_MODES = [
    SIMULATED_FAILURE_NONE,
    SIMULATED_FAILURE_COMPILE,
    SIMULATED_FAILURE_TIMEOUT,
    SIMULATED_FAILURE_MISSING_ARTIFACT,
]

# ESPHome Dashboard Backend (WP2)
ESPHOME_ADDON_SLUG = "5c53de3b_esphome"
ESPHOME_SUPERVISOR_URL = "http://supervisor"
ESPHOME_CONFIG_DIR = "/config/esphome"
ESPHOME_BUILD_DIR = "/config/esphome/.esphome/build"
ESPHOME_COMPILE_TIMEOUT = 600  # 10 min max compile time
ESPHOME_COMPILE_WS_PATH = "compile"
ESPHOME_DOWNLOAD_PATH = "download.bin"
ESPHOME_VERSION_PATH = "version"
ESPHOME_EDIT_PATH = "edit"

# ============================================================================
# Self-contained Discovery & Selection (EPIC-005-A1)
# ============================================================================

# Integration-owned device selection (replaces input_select helper)
CONF_SELECTED_DEVICE = "selected_device"

# Health computation: required capabilities per model family (PN-1)
# Keys are device_class values from HA Entity Registry.
# compute_device_health() uses er.async_entries_for_device() and checks
# device_class + state_class — never hardcoded entity_id strings.
HEALTH_REQUIRED_CAPABILITIES: dict[str, list[str]] = {
    "default": ["battery", "power"],  # battery SoC + any power sensor
    "mic600": ["power"],              # MIC has no battery
}

# ============================================================================
# Model Registry (EPIC-006 WP3 — Single Source of Truth for MVP)
# ============================================================================
# Registry-driven: MVP supports Growatt SPH10K + MIC600 only.
# New models are added here (no code change needed elsewhere).

MODEL_REGISTRY_MAP: dict[str, dict[str, str]] = {
    "sph10k": {
        "manufacturer": "growatt",
        "display_name": "Growatt SPH10K",
        "description": "Hybrid inverter with battery storage",
        "registry_file": "growatt/sph/sph10k.json",
    },
    "mic600": {
        "manufacturer": "growatt",
        "display_name": "Growatt MIC600",
        "description": "Micro-inverter",
        "registry_file": "growatt/mic/mic600.json",
    },
}

# Manufacturer → model slugs (derived at import time)
MANUFACTURER_MAP: dict[str, list[str]] = {}
for _slug, _info in MODEL_REGISTRY_MAP.items():
    MANUFACTURER_MAP.setdefault(_info["manufacturer"], []).append(_slug)

# ============================================================================
# Location Presets (Customer Setup UX Pack)
# ============================================================================
# Keys are stored in config entry options (language-neutral per D-ADDON-I18N-001).
# Display labels are in translations/{lang}.json.
LOCATION_PRESETS: dict[str, str] = {
    "home": "Haus / Home",
    "garage": "Garage",
    "garden": "Garten / Garden",
    "utility_room": "Technikraum / Utility Room",
    "custom": "Custom...",
}

# ============================================================================
# Config Flow Keys (EPIC-006 WP3)
# ============================================================================
CONFIG_ENTRY_VERSION = 2

# Config Flow step data keys
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL_SLUG = "model_slug"
CONF_SITE = "site"
CONF_NUMBER = "number"
CONF_DEVICE_SLUG = "device_slug"  # Immutable after first install (ADR-003, EPIC-011)

# _initial_device._setup_state values. "complete"/"partial" mark wizard runs
# that already built (and maybe flashed) firmware; "adopted" marks a device
# taken over via the Adopt flow — already-running hardware registered WITHOUT
# any build/install/reflash. All three skip the post-setup background build.
SETUP_STATE_COMPLETE = "complete"
SETUP_STATE_PARTIAL = "partial"
SETUP_STATE_ADOPTED = "adopted"
SETUP_STATES_SKIP_BUILD = (
    SETUP_STATE_COMPLETE,
    SETUP_STATE_PARTIAL,
    SETUP_STATE_ADOPTED,
)

# Config Flow first-screen menu options (setup new vs adopt running device).
MENU_OPTION_SETUP_NEW = "setup_new"
MENU_OPTION_ADOPT_EXISTING = "adopt_existing"

# Proxy config keys (canonical — used in Config Flow + Options Flow)
CONF_PROXY_BASE_URL = "proxy_base_url"
CONF_PROXY_API_KEY = "proxy_api_key"
CONF_PROXY_CUSTOMER_ID = "proxy_customer_id"

# Config Flow option keys
CONF_POLL_INTERVAL = "poll_interval_sec"
CONF_ARTIFACT_CHANNEL = "artifact_channel"
CONF_ARTIFACT_HW_FAMILY = "artifact_hw_family_default"
CONF_ARTIFACT_OWNER = "artifact_owner"
CONF_ARTIFACT_REPO = "artifact_repo"
CONF_FLASH_MIN_SIZE_KB = "flash_min_firmware_size_kb"
CONF_GATES_FRESHNESS_MIN = "gates_freshness_minutes"
CONF_STRICT_GATES = "strict_gates_required"
CONF_BUILD_BACKEND = "build_backend"
CONF_SIMULATED_FAILURE_MODE = "simulated_failure_mode"

# ============================================================================
# Default Values (EPIC-006 WP3 — centralized)
# ============================================================================
DEFAULT_POLL_INTERVAL = 60
DEFAULT_ARTIFACT_CHANNEL = "stable"
DEFAULT_ARTIFACT_HW_FAMILY = "edge101"
DEFAULT_ARTIFACT_OWNER = "PVAutonomy"
DEFAULT_ARTIFACT_REPO = "pvautonomy-firmware"
DEFAULT_FLASH_MIN_SIZE_KB = 300
DEFAULT_GATES_FRESHNESS_MIN = 10
DEFAULT_STRICT_GATES = True
DEFAULT_BUILD_BACKEND = BUILD_BACKEND_PROXY_REMOTE  # WP3: proxy is default for customers
DEFAULT_SIMULATED_FAILURE_MODE = "none"
DEFAULT_PROXY_BASE_URL = PROXY_DEFAULT_BASE_URL
DEFAULT_PROXY_API_KEY = ""
DEFAULT_PROXY_CUSTOMER_ID = ""
DEFAULT_PROXY_AUTO_REFRESH = True
DEFAULT_OTA_RETRIES = 3
DEFAULT_OTA_RETRY_DELAYS = "0,10,30"
DEFAULT_CACHE_KEEP_BUILDS = 10

# ============================================================================
# Registry Tier Gating (EPIC-010 Tiering v1.1)
# ============================================================================
# Tier values (ordered: standard < extended < unsafe)
TIER_STANDARD = "standard"
TIER_EXTENDED = "extended"
TIER_UNSAFE = "unsafe"
TIER_ORDER: dict[str, int] = {TIER_STANDARD: 0, TIER_EXTENDED: 1, TIER_UNSAFE: 2}
TIER_CHOICES = [TIER_STANDARD, TIER_EXTENDED, TIER_UNSAFE]

# Config entry option key for selected tier
CONF_SELECTED_TIER = "selected_tier"
DEFAULT_SELECTED_TIER = TIER_STANDARD

# Consent phrase required for unsafe tier (case-sensitive)
UNSAFE_CONSENT_PHRASE = "I UNDERSTAND"

# ============================================================================
# Registry Version-aware Gating (EPIC-010 vNext)
# ============================================================================
# Config entry option keys for modbus map verification results
CONF_MAP_CONFIRMED = "map_confirmed"
CONF_MODBUS_VERSION = "modbus_version"

# Sentinel: HR73 not readable (fresh install / factory firmware)
MODBUS_VERSION_UNKNOWN: None = None

# ============================================================================
# Secret-Blind Proxy Envelope (EPIC-006 PR-3)
# ============================================================================
# Phase-2 opt-in flag for HPKE compile_secret_envelope path.
# Default OFF: legacy payload.encrypted_secrets is the customer-safe baseline
# until production root pins land via a Judge-approved root-key ceremony.
# Concrete schema/constants live in secret_envelope.py.

CONF_ENVELOPE_MODE_ENABLED = "envelope_mode_enabled"
DEFAULT_ENVELOPE_MODE_ENABLED = False

# build_contract value that the GHA decoder requires for envelope mode
# (per ADR §6.3.1: yaml_authority is the only path that can produce a
# byte-stable yaml_hash before dispatch).
BUILD_CONTRACT_YAML_AUTHORITY = "yaml_authority"
