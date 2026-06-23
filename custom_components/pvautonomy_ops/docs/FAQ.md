# Frequently Asked Questions

## General

### What is PVAutonomy?

PVAutonomy is a Home Assistant integration for managing Edge101 devices — ESPHome-based bridges that connect solar inverters (Growatt, Huawei, etc.) to Home Assistant via Modbus.

### Do I need a GitHub account?

No. The PVAutonomy proxy handles all GitHub interaction on your behalf. You only need an API key from your PVAutonomy provider.

### What inverters are supported?

Currently supported:
- Growatt SPH series (SPH10K)
- Growatt MIC series (MIC600)

More inverter models are being added to the registry.

## Setup

### Where do I get my API key?

Your PVAutonomy provider issues the **Managed Build Service API key** (shown as
`pva_...`) **before** you set up the system. You enter it in the setup wizard
when adding the PVAutonomy integration.

This key is separate from the `COMPILE_SECRET_KEY` (a different,
operator-/provider-provisioned secret). Installing the integration — via the
Installer/Updater add-on or HACS — does **not** by itself produce an API key;
the provider issues it.

Without a valid API key the wizard cannot build firmware. Make sure you have
your key ready before starting the setup.

### What is the Customer ID?

A unique identifier for your installation. **Leave it empty** — it is automatically derived from your Home Assistant installation via the proxy's `/whoami` endpoint. Only set it if your provider explicitly gives you a specific one.

### Is HACS required for PVAutonomy?

It depends on the path you choose — both are supported and deliver the same
0.4.16 release artifact:

- **Normal customers:** no — the preferred path is the PVAutonomy
  Installer/Updater add-on (`stable` channel), which does not use HACS.
- **Developers / power-users:** HACS (`stable`) is supported, via the custom
  repository `PVAutonomy/pvautonomy-ops`.

(HACS distribution was previously described as planned; it is now live and
validated.) See [Installation](INSTALLATION.md) for both paths.

### Can I use multiple Edge101 devices?

Yes. All discovered Edge101 devices appear in the **Active device** dropdown. Select the device you want to manage. You can switch between devices at any time.

## Builds

### How long does a firmware build take?

Typically **8-10 minutes**. The firmware is compiled by GitHub Actions in the cloud. Build progress is shown in the integration status.

### What ESPHome version is used?

The build workflow pins ESPHome to version **2025.12.0**. This is managed by PVAutonomy and updated as part of workflow maintenance.

### Can I build firmware locally?

The `proxy_remote` backend builds firmware in the cloud. For local builds, you can switch to the `esphome_dashboard` backend if you have the ESPHome add-on installed, but this is not recommended for production use.

### What happens if a build fails?

Check the error message in the integration logs. Common causes:
- GitHub Actions runner unavailable (retry after a few minutes)
- Registry file not found (contact your provider)
- ESPHome compilation error (contact your provider)

## Security & Privacy

### Is my data private?

Yes. Only build request metadata (device name, inverter model) is sent to the proxy. No credentials, telemetry, or personal data leaves your system. See [Security](SECURITY.md) for details.

### What if my API key is compromised?

Contact your PVAutonomy provider immediately. They can revoke the key and issue a new one.

## Troubleshooting

### The integration shows "degraded" status

This means one or more expected entities are missing. Check:
- Is your Edge101 device online?
- Are ESPHome entities visible in Home Assistant?
- Check the `last_error` attribute on `sensor.pvautonomy_ops_status` for details.

### I see raw key names in the Options dialog

*(Historical — affects only very old builds.)* Update to the current version of
PVAutonomy. Translation files were completed in v0.2.0+ to include all option
labels; current releases (0.4.x) are unaffected.

For more issues, see [Troubleshooting](TROUBLESHOOTING.md).
