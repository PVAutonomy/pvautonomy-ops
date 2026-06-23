"""Single resolver for firmware-definition data locations (ADR-0001 P2-a).

The integration reads two firmware-definition data components — the inverter
**register database** and the ESPHome **base template**. This module centralises
resolution so the precedence and the fail-closed contract live in exactly one place.

Resolution:

1. **Override** — if an explicit ``override`` path is passed, it is returned
   directly without any other checks.
2. **Bundled package defs** — ``custom_components/pvautonomy_ops/data/firmware_defs/``
   resolved **module-relative** (independent of the process CWD, which is
   ``/config`` on a Home Assistant OS install). This is the ADR-0001 **D2**
   primary and only supported path.
3. **Fail-closed** — a clear, actionable :class:`DefsNotFoundError` when the
   bundle is absent. The ``/config`` path is **not** consulted; it is not a
   product path (ADR-0001 D8 — resolved).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Bundled package defs — module-relative so it is independent of the CWD.
PACKAGE_DEFS_ROOT = Path(__file__).resolve().parent / "data" / "firmware_defs"
BUNDLED_REGISTRY_ROOT = PACKAGE_DEFS_ROOT / "registry"
BUNDLED_BASE_DIR = PACKAGE_DEFS_ROOT / "base"

PRODUCTION_BASE_FILE = "edge101-production-base.yaml"

# Module-private aliases for the bundle paths so tests can monkeypatch a single
# attribute per kind.
_BUNDLE_REGISTRY = BUNDLED_REGISTRY_ROOT
_BUNDLE_BASE = BUNDLED_BASE_DIR


class DefsNotFoundError(Exception):
    """Bundled firmware definitions could not be located."""


def _resolve(
    kind: str,
    bundle: Path,
    check,
    override: Path | None,
) -> Path:
    """Return the bundle path when ``check`` is true, or raise :class:`DefsNotFoundError`.

    ``override`` short-circuits resolution (callers that pass an explicit root
    keep their existing semantics). Raises :class:`DefsNotFoundError` when the
    bundle is absent — no ``/config`` fallback, fail-closed.
    """
    if override is not None:
        return override
    if check(bundle):
        return bundle
    raise DefsNotFoundError(
        f"No firmware-definition {kind} found. Looked for bundled defs at "
        f"{bundle}. The integration ships firmware definitions in its own "
        f"package ({PACKAGE_DEFS_ROOT}); reinstall or update the integration."
    )


def resolve_registry_root(override: Path | None = None) -> Path:
    """Resolve the inverter-registry root directory (bundle-only, fail-closed)."""
    return _resolve(
        "registry", _BUNDLE_REGISTRY,
        lambda p: p.is_dir(), override,
    )


def resolve_base_dir(override: Path | None = None) -> Path:
    """Resolve the directory holding the ESPHome production base template.

    Existence is keyed on the base file itself (not just the directory) so an
    empty/partial bundle does not shadow a valid installation.
    """
    return _resolve(
        "base", _BUNDLE_BASE,
        lambda p: (p / PRODUCTION_BASE_FILE).is_file(), override,
    )


def read_defs_version() -> str | None:
    """Return ``defs_version`` from the bundled manifest, or None (never raises).

    Reads ``PACKAGE_DEFS_ROOT/defs-manifest.json`` (the shipped bundle). Returns
    None — without raising — when the manifest is absent, the ``defs_version``
    key is missing, the JSON is malformed, or the file cannot be read. The build
    path must degrade gracefully, never crash, on a missing or broken manifest
    (ADR-0001 P2-b2). The value (a string) is recorded next to ``yaml_hash``
    for traceability.
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
