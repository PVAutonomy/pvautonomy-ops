"""Auto-provision ESPHome secrets for customer first-install flow.

On a fresh customer system, ``/config/esphome/secrets.yaml`` may not
contain the device-specific API key or the per-device OTA password
required by the build pipeline.  This module generates and persists
them *idempotently* — existing values are never overwritten.

Two entry points, two naming schemes, one file:

- :func:`ensure_device_secrets_sync` — the MAC-bound managed path, which
  provisions ``edge101_api_key_{mac}`` / ``edge101_ota_password_{mac}``.
- :func:`ensure_selfbuild_secrets_sync` — the Community Alpha self-build
  path (PD-16), which provisions the four names the generated device YAML
  references through ``!secret``.  Two of them are **bound to the node
  name**, so two self-built devices never share a key:
  ``api_encryption_key_{node_name}`` and ``ota_password_{node_name}``.  The
  other two, ``wifi_ssid`` and ``wifi_password``, stay generic on purpose —
  they are site credentials chosen by the user, shared by every device on
  the network, and not ours to multiply.  The two device credentials are
  generated locally; the two Wi-Fi values can only come from the user and
  are written only when supplied.

The self-build writer is **append-only**: new entries are appended to the
existing file's original bytes, which are never re-serialised.  The
MAC-bound path keeps the rewriting writer it has always had (#272).

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
import contextlib
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

#: Prefixes of the two device-bound secret names. The node name is appended
#: with an underscore, so ``sph10k-bench-01`` yields
#: ``api_encryption_key_sph10k-bench-01``. These are what
#: ``generate_device_yaml(mac_suffix=None)`` emits as ``!secret``
#: placeholders, so ESPHome resolves exactly these.
SELFBUILD_API_KEY_PREFIX: str = "api_encryption_key"
SELFBUILD_OTA_KEY_PREFIX: str = "ota_password"

#: Site credentials, shared by every device on the network and chosen by the
#: user. Deliberately NOT device-bound: one Wi-Fi network, one entry.
SELFBUILD_WIFI_SSID_NAME: str = "wifi_ssid"
SELFBUILD_WIFI_PASSWORD_NAME: str = "wifi_password"

#: Only the user knows these — they are asked for, never invented.
SELFBUILD_WIFI_KEYS: tuple[str, ...] = (
    SELFBUILD_WIFI_SSID_NAME,
    SELFBUILD_WIFI_PASSWORD_NAME,
)


def selfbuild_api_key_name(node_name: str) -> str:
    """Name of the Noise PSK secret for *node_name*."""
    return f"{SELFBUILD_API_KEY_PREFIX}_{node_name}"


def selfbuild_ota_key_name(node_name: str) -> str:
    """Name of the OTA password secret for *node_name*."""
    return f"{SELFBUILD_OTA_KEY_PREFIX}_{node_name}"


#: WPA2 personal passphrase, per IEEE 802.11i: 8–63 characters, each in the
#: printable ASCII range. A longer or shorter string is not a passphrase a
#: device can join with, and a non-ASCII one is not portable — ESPHome would
#: compile it and the join would fail on the air, with nothing to point at.
WIFI_PASSWORD_MIN_LENGTH: int = 8
WIFI_PASSWORD_MAX_LENGTH: int = 63
#: An SSID is at most 32 units and carries no control characters.
WIFI_SSID_MAX_LENGTH: int = 32


def _is_printable_ascii(value: str) -> bool:
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def _has_control_characters(value: str) -> bool:
    """True if *value* carries a C0 or C1 control character.

    C1 (U+0080–U+009F) counts, not only C0 and DEL. The set is reachable
    from a paste — U+0085 NEXT LINE is the one that turns up in text copied
    out of a router's web UI — and it is invisible in the field, so without
    this check it would travel into ``secrets.yaml`` unseen and produce an
    SSID that never matches.

    U+0085 *is* whitespace to ``str``: ``"\u0085".isspace()`` is True and
    ``.strip()`` removes it. So the caller's leading ``if not value.strip()``
    already rejects an entry made only of it — as ``wifi_ssid_required``,
    which is the right answer for a field that looks empty. What this check
    adds is the case ``.strip()`` cannot reach: a control character *between*
    visible characters, where the entry looks like a name and is not one.
    """
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F
        for ch in value
    )


def wifi_ssid_error(value: str) -> str | None:
    """Return a translation key for a rejected SSID, or ``None``.

    Never returns anything derived from *value*: the caller renders the key,
    and the catalog text states the rule instead of quoting the entry.

    An SSID that cannot be encoded as UTF-8, such as one carrying a lone
    surrogate, is invalid too: ``secrets.yaml`` could not hold it (#335).
    """
    if not value.strip():
        return "wifi_ssid_required"
    if len(value) > WIFI_SSID_MAX_LENGTH or _has_control_characters(value):
        return "wifi_ssid_invalid"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return "wifi_ssid_invalid"
    return None


def wifi_password_error(value: str) -> str | None:
    """Return a translation key for a rejected passphrase, or ``None``.

    Empty and all-blank entries are ``required`` rather than ``invalid``: they
    are an unanswered question, not a wrong answer. Everything else is checked
    against the WPA2 passphrase shape.
    """
    if not value.strip():
        return "wifi_password_required"
    if not (
        WIFI_PASSWORD_MIN_LENGTH <= len(value) <= WIFI_PASSWORD_MAX_LENGTH
    ) or not _is_printable_ascii(value):
        return "wifi_password_invalid"
    return None


def selfbuild_secret_names(node_name: str) -> tuple[str, ...]:
    """The four secret names a self-built *node_name* resolves.

    Order is stable: the two device-bound credentials first, then the two
    site-wide Wi-Fi entries, so a caller can render missing fields in a
    predictable order.
    """
    return (
        selfbuild_api_key_name(node_name),
        selfbuild_ota_key_name(node_name),
        SELFBUILD_WIFI_SSID_NAME,
        SELFBUILD_WIFI_PASSWORD_NAME,
    )


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
    #: Names of the secrets this call added to the file — never values.
    #: The self-build path logs them by name, and uses the list to decide
    #: whether the file needs writing at all.
    created_keys: list[str] = field(default_factory=list)
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
    are not yet present — existing values are never touched.  Refuses
    on a present-but-unparsable file (#272) instead of treating it as
    empty.

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
        try:
            data = _read_secrets_strict(secrets_path)
        except SecretsFileUnreadableError as exc:
            # Refuse (#272). Reading the file as empty would make the write
            # below replace every secret in it. The reason is fixed text and
            # no traceback is logged: a parser traceback quotes the offending
            # line, which in this file is a secret.
            result.errors.append(str(exc))
            _LOGGER.error(
                "Refusing to provision %s: %s", secrets_path.name, exc.reason
            )
            return result
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


# ---------------------------------------------------------------------------
# Community Alpha self-build path (PD-16, #240)
# ---------------------------------------------------------------------------


class SecretsFileUnreadableError(Exception):
    """``secrets.yaml`` exists but could not be parsed as a mapping.

    Raised instead of treating the file as empty. A file we cannot parse is
    one whose contents we cannot reason about, so we neither report its keys
    as missing nor touch it.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path} could not be read: {reason}")
        self.path = path
        self.reason = reason


