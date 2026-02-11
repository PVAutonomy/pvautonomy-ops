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

After adding the integration, open the integration options:
**Settings → Devices & Services → PVAutonomy Ops → Configure**

Key options:
- **`channel`**: `stable` (production default) or `beta` (testing)
- **`min_firmware_size_kb`**: minimum allowed firmware size (guard against test stubs)

> **Tip:** For production, keep `channel=stable` and set a sensible `min_firmware_size_kb` (e.g. 1500–2000 KB depending on device).

---

## Optional: Operator Dashboard (Lovelace YAML Example)

HACS installs only the integration under `custom_components/`.  
The Lovelace dashboard is a **manual** (copy/paste) deployment artifact.

### 1) Copy the example dashboard file

Copy from this repository:
- [`examples/pvautonomy-ops-dashboard.yaml`](examples/pvautonomy-ops-dashboard.yaml)

to your Home Assistant config folder:
- `/config/lovelace/pvautonomy-ops-dashboard.yaml`

### 2) Register the dashboard in `configuration.yaml`

Add (see also [`examples/configuration-snippet.yaml`](examples/configuration-snippet.yaml)):

```yaml
lovelace:
  dashboards:
    pvautonomy-ops:
      mode: yaml
      title: "🏭 PVAutonomy Ops"
      icon: mdi:solar-power-variant
      show_in_sidebar: true
      filename: lovelace/pvautonomy-ops-dashboard.yaml
```

### 3) Restart Home Assistant

After restart you should see an **Operations** dashboard in the sidebar.

> **Timestamp note:** All timestamps in the dashboard are shown in local time.
> Home Assistant Developer Tools may display raw attributes in UTC — this is expected.

## Entity Overview

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.pvautonomy_ops_status` | Sensor | Overall system status (Output G) |
| `sensor.pvautonomy_ops_devices_count` | Sensor | Discovered devices with online/offline counts (Output H) |
| `button.pvautonomy_ops_discover` | Button | Trigger device discovery (Output I) |
| `button.pvautonomy_ops_run_gates` | Button | Run pre-flight safety gates (Output J) |
| `button.pvautonomy_ops_flash_production` | Button | Flash firmware to target device (Output K) |
| `button.pvautonomy_ops_restart_device` | Button | Restart target device (Output L) |

> Full entity list and attributes defined in the [Ops Contract v1.0.0](https://github.com/PVAutonomy/pvautonomy-ops/wiki).

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
