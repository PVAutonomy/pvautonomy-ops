# Customer Path Validation

> **Reading note.** The P1 sections below (P1 … P1j, roughly up to v0.4.5) are
> **historical validation notes from before bundle-first / P2-f**. They are kept
> as the running record of that work. Where those older sections mention
> `/config/inverter-registry` or `/config/esphome` as a registry/firmware-defs
> path, that is **no longer the product path** — firmware definitions now ship
> bundled in the integration release (`data/firmware_defs/**`). The current state
> is documented at the end in **“v0.4.16 stable / P2-f validation”**.

## 2026-06-08 SPH Wizard/Firmware Proof Run

### Status
- Current run closed as proof/staging evidence.
- Not product acceptance yet under Customer Path First.

### Proven Technically
- Firmware build/install via Home Assistant Developer Tools works technically.
- After dashboard refresh, the SPH dashboard rendered correctly:
  - Export Limit Mode showed RS485.
  - Export Limit showed Active.
  - Export Limit Power Rate was visible and functional.
  - The missing Status entity disappeared after refresh.

### Observed New/Live Entities
- `sensor.pvautonomy_modbus_bridge_2eb1e4_wifi_signal`
- `sensor.pvautonomy_modbus_bridge_2eb1e4_uptime`
- `sensor.sph10k_home_02_export_limit_power_w_device`

### Customer Path Classification
- Developer Tools service calls and manual dashboard refresh are proof/staging steps only.
- This run does not yet prove customer acceptance.
- Product acceptance requires the same outcome through the supported customer path:
  setup/reconfigure wizard, firmware prepare/install flow where needed, and automatic or clearly customer-visible dashboard generation/refresh.

### Open Product Question
- Why was Build/Install/Refresh not end-to-end customer-tauglich through the Wizard / Setup Dashboard path?
- If the Wizard flow does not automatically trigger dashboard refresh, or does not clearly guide the customer to refresh after install, create a Customer-Path follow-up.

### Follow-Up Candidate
- Customer-path Dashboard Refresh after firmware install:
  ensure the Wizard or Setup Dashboard either triggers the refresh automatically after successful install, or presents a clear customer-visible refresh action with success/failure feedback.

### Resolution (P1 — fix/customer-path-dashboard-refresh-after-install)
- The `pvautonomy_ops.install_prepared_firmware` service handler now rebuilds the customer dashboard automatically after a successful install, by invoking the idempotent `refresh_customer_dashboard` service for the resolved entry/device. This removes the manual dashboard-refresh step from the customer path — both the Setup Dashboard install card and the support Developer-Tools path go through this same handler.
- The rebuild is best-effort: a refresh failure is logged (no secrets) and never fails the already-successful install, so the customer can retry the refresh action.
- Out of scope for this change: the WiFi/Uptime dashboard-surface follow-up.

### Resolution (P1b — fix/customer-path-dashboard-diagnostics)
- The dashboard builder now surfaces **Edge WiFi Signal** and **Edge Uptime** in the SPH Status card as optional, **live-gated** rows: each renders only when its sensor is actually live (preferring the deterministic bridge sensor `sensor.pvautonomy_modbus_bridge_{mac_suffix}_{wifi_signal|uptime}`), and is omitted otherwise — so no "Entität nicht gefunden" rows appear. Combined with the P1 auto-refresh, the observed bridge diagnostics now appear on a customer system through the supported path once they go live, without EDATEC-specific special-casing.
- `sensor.…_export_limit_power_w_device` stays out of the dashboard; Active Power Rate and Export Limit Power Rate are unchanged.
- Still separate later GOs: HACS/release of the builder code, deploy, build/install/flash, and live validation.

