# HACS Export Runbook (config → pvautonomy-ops)

Sanctioned, auditable transport of the `pvautonomy_ops` integration from the
source/dev repo **`PVAutonomy/pvautonomy-config`** to the HACS distribution repo
**`PVAutonomy/pvautonomy-ops`**. This replaces ad-hoc cross-repo copies with an
allowlisted, scrubbed, manifested step. See `scripts/export_to_hacs.py`.

## Why
`pvautonomy-ops` is the HACS Custom Repository customers install
(`INSTALLATION.md`). It must receive only publishable integration code — no
`.storage`, secrets, private config, customer dashboards, deploy scripts, or
device-specific staging — and the operator must be able to review exactly what
ships. **Customer Path First:** a fix is only customer-deliverable once it
reaches `pvautonomy-ops` via this gate and a HACS release.

## Safety contract
- **Dry-run by default.** Without `--apply` the tool never writes to the target.
- **Allowlist:** `custom_components/pvautonomy_ops/**` only.
- **Excludes (skipped):** `tests/`, `__pycache__`, `*.pyc`, `.pytest_cache`,
  `.DS_Store`, `._*`. Tests are excluded because the HACS runtime package does
  not need them (leaner customer download; `pvautonomy-ops` has never shipped
  them).
- **Hard block (fail-closed in `--apply`):** `.storage`, `secrets*`, `.env*`,
  `lovelace/`, deploy scripts, `*.device.yaml` staging. These must never reach
  the public repo; a match aborts an apply.
- **Root reconciliation / target-owned:** `--apply` writes ONLY under the
  target's `custom_components/pvautonomy_ops/`. Target-owned root files
  (`hacs.json`, `README.md`, `LICENSE`, `examples/`) are never touched.
  Additionally, the **version fields are target-owned**: release authority
  lives in `pvautonomy-ops` (versions are bumped there via `[RELEASE]`
  commits), so on sync `manifest.json` `"version"` and `const.py` `VERSION`
  always keep the **target's** current value — the source placeholder (e.g.
  `0.3.0`) can never downgrade a released `0.4.x`. Every other change in
  those two files (e.g. new `requirements`, new constants) still syncs.
  On first sync (file absent in the target) the source file is copied
  verbatim. If the version field cannot be located in source or target, the
  apply **fails closed** (nothing written). The dry-run comparison applies
  the same merge, so `would-update` never reports a version-only diff;
  such files are listed as `unchanged (version target-owned)`.
- **Scrub gate:** scans for IPv4 (esp. `192.168.101.*`), `EDATEC`, secret
  keywords, and high-entropy hex/base64/`gh*_` token literals. CRITICAL
  findings fail `--apply` closed; all findings are reported with path + line +
  rule + **masked** context (never a raw secret value).
- **Manifest:** per-file sha256 + source commit SHA + target repo + timestamp +
  mode.

## Usage
```bash
# Dry-run (default): manifest + scrub report, no writes.
python3 scripts/export_to_hacs.py \
    --target-clone /path/to/pvautonomy-ops \
    --out-dir /tmp/hacs-export-dryrun

# Review the printed summary + the manifest and scrub report it points to.
```

`--apply` (separate, explicitly-authorized GO only) writes the allowlisted
files into a local `pvautonomy-ops` clone's integration dir, fails closed on any
CRITICAL scrub or hard-block finding, and never touches target root files. The
commit/push/PR/release in `pvautonomy-ops` are further separate GOs.

## GO sequence
1. **dry-run** (this gate) — review counts + scrub findings; scrub CRITICAL/
   hard-block findings must be resolved or consciously waived first.
2. **export `--apply`** into an ops clone (separate GO).
3. **release-prep** in `pvautonomy-ops` (bump `manifest.json` + `const.py` to a
   consistent version, release notes, commit, push, ready PR).
4. **merge / tag / GitHub Release** in `pvautonomy-ops`.
