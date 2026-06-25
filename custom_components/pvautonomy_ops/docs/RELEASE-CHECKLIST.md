# Release Checklist (Maintainer)

Steps to publish a new PVAutonomy release. Each phase below is a **separate
step** — do not bundle commit / push / PR / merge / export / release /
channel-pin / validation / issue-closure into one action.

## Repos in the release chain

- **`PVAutonomy/pvautonomy-config`** — source / integration-source repo (where
  this checklist lives). The integration code is authored here.
- **`PVAutonomy/pvautonomy-ops`** — HACS distribution / release target repo.
  Release authority (version, GitHub Release, asset) lives here.
- **`PVAutonomy/pvautonomy-addons`** — holds the Installer/Updater add-on
  `beta` / `stable` channel pins (`integration/beta.json`,
  `integration/stable.json`).

## Versioning — where the bump happens

The release version is **not** taken from the source repo's `const.py`.

- In `pvautonomy-config`, `const.py` `VERSION` is a **source-owned
  placeholder/default** (currently `0.3.0`). The HACS export treats the version
  field as **target-owned** (see `HACS-EXPORT.md`), so this placeholder can
  never downgrade a released version.
- The actual release version is **set and verified in the `pvautonomy-ops`
  target** (`manifest.json` `"version"` + `const.py` `VERSION`, kept in sync)
  during release-prep.
- `CONTRACT_VERSION` (e.g. `v1.0.0`) is a **separate** contract value, unrelated
  to the release version; it is still valid for the `/health` contract check.
- **After each stable release**, `pvautonomy-config` must also be updated to
  the release version (see _pvautonomy-config version sync_ step in Stable
  promotion). This protects staging deploys via `deploy_to_edatec.sh
  --scope=pvops`, which copy files verbatim from `pvautonomy-config` and do not
  have the HACS-export target-version guard.

## Pre-release (in `pvautonomy-ops` target, during release-prep)

- [ ] **Version bump** in both target files (must match):
  - `custom_components/pvautonomy_ops/manifest.json` → `"version": "X.Y.Z"`
  - `custom_components/pvautonomy_ops/const.py` → `VERSION = "X.Y.Z"`
- [ ] **Translation keys aligned:** all keys in `strings.json` must exist in `translations/en.json` and `translations/de.json`
  ```bash
  python3 -c "
  import json
  s = json.load(open('custom_components/pvautonomy_ops/strings.json'))
  e = json.load(open('custom_components/pvautonomy_ops/translations/en.json'))
  d = json.load(open('custom_components/pvautonomy_ops/translations/de.json'))
  sk = set(s['options']['step']['init']['data'].keys())
  assert sk == set(e['options']['step']['init']['data'].keys()), 'en.json mismatch'
  assert sk == set(d['options']['step']['init']['data'].keys()), 'de.json mismatch'
  print(f'OK: {len(sk)} keys aligned')
  "
  ```
- [ ] **hacs.json valid:** `python3 -c "import json; json.load(open('custom_components/pvautonomy_ops/hacs.json'))"`
- [ ] **Changelog updated** (if maintained)

## Packaging gates (source → export)

- [ ] **gitleaks:** clean on the staged change set.
- [ ] **Export dry-run / diff scope:** run `scripts/export_to_hacs.py` dry-run;
  review the manifest + scrub report and the diff scope before any `--apply`
  (see `HACS-EXPORT.md`).
- [ ] **`requirements = []`:** `manifest.json` declares no install-time pip
  requirements.
- [ ] **Vendored `pyhpke` present + license present:**
  `_vendor/pyhpke/**` ships with its `LICENSE`.
- [ ] **Bundled firmware definitions present:** `data/firmware_defs/**` is
  included in the export.
- [ ] **No `/config` defs dependency:** the package does not depend on
  `/config/inverter-registry` or `/config/esphome` (those are not product
  distribution paths).

## Compatibility check

- [ ] **Proxy version:** verify proxy `/health` returns a compatible `contract_version`
  ```bash
  curl -s https://pvautonomy-proxy.pvautonomy-proxy.workers.dev/health | python3 -m json.tool
  ```
- [ ] **Workflow contract:** confirm `ESPHOME_VERSION` in `build-firmware-on-demand.yml` matches expectations
- [ ] **HA minimum version:** `manifest.json` → `homeassistant` field (if set) or `hacs.json` → `homeassistant`

## Smoke test (E2E)

- [ ] **Trigger build:** use `proxy_remote` backend, confirm `POST /build` returns 200 + build_id
- [ ] **Poll status:** `GET /build/{id}` transitions through `queued → running → success`
- [ ] **Download artifact:** `GET /build/{id}/artifact/firmware.ota.bin` returns binary with correct Content-Length
- [ ] **Hash verify:** SHA-256 of downloaded binary matches manifest `sha256` field
- [ ] **Integration loads:** restart HA, confirm no errors in logs for `pvautonomy_ops`

## Release (in `pvautonomy-ops`)

- [ ] **Commit (target):** `[<WorkItem>] release: vX.Y.Z` — use the relevant
  directive/work-item id; do **not** use a hardcoded epic prefix as a generic
  default. Commit body uses `Refs #NN` only.