def _read_secrets_strict(path: Path) -> dict[str, str]:
    """Read a flat YAML secrets file, raising rather than silently emptying.

    An absent or empty file is ``{}``. A file that is present and
    unparseable, or that holds something other than a mapping, raises
    :class:`SecretsFileUnreadableError`.

    The exception text carries a fixed reason only. A ``yaml.YAMLError``
    quotes the offending *line*, which in this file is a secret, so the
    parser's own message is deliberately not propagated into a string that
    a caller may render or log.

    A file that is not valid UTF-8 raises with the fixed reason
    ``not valid UTF-8``; the decoder's message names a byte and an offset
    and is deliberately not propagated.

    Every other exception of the parse gets the fixed reason
    ``not valid YAML`` as well: a constructor that cannot convert a tagged
    value (``!!float``, ``!!int``, ``!!bool``, ``!!timestamp``) raises
    ``ValueError``, ``KeyError`` or ``AttributeError``, not a
    ``yaml.YAMLError``, and its message can quote the value (#335).
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError:
        raise SecretsFileUnreadableError(path, "not readable") from None
    except UnicodeDecodeError:
        raise SecretsFileUnreadableError(path, "not valid UTF-8") from None
    except yaml.YAMLError:
        raise SecretsFileUnreadableError(path, "not valid YAML") from None
    except Exception:
        raise SecretsFileUnreadableError(path, "not valid YAML") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SecretsFileUnreadableError(path, "not a mapping")
    return dict(data)


#: Characters a double-quoted YAML scalar must not carry literally.
_YAML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _double_quoted(value: str) -> str:
    """Render *value* as a double-quoted YAML scalar.

    Double-quoted is the one YAML style that survives everything a user can
    type into a Wi-Fi password box: leading and trailing spaces, ``#``,
    ``:``, quotes, a leading ``0`` that a plain scalar would read back as an
    octal integer, or a value that looks like ``yes``/``null``. The written
    form always round-trips to the exact ``str`` that went in, which
    :func:`_append_secrets_locked` verifies against the parser before the
    file is replaced.
    """
    out: list[str] = []
    for ch in value:
        if ch in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _values_equal(new: object, old: object) -> bool:
    """Compare two parsed YAML values for the preservation check.

    ``!=`` is the whole of it except for one value: YAML's ``.nan`` parses to
    a float that is not equal to itself, so an untouched entry holding it
    would read as "altered" and the write would be refused for no reason.
    NaN is treated as equal to NaN here — the question this answers is "did
    this entry survive", not "are these two numbers the same number".
    """
    if isinstance(new, float) and isinstance(old, float):
        import math

        if math.isnan(new) and math.isnan(old):
            return True
    return bool(new == old)


def _enforce_permissions_strict(path: Path) -> str | None:
    """Force ``_SECRET_FILE_MODE`` on *path*, reporting failure as an error.

    Unlike :func:`_enforce_permissions`, which warns and carries on for the
    MAC-bound path, this returns a message the caller must treat as fatal.
    A ``secrets.yaml`` left at ``0644`` that we could not tighten is a
    world-readable credential store, and reporting success over it would be
    exactly the "success with warnings" that CLAUDE.md §6.2 forbids.

    Returns ``None`` when the mode is correct (or the file does not exist),
    otherwise a message naming the path and the mode that stands.
    """
    try:
        if not path.exists():
            return None
        current = stat.S_IMODE(path.stat().st_mode)
        if current == _SECRET_FILE_MODE:
            return None
        os.chmod(path, _SECRET_FILE_MODE)
    except OSError as exc:
        return (
            f"Cannot set owner-only permissions on {path}: {exc}. "
            "The file would stay readable by other users on this system."
        )
    try:
        confirmed = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return f"Cannot verify permissions on {path}: {exc}"
    if confirmed != _SECRET_FILE_MODE:
        return (
            f"Permissions on {path} are 0o{confirmed:03o} and could not be "
            f"changed to 0o{_SECRET_FILE_MODE:03o}."
        )
    _LOGGER.info(
        "Tightened permissions on %s to 0o%03o", path.name, _SECRET_FILE_MODE
    )
    return None


def _append_secrets_locked(path: Path, entries: dict[str, str]) -> None:
    """Append *entries* to *path* without re-serialising what is there.

    The existing file's **bytes** are copied through untouched — comments,
    ordering, quoting style, hand formatting and any YAML feature this
    module does not model all survive, because nothing parses and re-emits
    them. Only the new lines are generated.

    The write goes to a temporary file created with ``O_EXCL`` and mode
    ``0600`` in one syscall, so the file never exists at a umask-dependent
    mode for even an instant, and is then renamed over the target — atomic
    on POSIX. The temporary file is removed on every failure path.

    Before the rename the assembled content is parsed and checked in **both**
    directions:

    * every new entry must read back as the exact value that went in — a
      quoting bug would otherwise write a password the user never chose, and
      ESPHome would compile it into firmware without complaint;
    * every entry that was already in the file must read back **unchanged**,
      and no entry may have disappeared. Appending text can reach backwards
      into the last construct in a file: a trailing block scalar
      (``key: |+``) swallows the blank separator line, so the value the
      *previous* owner of that key gets back is not the one they wrote. The
      new entries surviving says nothing about that, which is why the old map
      is compared in full rather than assumed intact.

    A file whose existing entries would not survive is **refused**, not
    written. It is rare — block scalars are unusual in a secrets file — and a
    refusal the user can act on beats a silent edit of somebody else's
    credential.

    Nothing derived from file content ever reaches the raised exception. A
    ``yaml.YAMLError`` quotes the line it choked on, which in this file is a
    secret, so parser errors are converted to fixed text with the context
    chain suppressed — ``raise ... from None`` — and the traceback carries no
    fragment either.

    Raises:
        OSError: the file could not be written or replaced.
        ValueError: the assembled file did not round-trip, including a
            constructor error of a concurrently edited file and a value that
            is not UTF-8-encodable; the message is fixed text or names keys
            only. Nothing is written in that case.
    """
    original = path.read_bytes() if path.exists() else b""

    try:
        before = yaml.safe_load(original.decode("utf-8")) if original else {}
    except Exception:
        raise ValueError("the existing file could not be read back") from None
    if before is None:
        before = {}
    if not isinstance(before, dict):
        raise ValueError("the existing file is not a mapping") from None

    appended = ""
    if original and not original.endswith(b"\n"):
        appended += "\n"
    if original:
        appended += "\n"
    appended += "# Added by PVAutonomy Ops — self-build device secrets.\n"
    for name, value in entries.items():
        appended += f"{name}: {_double_quoted(value)}\n"

    try:
        encoded = appended.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("the new entries could not be encoded as UTF-8") from None
    content = original + encoded

    # Fail closed: verify against the parser, not against our own belief about
    # what double-quoting does or about what appending cannot disturb.
    try:
        parsed = yaml.safe_load(content.decode("utf-8"))
    except Exception:
        raise ValueError("the file could not be read back after appending") from None
    if not isinstance(parsed, dict):
        raise ValueError("appended content did not parse as a mapping") from None

    for name, value in entries.items():
        if parsed.get(name) != value:
            raise ValueError(
                f"appended secret {name!r} did not round-trip"
            ) from None

    missing_after = [name for name in before if name not in parsed]
    if missing_after:
        raise ValueError(
            f"existing entries would be lost by the write: {sorted(missing_after)}"
        ) from None
    changed = sorted(
        name for name in before if not _values_equal(parsed[name], before[name])
    )
    if changed:
        # Names only. The values are exactly what must not appear here.
        raise ValueError(
            f"existing entries would be altered by the write: {changed}"
        ) from None

    tmp = path.with_suffix(".yaml.tmp")
    fd: int | None = None
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SECRET_FILE_MODE
        )
        with os.fdopen(fd, "wb") as fh:
            fd = None  # fdopen owns it now; closing twice would be a bug
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def selfbuild_secrets_path(config_dir: str | Path) -> Path:
    """Return the ESPHome secrets file path under *config_dir*."""
    return Path(config_dir) / ESPHOME_SECRETS_RELPATH


def missing_selfbuild_secrets_sync(
    config_dir: str | Path, node_name: str
) -> list[str]:
    """Return which of *node_name*'s four secrets are not in the file.

    Read-only: it opens nothing for writing and creates nothing. The test is
    **key presence, not value shape** — a name that is in the mapping counts
    as present whatever it holds. Judging a stored value would mean deciding
    that someone else's secret is wrong and replacing it, which is not this
    module's call to make.

    Older files may still carry the generic ``api_encryption_key`` /
    ``ota_password`` from before device-bound naming. They are not consulted,
    not reused and not removed: only the device-bound names decide whether
    something is missing.

    Raises:
        SecretsFileUnreadableError: the file is there but unparseable.
    """
    data = _read_secrets_strict(selfbuild_secrets_path(config_dir))
    return [name for name in selfbuild_secret_names(node_name) if name not in data]


def ensure_selfbuild_secrets_sync(
    config_dir: str | Path,
    node_name: str,
    wifi_ssid: str | None = None,
    wifi_password: str | None = None,
) -> ProvisionResult:
    """Ensure *node_name*'s four self-build secrets exist in ``secrets.yaml``.

    The self-build device YAML references
    ``!secret api_encryption_key_{node_name}``,
    ``!secret ota_password_{node_name}``, ``!secret wifi_ssid`` and
    ``!secret wifi_password``. ESPHome refuses to compile while any of them
    is absent, and before #240 the wizard's answer to that was a screen
    telling the user to add them by hand.

    What this does instead:

    * the two device-bound credentials are generated locally when absent —
      32 CSPRNG bytes Base64-encoded for the Noise PSK, 16 CSPRNG bytes as
      32 hex characters for the OTA password;
    * ``wifi_ssid`` / ``wifi_password`` are written **only** when absent
      *and* supplied by the caller, **exactly as given** — no trimming, no
      normalisation. A trailing space can be part of an SSID, and a
      passphrase is whatever the user typed.

    Idempotent: a name already in the file is never read back out, never
    re-generated and never overwritten, including a Wi-Fi value, which wins
    over anything the caller passes. When nothing is missing the file is not
    written at all and stays byte-identical.

    Permissions are enforced on every path, including the one that writes
    nothing, and a failure to enforce them is an **error** rather than a
    warning.

    Args:
        config_dir: HA config directory (``hass.config.config_dir``).
        node_name: ESPHome node name, e.g. ``sph10k-bench-01``.
        wifi_ssid: SSID to store if ``wifi_ssid`` is missing.
        wifi_password: Passphrase to store if ``wifi_password`` is missing.

    Returns:
        ProvisionResult; ``created_keys`` names what this call added — names
        only, never values. ``errors`` is non-empty on failure, and the file
        is then unchanged.
    """
    result = ProvisionResult()

    if not node_name:
        result.errors.append("Missing node name for secret naming")
        return result

    secrets_path = selfbuild_secrets_path(config_dir)
    result.secrets_file = str(secrets_path)
    lock_path = secrets_path.with_suffix(".yaml.lock")

    try:
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors.append(f"Cannot create {secrets_path.parent}: {exc}")
        return result

    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        result.errors.append(f"Cannot acquire lock {lock_path}: {exc}")
        return result

    try:
        # ── Critical section (under lock) ─────────────────────────
        try:
            data = _read_secrets_strict(secrets_path)
        except SecretsFileUnreadableError as exc:
            # Fail closed. Appending to a file we could not parse would
            # produce a file we still cannot parse, on top of one the user
            # may still be able to repair by hand.
            result.errors.append(str(exc))
            return result

        api_name = selfbuild_api_key_name(node_name)
        ota_name = selfbuild_ota_key_name(node_name)
        result.api_key_name = api_name
        result.ota_key_name = ota_name

        generators = {
            api_name: _generate_noise_psk,
            ota_name: _generate_ota_password,
        }
        supplied = {
            SELFBUILD_WIFI_SSID_NAME: wifi_ssid,
            SELFBUILD_WIFI_PASSWORD_NAME: wifi_password,
        }

        new_entries: dict[str, str] = {}
        for name in selfbuild_secret_names(node_name):
            if name in data:
                _LOGGER.debug("Secret '%s' already present — preserved", name)
                continue
            if name in generators:
                value: str | None = generators[name]()
            else:
                value = supplied.get(name)
                if value is None or not value.strip():
                    # Nothing to write, and nothing to invent. An all-blank
                    # value is rejected by the caller, not silently stored.
                    continue
            new_entries[name] = value
            # Names only. _mask() exists so a log line can say "a value is
            # there" without saying anything about the value.
            _LOGGER.info(
                "Provisioned ESPHome secret '%s' (%s)", name, _mask(str(value))
            )

        if new_entries:
            try:
                _append_secrets_locked(secrets_path, new_entries)
            except (OSError, ValueError) as exc:
                msg = f"Failed to write {secrets_path}: {exc}"
                _LOGGER.error(msg)
                result.errors.append(msg)
                return result
            except yaml.YAMLError:
                # Defence in depth. _append_secrets_locked converts parser
                # errors itself; if one ever escaped, its message would quote
                # the line it choked on — a secret. Never let that through.
                msg = (
                    f"Failed to write {secrets_path}: the file could not be "
                    "parsed while writing"
                )
                _LOGGER.error(msg)
                result.errors.append(msg)
                return result
            result.created_keys = list(new_entries)
            result.api_key_created = api_name in new_entries
            result.ota_key_created = ota_name in new_entries
            _LOGGER.info(
                "ESPHome self-build secrets appended to %s: %s",
                secrets_path.name,
                ", ".join(result.created_keys),
            )

        # Every path ends here, including the one that wrote nothing: a
        # pre-existing 0644 secrets.yaml is the case this catches.
        permission_error = _enforce_permissions_strict(secrets_path)
        if permission_error:
            _LOGGER.error("%s", permission_error)
            result.errors.append(permission_error)
        # ── End critical section ──────────────────────────────────
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    return result


async def missing_selfbuild_secrets(
    hass: HomeAssistant, node_name: str
) -> list[str]:
    """Async wrapper — runs the read in the executor, never on the loop."""
    return await hass.async_add_executor_job(
        missing_selfbuild_secrets_sync,
        hass.config.config_dir,
        node_name,
    )


async def ensure_selfbuild_secrets(
    hass: HomeAssistant,
    node_name: str,
    wifi_ssid: str | None = None,
    wifi_password: str | None = None,
) -> ProvisionResult:
    """Async wrapper — runs ``ensure_selfbuild_secrets_sync`` in executor."""
    return await hass.async_add_executor_job(
        ensure_selfbuild_secrets_sync,
        hass.config.config_dir,
        node_name,
        wifi_ssid,
        wifi_password,
    )
