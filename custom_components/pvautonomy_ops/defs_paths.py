"""Single resolver for firmware-definition data locations (ADR-0001 P2-a).

The integration reads two firmware-definition data components — the inverter
**register database** and the ESPHome **base template**. Historically each
reader (``yaml_generator``, ``entity_cleanup``, ``dashboard_builder``) carried
its own CWD-relative ``inverter-registry`` / ``/config`` fallback logic. This
module centralises that resolution so the precedence, the D8 migration fallback,
and the deprecation signal live in exactly one place.

Resolution precedence:

1. **Bundled package defs** — ``custom_components/pvautonomy_ops/data/firmware_defs/``
   resolved **module-relative** (independent of the process CWD, which is
   ``/config`` on a Home Assistant OS install). This is the ADR-0001 **D2**
   primary path.
2. **Legacy locations** — the repo-relative dev tree, then ``/config`` — used
   **only when the bundle is absent** (ADR-0001 **D8**, migration-only). Emits a
   **one-time** deprecation warning so the log is not spammed on every call.
3. **Fail-closed** — a clear, actionable :class:`DefsNotFoundError` when neither
   the bundle nor a legacy location resolves.

Scope note (P2-a): this module changes only **where** definitions are read from.
It does **not** create the bundled data (that is P2-b) and does **not** touch the
release/export pipeline. Until P2-b ships the bundle, resolution falls through to
the legacy tier.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# 1. Bundled package defs — module-relative so it is independent of the CWD.
PACKAGE_DEFS_ROOT = Path(__file__).resolve().parent / "data" / "firmware_defs"
BUNDLED_REGISTRY_ROOT = PACKAGE_DEFS_ROOT / "registry"
BUNDLED_BASE_DIR = PACKAGE_DEFS_ROOT / "base"

# 2. Legacy locations (migration-only, D8): repo-relative dev tree first, then
#    /config. On a HA OS install CWD is /config, so the first entry collapses
#    onto the second; in a dev checkout it points at the working tree.
_LEGACY_REGISTRY: tuple[Path, ...] = (
    Path("inverter-registry"),
    Path("/config/inverter-registry"),
)
_LEGACY_BASE: tuple[Path, ...] = (
    Path("esphome"),
    Path("/config/esphome"),
)

PRODUCTION_BASE_FILE = "edge101-production-base.yaml"

# Module-private aliases for the bundle paths so tests can monkeypatch a single
# attribute per kind.
_BUNDLE_REGISTRY = BUNDLED_REGISTRY_ROOT
_BUNDLE_BASE = BUNDLED_BASE_DIR

# One-time deprecation guard (per definition kind) — avoids per-call log spam.
_warned: set[str] = set()


class DefsNotFoundError(Exception):
    """Neither bundled nor legacy firmware definitions could be located."""


def reset_deprecation_warnings() -> None:
    """Clear the one-time deprecation guard. Test helper."""
    _warned.clear()


def _warn_once(kind: str, path: Path) -> None:
    if kind in _warned:
        return
    _warned.add(kind)
    _LOGGER.warning(
        "Firmware-definition %s resolved from LEGACY path %s — bundled "
        "definitions were not found under %s. This repo-local/`/config` "
        "fallback is migration-only (ADR-0001 D8) and is scheduled for removal; "
        "update/reinstall the integration so it ships the bundled definitions.",
        kind,
        path,
        PACKAGE_DEFS_ROOT,
    )


def _resolve(
    kind: str,
    bundle: Path,
    legacy: tuple[Path, ...],
    check,
    override: Path | None,
) -> Path:
    """Return the first location for which ``check`` is true (bundle preferred).

    ``override`` short-circuits resolution (callers that pass an explicit root
    keep their existing semantics). The legacy tier triggers a one-time
    deprecation warning. Raises :class:`DefsNotFoundError` if nothing matches.
    """
    if override is not None:
        return override
    if check(bundle):
        return bundle
    for cand in legacy:
        if check(cand):
            _warn_once(kind, cand)
            return cand
    raise DefsNotFoundError(
        f"No firmware-definition {kind} found. Looked for bundled defs at "
        f"{bundle} and legacy locations "
        f"{', '.join(str(p) for p in legacy)}. The integration ships firmware "
        f"definitions in its own package ({PACKAGE_DEFS_ROOT}); reinstall or "
        f"update the integration."
    )


def resolve_registry_root(override: Path | None = None) -> Path:
    """Resolve the inverter-registry root directory (bundle-first, D8 fallback)."""
    return _resolve(
        "registry", _BUNDLE_REGISTRY, _LEGACY_REGISTRY,
        lambda p: p.is_dir(), override,
    )


def resolve_base_dir(override: Path | None = None) -> Path:
    """Resolve the directory holding the ESPHome production base template.

    Existence is keyed on the base file itself (not just the directory) so an
    empty/partial bundle does not shadow a valid legacy location.
    """
    return _resolve(
        "base", _BUNDLE_BASE, _LEGACY_BASE,
        lambda p: (p / PRODUCTION_BASE_FILE).is_file(), override,
    )


def read_defs_version() -> str | None:
    """Return ``defs_version`` from the bundled manifest, or None (never raises).

    Reads ``PACKAGE_DEFS_ROOT/defs-manifest.json`` (the shipped bundle). Returns
    None — without raising — when the manifest is absent (e.g. the D8 legacy
    fallback is in effect, so there is no bundled, versioned definition set), the
    ``defs_version`` key is missing, the JSON is malformed, or the file cannot be
    read. The build path must degrade gracefully, never crash, on a missing or
    broken manifest (ADR-0001 P2-b2). The value (a string) is recorded next to
    ``yaml_hash`` for traceability; full C3 manifest assembly is P3.
    """
    manifest_path = PACKAGE_DEFS_ROOT / "defs-manifest.json"
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:  # missing / unreadable / malformed JSON
        _LOGGER.debug("defs_version unavailable (%s): %s", manifest_path, exc)
        return None
    version = data.get("defs_version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version:
        _LOGGER.debug("defs_version key missing/invalid in %s", manifest_path)
        return None
    return version
