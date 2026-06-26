"""Persistent keyring for ESPHome noise_psk management.

Stores production API encryption keys per device UID (mac_suffix) using
Home Assistant's Store API.  Keys persist across HA restarts.

Ref:
  - Directive D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001
  - Directive D-OPS-KEYRING-STRATEGY-001
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "pvautonomy_ops_keyring"
STORAGE_VERSION = 1

# TASK-20260520 Phase 2: AES-256 key is exactly 64 hex chars (32 bytes).
# Public alias used by the wizard preflight (fix/#113).
COMPILE_SECRET_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMPILE_SECRET_KEY_RE = COMPILE_SECRET_KEY_RE

# Timeouts for ESPHome entry operations
_ENTRY_FIND_TIMEOUT_S = 90.0
_ENTRY_FIND_POLL_S = 3.0
_RELOAD_VERIFY_TIMEOUT_S = 60.0
_VERIFY_POLL_S = 3.0


# ---------------------------------------------------------------------------
# Key masking (AD-2: no plaintext keys in any log output)
# ---------------------------------------------------------------------------


def mask_key(key: str | None) -> str:
    """Redact a secret for safe logging.

    Returns only a neutral marker — never any portion of the key (no
    prefix, suffix, length-derived hint, or any other fragment).
    Present is distinguished from empty/missing, which is safe and useful
    and reveals nothing about the key's value.

    >>> mask_key("K556Qm9wxN49LKkNIzTbdrR2lToHz9N3bss8XQ0TaO0=")
    '<redacted>'
    >>> mask_key("")
    '<empty>'
    >>> mask_key(None)
    '<empty>'
    """
    if not key:
        return "<empty>"
    return "<redacted>"


# ---------------------------------------------------------------------------
# Persistent Keyring (D1 — HA Store)
# ---------------------------------------------------------------------------


class PVAutonomyKeyring:
    """Persistent keyring using HA Store API.

    Stores noise_psk values keyed by device_uid (mac_suffix).
    Thread-safe via HA Store's internal locking.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"version": 1, "keys": {}}

    async def async_load(self) -> None:
        """Load keyring from persistent storage.  Call once during setup."""
        stored = await self._store.async_load()
        if stored and isinstance(stored, dict):
            self._data = stored
        else:
            self._data = {"version": 1, "keys": {}}
        count = len(self._data.get("keys", {}))
        _LOGGER.info("Keyring loaded: %d device key(s)", count)

    async def _async_save(self) -> None:
        """Persist current keyring state."""
        await self._store.async_save(self._data)

    async def set_production_noise_psk(
        self, device_uid: str, key: str
    ) -> None:
        """Store a production noise_psk for a device.

        Args:
            device_uid: Device identifier (mac_suffix, e.g. "2eb1e4").
            key: Base64 noise_psk value.
        """
        self._data.setdefault("keys", {})
        self._data["keys"][device_uid] = {
            "noise_psk": key,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._async_save()
        _LOGGER.info(
            "Keyring: stored noise_psk for device %s (key=%s)",
            device_uid,
            mask_key(key),
        )

    async def get_production_noise_psk(self, device_uid: str) -> str | None:
        """Retrieve a stored noise_psk for a device.

        Returns:
            Base64 noise_psk string or None if not found.
        """
        entry = self._data.get("keys", {}).get(device_uid)
        if entry:
            key = entry.get("noise_psk")
            _LOGGER.debug(
                "Keyring: retrieved noise_psk for %s (key=%s)",
                device_uid,
                mask_key(key),
            )
            return key
        _LOGGER.debug("Keyring: no key found for device %s", device_uid)
        return None

    async def clear_production_noise_psk(self, device_uid: str) -> None:
        """Remove a stored noise_psk for a device."""
        keys = self._data.get("keys", {})
        if device_uid in keys:
            del keys[device_uid]
            await self._async_save()
            _LOGGER.info("Keyring: cleared noise_psk for device %s", device_uid)
        else:
            _LOGGER.debug(
                "Keyring: clear requested for %s but no key stored", device_uid
            )

    # -----------------------------------------------------------------
    # TASK-20260520 Phase 2 (A′): repo-wide AES-256 compile-secret key
    # -----------------------------------------------------------------
    # This is the symmetric key HA uses to AES-256-GCM-encrypt the legacy
    # `encrypted_secrets` compile payload before it is sent to the proxy.
    # It MUST equal the GitHub Actions repo secret `COMPILE_SECRET_KEY`
    # (64 hex chars / 32 bytes) so the build workflow can decrypt it.
    #
    # It is repo-wide (not device-scoped) and is stored under a dedicated
    # top-level key, separate from the per-device noise_psk map. The real
    # value is provisioned out-of-band by the operator; it is never logged
    # in full (see mask_key) and never derived from a default.

    async def set_compile_secret_key(self, key: str) -> None:
        """Store the repo-wide AES-256 compile-secret key (64 hex chars).

        Args:
            key: 64-character hex string (32-byte AES-256 key) that matches
                the GitHub Actions ``COMPILE_SECRET_KEY`` repo secret.

        Raises:
            ValueError: if ``key`` is not exactly 64 hex characters. The
                rejected value is NOT included in the error or any log.
        """
        if not isinstance(key, str) or not _COMPILE_SECRET_KEY_RE.match(key):
            # Do not echo the rejected value.
            raise ValueError(
                "compile_secret_key must be 64 hex characters (AES-256)"
            )
        self._data["compile_secret_key"] = {
            "key": key,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._async_save()
        # L-1: never log any substring of the key (mask_key would still
        # reveal 8 hex chars). Log a non-reversible sha256 fingerprint only,
        # for correlation across rotations.
        fingerprint = hashlib.sha256(key.encode("ascii")).hexdigest()[:8]
        _LOGGER.info(
            "Keyring: stored compile_secret_key (fingerprint=%s)", fingerprint
        )

    async def get_compile_secret_key(self) -> str | None:
        """Return the stored repo-wide compile-secret key, or None.

        Returns the raw 64-hex string for in-process encryption use only;
        callers must never log it (use mask_key for any diagnostics).
        """
        entry = self._data.get("compile_secret_key")
        if entry and isinstance(entry, dict):
            return entry.get("key")
        return None

    async def clear_compile_secret_key(self) -> None:
        """Remove the stored repo-wide compile-secret key (e.g. on rotation)."""
        if "compile_secret_key" in self._data:
            del self._data["compile_secret_key"]
            await self._async_save()
            _LOGGER.info("Keyring: cleared compile_secret_key")


# ---------------------------------------------------------------------------
# D2 — Resolve noise_psk from esphome/secrets.yaml
# ---------------------------------------------------------------------------


def resolve_noise_psk_from_secrets(
    hass: HomeAssistant, mac_suffix: str
) -> str | None:
    """Read ``edge101_api_key_{mac_suffix}`` from esphome/secrets.yaml.

    Auflage 1: **Per-device key only** — no generic ``api_encryption_key``
    fallback.  Prevents cross-device key conflicts.

    Follows the same file-search pattern as
    ``flash_uploader.get_ota_password()`` (line 508).

    Args:
        hass: Home Assistant instance.
        mac_suffix: 6-char hex MAC suffix (e.g. "2eb1e4").

    Returns:
        Base64 API encryption key string, or None.
    """
    if not mac_suffix:
        _LOGGER.warning("resolve_noise_psk: empty mac_suffix — cannot resolve")
        return None

    secret_key = f"edge101_api_key_{mac_suffix}"

    config_dir = Path(hass.config.config_dir)
    search_paths = [
        config_dir / "esphome" / "secrets.yaml",  # ESPHome secrets (primary)
        config_dir / "secrets.yaml",  # HA core secrets (fallback)
    ]

    for secrets_path in search_paths:
        try:
            if not secrets_path.exists():
                continue
            with open(secrets_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and secret_key in data:
                value = str(data[secret_key])
                _LOGGER.info(
                    "noise_psk resolved from %s (key=%s, value=%s)",
                    secrets_path.name,
                    secret_key,
                    mask_key(value),
                )
                return value
        except Exception:
            _LOGGER.warning(
                "Failed to read %s for noise_psk resolution",
                secrets_path,
                exc_info=True,
            )

    # Migration aid: check if OTHER edge101_api_key_* entries exist
    # (indicates a MAC-suffix mismatch from old generator runs)
    other_keys: list[str] = []
    try:
        esp_secrets = config_dir / "esphome" / "secrets.yaml"
        if esp_secrets.exists():
            with open(esp_secrets, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            other_keys = [
                k for k in data
                if k.startswith("edge101_api_key_") and k != secret_key
            ]
    except Exception:
        _LOGGER.debug("secrets.yaml sibling-key scan failed", exc_info=True)

    if other_keys:
        _LOGGER.warning(
            "noise_psk not found: '%s' missing in esphome/secrets.yaml. "
            "However, OTHER device keys exist: %s — "
            "this may indicate a MAC-suffix mismatch from an old generator run. "
            "Verify that the production YAML uses the correct MAC suffix "
            "(canonical_mac_last6 of the device's WiFi MAC).",
            secret_key,
            ", ".join(sorted(other_keys)),
        )
    else:
        _LOGGER.warning(
            "noise_psk not found for mac_suffix '%s' — "
            "secret '%s' not provisioned in esphome/secrets.yaml. "
            "Device will require manual key entry.",
            mac_suffix,
            secret_key,
        )
    return None


# ---------------------------------------------------------------------------
# D2 supplementary — extract secret name from rendered YAML
# ---------------------------------------------------------------------------


def extract_noise_psk_from_yaml(yaml_text: str) -> str | None:
    """Extract the ``api.encryption.key`` secret name from rendered YAML.

    Since generated YAML uses ``!secret`` references (not literal values),
    this extracts the secret NAME.  Use ``resolve_noise_psk_from_secrets()``
    to get the actual value.

    Returns:
        Secret name (e.g. "edge101_api_key_2eb1e4") or None.
    """
    import re

    # Match: key: !secret edge101_api_key_2eb1e4
    m = re.search(r"^\s*key:\s*!secret\s+(\S+)", yaml_text, re.MULTILINE)
    if m:
        return m.group(1)

    # Fallback: unreplaced __SECRET_xxx__ token
    m = re.search(
        r'^\s*key:\s*["\']?__SECRET_([A-Za-z0-9_-]+)__["\']?',
        yaml_text,
        re.MULTILINE,
    )
    if m:
        return m.group(1)

    return None


# ---------------------------------------------------------------------------
# D3 — Find ESPHome config entry (MAC-first, Auflage 2)
# ---------------------------------------------------------------------------


def _find_esphome_config_entry_by_mac(
    hass: HomeAssistant, device_mac: str
) -> Any | None:
    """Find ESPHome config entry via Device Registry MAC connection.

    This is the primary (most stable) lookup strategy.

    Args:
        hass: Home Assistant instance.
        device_mac: Full MAC address (colon-separated or raw hex).

    Returns:
        ConfigEntry or None.
    """
    if not device_mac:
        return None

    dev_reg = dr.async_get(hass)
    mac_normalized = device_mac.lower().replace("-", ":").replace("_", ":")

    # If raw hex (no colons), insert colons: aabbccddeeff → aa:bb:cc:dd:ee:ff
    if ":" not in mac_normalized and len(mac_normalized) == 12:
        mac_normalized = ":".join(
            mac_normalized[i : i + 2] for i in range(0, 12, 2)
        )

    for device_entry in dev_reg.devices.values():
        for conn_type, conn_id in device_entry.connections:
            if (
                conn_type == dr.CONNECTION_NETWORK_MAC
                and conn_id.lower() == mac_normalized
            ):
                # Found device by MAC — look for ESPHome config entry
                for entry_id in device_entry.config_entries:
                    entry = hass.config_entries.async_get_entry(entry_id)
                    if entry and entry.domain == "esphome":
                        _LOGGER.debug(
                            "ESPHome entry found via MAC %s: entry_id=%s",
                            mask_key(mac_normalized),
                            entry_id[:8],
                        )
                        return entry
    return None


def _find_esphome_config_entry(
    hass: HomeAssistant,
    *,
    device_mac: str = "",
    ha_device_id: str = "",
    device_names: list[str] | None = None,
) -> Any | None:
    """Find the ESPHome config entry for a device.

    Search priority (Auflage 2 — MAC first):
      1. MAC lookup via Device Registry connections
      2. ha_device_id → device_entry.config_entries → domain=esphome
      3. Device name match in Device Registry
      4. Config entry title match (last resort)

    Args:
        hass: Home Assistant instance.
        device_mac: Full MAC address (preferred lookup key).
        ha_device_id: HA Device Registry UUID.
        device_names: List of possible device names to match (old + new).

    Returns:
        ConfigEntry or None.
    """
    _LOGGER.debug(
        "find_esphome: START mac=%s device_id=%s names=%s",
        mask_key(device_mac) if device_mac else "<none>",
        ha_device_id[:8] if ha_device_id else "<none>",
        device_names,
    )

    # Strategy 1: MAC (most stable)
    entry = _find_esphome_config_entry_by_mac(hass, device_mac)
    if entry:
        _LOGGER.debug(
            "find_esphome: S1-MAC HIT → entry=%s device_name='%s'",
            entry.entry_id[:8],
            entry.data.get("device_name", "?"),
        )
        return entry
    _LOGGER.debug("find_esphome: S1-MAC miss (mac=%s)", mask_key(device_mac))

    dev_reg = dr.async_get(hass)

    # Strategy 2: ha_device_id
    if ha_device_id:
        device_entry = dev_reg.async_get(ha_device_id)
        if device_entry:
            _LOGGER.debug(
                "find_esphome: S2-DEVID checking device '%s' entries=%s",
                device_entry.name,
                list(device_entry.config_entries),
            )
            for entry_id in device_entry.config_entries:
                cfg = hass.config_entries.async_get_entry(entry_id)
                if cfg and cfg.domain == "esphome":
                    _LOGGER.debug(
                        "find_esphome: S2-DEVID HIT → entry=%s "
                        "device_name='%s'",
                        entry_id[:8],
                        cfg.data.get("device_name", "?"),
                    )
                    return cfg
                _LOGGER.debug(
                    "find_esphome: S2-DEVID skip entry=%s domain=%s",
                    entry_id[:8] if entry_id else "?",
                    cfg.domain if cfg else "<gone>",
                )
        else:
            _LOGGER.debug(
                "find_esphome: S2-DEVID miss (device_id=%s not in registry)",
                ha_device_id[:8],
            )
    else:
        _LOGGER.debug("find_esphome: S2-DEVID skip (no ha_device_id)")

    # Strategy 3: Name match in Device Registry
    if device_names:
        targets = {
            n.lower().replace(" ", "_").replace("-", "_") for n in device_names if n
        }
        _LOGGER.debug("find_esphome: S3-NAME targets=%s", targets)
        for device_entry in dev_reg.devices.values():
            dev_name = (device_entry.name or "").lower().replace(" ", "_").replace("-", "_")
            if dev_name and dev_name in targets:
                for entry_id in device_entry.config_entries:
                    cfg = hass.config_entries.async_get_entry(entry_id)
                    if cfg and cfg.domain == "esphome":
                        _LOGGER.debug(
                            "find_esphome: S3-NAME HIT → device='%s' "
                            "entry=%s device_name='%s'",
                            device_entry.name,
                            entry_id[:8],
                            cfg.data.get("device_name", "?"),
                        )
                        return cfg
        _LOGGER.debug("find_esphome: S3-NAME miss")
    else:
        _LOGGER.debug("find_esphome: S3-NAME skip (no device_names)")

    # Strategy 4: Title match among all ESPHome entries (last resort)
    if device_names:
        node_names = {
            n.lower().replace("_", "-").replace(" ", "-") for n in device_names if n
        }
        _LOGGER.debug("find_esphome: S4-TITLE node_names=%s", node_names)
        for cfg in hass.config_entries.async_entries("esphome"):
            title = (cfg.title or "").lower().replace("_", "-").replace(" ", "-")
            for nn in node_names:
                if nn and (nn in title or title in nn):
                    _LOGGER.debug(
                        "find_esphome: S4-TITLE HIT → title='%s' matched "
                        "nn='%s' entry=%s device_name='%s'",
                        cfg.title,
                        nn,
                        cfg.entry_id[:8],
                        cfg.data.get("device_name", "?"),
                    )
                    return cfg
        _LOGGER.debug("find_esphome: S4-TITLE miss")

    _LOGGER.debug("find_esphome: ALL STRATEGIES EXHAUSTED → None")
    return None


# ---------------------------------------------------------------------------
# D3 — Verification helpers (AD-1)
# ---------------------------------------------------------------------------


def _check_entry_loaded(hass: HomeAssistant, entry_id: str) -> bool:
    """Check if a config entry is in LOADED state."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry:
        return False
    return entry.state == ConfigEntryState.LOADED


def _check_device_entity_available(
    hass: HomeAssistant, node_name: str
) -> bool:
    """Check if at least 1 core entity for the device is available.

    Looks for any entity matching ``*{node_slug}*_device`` that is not
    ``unavailable`` or ``unknown``.
    """
    node_slug = node_name.lower().replace("-", "_")
    for state in hass.states.async_all():
        eid = state.entity_id
        if node_slug in eid and eid.endswith("_device"):
            if state.state not in ("unavailable", "unknown"):
                _LOGGER.debug(
                    "Verification: entity %s is available (state=%s)",
                    eid,
                    state.state,
                )
                return True
    return False


async def _verify_esphome_connection(
    hass: HomeAssistant,
    entry_id: str,
    node_name: str,
    timeout_s: float,
) -> bool:
    """Poll until ESPHome entry is LOADED and at least 1 entity is available.

    AD-1: Verification criteria:
      - ConfigEntryState.LOADED
      - At least 1 core ``_device`` entity not unavailable/unknown

    Returns:
        True if verified within timeout, False otherwise.
    """
    elapsed = 0.0
    while elapsed < timeout_s:
        loaded = _check_entry_loaded(hass, entry_id)
        entity_ok = _check_device_entity_available(hass, node_name)
        if loaded and entity_ok:
            _LOGGER.info(
                "ESPHome connection verified: entry LOADED + entity available "
                "(%.0fs elapsed)",
                elapsed,
            )
            return True
        await asyncio.sleep(_VERIFY_POLL_S)
        elapsed += _VERIFY_POLL_S
        if elapsed % 15 < _VERIFY_POLL_S:
            _LOGGER.debug(
                "Waiting for ESPHome connection: loaded=%s entity_ok=%s "
                "(%.0fs/%.0fs)",
                loaded,
                entity_ok,
                elapsed,
                timeout_s,
            )
    return False


# ---------------------------------------------------------------------------
# D3 — Apply noise_psk to ESPHome config entry (Auflage 4: reauth flow)
# ---------------------------------------------------------------------------


async def apply_noise_psk_to_esphome_entry(
    hass: HomeAssistant,
    noise_psk: str,
    *,
    new_node_name: str = "",
    device_mac: str = "",
    ha_device_id: str = "",
    device_names: list[str] | None = None,
    find_timeout_s: float = _ENTRY_FIND_TIMEOUT_S,
) -> bool:
    """Find ESPHome config entry and apply noise_psk via reauth config flow.

    Auflage 4: Uses the ESPHome integration's own reauth config flow instead
    of direct async_update_entry + async_reload.  Direct config-entry patching
    does NOT trigger the aioesphomeapi Noise handshake; the reauth flow does.

    Auflage 2: MAC-first config entry lookup (unchanged).
    AD-2: No plaintext key in logs.

    Args:
        hass: Home Assistant instance.
        noise_psk: Base64 noise_psk value to set.
        new_node_name: Expected Production node name (for verification).
        device_mac: Full MAC address for entry lookup.
        ha_device_id: HA Device Registry UUID.
        device_names: Possible device names (old Factory + new Production).
        find_timeout_s: Max seconds to wait for entry discovery.

    Returns:
        True if noise_psk applied and connection verified, False otherwise.
    """
    _LOGGER.info(
        "apply_noise_psk: start (key=%s, mac=%s)",
        mask_key(noise_psk),
        mask_key(device_mac) if device_mac else "<none>",
    )

    # ── Step 1: Find the ESPHome config entry (with polling) ──
    esphome_entry = None
    elapsed = 0.0

    while elapsed < find_timeout_s:
        esphome_entry = _find_esphome_config_entry(
            hass,
            device_mac=device_mac,
            ha_device_id=ha_device_id,
            device_names=device_names,
        )
        if esphome_entry:
            break
        await asyncio.sleep(_ENTRY_FIND_POLL_S)
        elapsed += _ENTRY_FIND_POLL_S
        if elapsed % 15 < _ENTRY_FIND_POLL_S:
            _LOGGER.debug(
                "Waiting for ESPHome config entry (%.0fs/%.0fs)",
                elapsed,
                find_timeout_s,
            )

    if not esphome_entry:
        # Fallback: list all ESPHome entries for diagnostics
        all_esphome = hass.config_entries.async_entries("esphome")
        seen = [
            f"  {e.entry_id[:8]}… title='{e.title}' uid={e.unique_id}"
            for e in all_esphome
        ]
        _LOGGER.error(
            "apply_noise_psk: ESPHome config entry NOT FOUND (%.0fs). "
            "Searched: mac=%s, device_id=%s, names=%s. "
            "ESPHome entries seen (%d):\n%s",
            find_timeout_s,
            mask_key(device_mac) if device_mac else "<none>",
            ha_device_id[:8] if ha_device_id else "<none>",
            device_names,
            len(seen),
            "\n".join(seen) if seen else "  <none>",
        )
        return False

    entry_id = esphome_entry.entry_id
    _LOGGER.info(
        "apply_noise_psk: entry=%s title='%s' state=%s current_psk=%s",
        entry_id[:8],
        esphome_entry.title,
        esphome_entry.state,
        mask_key(esphome_entry.data.get("noise_psk", "")),
    )

    # ── Step 2: Find or start reauth config flow ──
    flow_id = None

    # Check for existing pending reauth flow for this entry
    for flow in hass.config_entries.flow.async_progress_by_handler("esphome"):
        if flow.get("context", {}).get("entry_id") == entry_id:
            flow_id = flow["flow_id"]
            _LOGGER.info(
                "apply_noise_psk: reusing existing reauth flow %s "
                "(step=%s)",
                flow_id,
                flow.get("step_id"),
            )
            break

    # Start a new reauth flow if none exists
    if not flow_id:
        try:
            result = await hass.config_entries.flow.async_init(
                "esphome",
                context={
                    "source": "reauth",
                    "entry_id": entry_id,
                    "unique_id": esphome_entry.unique_id,
                },
                data=dict(esphome_entry.data),
            )
        except Exception:
            _LOGGER.error(
                "apply_noise_psk: reauth flow init failed", exc_info=True
            )
            return False

        ft = result.get("type")
        flow_id = result.get("flow_id", "")
        _LOGGER.info(
            "apply_noise_psk: flow init — type=%s step=%s id=%s",
            ft,
            result.get("step_id"),
            flow_id,
        )

        # If the flow auto-resolved (dashboard key found), we're done
        if ft == "abort":
            reason = result.get("reason", "")
            if "successful" in reason:
                _LOGGER.info(
                    "apply_noise_psk: auto-resolved via reauth (%s)",
                    reason,
                )
                return True
            # already_in_progress — try to grab the existing flow
            if reason == "already_in_progress":
                for flow in hass.config_entries.flow.async_progress_by_handler(
                    "esphome"
                ):
                    if flow.get("context", {}).get("entry_id") == entry_id:
                        flow_id = flow["flow_id"]
                        _LOGGER.info(
                            "apply_noise_psk: found in-progress flow %s",
                            flow_id,
                        )
                        break
            if not flow_id:
                _LOGGER.error(
                    "apply_noise_psk: flow aborted (%s)", reason
                )
                return False

        elif ft != "form" or not flow_id:
            _LOGGER.error(
                "apply_noise_psk: unexpected flow result type=%s", ft
            )
            return False

    # ── Step 3: Submit noise_psk to the reauth_confirm step ──
    try:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            user_input={"noise_psk": noise_psk},
        )
    except Exception:
        _LOGGER.error(
            "apply_noise_psk: flow configure failed", exc_info=True
        )
        return False

    rt = result.get("type")
    reason = result.get("reason", "")
    _LOGGER.info(
        "apply_noise_psk: flow result — type=%s reason=%s",
        rt,
        reason,
    )

    if rt == "abort" and "successful" in reason:
        _LOGGER.info(
            "apply_noise_psk: SUCCESS — reauth completed, "
            "Noise connection established"
        )
        # The reauth flow schedules async_reload as a background task.
        # Wait briefly, then optionally verify entity availability.
        await asyncio.sleep(5)
        if new_node_name:
            verified = await _verify_esphome_connection(
                hass, entry_id, new_node_name, _RELOAD_VERIFY_TIMEOUT_S
            )
            if not verified:
                _LOGGER.warning(
                    "apply_noise_psk: reauth OK but entity verification "
                    "timed out (%.0fs)",
                    _RELOAD_VERIFY_TIMEOUT_S,
                )
            return verified
        return True

    if rt == "form":
        errors = result.get("errors", {})
        _LOGGER.error(
            "apply_noise_psk: key validation FAILED (errors=%s). "
            "The noise_psk may be wrong or the device is unreachable.",
            errors,
        )
        return False

    _LOGGER.warning(
        "apply_noise_psk: unexpected flow outcome type=%s reason=%s",
        rt,
        reason,
    )
    return False


# ---------------------------------------------------------------------------
# D3 — Update ESPHome config entry for relocate (EPIC-011 continuity)
# ---------------------------------------------------------------------------


async def update_esphome_entry_for_relocate(
    hass: HomeAssistant,
    *,
    new_node_name: str,
    device_mac: str = "",
    ha_device_id: str = "",
    old_device_names: list[str] | None = None,
) -> bool:
    """Update ESPHome config entry device_name for relocate continuity.

    Finds the existing ESPHome config entry via MAC / ha_device_id,
    verifies that noise_psk is present (Noise continuity), then updates
    device_name in-place so ESPHome reconnects to the renamed node.

    The noise_psk is MAC-based and does NOT change during relocate,
    so it is preserved naturally by only modifying device_name.

    Returns True only if the entry was found, has noise_psk, and was
    updated successfully.  Returns False (fail-closed) otherwise.

    EPIC-011 / BLOCKER-REPORT-RELOCATE-NOISE-PSK-LOSS
    """
    _LOGGER.debug(
        "relocate_continuity: START new_node='%s' expected_mac=%s "
        "device_id=%s old_names=%s",
        new_node_name,
        mask_key(device_mac) if device_mac else "<none>",
        ha_device_id[:8] if ha_device_id else "<none>",
        old_device_names,
    )

    # Step 1: Find the ESPHome config entry
    esphome_entry = _find_esphome_config_entry(
        hass,
        device_mac=device_mac,
        ha_device_id=ha_device_id,
        device_names=old_device_names,
    )

    if not esphome_entry:
        _LOGGER.warning(
            "Relocate continuity: ESPHome config entry not found "
            "(mac=%s, device_id=%s, names=%s)",
            mask_key(device_mac) if device_mac else "<none>",
            ha_device_id[:8] if ha_device_id else "<none>",
            old_device_names,
        )
        return False

    _LOGGER.debug(
        "relocate_continuity: entry FOUND → id=%s device_name='%s' "
        "psk=%s host=%s",
        esphome_entry.entry_id[:8],
        esphome_entry.data.get("device_name", "?"),
        "YES" if esphome_entry.data.get("noise_psk") else "NO",
        esphome_entry.data.get("host", "?"),
    )

    # Step 1b: Cross-device guard — verify the found entry actually belongs
    # to the expected physical device (MAC match).  Prevents cross-device
    # corruption when _find_esphome_config_entry returns a wrong entry
    # via a fallback strategy.
    # See: TASK-20260316-relocate-e2e-failure-analysis
    if device_mac:
        mac_norm = device_mac.lower().replace("-", ":").replace("_", ":")
        if ":" not in mac_norm and len(mac_norm) == 12:
            mac_norm = ":".join(mac_norm[i : i + 2] for i in range(0, 12, 2))

        dev_reg = dr.async_get(hass)
        entry_mac_match = False
        entry_actual_mac = "<not found in registry>"
        for dev_entry in dev_reg.devices.values():
            if esphome_entry.entry_id not in dev_entry.config_entries:
                continue
            # Found the device that owns this entry
            for conn_type, conn_id in dev_entry.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    entry_actual_mac = conn_id.lower()
                    if conn_id.lower() == mac_norm:
                        entry_mac_match = True
                    break
            _LOGGER.debug(
                "relocate_continuity: MAC guard — entry owner "
                "device='%s' actual_mac=%s expected_mac=%s → %s",
                dev_entry.name,
                mask_key(entry_actual_mac),
                mask_key(mac_norm),
                "MATCH" if entry_mac_match else "MISMATCH",
            )
            break  # only check the first device that owns this entry

        if not entry_mac_match:
            _LOGGER.error(
                "Relocate continuity BLOCKED: cross-device mismatch — "
                "found ESPHome entry=%s (device_name='%s') but it does "
                "not belong to the expected device MAC=%s. "
                "Aborting to prevent cross-device corruption.",
                esphome_entry.entry_id[:8],
                esphome_entry.data.get("device_name", "?"),
                mask_key(device_mac),
            )
            return False
    else:
        # No device_mac provided — cannot verify ownership, fail-closed.
        _LOGGER.warning(
            "Relocate continuity: no device_mac provided — "
            "cannot verify ESPHome entry ownership, aborting (fail-closed)"
        )
        return False

    # Step 2: Verify noise_psk exists (fail-closed if not)
    noise_psk = esphome_entry.data.get("noise_psk", "")
    if not noise_psk:
        _LOGGER.warning(
            "Relocate continuity: ESPHome entry has no noise_psk — "
            "cannot guarantee encrypted continuity (entry=%s)",
            esphome_entry.entry_id[:8],
        )
        return False

    # Step 3: Update device_name (preserving noise_psk + all other data)
    old_name = esphome_entry.data.get("device_name", "")
    if old_name == new_node_name:
        _LOGGER.info(
            "Relocate continuity: device_name already '%s', no update needed",
            new_node_name,
        )
        return True

    new_data = dict(esphome_entry.data)
    new_data["device_name"] = new_node_name

    hass.config_entries.async_update_entry(esphome_entry, data=new_data)

    _LOGGER.info(
        "Relocate continuity: device_name '%s' → '%s' "
        "(noise_psk=%s preserved, entry=%s)",
        old_name,
        new_node_name,
        mask_key(noise_psk),
        esphome_entry.entry_id[:8],
    )

    # Step 4: Reload entry so ESPHome starts looking for the new name.
    # Reload failure means we cannot confirm the integration accepted
    # the updated config — fail-closed to prevent silent breakage.
    try:
        await hass.config_entries.async_reload(esphome_entry.entry_id)
        _LOGGER.info(
            "Relocate continuity: ESPHome entry reloaded for '%s'",
            new_node_name,
        )
    except Exception:
        _LOGGER.error(
            "Relocate continuity FAILED: reload raised for entry=%s — "
            "cannot confirm ESPHome accepted the update. "
            "Relocate must not proceed.",
            esphome_entry.entry_id[:8],
            exc_info=True,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Post-flash ESPHome entry reload
# ---------------------------------------------------------------------------


_POST_FLASH_RELOAD_DELAY_S = 15  # ESP32 reboot takes ~10-15s


def _find_esphome_entry_by_mac_suffix(
    hass: HomeAssistant, mac_suffix: str
) -> Any | None:
    """Find ESPHome config entry whose device MAC ends with *mac_suffix*."""
    dev_reg = dr.async_get(hass)
    for device_entry in dev_reg.devices.values():
        for conn_type, conn_id in device_entry.connections:
            if (
                conn_type == dr.CONNECTION_NETWORK_MAC
                and conn_id.lower().replace(":", "").endswith(mac_suffix)
            ):
                for entry_id in device_entry.config_entries:
                    candidate = hass.config_entries.async_get_entry(entry_id)
                    if candidate and candidate.domain == "esphome":
                        return candidate
    return None


async def reload_esphome_entry_for_device(
    hass: HomeAssistant,
    mac_suffix: str,
) -> bool:
    """Reload the ESPHome config entry so it reconnects to the device.

    After a firmware update the device reboots.  The ESPHome integration may
    be in backoff from a previous failed handshake (e.g. pre-EPIC-011 builds
    without encryption).  An explicit reload forces an immediate reconnect
    with the current noise_psk.

    Returns True if the entry was found and reloaded.
    """
    from .mac_utils import canonical_mac_last6

    if not mac_suffix:
        return False

    suffix = canonical_mac_last6(mac_suffix)
    entry = _find_esphome_entry_by_mac_suffix(hass, suffix)

    if not entry:
        _LOGGER.debug(
            "post_flash_reload: no ESPHome entry found for suffix %s", suffix
        )
        return False

    _LOGGER.info(
        "post_flash_reload: reloading ESPHome entry (entry=%s, "
        "device_name='%s')",
        entry.entry_id[:8],
        entry.data.get("device_name", "?"),
    )
    try:
        await hass.config_entries.async_reload(entry.entry_id)
        _LOGGER.info(
            "post_flash_reload: reload complete for entry=%s",
            entry.entry_id[:8],
        )
        return True
    except Exception:
        _LOGGER.warning(
            "post_flash_reload: reload failed for entry=%s",
            entry.entry_id[:8],
            exc_info=True,
        )
        return False


async def schedule_post_flash_reload(
    hass: HomeAssistant,
    mac_suffix: str,
    delay_s: float = _POST_FLASH_RELOAD_DELAY_S,
) -> None:
    """Wait for the device to reboot, then reload its ESPHome entry.

    Intended as a fire-and-forget task after OTA flash.  The delay gives
    the ESP32 time to reboot before the integration attempts reconnection.
    """
    _LOGGER.info(
        "post_flash_reload: waiting %.0fs for device reboot (mac_suffix=%s)",
        delay_s, mac_suffix,
    )
    await asyncio.sleep(delay_s)
    await reload_esphome_entry_for_device(hass, mac_suffix)
