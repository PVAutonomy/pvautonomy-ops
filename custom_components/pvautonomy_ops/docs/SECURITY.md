# Security & Privacy

## Architecture Overview

PVAutonomy uses a **proxy architecture** to keep your system secure:

```
Your Home Assistant  --->  PVAutonomy Proxy  --->  GitHub Actions
     (no PAT)            (Cloudflare Worker)       (private repo)
```

Your Home Assistant system **never** holds a GitHub Personal Access Token (PAT). The proxy acts as a secure intermediary.

## What data is sent

When you trigger a firmware build, the following metadata is sent to the proxy:

| Data | Example | Purpose |
|------|---------|---------|
| Customer ID | `auto-derived-uuid` | Identify your installation |
| Device key | `a1b2c3` | MAC suffix of your Edge101 |
| Model | `edge101` | Hardware family |
| Registry file | `inverters/growatt/sph/sph10k.json` | Which inverter configuration to use |
| Device name | `sph-haus-01` | Name for the firmware build |

## What is NOT sent

- No WiFi passwords or network credentials
- No Home Assistant login credentials
- No device telemetry or sensor data
- No personal information
- No ESPHome API encryption keys
- No OTA passwords

## API Key Security

Your Proxy API key (`pva_...`) grants the following permissions:

- Trigger firmware builds (max 10/day)
- Check build status
- Download firmware artifacts

It does **not** grant:
- Access to other customers' builds
- Write access to any GitHub repository
- Access to the proxy admin functions

### If your API key is compromised

Contact your PVAutonomy provider immediately. They can revoke the key and issue a new one within minutes.

## Firmware Integrity

Every firmware download is verified before flashing:

1. **SHA-256 hash** — computed during download, compared against the build manifest
2. **Size check** — file size must match expected bytes
3. **Content-Length pre-check** — verified before download starts
4. **Flash guards** — minimum size threshold rejects stub/corrupt firmware

If any check fails, the firmware is **not** flashed and an error is shown.

## Build Isolation

- Each customer's builds are isolated by customer ID.
- The proxy enforces API key / customer ID binding (403 on mismatch).
- Build concurrency is limited to 1 per customer (409 on conflict).
- Daily build rate is limited (429 on excess).

## Operational Limits

| Limit | Value |
|-------|-------|
| Builds per day | 10 (default) |
| Concurrent builds | 1 |
| Build timeout | 15 minutes |
| Max payload size | 64 KB |
