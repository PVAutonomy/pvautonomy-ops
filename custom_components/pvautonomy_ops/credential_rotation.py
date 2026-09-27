"""Two-phase per-device credential rotation for Edge101 firmware.

The active ESPHome API key and OTA password cannot be replaced before an OTA
upload: the running firmware still authenticates with the old OTA password.
This module therefore keeps a pending credential pair next to the active pair,
binds a prepared firmware artifact to that pending pair, and promotes the pair
only after the OTA upload has succeeded.

No secret value is logged, returned in service metadata, or written to an
artifact marker.  Markers contain only SHA-256 digests of high-entropy values
and the firmware binary so the install step can fail closed on stale/mismatched
state.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from .esphome_secrets import SecretsFileUnreadableError, _read_secrets_strict
from .mac_utils import InvalidMACError, canonical_mac_last6


ESPHOME_SECRETS_RELPATH = Path("esphome") / "secrets.yaml"
ROTATION_MARKER_FILENAME = "credential-rotation.json"
ROTATION_MARKER_VERSION = 1
_SECRET_FILE_MODE = 0o600


class CredentialRotationError(RuntimeError):
    """Raised when rotation state is missing, inconsistent, or stale."""


@dataclass(frozen=True)
class RotationStageResult:
    """Safe metadata from staging a pending credential pair."""

    mac_suffix: str
    api_key_name: str
    ota_key_name: str
    api_key_created: bool
    ota_key_created: bool


@dataclass(frozen=True)
class RotationInstallContext:
    """Validated install context; secret fields are excluded from repr."""

    mac_suffix: str
    target_device: str
    new_api_key: str = field(repr=False)
    new_ota_password: str = field(repr=False)


def _names(mac_suffix: str) -> dict[str, str]:
    return {
        "active_api": f"edge101_api_key_{mac_suffix}",
        "active_ota": f"edge101_ota_password_{mac_suffix}",
        "pending_api": f"pvautonomy_next_api_key_{mac_suffix}",
        "pending_ota": f"pvautonomy_next_ota_password_{mac_suffix}",
        "previous_api": f"pvautonomy_previous_api_key_{mac_suffix}",
        "previous_ota": f"pvautonomy_previous_ota_password_{mac_suffix}",
    }


def _suffix(value: str) -> str:
    try:
        return canonical_mac_last6(value)
    except InvalidMACError as exc:
        raise CredentialRotationError("invalid device MAC suffix") from exc


def _validate_noise_psk(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    try:
        decoded = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise CredentialRotationError(f"{label} is not valid Base64") from exc
    if len(decoded) != 32:
        raise CredentialRotationError(f"{label} must decode to exactly 32 bytes")
    return text


def _validate_ota_password(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not text:
        raise CredentialRotationError(f"{label} is empty")
    return text


def _load_secrets(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CredentialRotationError(
            "ESPHome secrets file is missing; active credentials are required"
        )
    try:
        return _read_secrets_strict(path)
    except SecretsFileUnreadableError as exc:
        # from None: the parser's own error quotes the file, and every
        # traceback printed above this one would carry it (#318).
        raise CredentialRotationError(
            f"ESPHome secrets file cannot be read: {exc.reason}"
        ) from None


def _write_secrets(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.rotation.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write("# ESPHome secrets — auto-managed by PVAutonomy Ops\n")
            handle.write("# Do not remove entries used by active devices.\n\n")
            yaml.safe_dump(data, handle, default_flow_style=False, allow_unicode=True)
        os.chmod(tmp, _SECRET_FILE_MODE)
        tmp.replace(path)
        os.chmod(path, _SECRET_FILE_MODE)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def _locked_secrets(config_dir: str | Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    secrets_path = Path(config_dir) / ESPHOME_SECRETS_RELPATH
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = secrets_path.with_suffix(".yaml.lock")
    try:
        lock_handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
    except OSError as exc:
        raise CredentialRotationError("cannot acquire ESPHome secrets lock") from exc
    try:
        yield secrets_path, _load_secrets(secrets_path)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


def _active_pair(data: dict[str, Any], names: dict[str, str]) -> tuple[str, str]:
    api = _validate_noise_psk(data.get(names["active_api"]), label="active API key")
    ota = _validate_ota_password(
        data.get(names["active_ota"]), label="active OTA password"
    )
    return api, ota


def _pending_pair(data: dict[str, Any], names: dict[str, str]) -> tuple[str, str]:
    has_api = names["pending_api"] in data
    has_ota = names["pending_ota"] in data
    if has_api != has_ota:
        raise CredentialRotationError(
            "pending credential pair is incomplete; refusing partial rotation state"
        )
    if not has_api:
        raise CredentialRotationError("no pending credential rotation is staged")
    api = _validate_noise_psk(data[names["pending_api"]], label="pending API key")
    ota = _validate_ota_password(
        data[names["pending_ota"]], label="pending OTA password"
    )
    return api, ota


def stage_device_rotation_sync(
    config_dir: str | Path, mac_suffix: str
) -> RotationStageResult:
    """Idempotently generate a pending pair while preserving active values."""
    suffix = _suffix(mac_suffix)
    names = _names(suffix)
    with _locked_secrets(config_dir) as (path, data):
        active_api, active_ota = _active_pair(data, names)
        previous_present = (
            names["previous_api"] in data or names["previous_ota"] in data
        )
        if previous_present:
            raise CredentialRotationError(
                "a promoted credential rotation is still pending verification"
            )

        pending_api_present = names["pending_api"] in data
        pending_ota_present = names["pending_ota"] in data
        if pending_api_present != pending_ota_present:
            raise CredentialRotationError(
                "pending credential pair is incomplete; refusing to repair implicitly"
            )

        created = not pending_api_present
        if created:
            pending_api = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            pending_ota = secrets.token_hex(16)
            while pending_api == active_api:
                pending_api = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            while pending_ota == active_ota:
                pending_ota = secrets.token_hex(16)
            data[names["pending_api"]] = pending_api
            data[names["pending_ota"]] = pending_ota
            _write_secrets(path, data)
        else:
            pending_api, pending_ota = _pending_pair(data, names)
            if pending_api == active_api or pending_ota == active_ota:
                raise CredentialRotationError(
                    "pending credentials must differ from active credentials"
                )
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != _SECRET_FILE_MODE:
                os.chmod(path, _SECRET_FILE_MODE)

    return RotationStageResult(
        mac_suffix=suffix,
        api_key_name=names["pending_api"],
        ota_key_name=names["pending_ota"],
        api_key_created=created,
        ota_key_created=created,
    )


def resolve_pending_compile_secrets_sync(
    config_dir: str | Path, mac_suffix: str
) -> dict[str, str]:
    """Return pending values under canonical names for an in-memory compile."""
    suffix = _suffix(mac_suffix)
    names = _names(suffix)
    with _locked_secrets(config_dir) as (_path, data):
        _active_pair(data, names)
        pending_api, pending_ota = _pending_pair(data, names)
    return {
        names["active_api"]: pending_api,
        names["active_ota"]: pending_ota,
    }


def _digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rotation_marker_path(artifact_path: str | Path) -> Path:
    return Path(artifact_path).with_name(ROTATION_MARKER_FILENAME)


def invalidate_rotation_marker_sync(artifact_path: str | Path) -> None:
    """Remove only the non-secret marker; never touches a firmware artifact."""
    marker = rotation_marker_path(artifact_path)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def write_rotation_marker_sync(
    config_dir: str | Path,
    artifact_path: str | Path,
    *,
    target_device: str,
    mac_suffix: str,
    build_id: str | None,
) -> Path:
    """Bind an artifact to the currently staged pending credential pair."""
    artifact = Path(artifact_path)
    if not artifact.is_file():
        raise CredentialRotationError("prepared firmware artifact is missing")
    suffix = _suffix(mac_suffix)
    pending = resolve_pending_compile_secrets_sync(config_dir, suffix)
    names = _names(suffix)
    marker_data = {
        "schema_version": ROTATION_MARKER_VERSION,
        "credential_profile": "pending_rotation",
        "target_device": target_device,
        "mac_suffix": suffix,
        "build_id": build_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_sha256": _digest_file(artifact),
        "api_key_sha256": _digest_secret(pending[names["active_api"]]),
        "ota_password_sha256": _digest_secret(pending[names["active_ota"]]),
    }
    marker = rotation_marker_path(artifact)
    tmp = marker.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(marker_data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, _SECRET_FILE_MODE)
        tmp.replace(marker)
    finally:
        if tmp.exists():
            tmp.unlink()
    return marker


def validate_rotation_artifact_sync(
    config_dir: str | Path,
    artifact_path: str | Path,
    *,
    target_device: str,
    mac_suffix: str,
) -> RotationInstallContext:
    """Validate artifact, device identity, and pending-secret fingerprints."""
    artifact = Path(artifact_path)
    marker = rotation_marker_path(artifact)
    if not artifact.is_file() or not marker.is_file():
        raise CredentialRotationError(
            "credential-rotation artifact marker is missing; rebuild in rotation mode"
        )
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CredentialRotationError("credential-rotation marker is invalid") from exc
    suffix = _suffix(mac_suffix)
    if marker_data.get("schema_version") != ROTATION_MARKER_VERSION:
        raise CredentialRotationError("unsupported credential-rotation marker version")
    if marker_data.get("credential_profile") != "pending_rotation":
        raise CredentialRotationError("artifact was not built with pending credentials")
    if marker_data.get("target_device") != target_device:
        raise CredentialRotationError("rotation artifact belongs to a different device")
    if marker_data.get("mac_suffix") != suffix:
        raise CredentialRotationError("rotation artifact MAC suffix mismatch")
    if marker_data.get("artifact_sha256") != _digest_file(artifact):
        raise CredentialRotationError("rotation artifact digest mismatch")

    pending = resolve_pending_compile_secrets_sync(config_dir, suffix)
    names = _names(suffix)
    pending_api = pending[names["active_api"]]
    pending_ota = pending[names["active_ota"]]
    if marker_data.get("api_key_sha256") != _digest_secret(pending_api):
        raise CredentialRotationError("pending API key no longer matches artifact")
    if marker_data.get("ota_password_sha256") != _digest_secret(pending_ota):
        raise CredentialRotationError("pending OTA password no longer matches artifact")
    return RotationInstallContext(
        mac_suffix=suffix,
        target_device=target_device,
        new_api_key=pending_api,
        new_ota_password=pending_ota,
    )


def promote_device_rotation_sync(config_dir: str | Path, mac_suffix: str) -> None:
    """Atomically promote pending values, retaining the previous pair."""
    suffix = _suffix(mac_suffix)
    names = _names(suffix)
    with _locked_secrets(config_dir) as (path, data):
        active_api, active_ota = _active_pair(data, names)
        pending_api, pending_ota = _pending_pair(data, names)

        previous_api_present = names["previous_api"] in data
        previous_ota_present = names["previous_ota"] in data
        if previous_api_present != previous_ota_present:
            raise CredentialRotationError("previous credential pair is incomplete")

        if active_api == pending_api and active_ota == pending_ota:
            if not previous_api_present:
                raise CredentialRotationError(
                    "active credentials match pending values without rollback state"
                )
            return  # idempotent retry after promotion

        if previous_api_present:
            raise CredentialRotationError(
                "previous rotation state exists but active credentials do not match pending"
            )
        data[names["previous_api"]] = active_api
        data[names["previous_ota"]] = active_ota
        data[names["active_api"]] = pending_api
        data[names["active_ota"]] = pending_ota
        _write_secrets(path, data)


def finalize_device_rotation_sync(config_dir: str | Path, mac_suffix: str) -> None:
    """Remove pending/previous values after encrypted reconnect succeeds."""
    suffix = _suffix(mac_suffix)
    names = _names(suffix)
    with _locked_secrets(config_dir) as (path, data):
        active_api, active_ota = _active_pair(data, names)
        pending_api, pending_ota = _pending_pair(data, names)
        if active_api != pending_api or active_ota != pending_ota:
            raise CredentialRotationError(
                "cannot finalize before pending credentials are promoted"
            )
        if names["previous_api"] not in data or names["previous_ota"] not in data:
            raise CredentialRotationError("rollback credential pair is missing")
        for key in (
            names["pending_api"],
            names["pending_ota"],
            names["previous_api"],
            names["previous_ota"],
        ):
            del data[key]
        _write_secrets(path, data)
