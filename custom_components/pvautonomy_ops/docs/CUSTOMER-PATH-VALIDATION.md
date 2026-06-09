# Customer Path Validation

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