### Resolution (P1c — fix/dashboard-bridge-diagnostics-no-phantoms)
- Follow-up to P1b. After HACS v0.4.0 + Wizard Build/Flash + HA restart, the SPH Status card still showed two "Entität nicht gefunden" rows (Edge WiFi Signal / Edge Uptime). Root cause: `_resolve_bridge_diagnostic_entity_id` returned a **guessed** entity ID (the `mac_suffix` bridge name, or the synthetic `sensor.{device}_{key}_device`) whenever the live snapshot was **unknown** (`live_entity_ids is None`) — e.g. when `hass.states` could not be snapshotted at build time. Both optional diagnostics then rendered as non-existent guesses.
- Fix: optional bridge diagnostics now render **only** when their exact entity is present in live state. `live=None` (unknown snapshot) or no live match → omit. The `mac_suffix` / synthetic guess fallback for the unknown-snapshot case is removed; the "synthetic only when exactly live" path is unchanged.
- Proof: `pytest custom_components/pvautonomy_ops/tests/test_dashboard_builder.py` green, including a phantom-row matrix (both live / only WiFi / only Uptime / empty set / `None` snapshot / no synthetic fallback). `ruff` + `py_compile` clean.
- Product acceptance still pending (separate GO): live customer dashboard on a flashed SPH device showing the Status card with no missing-entity rows; WiFi/Uptime appear only when the firmware publishes them live. No HACS sync/release or device action in this GO.

### Resolution (P1f — fix/mic-dashboard-status-control)
- The MIC600 dashboard now renders a **Status** card (Inverter Status, Inverter Temperature) and a **Control** card (Active Power Rate). These three entities already exist in the MIC600 registry/generated firmware but the registry tags them `diagnostic` (status/temperature) and `config` (active_power_rate), so the generic classifier dropped them. The dashboard builder now promotes **exactly** these IDs for non-battery (MIC-style) builds via an explicit allow-list (`_MIC_STATUS_SURFACE` / `_MIC_CONTROL_SURFACE`), without changing the registry or the generated-firmware `entity_category`. No new firmware/ESPHome/Modbus entities are added.
- Raw/technical rows stay hidden: `modbus_unlock`, `save_modbus_write`, `dc_bus_voltage`, `modbus_version` are not allow-listed and remain excluded. SPH is unaffected — it keeps its own hybrid Status/Control builders (the `if not has_battery` gate skips SPH).
- Proof (this GO is proof/staging only): `pytest custom_components/pvautonomy_ops/tests/test_dashboard_builder.py` green (incl. new MIC Status/Control rows, raw-entity exclusion, and SPH non-leak regression). `ruff` + `py_compile` + `git diff --check` clean. No registry/generator/firmware edit; no HA/SSH/device/deploy/restart in this GO.
- Product acceptance still pending (separate GOs): config→ops sync, ops release v0.4.2, `stable.json` pin to v0.4.2, Add-on installer run + HA restart on the customer system, and live MIC Wizard build/install validation showing the Status + Control cards on a flashed MIC600 device with no missing-entity rows.

