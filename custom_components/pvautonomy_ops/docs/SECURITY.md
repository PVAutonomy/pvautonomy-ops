# Security & Privacy

## Architecture Overview

PVAutonomy uses a **proxy architecture** to keep your system secure:

```
Your Home Assistant  --->  PVAutonomy Proxy  --->  GitHub Actions
     (no PAT)            (Cloudflare Worker)       (private repo)
```

Your Home Assistant system **never** holds a GitHub Personal Access Token (PAT). The proxy acts as a secure intermediary.

### Dependencies

The integration installs without any install-time dependency resolution:

- `manifest.json` declares `requirements = []` — no pip packages are fetched when the integration is installed.
- `pyhpke` is **vendored** in-tree under `custom_components/pvautonomy_ops/_vendor/pyhpke/**`, and its MIT license is carried alongside it (`_vendor/pyhpke/LICENSE`).
- As a result, no `pyhpke` / `cryptography` pip resolution happens at installation time, which avoids dependency-conflict failures against the Home Assistant Core `cryptography` pin.

## What data is sent

When you trigger a firmware build, the following metadata is sent to the proxy:

| Data | Example | Purpose |
|------|---------|---------|
| Customer ID | *(server-derived)* | Identifies your installation. **Not** client-supplied: the proxy derives it server-side from your authenticated `pva_...` API key via `/whoami`. The client cannot freely set or spoof `customer_id`. |
| Device key | `a1b2c3` | MAC suffix of your Edge101 |
| Model | `edge101` | Hardware family |
| Registry file | `inverters/growatt/sph/sph10k.json` | Which inverter configuration to use |
| Device name | `sph-haus-01` | Name for the firmware build |
| `defs_version` | `1.0.0` | Provenance of the bundled firmware definitions used to generate the build |
| `yaml_hash` | *(hash)* | Integrity binding — guarantees the build service compiles exactly the YAML the integration generated from the bundled firmware definitions, byte-for-byte |

### `/whoami` and customer identity

The integration calls the proxy's `/whoami` endpoint, which is authenticated by your `pva_...` API key. The proxy returns the `customer_id` that is bound to that key on the server side. Your installation never asserts its own identity — identity is derived from the key, not from anything the client sends.

## What is NOT sent

- No WiFi passwords or network credentials
- No Home Assistant login credentials
- No device telemetry or sensor data
- No personal information
- No ESPHome API encryption keys
- No OTA passwords

## Firmware definitions are bundled

Firmware definitions ship **inside the integration release** — they are not distributed through your Home Assistant `/config` directory:

- Bundled location: `custom_components/pvautonomy_ops/data/firmware_defs/`.
- A normal customer installation needs **no** definitions under `/config/inverter-registry` or `/config/esphome`.
- Those `/config` paths are **no longer a product API or distribution path**. They are not consulted on a normal install.
- A legacy `/config`-based fallback (D8) still exists only as a **migration-only** path for older setups; it is not the target architecture and is being removed.

This keeps the install account-free and self-contained: the definitions that produce your firmware travel with the versioned integration release (see `defs_version`).

## API Key Security

Your Proxy API key (`pva_...`) is the **Managed Build Service key**. It is currently **provisioned by your PVAutonomy provider / operator** — it is provided out-of-band. Installing the open-source integration via HACS does **not** by itself grant access to the Managed Build Service; that requires a valid `pva_...` key.

The key grants the following permissions within the allowed scope:

- Trigger firmware builds (max 10/day)
- Check build status
- Download firmware artifacts

It does **not** grant:
- Access to other customers' builds
- Write access to any GitHub repository
- Access to the proxy admin functions

### If your API key is compromised

Contact your PVAutonomy provider immediately. They can **revoke and rotate** the key server-side and issue a new one within minutes. Revocation is provider-side; there is no self-service rotation in the integration today.

## COMPILE_SECRET_KEY

`COMPILE_SECRET_KEY` is a **separate** secret from the `pva_...` API key:

- It is currently **provider- / operator-provisioned** (out-of-band).
- It must **never** be shared in issues, logs, screenshots, or chat.
- A customer-friendly self-service onboarding flow for this key is a planned **follow-up** — it is not finished yet, so do not assume self-service provisioning exists today.

## Firmware Integrity

Every firmware download is verified before flashing:

1. **SHA-256 hash** — computed during download, compared against the build manifest
2. **Size check** — file size must match expected bytes
3. **Content-Length pre-check** — verified before download starts
4. **Flash guards** — minimum size threshold rejects stub/corrupt firmware

In addition, the `yaml_hash` sent with the build request binds the build to the exact YAML generated from the bundled firmware definitions, so the artifact you receive corresponds to the inputs your integration produced.

If any check fails, the firmware is **not** flashed and an error is shown.

## Build Isolation

- Each customer's builds are isolated by customer ID (server-derived; see `/whoami`).
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
