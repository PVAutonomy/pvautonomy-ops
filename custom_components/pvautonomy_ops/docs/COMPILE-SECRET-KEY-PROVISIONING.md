# Provisioning the compile_secret_key (operator runbook)

## Purpose and audience

This is an **operator / beta runbook**, not a customer self-service guide. The
`compile_secret_key` is currently **provider- / operator-provisioned**; there
is no in-product self-service onboarding for it today (customer-friendly
onboarding is tracked as a follow-up).

`compile_secret_key` is the repo-wide **AES-256** key (64 hex / 32 bytes) that
Home Assistant uses to AES-256-GCM-encrypt the per-build compile secrets
(per-device API encryption key + OTA password) before they are sent to the
proxy. The GitHub Actions firmware-build workflow decrypts them with the
**matching** repository secret.

> The key value is sensitive. Treat it like a private key. It must never be
> committed, pasted into chat or issues, screen-shared, or logged. This
> integration only ever logs a non-secret `sha256(key)[:8]` fingerprint.

## Customer handover prerequisites

Before handing a system over to a customer where firmware builds are expected:

1. **Provision `compile_secret_key`** on the target HA instance (see
   §*Safe provisioning procedure* below).
2. **Verify presence** via Developer Tools → Actions →
   `pvautonomy_ops.compile_secret_key_status`:
   expected response: `{present: true, fingerprint: <8 hex chars>}`.
   Do not report or log the key itself — fingerprint only.
3. **Do not hand over without this step** — customers cannot and must not enter
   `compile_secret_key`. It is an operator/build-service secret with no
   customer-facing wizard field.

### Paths that require `compile_secret_key` (firmware build)

| Path | Notes |
|------|-------|
| Wizard → *Neues Gerät erstmalig einrichten* | Full setup build + flash |
| Setup Dashboard → *Firmware vorbereiten* | Calls `pvautonomy_ops.build_firmware` |
| `pvautonomy_ops.build_firmware` service | Explicit build-only service call |
| Build/Flash button (`PVAutonomyOpsFlashButton`) | Build + OTA operator button |

Without a provisioned key all of the above fail closed before any `/build`
request is sent — no plaintext secret is transmitted.

### Paths that do **not** require `compile_secret_key`

| Path | Notes |
|------|-------|
| Wizard → *Schon laufendes Gerät nachträglich registrieren* | Adoption only, no build |
| `pvautonomy_ops.install_prepared_firmware` | Install-only, no build |
| `pvautonomy_ops.refresh_customer_dashboard` | UI/metadata rebuild, no build |

These paths are customer-safe and can be used even when the key is absent.

### Future direction

HPKE envelope mode (`secret_envelope.py`) is scaffolded but **not active today**
(`ROOT_PUBKEYS_PINNED = {}`). Once HPKE is activated, the symmetric
`compile_secret_key` will no longer be required for proxy builds, removing this
operator provisioning step entirely. Until then, the steps above apply.

## Current architecture (pvautonomy_ops 0.4.16)

Context for where this key fits in the current build model:

- Builds run through the **`proxy_remote`** backend (HA → proxy → GitHub
  Actions); Home Assistant never holds a GitHub PAT.
- Customer identity is **server-derived via `/whoami`** from the authenticated
  `pva_...` Managed Build Service API key — the client does not assert it.
- Firmware definitions are **bundled in the integration release** under
  `data/firmware_defs/**`. `/config/inverter-registry` and `/config/esphome`
  are **no longer** product API or distribution paths.
- `defs_version` is sent as a **provenance** metadatum for the bundled defs;
  `yaml_hash` is the **integrity binding** so the runner compiles exactly the
  YAML the integration generated, byte-for-byte.
- `manifest.json` declares `requirements = []`; `pyhpke` is **vendored**
  in-tree. The vendored `pyhpke` backs the HPKE envelope code, which is present
  but **not active today** (see *Crypto model*).

See `SECURITY.md` in this directory for the customer-facing security overview.

## Key roles and separation

These are distinct secrets/values — do **not** conflate them:

| Name | What it is | Where it lives | Customer-visible? |
|------|------------|----------------|-------------------|
| `pva_...` Managed Build Service API key | Authenticates this installation to the proxy; build/status/artifact scope only | HA integration options | Provisioned out-of-band by provider |
| `compile_secret_key` (HA-stored) | Repo-wide AES-256 key HA uses to encrypt per-build compile secrets | `PVAutonomyKeyring` (HA Store) only | No — operator-provisioned |
| GitHub Actions secret `COMPILE_SECRET_KEY` | The **matching** value the build workflow decrypts with | GHA repository secret (backend) | No — backend detail |
| per-device API encryption key | ESPHome API encryption key compiled into one device's firmware | Inside the device firmware | No |
| per-device OTA password | OTA auth secret compiled into one device's firmware | Inside the device firmware | No |
| `defs_version` | Provenance of the bundled firmware definitions | Build payload metadata | Non-secret |
| `yaml_hash` | Integrity binding of the generated YAML | Build payload metadata | Non-secret |

