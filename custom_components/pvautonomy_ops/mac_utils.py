"""Canonical MAC address utilities for PVAutonomy Ops.

Single Source of Truth for MAC-suffix extraction and validation.
All code that builds secret names (edge101_api_key_<suffix>,
edge101_ota_password_<suffix>) or resolves device identifiers
MUST use these helpers.

Ref: D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001
"""

from __future__ import annotations

import re

# Pre-compiled patterns for MAC parsing
_RE_HEX6 = re.compile(r"^[0-9a-f]{6}$")
_RE_FULL_MAC_COLON = re.compile(
    r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$"
)
_RE_FULL_MAC_DASH = re.compile(
    r"^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$"
)
_RE_FULL_MAC_RAW = re.compile(r"^[0-9a-fA-F]{12}$")
_RE_TRAILING_HEX6 = re.compile(r"[_\s-]([0-9a-fA-F]{6})$")


class InvalidMACError(ValueError):
    """Raised when a MAC address or suffix cannot be parsed."""


def canonical_mac_last6(mac: str) -> str:
    """Extract the canonical 6-char lowercase hex MAC suffix.

    Accepts any common MAC format:
      - Full colon-separated:  "68:25:DD:2E:B1:E4" → "2eb1e4"
      - Full dash-separated:   "68-25-DD-2E-B1-E4" → "2eb1e4"
      - Full raw hex:          "6825DD2EB1E4"       → "2eb1e4"
      - Already last-6:        "2eb1e4"             → "2eb1e4"
      - Display name trailing: "PVAutonomy Modbus Bridge 2eb1e4" → "2eb1e4"

    Args:
        mac: MAC address string in any supported format.

    Returns:
        6-character lowercase hex string (e.g. "2eb1e4").

    Raises:
        InvalidMACError: If the input cannot be parsed as a valid MAC.
    """
    if not mac or not isinstance(mac, str):
        raise InvalidMACError(f"Empty or invalid MAC input: {mac!r}")

    stripped = mac.strip()

    # Case 1: Already a 6-char hex suffix
    lower = stripped.lower()
    if _RE_HEX6.match(lower):
        return lower

    # Case 2: Full MAC with colons (68:25:DD:2E:B1:E4)
    if _RE_FULL_MAC_COLON.match(stripped):
        raw = stripped.replace(":", "").lower()
        return raw[-6:]

    # Case 3: Full MAC with dashes (68-25-DD-2E-B1-E4)
    if _RE_FULL_MAC_DASH.match(stripped):
        raw = stripped.replace("-", "").lower()
        return raw[-6:]

    # Case 4: Full MAC raw hex (6825DD2EB1E4)
    if _RE_FULL_MAC_RAW.match(stripped):
        return stripped[-6:].lower()

    # Case 5: Display name with trailing hex suffix
    # e.g. "PVAutonomy Modbus Bridge 2eb1e4" or "pvautonomy_edge101_2eb1e4"
    m = _RE_TRAILING_HEX6.search(stripped)
    if m:
        return m.group(1).lower()

    raise InvalidMACError(
        f"Cannot extract MAC-last6 from {mac!r}. "
        "Expected: full MAC (aa:bb:cc:dd:ee:ff), raw hex (aabbccddeeff), "
        "6-char suffix (ddeeff), or display name ending in hex suffix."
    )


def validate_mac_suffix(suffix: str) -> bool:
    """Check whether a string is a valid 6-char lowercase hex MAC suffix.

    Args:
        suffix: String to validate.

    Returns:
        True if valid, False otherwise.
    """
    return bool(suffix and _RE_HEX6.match(suffix.lower()))


def mac_secret_name(mac: str, prefix: str = "edge101_api_key") -> str:
    """Build the canonical secret name for a device MAC.

    Args:
        mac: MAC address in any format accepted by canonical_mac_last6().
        prefix: Secret name prefix (default: "edge101_api_key").

    Returns:
        Secret name string, e.g. "edge101_api_key_2eb1e4".

    Raises:
        InvalidMACError: If MAC cannot be parsed.
    """
    return f"{prefix}_{canonical_mac_last6(mac)}"
