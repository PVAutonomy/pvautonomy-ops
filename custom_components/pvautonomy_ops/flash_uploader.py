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
import gzip
import hashlib
import logging
import secrets as secrets_module
import socket as socket_module
from collections.abc import Awaitable, Callable
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
            try:
                await writer.wait_closed()
            except Exception:
                pass  # Device may reset connection during reboot


# ── Device resolution helpers ────────────────────────────────────────


def resolve_device_ip(hass: HomeAssistant, device_id: str) -> str | None:
    """Resolve device IP from HA entity states.

    Search strategy (ordered by priority):
      1. text_sensor.{device_id}_ip_adresse  (ESPHome wifi_info German)
      2. text_sensor.{device_id}_ip_address  (ESPHome wifi_info English)
      3. sensor.{device_id}_ip_*             (fallback pattern)
      4. Fuzzy: any text_sensor with device_id + 'ip' in name

    Args:
        hass: Home Assistant instance
        device_id: Device identifier (e.g., "sph10k_haus_03")

    Returns:
        IP address string, or None if not resolvable
    """
    # Priority patterns (most specific first)
    patterns = [
        f"text_sensor.{device_id}_ip_adresse",
        f"sensor.{device_id}_ip_adresse",
        f"text_sensor.{device_id}_ip_address",
        f"sensor.{device_id}_ip_address",
    ]

    for entity_id in patterns:
        state = hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable", ""):
            _LOGGER.info("Device IP resolved via %s → %s", entity_id, state.state)
            return state.state

    # Fuzzy fallback: scan text_sensors for device_id + "ip"
    for entity_id in hass.states.async_entity_ids("text_sensor"):
        if device_id in entity_id and "ip" in entity_id.lower():
            state = hass.states.get(entity_id)
            if state and state.state not in ("unknown", "unavailable", ""):
                _LOGGER.info("Device IP resolved via %s → %s (fuzzy)", entity_id, state.state)
                return state.state

    _LOGGER.warning("Could not resolve IP for device '%s'", device_id)
    return None


def get_ota_password(hass: HomeAssistant, device_id: str) -> str | None:
    """Read OTA password from HA or ESPHome secrets.yaml.

    MVP: Reads ``edge101_ota_password_17e9c4`` from ESPHome secrets.
    Future: Map device_id → secret key via device registry metadata.

    Args:
        hass: Home Assistant instance
        device_id: Device identifier (unused in MVP — single-device)

    Returns:
        OTA password string, or None
    """
    # MVP: single known secret key for Edge101 production
    secret_key = "edge101_ota_password_17e9c4"

    config_dir = Path(hass.config.config_dir)
    search_paths = [
        config_dir / "esphome" / "secrets.yaml",  # ESPHome secrets (primary)
        config_dir / "secrets.yaml",               # HA core secrets (fallback)
    ]

    for secrets_path in search_paths:
        try:
            if not secrets_path.exists():
                continue
            with open(secrets_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and secret_key in data:
                _LOGGER.debug(
                    "OTA password loaded from %s (key: %s)", secrets_path.name, secret_key
                )
                return data[secret_key]
        except Exception as exc:
            _LOGGER.warning("Failed to read %s: %s", secrets_path, exc)

    _LOGGER.warning("OTA password not found for key '%s'", secret_key)
    return None
