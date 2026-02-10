# PVAutonomy Ops

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![License](https://img.shields.io/github/license/PVAutonomy/pvautonomy-ops)](LICENSE)

> **⚠️ BETA** — This integration is under active development. Use at your own risk.

Home Assistant integration for fleet management of PVAutonomy edge devices (ESP32-based solar inverter bridges).

## Features

- **Device Discovery** — Automatic detection of PVAutonomy edge devices on the local network
- **OTA Firmware Updates** — One-click firmware flashing via espota2 protocol (SHA256 auth)
- **Operational Sensors** — Real-time device status, firmware version, connectivity health
- **Flash Guard** — 7-stage state machine with safety gates (pre-flight checks, rollback detection)
- **Contract-Based API** — Stable entity surface defined by [Ops Contract v1](https://github.com/PVAutonomy/pvautonomy-ops/wiki)

## Requirements

- Home Assistant **2025.12.0** or newer
- HACS installed
- PVAutonomy edge device(s) on the same network

## Installation via HACS

1. Open HACS in your Home Assistant instance
2. Click the **⋮** menu (top right) → **Custom repositories**
3. Add this repository URL:
   ```
   https://github.com/PVAutonomy/pvautonomy-ops
   ```
   Category: **Integration**
4. Click **Add**
5. Search for **PVAutonomy Ops** in HACS and install it
6. Restart Home Assistant
7. Go to **Settings → Devices & Services → Add Integration → PVAutonomy Ops**

## Configuration

The integration is configured via the Home Assistant UI (config flow). No YAML configuration needed.

## Entity Overview

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.pvautonomy_ops_status` | Sensor | Overall system status |
| `sensor.pvautonomy_ops_device_count` | Sensor | Number of discovered devices |
| `button.pvautonomy_ops_flash_*` | Button | Trigger firmware flash for a device |

> Full entity list defined in the Ops Contract v1.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Device not discovered | Ensure edge device is on the same subnet and ESPHome API is reachable |
| Flash fails at Stage 3 | Pre-flight gate failed — check `flash_stage` attribute for details |
| OTA upload timeout | Verify port 3232 is not blocked; check device is not already flashing |

## Development

This integration is developed as part of the [PVAutonomy](https://github.com/PVAutonomy) ecosystem.

- Firmware: [pvautonomy-firmware](https://github.com/PVAutonomy/pvautonomy-firmware)
- OTA Staging: [pvautonomy-ota-staging](https://github.com/PVAutonomy/pvautonomy-ota-staging)

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
