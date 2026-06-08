# Legacy Release Cleanup Inventory — sph10k-home-02

**Scope:** Read-only inventory of two legacy firmware releases flagged as
cleanup candidates for `sph10k-home-02`.
**Date:** 2026-06-01
**Mode:** Inventory phase was read-only (no assets downloaded, no
build/install/deploy). The two candidate releases were **subsequently deleted
on 2026-06-01** after a separate explicit GO — see [Status](#status).
**Repo holding the releases:** `PVAutonomy/inverter-registry`.

> The inventory tables below are retained as the historical record of the two
> releases **as they existed before deletion**.

---

## Cleanup candidates

| # | tag_name | release id | draft | prerelease | published_at |
|---|----------|-----------:|:-----:|:----------:|--------------|
| A | `sph10k-home-02-2026.05.19-1917` | 325287251 | false | false | 2026-05-19T19:24:17Z |
| B | `sph10k-home-02-2026.05.22-1050` | 327789074 | false | false | 2026-05-22T10:57:11Z |

### Release body fields

| Field | A (…05.19-1917) | B (…05.22-1050) |
|-------|-----------------|-----------------|
| Device | `sph10k-home-02` | `sph10k-home-02` |
| Registry | `inverters/growatt/sph/sph10k.json` | `inverters/growatt/sph/sph10k.json` |
| Version | `sph10k-home-02-2026.05.19-1917` | `sph10k-home-02-2026.05.22-1050` |
| SHA | `dd9471027479d7195460866cf0ad3bc852480a80` | `79782c7fa13026d6060596ab590c63b321fca789` |
| Build ID | `d191cf74-eb7a-45c2-bd0d-72bd76d45053` | `be85fc2e-b24f-4dd1-860f-dcebd05a6411` |
| Build Contract | *(absent — older schema)* | `yaml_authority` |
| Author | `github-actions[bot]` | `github-actions[bot]` |

---

## Asset summary

### A — `sph10k-home-02-2026.05.19-1917`

| asset | size (bytes) | sha256 digest | download_count | content_type | state |
|-------|-------------:|---------------|---------------:|--------------|-------|
| `sph10k-home-02-manifest.json` | 563 | `5a57fe24cda6f453153883b2d17041fc2684bafae077f06d394c7e94fd8fd5c5` | 2 | application/json | uploaded |
| `sph10k-home-02.bin` | 1 209 280 | `dfb0ff0ef953e17a510c1fe4be549d8f9da40d1c5c4e149a19bc9e02f49ee6d9` | 0 | octet-stream | uploaded |
| `sph10k-home-02.ota.bin` | 1 209 280 | `dfb0ff0ef953e17a510c1fe4be549d8f9da40d1c5c4e149a19bc9e02f49ee6d9` | 2 | octet-stream | uploaded |

### B — `sph10k-home-02-2026.05.22-1050`

| asset | size (bytes) | sha256 digest | download_count | content_type | state |
|-------|-------------:|---------------|---------------:|--------------|-------|
| `sph10k-home-02-manifest.json` | 563 | `c76d183d02aae1f68ba2586a2bd8a0c7fcafe8f559f8f1fd080ba633f35ffc7b` | 2 | application/json | uploaded |
| `sph10k-home-02.bin` | 1 115 408 | `e8acc7f08f02e20fb33ac17c7cff1362e12370f86d9540257be27c9f3ccf075d` | 0 | octet-stream | uploaded |
| `sph10k-home-02.ota.bin` | 1 115 408 | `e8acc7f08f02e20fb33ac17c7cff1362e12370f86d9540257be27c9f3ccf075d` | 1 | octet-stream | uploaded |

Note: within each release the `.bin` and `.ota.bin` share an identical digest
(pipeline-consistent); download counts reflect past verify/test pulls, not
active deployment.

---

## Local reference search (current checkout)

Searched with `rg` across the working tree (excluding `.git`):

| Search pattern | Result |
|----------------|--------|
| exact tags (both) | **no hits** |
| Build IDs (`d191cf74…`, `be85fc2e…`) | **no hits** |
| Release SHAs (`dd947102…`, `79782c7f…`) | **no hits** |
| Asset sha256 digests (all four) | **no hits** |
| pinned tag pattern `sph10k-home-02-2026…` | **no hits** |

Generic install paths exist but are **not pinned** to these tags:
- `packages/setup/edge101_ota_wizard.yaml` builds the manifest URL dynamically
  from the operator `release_tag` input, with a `releases/latest/download/…`
  fallback.
- `custom_components/pvautonomy_ops/const.py` constructs `ARTIFACTS_BASE_URL`
  dynamically.

Consequence: both releases are only reachable by **explicit manual tag entry**
or their direct download URLs. The `releases/latest/download` fallback does
**not** serve them (latest is `…05.22-1916`).

---

## Delimitation vs. final successful install

| Release | Build ID | SHA | Role |
|---------|----------|-----|------|
| `sph10k-home-02-2026.05.22-1916` (id 328109066) | `9e94b51e-b2c9-4113-a234-6fc4dcc04544` | `514497d9123dbfd559f446d9f322ed8edb0090a7` | **Final successful install** (build-ID prefix `9e94b51e…` confirmed; device `edge101_sph10k_home_02`, install completed 2026-05-22 21:24). **Not a cleanup target.** |
| `…05.22-1050` (candidate B) | `be85fc2e…` | `79782c7f…` | superseded |
| `…05.19-1917` (candidate A) | `d191cf74…` | `dd947102…` | superseded |

After `…05.22-1050`, the same day produced further builds (1422, 1639, 1802,
1842, **1916**=final). Neither candidate matches the final build-ID prefix
`9e94b51e…`.

---

## Classification

- **Candidate A (`…05.19-1917`):** legacy-path smoke / pollution release.
  Iteration build three days before the final install; older pipeline schema
  (no `Build Contract`). No compromise indicators (author `github-actions[bot]`,
  all assets `state=uploaded`, consistent digests, plausible low download
  counts). **Not compromised.**
- **Candidate B (`…05.22-1050`):** pollution release — superseded
  `yaml_authority` intermediate build on the final day, before 1422/…/1916.
  Same clean indicators. **Not compromised.**

Both are **superseded iteration builds / release clutter**, with no sign of
manipulation.

---

## Decision — executed

- **Done (2026-06-01):** both GitHub Release objects + their assets were
  **deleted** after a separate explicit GO. The original recommendation
  (superseded, referenced nowhere, not pinned, not served by `latest`) was
  followed. The RETIRED-marking alternative was therefore not used.
- **Still open (optional, separate GO only):** delete the underlying **git
  tags** `sph10k-home-02-2026.05.19-1917` and `sph10k-home-02-2026.05.22-1050`.
  These remain as git refs in the remote; tag deletion was **not** part of the
  delete GO and has not been performed.

Residual note: deleting the releases removed the asset/download URLs (the
`releases/download/<tag>/…` paths now 404), which was the re-install path the
wizard's free-form `release_tag` input could reach. The remaining git tags
carry no assets and do not restore that path.

---

## Status

**Releases deleted on 2026-06-01** (after a separate explicit GO):

- `sph10k-home-02-2026.05.19-1917` (release id 325287251) — deleted.
- `sph10k-home-02-2026.05.22-1050` (release id 327789074) — deleted.

**Scope of the deletion:** only the GitHub **Release objects + their assets**
were removed. The underlying **git tags were NOT deleted** and still exist as
remote refs.

**Verification (read-only, post-delete):**

- Both deleted release tags now return Release-API **404** (Not Found).
- Asset / download URLs are gone (`releases/download/<tag>/…` → 404).
- Final release `sph10k-home-02-2026.05.22-1916` (release id 328109066) is
  **untouched and still present**.
- Git tags `refs/tags/sph10k-home-02-2026.05.19-1917` and
  `refs/tags/sph10k-home-02-2026.05.22-1050` still exist (intentionally; not in
  scope of the GO).

Any further action (git-tag deletion) requires a separate explicit GO.