The HA-stored `compile_secret_key` and the GHA `COMPILE_SECRET_KEY` repository
secret are **two copies of the same value on two sides**. The `pva_...` API key
and the per-device secrets are unrelated to it.

## Hard requirement

The operative path today is **Legacy AES-256-GCM**. For it:

The value provisioned into Home Assistant **must byte-for-byte equal** the
GitHub Actions repository secret `COMPILE_SECRET_KEY`. Provision the GHA repo
secret **out-of-band** (GitHub → Settings → Secrets and variables → Actions)
using the same value.

- The repository that holds this GHA secret (`PVAutonomy/inverter-registry`) is
  a **backend implementation detail** — not a customer or product API path.
  Operators/customers do not interact with that repo as a product surface.
- If the two values differ, every build fails to decrypt (`InvalidTag`).
- If no valid 64-hex key is provisioned, the build **fails closed before the
  `/build` POST** — no plaintext compile secret is ever transmitted.

## Crypto model

**Operative path today — Legacy AES-256-GCM:**

- Repo-wide key, 64 hex / 32 bytes.
- Home Assistant encrypts the per-build compile secrets HA-side before they
  leave HA.
- The proxy forwards the encrypted blob unchanged; the GitHub Actions workflow
  decrypts it with the matching `COMPILE_SECRET_KEY` repo secret.
- This is the **default** and currently the only live secret-bearing path.

**Forward path — HPKE `compile_secret_envelope` (gated, not active today):**

- The HPKE envelope code exists in the integration as a **gated,
  forward-compatible mechanism**. It is **not** the active customer path today.
- Activating it requires both a **pinned production root anchor** and a
  **proxy keyset endpoint**. Today: no pinned production root is shipped, and
  no proxy keyset route is deployed. The proxy can validate and forward a
  `compile_secret_envelope`, but the integration cannot produce one in
  production until both prerequisites exist.
- This runbook will need to be updated **before** HPKE is activated (the
  provisioning and hard-requirement sections change when the envelope path
  goes live).

## Safe provisioning procedure

1. **Use Developer Tools → Actions (UI) only.**
   Do **not** call `pvautonomy_ops.set_compile_secret_key` from an automation,
   script, scene, or `rest`/`websocket` command. Automation/script engines
   may persist `service_data` (and therefore the key) in traces, the recorder
   database, or logbook.
2. **Avoid YAML mode for the call** where the key would be typed into a
   shareable text blob. Prefer the UI field (it uses a password selector).
   Do not screen-share or record while entering the key, and clear any
   clipboard manager history afterward.
3. **Verify capture surfaces first.** Before entering the key, confirm no
   custom integration, recorder include, logbook, or `event`/`call_service`
   listener captures `call_service` event data for the `pvautonomy_ops`
   domain. The recorder stores `call_service` events by default only as
   metadata, but custom listeners or debug logging can capture `service_data`.
4. **Call the service.**
   `pvautonomy_ops.set_compile_secret_key`
   - `entry_id`: the target config entry id
   - `compile_secret_key`: the 64-hex value (same as the GHA repo secret)
5. **Verify (fingerprint only).**
   Call `pvautonomy_ops.compile_secret_key_status` (response-only). Expect
   `{present: true, fingerprint: <sha256[:8]>}`. Compare the fingerprint to
   the expected one computed out-of-band from the GHA secret value
   (`python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode('ascii')).hexdigest()[:8])" <KEY>`,
   run on a trusted host). Never compare raw key bytes through HA.

## Rotation / compromise response

If the key is exposed (logs, screen share, trace, clipboard, etc.), treat it as
compromised and rotate **both sides**:

1. `pvautonomy_ops.clear_compile_secret_key` (HA stops sending secrets;
   builds fail closed).
2. Rotate the GitHub Actions `COMPILE_SECRET_KEY` repo secret (backend) to a
   new value.
3. Re-provision the new value into HA via `set_compile_secret_key` (steps
   above).
4. Because per-device API/OTA secrets are compiled into firmware, also rotate
   and reflash any device whose secrets were exposed (tracked separately as the
   device-secret-rotation task; internal tracking reference only).

## Notes / limitations

- **Storage:** the key lives only in `PVAutonomyKeyring` (HA Store), never in a
  config/options field, env var, or `secrets.yaml` reference.
- **Fail-closed:** without a provisioned key, secret-bearing proxy builds fail
  closed before the build request is sent — no plaintext secret is transmitted.
- **Access:** installing the open-source integration via HACS does **not** by
  itself grant Managed Build Service access; that needs a valid `pva_...` key.
- **Onboarding:** customer-friendly onboarding for both the `pva_...` API key
  and `compile_secret_key` is a planned follow-up — do not assume self-service
  provisioning exists today.
