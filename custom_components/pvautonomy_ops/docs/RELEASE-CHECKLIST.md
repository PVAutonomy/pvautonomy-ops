# Release Checklist (Maintainer)

Steps to publish a new PVAutonomy release.

## Pre-release

- [ ] **Bump version** in both files (must match):
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

## Compatibility Check

- [ ] **Proxy version:** verify proxy `/health` returns a compatible `contract_version`
  ```bash
  curl -s https://pvautonomy-proxy.pvautonomy-proxy.workers.dev/health | python3 -m json.tool
  ```
- [ ] **Workflow contract:** confirm `ESPHOME_VERSION` in `build-firmware-on-demand.yml` matches expectations
- [ ] **HA minimum version:** `manifest.json` → `homeassistant` field (if set) or `hacs.json` → `homeassistant`

## Smoke Test (E2E)

- [ ] **Trigger build:** use `proxy_remote` backend, confirm `POST /build` returns 200 + build_id
- [ ] **Poll status:** `GET /build/{id}` transitions through `queued → running → success`
- [ ] **Download artifact:** `GET /build/{id}/artifact/firmware.ota.bin` returns binary with correct Content-Length
- [ ] **Hash verify:** SHA-256 of downloaded binary matches manifest `sha256` field
- [ ] **Integration loads:** restart HA, confirm no errors in logs for `pvautonomy_ops`

## Tag & Release

- [ ] **Commit:** `[EPIC-006] release: vX.Y.Z`
- [ ] **Tag:** `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] **GitHub Release:** create release from tag with changelog summary
- [ ] **HACS visible:** confirm new version appears in HACS after ~30 min

## Post-release

- [ ] **Verify HACS install:** test on a clean HA instance (or EDATEC staging)
- [ ] **Verify HACS update:** test upgrade from previous version
- [ ] **Notify:** inform customers/testers of new release
