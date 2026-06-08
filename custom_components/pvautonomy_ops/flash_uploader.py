"""OTA Flash Uploader — espota2 SHA256 protocol (Phase 3.4 MVP).

Implements the ESPHome OTA protocol (v2.0, SHA256 auth) as an async
coroutine for use within Home Assistant custom integrations.

Protocol reference: esphome/esphome/espota2.py (canonical)
Directive: WORKER-PROMPT-ADDON-P3-4-001.v2

STOP-THE-LINE:
  - SHA256 auth ONLY (no MD5 auth fallback)
  - No hardcoded IPs or passwords
  - Secrets sourced from HA/ESPHome secrets.yaml at runtime
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import logging
import secrets as secrets_module
import socket as socket_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# ── espota2 protocol constants (from esphome/espota2.py, canonical) ──

MAGIC_BYTES = bytes([0x6C, 0x26, 0xF7, 0x5C, 0x45])

# Response codes
RESPONSE_OK = 0x00
RESPONSE_REQUEST_AUTH = 0x01        # MD5 — REFUSE
RESPONSE_REQUEST_SHA256_AUTH = 0x02  # SHA256 — ACCEPT

RESPONSE_HEADER_OK = 0x40
RESPONSE_AUTH_OK = 0x41
RESPONSE_UPDATE_PREPARE_OK = 0x42
RESPONSE_BIN_MD5_OK = 0x43
RESPONSE_RECEIVE_OK = 0x44
RESPONSE_UPDATE_END_OK = 0x45
RESPONSE_SUPPORTS_COMPRESSION = 0x46
RESPONSE_CHUNK_OK = 0x47

# Error codes
RESPONSE_ERROR_MAGIC = 0x80
RESPONSE_ERROR_UPDATE_PREPARE = 0x81
RESPONSE_ERROR_AUTH_INVALID = 0x82
RESPONSE_ERROR_WRITING_FLASH = 0x83
RESPONSE_ERROR_UPDATE_END = 0x84
RESPONSE_ERROR_INVALID_BOOTSTRAPPING = 0x85
RESPONSE_ERROR_WRONG_CURRENT_FLASH_CONFIG = 0x86
RESPONSE_ERROR_WRONG_NEW_FLASH_CONFIG = 0x87
RESPONSE_ERROR_ESP8266_NOT_ENOUGH_SPACE = 0x88
RESPONSE_ERROR_ESP32_NOT_ENOUGH_SPACE = 0x89
RESPONSE_ERROR_NO_UPDATE_PARTITION = 0x8A
RESPONSE_ERROR_MD5_MISMATCH = 0x8B
RESPONSE_ERROR_UNKNOWN = 0xFF

# Version / feature flags
OTA_VERSION_2_0 = 2
FEATURE_SUPPORTS_COMPRESSION = 0x01
FEATURE_SUPPORTS_SHA256_AUTH = 0x02

# Transfer constants
UPLOAD_BLOCK_SIZE = 8192
OTA_DEFAULT_PORT = 3232
OTA_CONNECT_TIMEOUT = 20.0
OTA_DATA_TIMEOUT = 90.0

# Error descriptions
_ERROR_MESSAGES: dict[int, str] = {
    RESPONSE_ERROR_MAGIC: "Invalid magic byte",
    RESPONSE_ERROR_UPDATE_PREPARE: "Couldn't prepare flash memory for update",
    RESPONSE_ERROR_AUTH_INVALID: "Authentication invalid (wrong OTA password?)",
    RESPONSE_ERROR_WRITING_FLASH: "Writing OTA data to flash memory failed",
    RESPONSE_ERROR_UPDATE_END: "Finishing update failed",
    RESPONSE_ERROR_INVALID_BOOTSTRAPPING: "Please press the reset button on the ESP",
    RESPONSE_ERROR_WRONG_CURRENT_FLASH_CONFIG: "ESP has wrong flash size",
    RESPONSE_ERROR_WRONG_NEW_FLASH_CONFIG: "ESP does not have the requested flash size",
    RESPONSE_ERROR_ESP8266_NOT_ENOUGH_SPACE: "ESP8266 not enough space",
    RESPONSE_ERROR_ESP32_NOT_ENOUGH_SPACE: "ESP32 OTA partition too small",
    RESPONSE_ERROR_NO_UPDATE_PARTITION: "OTA partition not found",
    RESPONSE_ERROR_MD5_MISMATCH: "Application MD5 mismatch",
    RESPONSE_ERROR_UNKNOWN: "Unknown error from device",
}


class OTAError(Exception):
    """OTA upload failed."""


# ── Protocol helpers ─────────────────────────────────────────────────


async def _recv_exactly(
    reader: asyncio.StreamReader, n: int, description: str
) -> bytes:
    """Read exactly *n* bytes from stream, raising on timeout or short read."""
    try:
        data = await asyncio.wait_for(reader.readexactly(n), timeout=OTA_DATA_TIMEOUT)
    except asyncio.IncompleteReadError as exc:
        raise OTAError(
            f"Connection closed while reading {description} "
            f"(got {len(exc.partial)}/{n} bytes)"
        ) from exc
    except asyncio.TimeoutError as exc:
        raise OTAError(f"Timeout reading {description} (waited {OTA_DATA_TIMEOUT}s)") from exc
    return data


def _check_response(
    data: bytes, expected: int | list[int] | None, description: str
) -> None:
    """Validate a single response byte against expected value(s)."""
    if expected is None:
        return
    if not data:
        raise OTAError(f"Empty response for {description}")

    byte_val = data[0]

    # Known error codes
    if byte_val in _ERROR_MESSAGES:
        raise OTAError(f"OTA error ({description}): {_ERROR_MESSAGES[byte_val]}")

    # Validate against expected
    if isinstance(expected, int):
        expected = [expected]
    if byte_val not in expected:
        raise OTAError(
            f"Unexpected response for {description}: 0x{byte_val:02X} "
            f"(expected {[f'0x{e:02X}' for e in expected]})"
        )


# ── Main OTA upload coroutine ───────────────────────────────────────


async def ota_upload(
    hass: HomeAssistant,
    *,
    host: str,
    port: int = OTA_DEFAULT_PORT,
    password: str | None = None,
    firmware_path: Path,
    progress_cb: Callable[[int], Awaitable[None]] | None = None,
    timeout_s: float = 120.0,
) -> None:
    """Upload firmware to device via espota2 protocol (SHA256 auth, OTA v2.0).

    Args:
        hass: Home Assistant instance
        host: Device IP address or hostname
        port: OTA port (default 3232)
        password: OTA password (None = no auth expected)
        firmware_path: Path to firmware.bin file
        progress_cb: Async callback receiving progress percentage (0-100)
        timeout_s: Overall timeout in seconds

    Raises:
        OTAError: If upload fails at any protocol stage
        asyncio.TimeoutError: If overall timeout exceeded
    """
    _LOGGER.info("OTA upload starting → %s:%d", host, port)

    # Read firmware in executor (blocking I/O)
    file_contents = await hass.async_add_executor_job(firmware_path.read_bytes)
    file_size = len(file_contents)
    _LOGGER.info("Firmware: %s (%d bytes)", firmware_path.name, file_size)

    if file_size == 0:
        raise OTAError("Firmware file is empty")

    writer: asyncio.StreamWriter | None = None

    try:
        # ── CONNECT ──────────────────────────────────────────────
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=OTA_CONNECT_TIMEOUT,
        )
        _LOGGER.info("Connected to %s:%d", host, port)

        # Enable TCP_NODELAY for handshake phase
        sock = writer.transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)

        # ── HANDSHAKE: magic + version ───────────────────────────
        writer.write(MAGIC_BYTES)
        await writer.drain()

        version_data = await _recv_exactly(reader, 2, "version")
        _check_response(version_data[:1], RESPONSE_OK, "magic response")
        version = version_data[1]
        _LOGGER.debug("Device OTA version: %d", version)

        if version != OTA_VERSION_2_0:
            raise OTAError(f"Unsupported OTA version: {version} (require v2.0)")

        # ── FEATURES ─────────────────────────────────────────────
        features_byte = FEATURE_SUPPORTS_COMPRESSION | FEATURE_SUPPORTS_SHA256_AUTH
        writer.write(bytes([features_byte]))
        await writer.drain()

        features_resp = await _recv_exactly(reader, 1, "features")
        use_compression = features_resp[0] == RESPONSE_SUPPORTS_COMPRESSION

        if use_compression:
            upload_data = gzip.compress(file_contents, compresslevel=9)
            _LOGGER.info("Compression: %d → %d bytes", file_size, len(upload_data))
        else:
            upload_data = file_contents

        # ── AUTHENTICATION ───────────────────────────────────────
        auth_resp = await _recv_exactly(reader, 1, "auth type")
        auth_type = auth_resp[0]

        if auth_type == RESPONSE_AUTH_OK:
            _LOGGER.debug("Device requires no authentication")

        elif auth_type == RESPONSE_REQUEST_SHA256_AUTH:
            if password is None:
                raise OTAError("Device requires OTA password, but none provided")

            # Receive 64-byte hex nonce
            nonce_bytes = await _recv_exactly(reader, 64, "SHA256 nonce")
            nonce = nonce_bytes.decode("ascii")
            _LOGGER.debug("SHA256 nonce received")

            # Generate client nonce (64 hex chars = 32 bytes of entropy)
            cnonce = secrets_module.token_hex(32)

            # Send cnonce
            writer.write(cnonce.encode("ascii"))
            await writer.drain()

            # Compute challenge response: SHA256(password ‖ nonce ‖ cnonce)
            hasher = hashlib.sha256()
            hasher.update(password.encode("utf-8"))
            hasher.update(nonce.encode("ascii"))
            hasher.update(cnonce.encode("ascii"))
            auth_result = hasher.hexdigest()

            # Send response
            writer.write(auth_result.encode("ascii"))
            await writer.drain()

            # Verify
            auth_ok = await _recv_exactly(reader, 1, "auth result")
            _check_response(auth_ok, RESPONSE_AUTH_OK, "SHA256 authentication")
            _LOGGER.info("SHA256 authentication successful")

        elif auth_type == RESPONSE_REQUEST_AUTH:
            raise OTAError(
                "Device requested MD5 auth — refused (SHA256 required per policy)"
            )
        else:
            raise OTAError(f"Unknown auth type from device: 0x{auth_type:02X}")

        # ── UPLOAD PREPARATION ───────────────────────────────────
        # Disable nodelay for bulk transfer
        if sock:
            sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 0)
            sock.setsockopt(
                socket_module.SOL_SOCKET,
                socket_module.SO_SNDBUF,
                UPLOAD_BLOCK_SIZE * 8,
            )

        upload_size = len(upload_data)

        # Send binary size (4 bytes big-endian)
        size_bytes = upload_size.to_bytes(4, byteorder="big")
        writer.write(size_bytes)
        await writer.drain()

        prep_resp = await _recv_exactly(reader, 1, "update prepare")
        _check_response(prep_resp, RESPONSE_UPDATE_PREPARE_OK, "update prepare")

        # Send file MD5 (integrity check required by protocol)
        upload_md5 = hashlib.md5(upload_data).hexdigest()
        writer.write(upload_md5.encode("ascii"))
        await writer.drain()

        md5_resp = await _recv_exactly(reader, 1, "binary MD5")
        _check_response(md5_resp, RESPONSE_BIN_MD5_OK, "binary MD5 check")

        # ── CHUNK TRANSFER ───────────────────────────────────────
        _LOGGER.info("Uploading %d bytes in %d-byte chunks...", upload_size, UPLOAD_BLOCK_SIZE)
        offset = 0
        last_reported_pct = -1

        while offset < upload_size:
            chunk = upload_data[offset : offset + UPLOAD_BLOCK_SIZE]
            writer.write(chunk)
            await writer.drain()
            offset += len(chunk)

            # Per-chunk ACK (OTA v2.0)
            chunk_resp = await _recv_exactly(reader, 1, f"chunk@{offset}")
            _check_response(chunk_resp, RESPONSE_CHUNK_OK, f"chunk@{offset}")

            # Progress callback (suppress log spam: report every 5%)
            pct = int((offset / upload_size) * 100)
            if progress_cb and pct >= last_reported_pct + 5:
                last_reported_pct = pct
                await progress_cb(pct)

        # ── FINALIZE ─────────────────────────────────────────────
        if sock:
            sock.setsockopt(socket_module.IPPROTO_TCP, socket_module.TCP_NODELAY, 1)

        recv_resp = await _recv_exactly(reader, 1, "receive OK")
        _check_response(recv_resp, RESPONSE_RECEIVE_OK, "receive complete")

        end_resp = await _recv_exactly(reader, 1, "update end")
        _check_response(end_resp, RESPONSE_UPDATE_END_OK, "update end")

        # Final acknowledgement
        writer.write(bytes([RESPONSE_OK]))
        await writer.drain()

        _LOGGER.info("OTA upload complete (%d bytes transferred to %s)", upload_size, host)

    except (OSError, ConnectionError) as exc:
        raise OTAError(f"Network error during OTA upload: {exc}") from exc
    finally:
        if writer:
            writer.close()
            # Device may reset connection during reboot
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ── OTA upload with retry (EPIC-006-A5) ─────────────────────────────


async def ota_upload_with_retry(
    hass: HomeAssistant,
    *,
    host: str,
    port: int = OTA_DEFAULT_PORT,
    password: str | None = None,
    firmware_path: Path,
    progress_cb: Callable[[int], Awaitable[None]] | None = None,
    timeout_s: float = 120.0,
    retries: int = 3,
    delays: tuple[float, ...] = (0, 10, 30),
    retry_cb: Callable[[int, int, Exception], Awaitable[None]] | None = None,
) -> None:
    """OTA upload with automatic retry for transient failures.

    Wraps ``ota_upload()`` with ``retry_async()``.  Hard failures (auth,
    version mismatch) are raised immediately; soft failures (timeout,
    network) are retried.

    Args:
        retry_cb: Optional async callback ``(attempt, max_attempts, exc)``
            called before each retry — use for UI messaging.
        retries: Total attempts (including first). Set to 1 to disable retry.
        delays: Seconds to wait before each retry (index 0 = before attempt 2).

    EPIC-006-A5: OTA Robustness.
    """
    from .retry import is_hard_ota_failure, retry_async

    async def _on_retry(attempt: int, exc: Exception) -> None:
        _LOGGER.info(
            "OTA retry %d/%d for %s:%d after: %s",
            attempt + 1, retries, host, port, exc,
        )
        if retry_cb:
            await retry_cb(attempt, retries, exc)

    async def _guarded_upload() -> None:
        try:
            await ota_upload(
                hass,
                host=host,
                port=port,
                password=password,
                firmware_path=firmware_path,
                progress_cb=progress_cb,
                timeout_s=timeout_s,
            )
        except OTAError as exc:
            if is_hard_ota_failure(exc):
                raise _HardOTAFailure(str(exc)) from exc
            raise

    try:
        await retry_async(
            _guarded_upload,
            retries=retries,
            delays=delays,
            retry_on=(OTAError, asyncio.TimeoutError, OSError, ConnectionError),
            no_retry_on=(_HardOTAFailure,),
            on_retry=_on_retry,
        )
    except _HardOTAFailure as exc:
        raise OTAError(str(exc)) from exc.__cause__


class _HardOTAFailure(Exception):
    """Internal sentinel: non-retryable OTA failure."""


# ── Device resolution helpers ────────────────────────────────────────


def _slugify_device_id(device_id: str) -> str:
    """Convert device display name to HA entity-friendly slug.

    Example: "PVAutonomy Modbus Bridge 2eb1e4" → "pvautonomy_modbus_bridge_2eb1e4"
    Already-slugified IDs like "sph10k_haus_03" pass through unchanged.
    """
    return device_id.lower().replace(" ", "_").replace("-", "_")


def resolve_device_ip(
    hass: HomeAssistant, device_id: str, *, ha_device_id: str | None = None
) -> tuple[str | None, str, int]:
    """Resolve device IP from HA entity states.

    Search strategy (ordered by priority):
      1. Registry-based: If ha_device_id is provided (or resolved from display
         name), query Entity Registry for all entities of that HA device,
         find the IP entity, and return its state. This works regardless of
         entity_id prefix (handles legacy naming before entity_reset runs).
      2. Slug-based patterns: text_sensor/sensor.{slug}_ip_adresse/ip_address
      3. Fuzzy: any text_sensor with slug + 'ip' in name
      4. ESPHome config entry host: Falls back to the host stored in the
         ESPHome config entry data. This handles the case where entity states
         are unavailable (e.g. Factory device with stale noise_psk in config
         entry — entities are unavailable but the host IP is still valid).

    Args:
        hass: Home Assistant instance
        device_id: Device display name or slug (for fallback patterns)
        ha_device_id: HA Device Registry UUID (primary, robust lookup)

    Returns:
        Tuple of (ip, method, duration_ms):
            - ip: IP address string, or None if not resolvable
            - method: Resolution method used ("registry"|"slug_pattern"|"fuzzy"|"esphome_config"|"none")
            - duration_ms: Time spent resolving in milliseconds
    """
    import time
    t0 = time.monotonic()

    slug = _slugify_device_id(device_id)
    _LOGGER.debug(
        "Resolving IP for device '%s' (slug: '%s', ha_device_id: %s)",
        device_id, slug, ha_device_id or "None",
    )

    # ── Priority 1: Registry-based lookup (robust, prefix-independent) ──
    resolved_dev_id = ha_device_id
    if not resolved_dev_id:
        resolved_dev_id = resolve_ha_device_id_by_name(hass, device_id)

    if resolved_dev_id:
        ip = _resolve_ip_from_device_entities(hass, resolved_dev_id)
        if ip:
            duration_ms = int((time.monotonic() - t0) * 1000)
            _LOGGER.info(
                "IP resolved: device=%s ip=%s method=registry duration_ms=%d",
                device_id, ip, duration_ms,
            )
            return ip, "registry", duration_ms
        _LOGGER.debug("Registry lookup found device %s but no IP entity; trying slug patterns", resolved_dev_id[:8])

    # ── Priority 2: Slug-based patterns (most specific first) ──
    patterns = [
        f"text_sensor.{slug}_ip_adresse",
        f"sensor.{slug}_ip_adresse",
        f"text_sensor.{slug}_ip_address",
        f"sensor.{slug}_ip_address",
    ]

    for entity_id in patterns:
        state = hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable", ""):
            duration_ms = int((time.monotonic() - t0) * 1000)
            _LOGGER.info(
                "IP resolved: device=%s ip=%s method=slug_pattern entity=%s duration_ms=%d",
                device_id, state.state, entity_id, duration_ms,
            )
            return state.state, "slug_pattern", duration_ms

    # ── Priority 3: Fuzzy fallback (scan text_sensors for slug + "ip") ──
    for entity_id in hass.states.async_entity_ids("text_sensor"):
        if slug in entity_id and "ip" in entity_id.lower():
            state = hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable", ""):
                duration_ms = int((time.monotonic() - t0) * 1000)
                _LOGGER.info(
                    "IP resolved: device=%s ip=%s method=fuzzy entity=%s duration_ms=%d",
                    device_id, state.state, entity_id, duration_ms,
                )
                return state.state, "fuzzy", duration_ms

    # ── Priority 4: ESPHome config entry host (last resort) ──
    if resolved_dev_id is None:
        _LOGGER.debug(
            "ESPHome config-entry IP fallback skipped: no resolved_dev_id "
            "(device='%s', slug='%s', ha_device_id was not provided and "
            "name lookup returned None)",
            device_id, slug,
        )
    ip = _resolve_ip_from_esphome_config_entry(hass, resolved_dev_id)
    if ip:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _LOGGER.info(
            "IP resolved: device=%s ip=%s method=esphome_config duration_ms=%d "
            "(entity states were unavailable)", device_id, ip, duration_ms,
        )
        return ip, "esphome_config", duration_ms

    duration_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.warning(
        "IP resolve FAILED: device=%s slug=%s duration_ms=%d (all methods exhausted)",
        device_id, slug, duration_ms,
    )
    return None, "none", duration_ms


def resolve_ha_device_id_by_name(hass: HomeAssistant, display_name: str) -> str | None:
    """Resolve HA Device Registry UUID from device display name.

    Searches the Device Registry for a device whose name matches the
    given display name (case-insensitive, slug-normalized comparison).

    Args:
        hass: Home Assistant instance
        display_name: Device display name (e.g. "Sph10K Haus 03")

    Returns:
        Device Registry UUID string, or None
    """
    from homeassistant.helpers import device_registry as dr

    try:
        dev_reg = dr.async_get(hass)
    except Exception:
        _LOGGER.debug("Device Registry not available for name lookup")
        return None

    target = display_name.lower().replace(" ", "_").replace("-", "_")

    for device_entry in dev_reg.devices.values():
        dev_name = (device_entry.name or "").lower().replace(" ", "_").replace("-", "_")
        if dev_name == target:
            _LOGGER.debug(
                "Resolved display name '%s' → device_id=%s", display_name, device_entry.id
            )
            return device_entry.id

    _LOGGER.debug("No device found for display name '%s'", display_name)
    return None


def _resolve_ip_from_device_entities(hass: HomeAssistant, ha_device_id: str) -> str | None:
    """Find IP address entity among all entities of an HA device.

    Queries the Entity Registry for all entities belonging to the device,
    then looks for one whose entity_id or unique_id contains 'ip_adresse'
    or 'ip_address' and returns its current state.

    Args:
        hass: Home Assistant instance
        ha_device_id: HA Device Registry UUID

    Returns:
        IP address string, or None
    """
    from homeassistant.helpers import entity_registry as er

    try:
        ent_reg = er.async_get(hass)
    except Exception:
        return None

    # Get all entities for this device
    for entry in ent_reg.entities.values():
        if entry.device_id != ha_device_id:
            continue

        eid_lower = entry.entity_id.lower()
        uid_lower = (entry.unique_id or "").lower()

        if "ip_adresse" in eid_lower or "ip_address" in eid_lower \
                or "ip_adresse" in uid_lower or "ip_address" in uid_lower:
            state = hass.states.get(entry.entity_id)
            if state and state.state not in ("unknown", "unavailable", ""):
                return state.state

    return None


def _resolve_ip_from_esphome_config_entry(
    hass: HomeAssistant, ha_device_id: str | None
) -> str | None:
    """Resolve device IP from ESPHome config entry's stored host.

    ESPHome config entries store the device host/IP in entry.data["host"].
    This works even when entity states are unavailable (e.g. Factory device
    with stale noise_psk causing Noise handshake failure).

    Args:
        hass: Home Assistant instance.
        ha_device_id: HA Device Registry UUID.

    Returns:
        IP address string, or None.
    """
    if not ha_device_id:
        return None

    from homeassistant.helpers import device_registry as dr

    try:
        dev_reg = dr.async_get(hass)
    except Exception:
        return None

    device_entry = dev_reg.async_get(ha_device_id)
    if not device_entry:
        return None

    for entry_id in device_entry.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or entry.domain != "esphome":
            continue

        host = entry.data.get("host", "")
        if not host:
            continue

        # Validate it looks like an IP address (not a hostname that might
        # not resolve). Accept dotted-quad IPv4 addresses directly.
        parts = host.split(".")
        if len(parts) == 4:
            with contextlib.suppress(ValueError):
                if all(0 <= int(p) <= 255 for p in parts):
                    _LOGGER.debug(
                        "ESPHome config entry %s has host=%s",
                        entry_id[:8], host,
                    )
                    return host

        # Also accept raw hostname — caller can try connecting
        _LOGGER.debug(
            "ESPHome config entry %s has non-IP host=%s, returning anyway",
            entry_id[:8], host,
        )
        return host

    return None


def _extract_mac_suffix(device_id: str) -> str | None:
    """Extract 6-char hex MAC suffix from device display name or slug.

    Examples:
        "PVAutonomy Modbus Bridge 2eb1e4" → "2eb1e4"
        "pvautonomy_modbus_bridge_2eb1e4" → "2eb1e4"
        "68:25:DD:2E:B1:E4"              → "2eb1e4"
        "sph10k_haus_03"                 → None (no MAC suffix)
    """
    from .mac_utils import InvalidMACError, canonical_mac_last6
    try:
        return canonical_mac_last6(device_id)
    except InvalidMACError:
        return None


@dataclass
class OtaPasswordResult:
    """Metadata-rich OTA password lookup result.

    Ref: WORKER-PROMPT-OTA-PASSWORD-POLICY-RECONCILIATION D1a.
    """

    password: str
    key_name: str       # e.g. "ota_password" or "edge101_ota_password_2eb1e4"
    source_file: str    # e.g. "/config/esphome/secrets.yaml"
    scope: str          # "site-wide" or "per-device"


def get_ota_password(hass: HomeAssistant, device_id: str) -> OtaPasswordResult | None:
    """Read OTA password from HA or ESPHome secrets.yaml.

    Resolves device_id → MAC suffix → secret key dynamically.

    Priority (aligned with production YAML contract):
      1. edge101_ota_password_{mac_suffix}  (per-device — supported path)
      2. ota_password                       (site-wide — local-only legacy)

    The per-device name is tried first because the production YAML
    (yaml_generator) emits ``!secret edge101_ota_password_{suffix}``
    and the compile_secrets path must forward the matching key name.

    Site-wide ``ota_password`` is accepted as a **local-only** legacy
    fallback so the SEC-010 gate does not hard-fail on old dev/test
    systems.  It is **not** a supported end-to-end path: the remote
    GHA workflow only injects per-device OTA secrets.  Systems that
    still rely on site-wide-only should be migrated to per-device
    secrets via the wizard.

    Args:
        hass: Home Assistant instance
        device_id: Device display name or slug (must contain MAC suffix)

    Returns:
        OtaPasswordResult with password + metadata, or None
    """
    mac_suffix = _extract_mac_suffix(device_id)

    # Per-device FIRST (supported path), site-wide SECOND (local-only legacy)
    secret_keys: list[tuple[str, str]] = []
    if mac_suffix:
        secret_keys.append(
            (f"edge101_ota_password_{mac_suffix}", "per-device"),
        )
    secret_keys.append(("ota_password", "site-wide"))
    if not mac_suffix:
        _LOGGER.debug(
            "No MAC suffix in device_id '%s' — site-wide ota_password only",
            device_id,
        )

    config_dir = Path(hass.config.config_dir)
    search_paths = [
        config_dir / "esphome" / "secrets.yaml",  # ESPHome secrets (primary)
        config_dir / "secrets.yaml",               # HA core secrets (fallback)
    ]

    tried_keys: list[str] = []
    tried_files: list[str] = []

    for secret_key, scope in secret_keys:
        tried_keys.append(secret_key)
        for secrets_path in search_paths:
            if str(secrets_path) not in tried_files:
                tried_files.append(str(secrets_path))
            try:
                if not secrets_path.exists():
                    continue
                with open(secrets_path, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if data and secret_key in data:
                    _LOGGER.info(
                        "OTA password loaded from %s (key: %s, scope: %s, device: %s)",
                        secrets_path.name, secret_key, scope, device_id,
                    )
                    return OtaPasswordResult(
                        password=data[secret_key],
                        key_name=secret_key,
                        source_file=str(secrets_path),
                        scope=scope,
                    )
            except Exception as exc:
                _LOGGER.warning("Failed to read %s: %s", secrets_path, exc)

    _LOGGER.warning(
        "OTA password not found (tried: %s in %s)",
        ", ".join(tried_keys),
        ", ".join(p.rsplit("/", 1)[-1] for p in tried_files),
    )
    return None
