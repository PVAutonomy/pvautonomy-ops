# Troubleshooting

## Error Reference

| Error | Meaning | What to do |
|-------|---------|------------|
| **401 Unauthorized** | API key is invalid or missing | Go to **Configure** and check your Proxy API key. It must start with `pva_`. |
| **403 Forbidden** | Customer ID does not match the API key | Leave the **Customer ID** field empty (auto-derive) or contact your PVAutonomy provider. |
| **409 Conflict** | A build is already in progress | Wait for the current build to finish before starting a new one. |
| **429 Too Many Requests** | Daily build limit reached (10 builds/day) | Try again tomorrow. Contact your provider if you need a higher limit. |
| **502 Bad Gateway** | GitHub Actions workflow dispatch failed | Check [GitHub Status](https://www.githubstatus.com/) for outages. Retry after a few minutes. |
| **Timeout** | Build exceeded 15 minutes | Retry the build. If it keeps timing out, the GitHub Actions runner may be overloaded. |
| **Hash mismatch** | Firmware integrity check failed | **Do NOT flash.** Retry the build. If the issue persists, contact your provider. |

## Version & setup errors (0.4.x)

Most of the issues below were resolved at or before the `pvautonomy_ops` 0.4.16
stable release. The first fix to try is almost always: **update to 0.4.16
stable** (via the Installer/Updater add-on or HACS — both deliver the same
release). See [Installation](INSTALLATION.md).

### Install / startup fails: `pyhpke` / `cryptography` dependency conflict

**Symptom:** Installation or Home Assistant startup fails with a `pyhpke` /
`cryptography` resolver / dependency-conflict error. Older pre-0.4.16 builds
could collide with the Home Assistant Core `cryptography` pin.

**Fix:**
- Update to `pvautonomy_ops` **0.4.16 stable**.
- 0.4.16 declares `manifest.json` `requirements = []` and vendors `pyhpke`
  in-tree, so there is no install-time `pyhpke` / `cryptography` resolution.
- Do **not** pip-install `pyhpke` or `cryptography` manually.

### `customer_id_missing` during setup or build

**Symptom:** The config flow or a build aborts because `customer_id` is missing.

**Fix:**
- Confirm the current **0.4.16** version is installed.
- Confirm your Managed Build Service API key (`pva_...`) is entered correctly in
  the integration options.
- The proxy's `/whoami` endpoint must be reachable.
- `customer_id` is **derived server-side** from the API key — leave the Customer
  ID field empty; do not try to set it manually.

### `HTTP 400 payload.defs_version is not a known field`

**Symptom:** A build request fails with HTTP 400 reporting an unknown
`defs_version` field.

**Fix:**
- This was a proxy/version mismatch from before the final 0.4.16 stable state.
- The current proxy accepts `defs_version` (sent as provenance metadata).
- Use the current **stable channel (0.4.16)**.
- If it recurs, check the proxy/service version — do **not** create local
  `/config` firmware definitions as a workaround.

### D8 fallback warning / `/config` firmware definitions

**Symptom:** A warning about a "D8 fallback" or about firmware definitions under
`/config`.

**What it means:**
- The normal customer path uses the **bundled** definitions under
  `data/firmware_defs/**`. `/config/inverter-registry` and `/config/esphome` are
  **no longer** product distribution paths.
- A D8 warning is a **migration/legacy** hint, not a normal-path requirement.
- Do **not** create `/config` definitions as a standard fix; running the current
  bundled release is the supported path. (Removal of the D8 fallback is tracked
  separately as a later code cleanup.)

### `COMPILE_SECRET_KEY` missing or invalid

**Symptom:** A secret-bearing build fails closed before the request is sent,
citing a missing or invalid compile key.

**Fix / notes:**
- A missing/invalid `COMPILE_SECRET_KEY` causes the build to **fail closed
  before the `/build` POST** — no plaintext secret is transmitted. This is by
  design.
- `COMPILE_SECRET_KEY` is **not** the same as the `pva_...` API key — it is a
  separate secret, provisioned out-of-band by your provider/operator.
- Verify it via the fingerprint-only status (`compile_secret_key_status`); never
  compare raw key bytes, and never share the key in logs, screenshots, or chat.
- Full procedure:
  [COMPILE-SECRET-KEY-PROVISIONING.md](COMPILE-SECRET-KEY-PROVISIONING.md).

### Wrong installation path / "where did my install go?"

- **Customer / app path:** PVAutonomy Installer/Updater add-on (`stable`).
- **Developer / HACS path:** HACS (`stable`).
- Both deliver `pvautonomy_ops` 0.4.16. Installing via HACS or the add-on does
  **not** by itself create a Managed Build Service API key — the provider issues
  that separately.

## Common Issues

### Proxy unreachable

**Symptom:** "Proxy unreachable" or connection timeout errors.

**Fixes:**
- Check your internet connection.
- Verify the **Proxy URL** in Configure is correct (default: `https://pvautonomy-proxy.pvautonomy-proxy.workers.dev`).
- Try opening the proxy health endpoint in a browser: `https://pvautonomy-proxy.pvautonomy-proxy.workers.dev/health`

### No devices found

**Symptom:** Active device dropdown is empty.

**Fixes:**
- Ensure your Edge101 device is powered on and connected to WiFi.
- Check **Settings > Devices & Services > ESPHome** — the device should appear there.
- Wait 60 seconds for the next discovery cycle.
- Check Home Assistant logs for ESPHome connection errors.

### Device offline during flash

**Symptom:** Flash fails with "device offline" error.

**Fixes:**
- Ensure the device is connected and showing as online in ESPHome.
- Move the device closer to your WiFi access point.
- Check device power supply.

### Flash rejected by guards

**Symptom:** "Gates must pass before flash" error.

**Fixes:**
- Run the readiness gates first (Run Gates button).
- If gates fail, check the failed gate details in the button attributes.
- Disable **Require gates to pass before flash** in Options if you want to skip gate checks (not recommended).

### Build succeeds but firmware is wrong

**Symptom:** Device behaves unexpectedly after flashing.

**Fixes:**
- Verify you selected the correct device in the Active device dropdown.
- Check the firmware version in the build status matches what you expected.
- The firmware is built from the inverter registry — contact your provider if the configuration is wrong.

## Collecting Logs

For support requests, collect these logs:

1. **Home Assistant logs:**
   Settings > System > Logs > search for `pvautonomy`

2. **Integration status:**
   Check `sensor.pvautonomy_ops_status` in Developer Tools > States

3. **Build status:**
   After a failed build, note the build ID from the logs.

---

# Fehlerbehebung (Deutsch)

## Fehler-Referenz

| Fehler | Bedeutung | Loesung |
|--------|-----------|---------|
| **401 Unauthorized** | API-Schluessel ungueltig oder fehlt | Unter **Konfigurieren** den Proxy-API-Schluessel pruefen. Muss mit `pva_` beginnen. |
| **403 Forbidden** | Kunden-ID stimmt nicht mit API-Schluessel ueberein | **Kunden-ID** Feld leer lassen (automatische Ableitung) oder PVAutonomy-Anbieter kontaktieren. |
| **409 Conflict** | Ein Build laeuft bereits | Warten bis der aktuelle Build abgeschlossen ist. |
| **429 Too Many Requests** | Tageslimit erreicht (10 Builds/Tag) | Morgen erneut versuchen. Anbieter kontaktieren fuer hoeheres Limit. |
| **502 Bad Gateway** | GitHub Actions Workflow-Start fehlgeschlagen | [GitHub Status](https://www.githubstatus.com/) pruefen. Nach einigen Minuten erneut versuchen. |
| **Timeout** | Build hat 15 Minuten ueberschritten | Build erneut starten. Bei wiederholtem Timeout kann der GitHub Actions Runner ueberlastet sein. |
| **Hash-Fehler** | Firmware-Integritaetspruefung fehlgeschlagen | **NICHT flashen.** Build erneut starten. Bei anhaltendem Problem Anbieter kontaktieren. |

## Haeufige Probleme

### Proxy nicht erreichbar
- Internetverbindung pruefen.
- **Proxy-URL** in den Optionen pruefen.
- Proxy-Health-Endpunkt im Browser testen.

### Keine Geraete gefunden
- Edge101 eingeschaltet und mit WiFi verbunden?
- Unter **Einstellungen > Geraete & Dienste > ESPHome** pruefen.
- 60 Sekunden auf naechsten Discovery-Zyklus warten.

### Flash wird von Gates abgelehnt
- Zuerst Readiness Gates ausfuehren.
- Details der fehlgeschlagenen Gates in den Button-Attributen pruefen.

## Log-Informationen sammeln

1. **Home Assistant Logs:** Einstellungen > System > Protokolle > nach `pvautonomy` suchen
2. **Integrationsstatus:** `sensor.pvautonomy_ops_status` in Entwicklerwerkzeuge > Zustaende
