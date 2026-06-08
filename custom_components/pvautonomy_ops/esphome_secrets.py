"""Auto-provision ESPHome secrets for customer first-install flow.

On a fresh customer system, ``/config/esphome/secrets.yaml`` may not
contain the device-specific API key or the per-device OTA password
required by the build pipeline.  This module generates and persists
them *idempotently* — existing values are never overwritten.

Security boundaries preserved:
- Secrets are generated locally (``secrets`` stdlib, CSPRNG).
- No plaintext secrets are logged (values are masked).
- No secrets transit to external systems from this module.
- File permissions are enforced to 0o600 (owner-only read/write).
- File-level advisory lock prevents lost updates on parallel provision.

Ref: SEC-010, D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001.
"""

from __future__ import annotations

import base64
import fcntl
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ESPHOME_SECRETS_RELPATH = Path("esphome") / "secrets.yaml"

#: Owner-only read/write.  Matches the expectation for secret material.
_SECRET_FILE_MODE = 0o600


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class ProvisionResult:
    """Outcome of an ``ensure_device_secrets`` call."""

    api_key_name: str = ""
    api_key_created: bool = False
    ota_key_name: str = ""
    ota_key_created: bool = False
    secrets_file: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _generate_noise_psk() -> str:
    """Generate a 32-byte random Noise PSK, Base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _generate_ota_password() -> str:
    """Generate a random OTA password (32 hex chars)."""
    return secrets.token_hex(16)


def _mask(value: str) -> str:
    """Redact a secret for safe logging.

    Returns only a neutral marker — never any portion of the value (no
    prefix, suffix, or other fragment). Present vs. empty is the only
    distinction, which leaks nothing about the secret itself.
    """
    if not value:
        return "<empty>"
    return "<redacted>"


def _read_secrets(path: Path) -> dict[str, str]:
    """Read a flat YAML secrets file, returning {} on any error."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        _LOGGER.warning("Failed to parse %s", path, exc_info=True)
        return {}


def _write_secrets_locked(path: Path, data: dict[str, str]) -> None:
    """Write the secrets dict to *path* with advisory lock + restrictive mode.

    The caller already holds the lock on *lock_fd* — this function only
    performs the atomic write and permission enforcement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# ESPHome secrets — auto-managed by PVAutonomy Ops\n")
        fh.write("# Do not remove entries used by active devices.\n\n")
        yaml.safe_dump(data, fh, default_flow_style=False, allow_unicode=True)
    # Enforce restrictive permissions on the temp file BEFORE rename
    os.chmod(tmp, _SECRET_FILE_MODE)
    tmp.replace(path)  # atomic on POSIX


def _enforce_permissions(path: Path) -> None:
    """Ensure *path* has ``_SECRET_FILE_MODE`` if it exists."""
    try:
        if path.exists():
            current = stat.S_IMODE(path.stat().st_mode)
            if current != _SECRET_FILE_MODE:
                os.chmod(path, _SECRET_FILE_MODE)
                _LOGGER.info(
                    "Tightened permissions on %s: 0o%03o → 0o%03o",
                    path.name,
                    current,
                    _SECRET_FILE_MODE,
                )
    except OSError:
        _LOGGER.warning(
            "Could not enforce permissions on %s", path, exc_info=True,
        )


def ensure_device_secrets_sync(
    config_dir: str | Path,
    mac_suffix: str,
) -> ProvisionResult:
    """Ensure required ESPHome secrets exist for *mac_suffix*.

    Creates the secrets file if missing.  Generates only values that
    are not yet present — existing values are never touched.

    The entire read-merge-write cycle is protected by an advisory file
    lock (``fcntl.flock`` / ``LOCK_EX``) so parallel provisioning for
    different devices does not lose updates.

    Secrets provisioned:
        ``edge101_api_key_{mac_suffix}``       — per-device Noise PSK (Base64)
        ``edge101_ota_password_{mac_suffix}``  — per-device OTA password

    Args:
        config_dir: HA config directory (``hass.config.config_dir``).
        mac_suffix: 6-char hex MAC suffix (e.g. ``"2eb1e4"``).

    Returns:
        ProvisionResult with details of what was created.
    """
    result = ProvisionResult()

    if not mac_suffix or len(mac_suffix) < 4:
        result.errors.append(f"Invalid mac_suffix: {mac_suffix!r}")
        return result

    secrets_path = Path(config_dir) / ESPHOME_SECRETS_RELPATH
    result.secrets_file = str(secrets_path)
    lock_path = secrets_path.with_suffix(".yaml.lock")

    # Ensure parent directory exists before opening the lock file.
    secrets_path.parent.mkdir(parents=True, exist_ok=True)

    # Advisory file lock — serialises concurrent read-merge-write cycles
    # so two parallel wizard flows for different devices both survive.
    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        result.errors.append(f"Cannot acquire lock {lock_path}: {exc}")
        return result

    try:
        # ── Critical section (under lock) ─────────────────────────
        data = _read_secrets(secrets_path)
        changed = False

        # 1) Per-device API encryption key (Noise PSK)
        api_key_name = f"edge101_api_key_{mac_suffix}"
        result.api_key_name = api_key_name
        if api_key_name in data:
            _LOGGER.info(
                "Secret '%s' already exists (%s) — preserved",
                api_key_name,
                _mask(str(data[api_key_name])),
            )
        else:
            value = _generate_noise_psk()
            data[api_key_name] = value
            result.api_key_created = True
            changed = True
            _LOGGER.info(
                "Generated new secret '%s' (%s)",
                api_key_name,
                _mask(value),
            )

        # 2) Per-device OTA password (matches yaml_generator output:
        #    !secret edge101_ota_password_{suffix})
        ota_key_name = f"edge101_ota_password_{mac_suffix}"
        result.ota_key_name = ota_key_name
        if ota_key_name in data:
            _LOGGER.info(
                "Secret '%s' already exists — preserved",
                ota_key_name,
            )
        else:
            value = _generate_ota_password()
            data[ota_key_name] = value
            result.ota_key_created = True
            changed = True
            _LOGGER.info(
                "Generated new per-device secret '%s'",
                ota_key_name,
            )

        if changed:
            try:
                _write_secrets_locked(secrets_path, data)
                _LOGGER.info(
                    "ESPHome secrets updated: %s (api_key=%s, ota=%s)",
                    secrets_path.name,
                    "created" if result.api_key_created else "existed",
                    "created" if result.ota_key_created else "existed",
                )
            except Exception as exc:
                msg = f"Failed to write {secrets_path}: {exc}"
                _LOGGER.error(msg)
                result.errors.append(msg)
        else:
            # Even if no new secrets, ensure permissions are tight.
            _enforce_permissions(secrets_path)
        # ── End critical section ──────────────────────────────────
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return result


async def ensure_device_secrets(
    hass: HomeAssistant,
    mac_suffix: str,
) -> ProvisionResult:
    """Async wrapper — runs ``ensure_device_secrets_sync`` in executor."""
    return await hass.async_add_executor_job(
        ensure_device_secrets_sync,
        hass.config.config_dir,
        mac_suffix,
    )