### Resolution (P1g — fix/mic-dashboard-status-current-format)
- Follow-up to P1f, making two existing MIC600 entities customer-readable. Protocol basis: Growatt MIC600 Modbus v3.14 §4.2 — Input Reg 00 (Inverter run state: 0=waiting, 1=normal, 3=fault) and Input Reg 15 (Iac1, unit 0.1A).
- **Inverter Status** (dashboard-only, reflash-invariant): the MIC Status card is now a markdown card that maps the raw value to a customer label — `0 → Standby`, `1 → Normal`, `3 → Fault`; `unknown`/`unavailable`/missing → `Unknown`; any other number → `Unknown (<raw>)`. The raw `1.0` is never shown. Pure helper `_mic_inverter_status_label` is unit-tested and mirrored by the card's Jinja. Inverter Temperature stays on the same card (with °C); no "Entität nicht gefunden" rows (markdown references existing entities only).
- **AC Current precision** (registry/generator metadata — firmware-surface): root cause was display precision, not the value. The registry already scales raw → 0.3 A, but `ac_current` carried no `accuracy_decimals`, so HA defaulted the entity's display precision to 0 and rounded 0.3 A → "0 A" everywhere (dashboard row, more-info, history). HA's core `entities` card has no per-row precision override, so this is not fixable in the dashboard builder. Fix at the smallest metadata point: `accuracy_decimals: 1` on the `ac_current` registry entry, plus a one-line generator change so `yaml_generator` propagates `accuracy_decimals` into the generated entity metadata. The Modbus register is unchanged (address 39, scale 0.1, unit A). No ESPHome component / firmware-register edit.
- Scope: `dashboard_builder.py`, `tests/test_dashboard_builder.py`, `inverter-registry/growatt/mic/mic600.json`, `custom_components/pvautonomy_ops/yaml_generator.py`, `tests/test_yaml_generator_mic600_guardrails.py`, this doc. No add-on/HACS/ops-release/stable.json change; no HA/SSH/device/deploy/restart/build/flash.
- Proof: `pytest custom_components/pvautonomy_ops/tests/test_dashboard_builder.py` green (185, incl. status-mapping matrix 0/1/3/unknown/unavailable + markdown Status card + SPH non-leak regression). `pytest tests/test_yaml_generator_mic600_guardrails.py` — the two new precision tests pass; 3 pre-existing `firmware_version` failures are unrelated to this change (reproduced on origin/main with these edits stashed). `ruff` + `py_compile` + `git diff --check` clean.
- Reflash dependency: the **Inverter Status** mapping is effective on a dashboard refresh. The **AC Current** precision only takes effect once the MIC firmware is regenerated and reflashed (the entity's `suggested_display_precision` becomes 1) — covered by the later MIC Wizard build/install GO. Until then the deployed device still shows "0 A".
- Product acceptance still pending (separate GOs): merge → config→ops sync → ops v0.4.3 release → `stable.json` pin → customer Add-on installer + HA restart → MIC Wizard build/install validation (live MIC600 shows Status mapped to Normal/Standby/Fault and AC Current at 0.1 A resolution).

### Resolution (P1h — fix/mic-dashboard-numeric-formatting)
- Regression found on the live customer MIC600 after v0.4.3 + Wizard Build/Flash: **Inverter Temperature** rendered as a raw long float (`33.4000015258789 °C`) and **AC Current** still showed `0 A`.
- **Inverter Temperature root cause + fix (dashboard, reflash-invariant):** the registry `inverter_temperature` carries no `accuracy_decimals` and the generated sensor has only a `multiply: 0.1` filter (no `round`), so the published state is the float32 value `33.4000015258789`. The P1g Status markdown rendered it via a bare `{{ states(...) }}`, which shows the raw state (a Jinja `states()` read ignores the entity's display precision). Fix: the Status-card markdown now coerces and rounds — `{{ states(...) | float(none) | ... | round(1) }} °C` — so it always shows `33.4 °C` regardless of the entity state's precision; non-numeric states degrade to `Unknown`. No registry/firmware change for temperature.
- **AC Current proof (no new fix):** `generate_device_yaml` for MIC600 was generated and parsed end-to-end — the `ac_current` sensor **already emits `accuracy_decimals: 1`** (register unchanged: addr 39, scale 0.1, unit A, `multiply: 0.1`). So the generated entity metadata is correct (P1g) and HA's entities-card row will display `0.3 A` once the entity carries that precision. The live `0 A` is because the customer's `/config/inverter-registry` predates P1g (the registry is **not** shipped in `pvautonomy-ops`, so it reaches the device only via the separate registry-deploy + MIC Wizard reflash). No dashboard fallback was added: with `accuracy_decimals: 1` the standard entities-card row shows sub-amp precision reliably, so AC Current is intentionally left as a normal AC Output row (no layout fragmentation).
- Scope: `dashboard_builder.py` (temperature formatting), `tests/test_dashboard_builder.py` (temperature one-decimal test), `tests/test_yaml_generator_mic600_guardrails.py` (end-to-end `generate_device_yaml` ac_current accuracy_decimals proof), this doc. SPH dashboard unchanged; no raw technical MIC entities exposed; no missing-entity rows.
- Proof: `pytest custom_components/pvautonomy_ops/tests/test_dashboard_builder.py` green (186). The two MIC ac_current precision tests pass; the 3 pre-existing `firmware_version` generator failures are unrelated (reproduced on origin/main @ 8d22475 with these edits stashed: `3 failed, 21 passed`). `ruff` clean on changed runtime/test files (the generator test file's 2 ruff findings — E402:66, E741:367 — are pre-existing and identical to origin/main; no new ones introduced). `py_compile` + `git diff --check` clean.
- Still pending (separate GOs): merge → config→ops sync → ops v0.4.4 release → `stable.json` pin → customer Add-on installer + HA restart → registry-deploy + MIC Wizard build/install validation (live MIC600: temperature `33.4 °C`, AC Current `0.1/0.3 A`).

### Resolution (P1i — fix/mic-ac-current-dashboard-format)
- Live v0.4.4 result on the customer MIC600: Inverter Status (Normal/Standby/Fault) and Inverter Temperature (`32.8 °C`) now render correctly, but **AC Current still showed `0 A`** in the AC Output entities card — even though the registry/generator path carries `accuracy_decimals: 1` (P1g). This **disproves the P1h assumption** that the standard entities-card row reliably honors sub-amp display precision: HA's core entities card has no reliable per-row display-precision override, so the sub-amp Iac1 (e.g. 0.3 A) is shown rounded to `0 A`.
- **Fix (MIC-specific dashboard rendering, reflash-invariant):** MIC AC Current is now rendered as a one-decimal **markdown** line — `**AC Current:** {{ states(...) | float(none) | round(1) }} A` — that reads the actual entity state (already scaled to 0.3 by the registry/generator) and formats it, bypassing the unreliable entities-card precision. `0.3 → 0.3 A`, `0.1 → 0.1 A`, `0 → 0.0 A`; non-numeric (unknown/unavailable/missing) → `Unknown`. The MIC "AC Output" card is now a `vertical-stack` of the native entities card (AC Frequency / AC Power / AC Voltage, unchanged) plus this AC Current markdown line, so the layout stays a single AC Output region. No new entity row exposes the raw `0 A`.
- **No firmware/registry/generator change.** The Modbus register and `ac_current accuracy_decimals: 1` are untouched; only the dashboard presentation changed. SPH is unaffected (`has_battery` gate; SPH uses `*_ac_current_l1_device`, not the MIC `*_ac_current_device` suffix) — SPH AC Output stays a plain entities card. No raw technical MIC entities exposed; no missing-entity rows.
- Scope: `dashboard_builder.py` (`_build_mic_ac_current_card` + MIC AC Output vertical-stack), `tests/test_dashboard_builder.py` (AC Current one-decimal + AC Output freq/power/voltage + SPH non-regression), this doc.
- Proof: `pytest custom_components/pvautonomy_ops/tests/test_dashboard_builder.py` green (189, incl. the 3 new P1i tests). `ruff` clean on changed runtime/test files; `py_compile` + `git diff --check` clean. No generator/registry edit, so the `firmware_version` generator suite is untouched by this change.
- Still pending (separate GOs): merge → config→ops sync → ops v0.4.5 release → `stable.json` pin → customer Add-on installer + HA restart → live MIC dashboard validation (AC Current renders `0.1/0.3 A`, effective on a dashboard refresh after install — no reflash needed).

### Resolution (P1j — fix/mic-ac-output-native-current-row)
- Live v0.4.5 customer feedback: the P1i markdown AC Current line renders the value correctly (`0.7 A`), but **looks inconsistent** with the other AC rows (separate markdown block under the AC Output entities card). Product decision: AC Current returns to a **native entity row**, ordered `AC Power → AC Frequency → AC Voltage → AC Current`.
- **P1i's "disproof" of the entities-card precision was tested against stale firmware.** Evidence from the live system (2026-06-10, read-only): the `sensor.…_ac_current_device` entity carries `suggested_display_precision: 0` — ESPHome derives that directly from the **flashed firmware's** `accuracy_decimals`. A firmware built from the P1g registry (`ac_current accuracy_decimals: 1`, emitted by the generator — both verified present on main) publishes suggested precision **1**, and HA's entities card honors `suggested_display_precision`. So the native row does show `0.3 A` on the supported customer path **once the firmware is rebuilt + installed**; the P1i observation (`0 A` on v0.4.4) reflected a device still running pre-P1g firmware, not an entities-card defect.
- **Change (dashboard-only):** the P1i MIC special case (vertical-stack + `_build_mic_ac_current_card` markdown) is removed; MIC AC Current is a plain AC Output row again. `_SECTION_ROW_ORDER["AC Output"]` gains the MIC single-phase tokens (`ac_power`, `ac_frequency`, `ac_voltage`, `ac_current`), interleaved so SPH relative order is untouched (full-token matching prevents `ac_power` from capturing `ac_power_total`, etc.). No registry/generator change — the `accuracy_decimals: 1` path already exists end-to-end.
- **Interim phase (explicit):** until the customer MIC600 firmware is rebuilt + installed via the Wizard (separate Customer-Path GO), the live entity keeps `suggested_display_precision: 0` and the native row rounds to `0 A`/`1 A`. This is a consciously accepted intermediate state; the firmware rebuild closes it (no dashboard change needed afterwards — refresh picks up the row automatically on the next install per P1).
- Scope: `dashboard_builder.py` (remove P1i special case + MIC row order), `tests/test_dashboard_builder.py` (native-row + exact-order tests replace the markdown tests; SPH non-regression kept), this doc.

---

## v0.4.16 stable / P2-f validation

This section supersedes the historical P1 notes above and reflects the current,
bundle-first product path.

### Release / channel state

- `pvautonomy_ops` **v0.4.16** is stable/latest.
- `pvautonomy-addons` `integration/stable.json` points at **0.4.16**.
- `pvautonomy-addons` `integration/beta.json` points at **0.4.16** as well.
- Stable channel validation: **PASS**.
- Stable asset reachable: **HTTP 200**.
- Stable asset SHA-256 was computed and **matches the published SHA**.
- GitHub Release target commit was validated.
- v0.4.15 remains **prerelease**.
- v0.4.14 remains available as a **fallback**.

### P2-f runtime evidence

- Test system: **.120**, reached **by IP only** (never `homeassistant.local`).
- Fresh / runtime validation: **PASS**.
- Config flow: **PASS**.
- `/whoami`: **PASS**.
- `customer_id_missing`: **resolved**.
- `pyhpke` / `cryptography` resolver issue: **resolved**.
- `defs_version`: **accepted**.
- `yaml_hash`: **accepted**.
- Build backend: `proxy_remote`.
- Build ID: `02a34a35-8acf-47a4-b47a-8b177f8917d6`.
- Build result: **success**.
- Firmware artifact produced, approximately **1.12 MB**.
- **No OTA. No flash. No firmware upload.**
- **No D8 warning** observed in normal operation.

### Dual Install Validation (.120)

**Customer / app path:**
- PVAutonomy Installer/Updater add-on, `stable` channel, **0.4.16** — **PASS**.
- No HACS involved.

**Developer / HACS path:**
- HACS custom repository `PVAutonomy/pvautonomy-ops`, Integration category,
  `stable` **0.4.16** — **PASS**.
- No Installer/add-on split-brain.

**Both paths:**
- Same release artifact, same SHA.
- No `/config/inverter-registry` definitions needed.
- No `/config/esphome` firmware definitions needed.
- Firmware definitions bundled under `data/firmware_defs/**`.
- No build / OTA / flash / upload in the dual-install test.
- No secrets involved.

### Current outcome

- Customer delivery **no longer depends on** `/config/inverter-registry`.
- Firmware definitions are delivered **bundle-first**.
- HACS (`stable`) and the Installer/add-on (`stable`) deliver the **same
  validated** state.
- P2-f / stable promotion is **complete**.
- Issues #66, #79, #94, #96, #97 are **closed**.
- #92 remains **open** for the later D8-fallback code cleanup (mentioned here as
  a follow-up only; no auto-close).