- [ ] **Tag:** `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] **GitHub Release:** create the release from the tag. It MAY start as
  `prerelease=true` (beta) — promoting to stable is a separate step below.

## Stable promotion (separate GO)

Stable promotion is **more** than flipping `prerelease=false`. Do all of:

- [ ] **GO required:** no stable promotion without an explicit GO.
- [ ] **GitHub Release flag:** set the `pvautonomy-ops` release to
  `prerelease=false` (stable/latest).
- [ ] **GitHub Latest set:** after `prerelease=false`, explicitly mark the
  release as the GitHub Latest so `releases/latest` API and HACS resolve the
  correct version. Promoting a prerelease to stable does **not** automatically
  set Latest — this step is always required:
  ```bash
  gh release edit vX.Y.Z --repo PVAutonomy/pvautonomy-ops --latest
  # Verify:
  gh api repos/PVAutonomy/pvautonomy-ops/releases/latest --jq .tag_name
  # Expected: vX.Y.Z
  ```
- [ ] **pvautonomy-config version sync:** update the source repo version so
  staging deploys via `deploy_to_edatec.sh --scope=pvops` write the correct
  version to disk. This is NOT part of the HACS export; it is a separate commit
  in `pvautonomy-config`:
  - `custom_components/pvautonomy_ops/manifest.json` → `"version": "X.Y.Z"`
  - `custom_components/pvautonomy_ops/const.py` → `VERSION = "X.Y.Z"`
  ```bash
  # Verify after the commit:
  grep '"version"' custom_components/pvautonomy_ops/manifest.json
  grep '^VERSION' custom_components/pvautonomy_ops/const.py
  python3 -m json.tool custom_components/pvautonomy_ops/manifest.json >/dev/null
  # Commit message:
  # [fix/#NNN] sync pvautonomy ops version to vX.Y.Z
  ```
- [ ] **Asset reachable:** the release asset (`pvautonomy_ops-X.Y.Z.zip`)
  returns HTTP 200.
- [ ] **Compute + compare SHA-256:** compute the asset SHA-256 and compare it
  exactly to the value the channel pins will carry.
- [ ] **Pin `stable.json`:** in `pvautonomy-addons`, point
  `integration/stable.json` at the same version / URL / SHA.
- [ ] **Check `beta.json`:** confirm `integration/beta.json` is consistent
  (it may point at the same version/asset/SHA).
- [ ] **Release-notes URL:** verify the release notes / title are correct.
- [ ] **Stable-channel validation:** validate that the stable channel actually
  serves the intended version (separate from the GitHub-flag flip).

> For each release, compute and verify the artifact SHA from the published
> asset. Do not copy SHA values from older release notes or previous checklist
> runs. The channel pin must use the SHA of the exact artifact being promoted.

## Post-release validation

- [ ] **Verify HACS install:** test on a clean HA instance.
- [ ] **Verify HACS update:** test upgrade from the previous version.
- [ ] **Dual Install Validation (at stable promotion):**

  **HACS path** (on a HACS-managed staging host, e.g. `.106`):
  - Trigger a metadata refresh so HACS fetches the current `releases/latest`:
    ```bash
    POST /api/services/homeassistant/update_entity
    {"entity_id": "update.pvautonomy_ops_update"}
    ```
  - Verify the update entity reflects the target version:
    ```bash
    GET /api/states/update.pvautonomy_ops_update
    # Expected:
    #   attributes.latest_version == "vX.Y.Z"
    #   attributes.installed_version is plausible (previous version)
    #   state != "unavailable"
    ```
  - Note: `update.pvautonomy_ops_update` is only present on HACS-managed hosts.
    Its absence on an Installer-managed host is expected, not an error.

  **Installer / customer path** (on a non-HACS staging host, e.g. `.120`):
  - Restart the PVAutonomy Installer add-on. The slug includes a repository
    hash prefix — retrieve it from the add-on's entity or the Supervisor API
    before running (use `<installer_slug>` as a placeholder here):
    ```bash
    POST /api/services/hassio/addon_restart
    {"addon": "<installer_slug>"}
    ```
  - Check the add-on log for install/SHA confirmation:
    ```bash
    GET /api/hassio/addons/<installer_slug>/logs
    # Expected log lines:
    #   sha256 verified: <sha>
    #   PVAutonomy Ops X.Y.Z installed at /config/custom_components/pvautonomy_ops
    ```
  - Restart Home Assistant if the installer requests it (it always does after
    an upgrade):
    ```bash
    POST /api/services/homeassistant/restart
    ```
  - After HA comes back up, verify the loaded version:
    ```bash
    GET /api/states/sensor.pvautonomy_status
    # Expected: attributes.version == "X.Y.Z"
    ```
  - Note: there is currently no `update.pvautonomy_ops_update` entity on
    Installer-managed hosts. Validation relies on the add-on log and
    `sensor.pvautonomy_status` instead.

  **Cross-path invariants:**
  - **Same artifact / SHA** on both paths (compare SHA from add-on log and
    from `pvautonomy-addons/integration/stable.json`).
  - **No split-brain:** a host that uses the Installer must not also have HACS
    managing `pvautonomy_ops` (the installer enforces this with a hard guard).
- [ ] **Notify:** inform customers/testers of the new release.

## Issue closure (separate, last)

- [ ] **Never auto-close via PR/commit/issue body.** Use `Refs #NN` only — no
  `close` / `fix` / `resolve` tokens (not even negated).
- [ ] **Close only after runtime/stable validation passes**, with a separate
  comment + close action.
- [ ] For a validated release cycle, comment on and close only the issues that
  were explicitly in scope for that cycle, and only after runtime/channel
  validation has passed. Do not hardcode issue numbers into this checklist as
  standing closures.
