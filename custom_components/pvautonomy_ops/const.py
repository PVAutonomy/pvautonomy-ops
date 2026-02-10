"""Constants for PVAutonomy Ops integration.

Contract: ops-contract-v1.md (v1.0.0)
All entity IDs defined here - NO hardcodes elsewhere.
"""

# Integration metadata
DOMAIN = "pvautonomy_ops"
VERSION = "0.1.0"
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

# Output G: Add-on Status Sensor
ENTITY_STATUS_SENSOR = "sensor.pvautonomy_ops_status"

# Output H: Device Count Sensor
ENTITY_DEVICE_COUNT_SENSOR = "sensor.pvautonomy_ops_devices_count"

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

# Release channel priorities (for future auto-update logic)
ARTIFACTS_CHANNELS = ["stable", "beta", "dev"]
