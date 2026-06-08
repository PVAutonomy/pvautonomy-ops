# Setup Guide

After installation, configure PVAutonomy to build and flash firmware for your Edge101 devices.

## Installation

For the current deployment cycle, `pvautonomy_ops` is installed as a **local custom component** bundled into the customer master image at `/config/custom_components/pvautonomy_ops/`.

HACS is **not required for installing `pvautonomy_ops`** in this cycle. HACS-based distribution of `pvautonomy_ops` is the planned product direction and will be validated separately. Note that the broader master image may still use HACS for other components (frontend cards, third-party integrations).

## Preflight Checklist

Before your first build, verify these items:

- [ ] **Proxy API key ready:** You have a key starting with `pva_` (40 hex characters). This key is **required** before starting the wizard — obtain it from your PVAutonomy provider before the system is set up.
- [ ] **Proxy reachable:** Open `https://pvautonomy-proxy.pvautonomy-proxy.workers.dev/health` in a browser. You should see `{"status":"ok",...}`.
- [ ] **Customer ID:** Leave empty — it is automatically derived from your installation. Only set if your provider explicitly gives you a specific one.
- [ ] **Edge101 online:** Your device is powered on and visible in Settings > Devices & Services > ESPHome.
- [ ] **Expected build time:** A firmware build takes **8-10 minutes** (GitHub Actions). The first build may take slightly longer.

## Open Options

1. Go to **Settings > Devices & Services**.
2. Find **PVAutonomy** and click **Configure**.

## Configure the Build Backend

Set **Build backend** to `proxy_remote`. This uses the PVAutonomy cloud proxy to compile firmware via GitHub Actions.

### Required fields

| Field | Value | Notes |
|-------|-------|-------|
| **Build backend** | `proxy_remote` | Cloud-based firmware compilation |
| **Proxy API key** | `pva_...` | Your API key from your PVAutonomy provider |

### Optional fields

| Field | Default | Notes |
|-------|---------|-------|
| **Proxy URL** | `https://pvautonomy-proxy...workers.dev` | Only change if directed by your provider |
| **Customer ID** | *(empty)* | Leave empty to auto-derive from your installation. Only set if your provider gives you a specific ID |

## Select a Device

Choose your Edge101 device from the **Active device** dropdown. The list is populated automatically from discovered devices in your Home Assistant instance.

If no devices appear:
- Ensure your Edge101 is powered on and connected to WiFi.
- Check that ESPHome can see the device (Settings > Devices > ESPHome).
- Wait for the next discovery cycle (default: 60 seconds).

## Trigger a Build

Once the proxy backend is configured and a device is selected:

1. Use the **Build Production Firmware** button in the PVAutonomy integration panel.
2. The build is dispatched to GitHub Actions via the proxy.
3. **Expected build time: 8-10 minutes.**
4. Progress is shown in the integration status sensor (`sensor.pvautonomy_ops_status`).

### Build lifecycle

```
Queued  -->  Compiling  -->  Success  -->  Ready to Flash
                              |
                              v
                           Failed (check logs)
```

## Flash Firmware

After a successful build:

1. The firmware artifact is downloaded and verified (SHA-256 hash check).
2. Use the **Flash Production Firmware** button.
3. The device will reboot after flashing (~30 seconds offline).
4. Verify the device comes back online with the new firmware.

## Other Options

| Option | Description |
|--------|-------------|
| **Poll interval** | How often device status is refreshed (10-300 sec) |
| **Firmware channel** | `stable` for production, `beta` for testing |
| **Minimum firmware size** | Reject firmware smaller than this (stub protection, default: 300 KB) |
| **Require gates** | If enabled, readiness gates must pass before flashing |
| **Gates freshness** | How long gate results remain valid (1-60 min) |

## Next Steps

- [Troubleshooting](TROUBLESHOOTING.md) if something goes wrong
- [Security](SECURITY.md) to understand what data is sent
- [FAQ](FAQ.md) for common questions
