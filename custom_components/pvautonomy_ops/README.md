# PVAutonomy

Manage Edge101 ESPHome devices for solar inverter monitoring in Home Assistant.

## Features

- **Device Discovery** — automatically finds Edge101 devices via the HA Device Registry
- **Remote Firmware Builds** — compile production firmware in the cloud via GitHub Actions
- **OTA Flash** — update device firmware over-the-air with integrity verification (SHA-256)
- **Readiness Gates** — validate device health, entity naming, and configuration before flashing
- **Multi-device Support** — manage multiple Edge101 devices from a single integration

## Quick Start

1. Install via HACS (custom repository: `PVAutonomy/pvautonomy-ops`)
2. Add the integration: Settings > Devices & Services > Add > PVAutonomy
3. Configure: set build backend to `proxy_remote`, enter your API key
4. Select a device and trigger your first build

See [Installation](docs/INSTALLATION.md) for detailed steps.

## Documentation

- [Installation](docs/INSTALLATION.md) — install and add the integration
- [Setup Guide](docs/SETUP-WIZARD.md) — configure the build backend and options
- [Troubleshooting](docs/TROUBLESHOOTING.md) — error reference and common fixes
- [Security & Privacy](docs/SECURITY.md) — what data is sent, integrity checks
- [FAQ](docs/FAQ.md) — frequently asked questions

## Entity Naming Convention (Ops Contract v1, Section 1.4)

ESPHome production firmware uses `esphome.name` as the **node name** (e.g., `mic600-garage-01`).
HA converts dashes to underscores for entity IDs.

**Pattern:** `{domain}.{device_name}_{metric}_device`

| ESPHome node name | HA entity_id example |
|-------------------|---------------------|
| `mic600-garage-01` | `sensor.mic600_garage_01_energy_today_device` |
| `sph10k-haus-05` | `sensor.sph10k_haus_05_battery_soc_device` |

**Rules:**
- Modbus sensor/number/switch entities include `_device` suffix (set in ESPHome YAML `name:` field)
- System entities (Uptime, WiFi Signal, IP) do NOT have `_device` suffix
- `esphome.project.name: PVAutonomy.Edge101` is required for discovery (sets `manufacturer=PVAutonomy`, `model=Edge101`)
- Changing `esphome.name` after first HA registration requires entity registry cleanup (HA preserves original entity_ids)

**Diagnostic:** If entity IDs show an old prefix (e.g., `growatt_mic600tl_x_*`), the device was registered under a previous firmware name. Fix: remove stale entity registry entries + restart HA.

## Compatibility

| Component | Version |
|-----------|---------|
| Home Assistant | >= 2024.1.0 |
| PVAutonomy Ops | 0.2.0 |
| Ops Contract | v1.0.0 |

## License

Copyright PVAutonomy. All rights reserved.
