# Provisioning the compile_secret_key (operator runbook)

`compile_secret_key` is the repo-wide **AES-256** key (64 hex / 32 bytes) that
Home Assistant uses to AES-256-GCM-encrypt the per-build compile secrets
(api encryption key + OTA password) before they are sent to the proxy. The
GitHub Actions firmware-build workflow decrypts them with the **matching**
repository secret.

> The key value is sensitive. Treat it like a private key. It must never be
> committed, pasted into chat, screen-shared, or logged. This integration only
> ever logs a non-secret `sha256(key)[:8]` fingerprint.

## Hard requirement

The value provisioned into Home Assistant **must byte-for-byte equal** the
GitHub Actions repository secret `COMPILE_SECRET_KEY` in
`PVAutonomy/inverter-registry`. If they differ, every build fails to decrypt
(`InvalidTag`). Provision the GHA repo secret **out-of-band** (GitHub →
Settings → Secrets and variables → Actions) using the same value.

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

## If the key is exposed (logs, screen share, trace, clipboard, etc.)

Treat it as compromised and rotate **both sides**:

1. `pvautonomy_ops.clear_compile_secret_key` (HA stops sending secrets;
   builds fail closed).
2. Rotate the GitHub Actions `COMPILE_SECRET_KEY` repo secret to a new value.
3. Re-provision the new value into HA via `set_compile_secret_key` (steps
   above).
4. Because per-device api/OTA secrets are compiled into firmware, also rotate
   and reflash any device whose secrets were exposed (tracked separately as
   the device-rotation task; e.g. `2eb1e4` under
   `TASK-20260520-EPIC006-COMPILE-SECRETS-PROTOCOL-LOG-EXPOSURE`).

## Notes

- Storage: the key lives only in `PVAutonomyKeyring` (HA Store), never in a
  config/options field, env var, or `secrets.yaml` reference.
- Without a provisioned key, secret-bearing proxy builds **fail closed**
  before the build request is sent — no plaintext secret is transmitted.
