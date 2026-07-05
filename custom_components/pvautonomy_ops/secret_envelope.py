"""HPKE compile-secret envelope sealer + signed-keyset verification (EPIC-006 PR-3).

Implements the HA-side of the secret-blind proxy migration:

* Verify a build-backend keyset signed by an Ed25519 root anchor pinned in HA.
* Cache the accepted signed keyset in HA storage with anti-rollback.
* HPKE-seal compile-secret K=V plaintext into ``payload.compile_secret_envelope``.

Pinned ciphersuite (RFC 9180, all numeric ids):

* KEM  = DHKEM(X25519, HKDF-SHA256)  (0x0020)
* KDF  = HKDF-SHA256                 (0x0001)
* AEAD = ChaCha20-Poly1305           (0x0003)

Source of truth: ADR-20260426 §2.1, §3.1, §3.4, §4.2-§4.8, §6.1, §6.3.1.

Trust-anchor safety
-------------------
This module never invents a production trust anchor. Production envelope mode
is only available if at least one Judge-approved real Ed25519 root public key
is configured in :data:`ROOT_PUBKEYS_PINNED`. Tests inject fixture roots via
:func:`verify_signed_keyset`'s ``root_pubkeys`` argument and never mutate
:data:`ROOT_PUBKEYS_PINNED`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# pyhpke is vendored under ._vendor (not a manifest requirement): its published
# metadata pins cryptography<47, which is unsatisfiable against HA Core's bundled
# cryptography==48. The cap is defensive only; the code runs on cryptography 48.
# See _vendor/__init__.py and issue #94.
from ._vendor.pyhpke import (
    AEADId,
    CipherSuite,
    KDFId,
    KEMId,
)
from ._vendor.pyhpke.exceptions import OpenError, PyHPKEError, SealError


# ---------------------------------------------------------------------------
# Public constants — pinned ciphersuite + envelope schema
# ---------------------------------------------------------------------------

#: Pinned RFC 9180 ciphersuite string used in envelope ``alg`` and
#: ``keyset.alg_required``. Must match the GHA-side and proxy-side string
#: byte-for-byte.
HPKE_CIPHERSUITE: Final[str] = (
    "HPKE-Base-DHKEM_X25519_HKDF_SHA256-HKDF_SHA256-CHACHA20_POLY1305"
)

#: Per-key algorithm string for the build-backend keypair entries.
HPKE_KEM_ALG: Final[str] = "DHKEM_X25519_HKDF_SHA256"

#: HPKE info parameter (KDF labelling) — locks keysets to envelope purpose.
HPKE_INFO: Final[bytes] = b"pva-compile-secret-envelope/v1"

#: Current envelope version.
ENVELOPE_VERSION: Final[int] = 1

#: Bytes of fresh random material per build for ``aad.request_nonce``.
REQUEST_NONCE_LEN: Final[int] = 16

#: Bytes of ``sha256(enc || ciphertext)`` to keep for ``envelope_fingerprint``.
ENVELOPE_FINGERPRINT_LEN: Final[int] = 12

#: Pinned production root public keys (Ed25519, raw 32-byte form).
#:
#: G6 (HPKE-4, ADR-0003): pins the PUBLIC verify key from the offline root
#: ceremony of 2026-07-01 (G3). PUBLIC material only — the private root key
#: never leaves the offline ceremony medium. Binding evidence:
#: sha256(raw 32B) == 0831d9b3941c987a78f6dc9a5452beb84aea4ed29b7be8fb9e3fda37b1bdafac
#: (enforced by test_production_root_anchor_root_2026a_pinned). Rotation per
#: runbook G8: pin the successor alongside, retire the old key after client
#: propagation. Tests pass fixture roots directly to the verification helpers
#: and must never mutate this constant.
ROOT_PUBKEYS_PINNED: Final[dict[str, bytes]] = {
    "root-2026-a": base64.b64decode("gJBO4eXdO9x2wxT6z61x+6S5+e57Rz89+aVsaj4/Ybg="),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SecretEnvelopeError(Exception):
    """Base error for secret-envelope construction."""


class KeysetVerificationError(SecretEnvelopeError):
    """Raised when a fetched/cached signed keyset fails validation."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class KeysetEndpointUnsupported(SecretEnvelopeError):
    """Raised when the proxy responds 404/405 to ``GET /build-backend/keys``.

    This is the **only** condition under which legacy fallback is permitted.
    """


class EnvelopeSealError(SecretEnvelopeError):
    """Raised when HPKE seal fails. Always fail-closed; never fall back."""


# ---------------------------------------------------------------------------
# Canonical JSON helper (RFC 8785 JCS subset: sort keys, no whitespace, UTF-8)
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    """Serialize *obj* as canonical JSON bytes.

    The serialization rules match what the proxy and GHA decoder expect:
    keys sorted, no whitespace, UTF-8. ``ensure_ascii=False`` so non-ASCII
    text is encoded as proper UTF-8 (not ``\\uXXXX`` escapes).
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Signed keyset verification (Section 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveKey:
    """Resolved active build-backend public key from a verified signed keyset."""

    key_id: str
    alg: str
    public_key_b64: str
    public_key_raw: bytes


@dataclass(frozen=True)
class VerifiedKeyset:
    """Outcome of :func:`verify_signed_keyset` — the full signed keyset plus
    the resolved active key. Holds **public** material only; never private."""

    raw: dict[str, Any]
    keyset: dict[str, Any]
    keyset_serial: int
    active_key: ActiveKey
    issued_at: str
    expires_at: str
    expires_at_dt: datetime
    environment: str | None = None


def _parse_iso8601(value: str, *, field_name: str) -> datetime:
    """Parse an ISO 8601 timestamp; treat naive as UTC."""
    if not isinstance(value, str) or not value:
        raise KeysetVerificationError(
            "keyset_invalid", f"{field_name} missing or not a string"
        )
    raw = value
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KeysetVerificationError(
            "keyset_invalid", f"{field_name} not ISO 8601 ({raw!r})"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _contains_private_key_field(value: Any) -> bool:
    """True if a ``private_key`` field appears anywhere in *value*.

    Defense-in-depth (ADR §4.4 / §4.8): the proxy must serve PUBLIC keysets
    only — a build-backend keyset endpoint that ever ships ``private_key``
    indicates a misconfigured proxy / a leaked private half. Reject before
    any persistence so the cache cannot be poisoned with private material.
    The walk is non-recursive on identity to keep stack usage bounded; we
    do not log the offending bytes.
    """
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            for k, v in node.items():
                if k == "private_key":
                    return True
                stack.append(v)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False


def _b64decode_strict(value: str, *, field_name: str, expected_len: int | None = None) -> bytes:
    """Strict base64 decode; raises ``KeysetVerificationError`` on failure."""
    if not isinstance(value, str):
        raise KeysetVerificationError(
            "keyset_invalid", f"{field_name} not a string"
        )
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise KeysetVerificationError(
            "keyset_invalid", f"{field_name} not valid base64"
        ) from exc
    if expected_len is not None and len(raw) != expected_len:
        raise KeysetVerificationError(
            "keyset_invalid",
            f"{field_name} length {len(raw)} != expected {expected_len}",
        )
    return raw


def verify_signed_keyset(
    response: Mapping[str, Any],
    *,
    root_pubkeys: Mapping[str, bytes],
    stored_max_serial: int,
    now: datetime | None = None,
    require_environment: str | None = None,
) -> VerifiedKeyset:
    """Verify a ``GET /build-backend/keys`` response.

    The validation contract is §4.4 of the ADR:

    1. ``keyset.keyset_serial > stored_max_serial`` (anti-rollback).
    2. ``now < keyset.expires_at``.
    3. At least one signature in ``signatures[]`` verifies against a pinned
       root public key whose id matches ``root_key_id``.
    4. ``keyset.alg_required`` matches HA's pinned ciphersuite.
    5. The named ``active_key_id`` exists in ``keys[]`` with alg
       :data:`HPKE_KEM_ALG` and a 32-byte raw public key.
    6. When ``require_environment`` is given (G6, ADR-0003 D-A/D-E):
       ``keyset.environment`` must exist and equal it exactly. Missing or
       mismatching environment fails closed — a "test" keyset must never be
       accepted where "production" is required, even though the proxy also
       enforces this server-side (defense-in-depth).

    Args:
        response: Parsed JSON response body.
        root_pubkeys: ``{root_key_id: ed25519_pub_raw_32B}`` — for production,
            pass :data:`ROOT_PUBKEYS_PINNED`. Tests pass fixture roots.
        stored_max_serial: Highest ``keyset_serial`` HA has ever accepted.
            Pass ``-1`` (or any negative) on first contact.
        now: Override clock for tests (defaults to ``datetime.now(timezone.utc)``).

    Raises:
        KeysetVerificationError: with a stable ``code`` for fail-closed handling.

    Returns:
        :class:`VerifiedKeyset` containing the canonical-verified keyset and
        the resolved active key. Never contains any private material.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not isinstance(response, Mapping):
        raise KeysetVerificationError("keyset_invalid", "response not an object")

    # Defense-in-depth: a public keyset endpoint must never carry a
    # ``private_key`` field. Reject before persistence so a misbehaving
    # proxy cannot poison the cache with private material. Performed
    # before signature / serial / persistence checks; the caller never
    # gets back a VerifiedKeyset whose raw response held private bytes.
    if _contains_private_key_field(response):
        raise KeysetVerificationError(
            "keyset_private_material_present",
            "response contains a private_key field; refusing to cache",
        )

    keyset = response.get("keyset")
    signatures = response.get("signatures")
    if not isinstance(keyset, Mapping):
        raise KeysetVerificationError("keyset_invalid", "keyset object missing")
    if not isinstance(signatures, list) or not signatures:
        # No signature at all -> reject. First-contact has no exception.
        raise KeysetVerificationError(
            "keyset_unsigned", "signatures[] missing or empty"
        )

    # ---- Anti-rollback (1)
    serial = keyset.get("keyset_serial")
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise KeysetVerificationError(
            "keyset_invalid", "keyset_serial missing or not int"
        )
    if serial <= stored_max_serial:
        raise KeysetVerificationError(
            "keyset_serial_rollback",
            f"keyset_serial {serial} <= stored_max_serial {stored_max_serial}",
        )

    # ---- Expiry (2)
    issued_at_str = keyset.get("issued_at")
    expires_at_str = keyset.get("expires_at")
    issued_at = _parse_iso8601(issued_at_str, field_name="keyset.issued_at")
    expires_at = _parse_iso8601(expires_at_str, field_name="keyset.expires_at")
    if issued_at > expires_at:
        raise KeysetVerificationError(
            "keyset_invalid", "issued_at after expires_at"
        )
    if now >= expires_at:
        raise KeysetVerificationError(
            "keyset_expired",
            f"now {now.isoformat()} >= expires_at {expires_at.isoformat()}",
        )

    # ---- alg pin (4)
    alg_required = keyset.get("alg_required")
    if alg_required != HPKE_CIPHERSUITE:
        raise KeysetVerificationError(
            "keyset_alg_mismatch",
            f"alg_required={alg_required!r} != pinned {HPKE_CIPHERSUITE!r}",
        )

    # ---- min envelope version
    min_v = keyset.get("min_envelope_version", 1)
    if not isinstance(min_v, int) or min_v > ENVELOPE_VERSION:
        raise KeysetVerificationError(
            "keyset_min_version_unsupported",
            f"min_envelope_version={min_v!r} > supported {ENVELOPE_VERSION}",
        )

    # ---- keys[] shape
    keys = keyset.get("keys")
    if not isinstance(keys, list) or not keys:
        raise KeysetVerificationError("keyset_invalid", "keys[] missing or empty")
    parsed_keys: dict[str, ActiveKey] = {}
    for entry in keys:
        if not isinstance(entry, Mapping):
            raise KeysetVerificationError("keyset_invalid", "keys[] entry not object")
        kid = entry.get("key_id")
        kalg = entry.get("alg")
        kpub_b64 = entry.get("public_key")
        if not isinstance(kid, str) or not kid:
            raise KeysetVerificationError("keyset_invalid", "key_id missing")
        if kalg != HPKE_KEM_ALG:
            raise KeysetVerificationError(
                "keyset_alg_mismatch",
                f"key {kid!r} alg={kalg!r} != {HPKE_KEM_ALG}",
            )
        kpub_raw = _b64decode_strict(
            kpub_b64,
            field_name=f"keys[{kid}].public_key",
            expected_len=32,
        )
        parsed_keys[kid] = ActiveKey(
            key_id=kid,
            alg=HPKE_KEM_ALG,
            public_key_b64=kpub_b64,
            public_key_raw=kpub_raw,
        )

    active_key_id = keyset.get("active_key_id")
    if not isinstance(active_key_id, str) or active_key_id not in parsed_keys:
        raise KeysetVerificationError(
            "keyset_invalid",
            f"active_key_id={active_key_id!r} not in keys[]",
        )

    # ---- Signature verification (3)
    canonical = canonical_json(keyset)
    accepted_root = None
    for sig_entry in signatures:
        if not isinstance(sig_entry, Mapping):
            continue
        if sig_entry.get("alg") != "Ed25519":
            continue
        root_key_id = sig_entry.get("root_key_id")
        signature_b64 = sig_entry.get("signature")
        if not isinstance(root_key_id, str) or not isinstance(signature_b64, str):
            continue
        root_pub = root_pubkeys.get(root_key_id)
        if root_pub is None:
            continue
        try:
            sig_raw = base64.b64decode(signature_b64, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        if len(sig_raw) != 64:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(root_pub).verify(sig_raw, canonical)
        except (InvalidSignature, ValueError):
            continue
        accepted_root = root_key_id
        break

    if accepted_root is None:
        raise KeysetVerificationError(
            "keyset_signature_invalid",
            "no signature verified against pinned root anchors",
        )

    # ---- Environment binding (6) — D-A / ADR-0003 D-E. Runs AFTER the
    # signature check so the environment claim is only ever judged on
    # authenticated data: a forged response surfaces as a signature error,
    # not as a misleading environment-mismatch diagnosis.
    environment = keyset.get("environment")
    if require_environment is not None and environment != require_environment:
        raise KeysetVerificationError(
            "keyset_environment_mismatch",
            f"environment={environment!r} != required {require_environment!r}",
        )

    return VerifiedKeyset(
        raw=dict(response),
        keyset=dict(keyset),
        keyset_serial=serial,
        active_key=parsed_keys[active_key_id],
        issued_at=issued_at_str,
        expires_at=expires_at_str,
        expires_at_dt=expires_at,
        environment=environment if isinstance(environment, str) else None,
    )


# ---------------------------------------------------------------------------
# Envelope sealing (Section 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeRequestContext:
    """Per-build context bound into ``aad``."""

    build_profile: str
    registry_file: str
    device_name: str
    device_key: str
    yaml_hash: str  # full sha256 hex


@dataclass(frozen=True)
class SealedEnvelope:
    """Result of :func:`seal_compile_secret_envelope`."""

    payload: dict[str, Any]
    key_id: str
    keyset_serial: int
    envelope_fingerprint: str = field(init=False, default="")

    def __post_init__(self) -> None:  # pragma: no cover - dataclass plumbing
        # Mirror fingerprint at the dataclass level for ergonomic logging.
        object.__setattr__(self, "envelope_fingerprint", self.payload["envelope_fingerprint"])


def _suite() -> CipherSuite:
    return CipherSuite.new(
        KEMId.DHKEM_X25519_HKDF_SHA256,
        KDFId.HKDF_SHA256,
        AEADId.CHACHA20_POLY1305,
    )


def _validate_yaml_hash(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise EnvelopeSealError(
            f"yaml_hash must be 64 hex chars, got {value!r}"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise EnvelopeSealError("yaml_hash not hex") from exc


def _new_request_nonce() -> bytes:
    return os.urandom(REQUEST_NONCE_LEN)


def seal_compile_secret_envelope(
    plaintext: bytes,
    *,
    active_key: ActiveKey,
    keyset_serial: int,
    context: EnvelopeRequestContext,
    request_nonce: bytes | None = None,
) -> SealedEnvelope:
    """Build the ``payload.compile_secret_envelope`` value.

    Args:
        plaintext: K=V compile-secret bytes (UTF-8). Must be non-empty.
        active_key: Verified active public key from a signed keyset.
        keyset_serial: Serial of the signed keyset that produced ``active_key``
            (carried in the result for audit, **not** in the wire envelope).
        context: Per-build metadata bound to ``aad``. ``yaml_hash`` must be the
            full 64-hex sha256 of the YAML the runner will compile.
        request_nonce: Override for tests; production must let this default to
            a fresh ``os.urandom`` value.

    Raises:
        EnvelopeSealError: on any seal failure or invalid input. Never
            silently widen the trust boundary.

    Returns:
        :class:`SealedEnvelope` whose ``payload`` is the JSON-serializable
        wire object.
    """
    if not isinstance(plaintext, (bytes, bytearray)) or not plaintext:
        raise EnvelopeSealError("plaintext must be non-empty bytes")

    if active_key.alg != HPKE_KEM_ALG:
        raise EnvelopeSealError(
            f"active_key.alg={active_key.alg!r} != {HPKE_KEM_ALG}"
        )
    if len(active_key.public_key_raw) != 32:
        raise EnvelopeSealError("active_key.public_key_raw must be 32 bytes")

    if not isinstance(context, EnvelopeRequestContext):
        raise EnvelopeSealError("context must be EnvelopeRequestContext")
    if context.build_profile not in ("production", "factory"):
        # GHA only accepts envelope path for production; factory is forbidden.
        raise EnvelopeSealError(
            f"unsupported build_profile {context.build_profile!r}"
        )
    if not context.registry_file:
        raise EnvelopeSealError("registry_file empty")
    if not context.device_name:
        raise EnvelopeSealError("device_name empty")
    if not context.device_key:
        raise EnvelopeSealError("device_key empty")
    _validate_yaml_hash(context.yaml_hash)

    if request_nonce is None:
        request_nonce = _new_request_nonce()
    if len(request_nonce) != REQUEST_NONCE_LEN:
        raise EnvelopeSealError(
            f"request_nonce must be {REQUEST_NONCE_LEN} bytes"
        )

    aad: dict[str, Any] = {
        "envelope_v": ENVELOPE_VERSION,
        "alg": HPKE_CIPHERSUITE,
        "key_id": active_key.key_id,
        "build_profile": context.build_profile,
        "registry_file": context.registry_file,
        "device_name": context.device_name,
        "device_key": context.device_key,
        "yaml_hash": context.yaml_hash,
        "request_nonce": base64.b64encode(request_nonce).decode("ascii"),
    }
    aad_bytes = canonical_json(aad)

    suite = _suite()
    try:
        recipient_pub = suite.kem.deserialize_public_key(active_key.public_key_raw)
        enc, sender = suite.create_sender_context(
            recipient_pub, info=HPKE_INFO,
        )
        ciphertext = sender.seal(bytes(plaintext), aad=aad_bytes)
    except (PyHPKEError, SealError, ValueError) as exc:
        # Do NOT include any plaintext or key bytes in the error message.
        raise EnvelopeSealError(f"hpke_seal_failed: {type(exc).__name__}") from exc

    fingerprint_full = hashlib.sha256(enc + ciphertext).digest()
    envelope_fingerprint = fingerprint_full[:ENVELOPE_FINGERPRINT_LEN].hex()

    payload: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "alg": HPKE_CIPHERSUITE,
        "key_id": active_key.key_id,
        "enc": base64.b64encode(enc).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "aad": aad,
        "envelope_fingerprint": envelope_fingerprint,
    }

    return SealedEnvelope(
        payload=payload,
        key_id=active_key.key_id,
        keyset_serial=keyset_serial,
    )


# ---------------------------------------------------------------------------
# Envelope opening — used by the cross-implementation interop test only
# ---------------------------------------------------------------------------


def open_envelope_for_test(
    envelope: Mapping[str, Any],
    *,
    private_key_raw: bytes,
    expected_aad: Mapping[str, Any] | None = None,
) -> bytes:
    """Open an HPKE envelope. Test-only helper.

    Models the GHA-side decryption contract used by
    ``PVAutonomy/inverter-registry`` after PR-2: parse the envelope, validate
    the alg pin, deserialize the recipient private key, and call HPKE open
    with the canonical-JSON AAD.

    Production HA never decrypts envelopes; this helper exists to prove that
    HA-produced envelopes are interop-compatible with the GHA opener contract.
    """
    if envelope.get("alg") != HPKE_CIPHERSUITE:
        raise EnvelopeSealError(
            f"alg={envelope.get('alg')!r} != pinned {HPKE_CIPHERSUITE!r}"
        )
    if envelope.get("v") != ENVELOPE_VERSION:
        raise EnvelopeSealError("envelope version mismatch")

    aad = envelope.get("aad")
    if not isinstance(aad, Mapping):
        raise EnvelopeSealError("aad missing")
    if expected_aad is not None and dict(aad) != dict(expected_aad):
        raise EnvelopeSealError("aad mismatch vs expected")

    enc = base64.b64decode(envelope["enc"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)

    if len(private_key_raw) != 32:
        raise EnvelopeSealError("private_key_raw must be 32 bytes")

    suite = _suite()
    priv = suite.kem.deserialize_private_key(private_key_raw)
    aad_bytes = canonical_json(dict(aad))
    try:
        recipient = suite.create_recipient_context(enc, priv, info=HPKE_INFO)
        return recipient.open(ciphertext, aad=aad_bytes)
    except (PyHPKEError, OpenError, ValueError) as exc:
        raise EnvelopeSealError(f"hpke_open_failed: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# Trust-anchor posture helper
# ---------------------------------------------------------------------------


def production_root_anchors_available() -> bool:
    """True iff at least one real production root pin is configured.

    Since G6 (HPKE-4) this is True: root-2026-a from the G3 ceremony is
    pinned in :data:`ROOT_PUBKEYS_PINNED`, making production envelope mode
    reachable. Activation still requires the D-E runtime gates (wired
    backend, verified production keyset; 404/405 keeps legacy fallback).
    """
    return bool(ROOT_PUBKEYS_PINNED)


# ---------------------------------------------------------------------------
# Signed-keyset cache + HTTP client (HA glue)
# ---------------------------------------------------------------------------

#: HA Store key for the signed build-backend keyset cache.
KEYSET_STORAGE_KEY: Final[str] = "pvautonomy_ops_keyring_buildbackend"
#: HA Store schema version.
KEYSET_STORAGE_VERSION: Final[int] = 1
#: HTTP path for the proxy keyset endpoint.
KEYSET_ENDPOINT_PATH: Final[str] = "/build-backend/keys"
#: Network timeout (seconds) for keyset fetch.
KEYSET_FETCH_TIMEOUT_S: Final[float] = 15.0


class BuildBackendKeysetCache:
    """Persistent cache for the latest verified build-backend keyset.

    Persists ONLY public, signed metadata + audit timestamps:

    * ``signed_keyset``: the entire response body that previously verified
      successfully (so we can re-verify the cached blob deterministically
      against pinned roots after restart).
    * ``stored_max_serial``: highest accepted ``keyset_serial`` ever
      (anti-rollback fence).
    * ``last_envelope_used_at`` / ``last_envelope_key_id``: per-build audit.
    * ``last_keyset_serial``: serial of the last keyset used for a build.

    Never persists private keys, plaintext compile secrets, ``enc``,
    ``ciphertext``, or full envelope JSON.

    Storage backend is :class:`homeassistant.helpers.storage.Store` (matches
    the keyring pattern used by :mod:`.keyring`).
    """

    def __init__(self, hass: Any) -> None:
        # Lazy HA import — keeps the pure-crypto half importable in tests
        # that do not stub Home Assistant.
        from homeassistant.helpers.storage import Store

        self._hass = hass
        self._store: Any = Store(hass, KEYSET_STORAGE_VERSION, KEYSET_STORAGE_KEY)
        self._data: dict[str, Any] = {
            "version": KEYSET_STORAGE_VERSION,
            "signed_keyset": None,
            "stored_max_serial": -1,
            "last_envelope_used_at": None,
            "last_envelope_key_id": None,
            "last_keyset_serial": None,
        }

    async def async_load(self) -> None:
        """Load cache from persistent storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._data.update(
                {k: v for k, v in stored.items() if k in self._data}
            )

    async def _async_save(self) -> None:
        await self._store.async_save(self._data)

    @property
    def stored_max_serial(self) -> int:
        return int(self._data.get("stored_max_serial", -1))

    @property
    def cached_signed_keyset(self) -> dict[str, Any] | None:
        ks = self._data.get("signed_keyset")
        return dict(ks) if isinstance(ks, dict) else None

    @property
    def last_envelope_key_id(self) -> str | None:
        v = self._data.get("last_envelope_key_id")
        return v if isinstance(v, str) else None

    @property
    def last_envelope_used_at(self) -> str | None:
        v = self._data.get("last_envelope_used_at")
        return v if isinstance(v, str) else None

    @property
    def last_keyset_serial(self) -> int | None:
        v = self._data.get("last_keyset_serial")
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    async def async_record_accepted_keyset(self, verified: VerifiedKeyset) -> None:
        """Persist a freshly-verified keyset and bump the serial fence."""
        self._data["signed_keyset"] = dict(verified.raw)
        self._data["stored_max_serial"] = max(
            self.stored_max_serial, verified.keyset_serial
        )
        await self._async_save()

    async def async_record_envelope_used(
        self, *, key_id: str, keyset_serial: int, when: datetime | None = None
    ) -> None:
        """Persist audit fields after a successful envelope build dispatch."""
        if when is None:
            when = datetime.now(timezone.utc)
        self._data["last_envelope_used_at"] = when.isoformat()
        self._data["last_envelope_key_id"] = key_id
        self._data["last_keyset_serial"] = int(keyset_serial)
        await self._async_save()

    def diagnostic_state(self) -> dict[str, Any]:
        """Return a redacted snapshot of the audit state for diagnostics."""
        return {
            "stored_max_serial": self.stored_max_serial,
            "last_envelope_used_at": self.last_envelope_used_at,
            "last_envelope_key_id": self.last_envelope_key_id,
            "last_keyset_serial": self.last_keyset_serial,
            "has_cached_signed_keyset": (
                self._data.get("signed_keyset") is not None
            ),
        }


async def fetch_signed_keyset(
    session: Any,
    *,
    base_url: str,
    api_key: str,
    timeout_s: float = KEYSET_FETCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Call ``GET /build-backend/keys`` and return the JSON response body.

    The caller is responsible for verifying the returned body via
    :func:`verify_signed_keyset`.

    Raises:
        KeysetEndpointUnsupported: on 404 or 405 — the **only** condition
            under which the caller may legitimately fall back to the legacy
            ``payload.encrypted_secrets`` path.
        KeysetVerificationError: on any other non-2xx response or transport
            failure (mapped to ``code='keyset_endpoint_error'``).
    """
    import aiohttp  # local import — match build_backend.py

    url = base_url.rstrip("/") + KEYSET_ENDPOINT_PATH
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            status = resp.status
            if status in (404, 405):
                raise KeysetEndpointUnsupported(
                    f"proxy {status} on {KEYSET_ENDPOINT_PATH}"
                )
            if status != 200:
                body_excerpt = (await resp.text())[:200]
                raise KeysetVerificationError(
                    "keyset_endpoint_error",
                    f"HTTP {status} on {KEYSET_ENDPOINT_PATH}: {body_excerpt}",
                )
            try:
                return await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise KeysetVerificationError(
                    "keyset_endpoint_error",
                    "response not JSON",
                ) from exc
    except KeysetEndpointUnsupported:
        raise
    except KeysetVerificationError:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        # TimeoutError: aiohttp's TOTAL timeout (read/body phase) raises the
        # builtin TimeoutError (== asyncio.TimeoutError on py3.11+), NOT a
        # ClientError subclass — without this clause it would escape the
        # keyset_endpoint_error mapping AND the cache-recovery path.
        raise KeysetVerificationError(
            "keyset_endpoint_error",
            f"transport error: {type(exc).__name__}",
        ) from exc


async def load_or_refresh_keyset(
    *,
    session: Any,
    cache: BuildBackendKeysetCache,
    base_url: str,
    api_key: str,
    root_pubkeys: Mapping[str, bytes] | None = None,
    now: datetime | None = None,
    require_environment: str | None = "production",
) -> VerifiedKeyset:
    """Fetch + verify a fresh keyset; on transport failure, try the cache.

    Trust-anchor and fail-closed semantics (ADR §6.1):

    * 404/405 → propagate :class:`KeysetEndpointUnsupported` so the caller
      can use the legacy path. **Only** legal fallback condition.
    * 200 + valid signed keyset → accept, persist, record max serial, return.
    * 200 + invalid keyset (bad signature, alg, serial, expiry) → re-raise
      :class:`KeysetVerificationError`. **Never** fall back to legacy.
    * Network/transport failure → if the cache holds a still-valid signed
      keyset that re-verifies, return it; otherwise re-raise the error.
    """
    if root_pubkeys is None:
        root_pubkeys = ROOT_PUBKEYS_PINNED

    # Re-verification floor for a document we may already have accepted:
    # between rotations the proxy re-serves the CURRENT keyset, so both the
    # fresh fetch and the cache-recovery path must idempotently re-accept
    # serial == stored fence. Verifying against fence-1 keeps true rollbacks
    # rejected (`serial <= fence-1` ⟺ `serial < fence`) while allowing the
    # steady-state equal serial (#151 A0: the old fresh path passed the raw
    # fence and fail-closed every managed build after the first).
    reverify_floor = (
        cache.stored_max_serial - 1 if cache.stored_max_serial >= 0 else -1
    )

    fresh_error: KeysetVerificationError | None = None
    try:
        body = await fetch_signed_keyset(
            session, base_url=base_url, api_key=api_key
        )
        verified = verify_signed_keyset(
            body,
            root_pubkeys=root_pubkeys,
            stored_max_serial=reverify_floor,
            now=now,
            require_environment=require_environment,
        )
        await cache.async_record_accepted_keyset(verified)
        return verified
    except KeysetEndpointUnsupported:
        raise
    except KeysetVerificationError as exc:
        if exc.code == "keyset_endpoint_error":
            fresh_error = exc
        else:
            # Bad signature / alg / serial / expiry from a reachable proxy:
            # ALWAYS fail-closed. No legacy fallback, no cache fallback.
            raise

    # Transport failure — try cached keyset (re-verifies against pinned roots).
    cached = cache.cached_signed_keyset
    if cached is None:
        if fresh_error is None:
            raise KeysetVerificationError(
                "keyset_endpoint_error", "no cache available"
            )
        raise fresh_error
    try:
        return verify_signed_keyset(
            cached,
            root_pubkeys=root_pubkeys,
            stored_max_serial=reverify_floor,
            now=now,
            require_environment=require_environment,
        )
    except KeysetVerificationError as cache_exc:
        # Cache is unusable (expired, root rotated out, environment
        # mismatch). Surface the fresh transport error, chaining the cache
        # rejection so its code stays visible in the traceback.
        if fresh_error is None:
            raise
        raise fresh_error from cache_exc


__all__ = [
    "ActiveKey",
    "BuildBackendKeysetCache",
    "EnvelopeRequestContext",
    "EnvelopeSealError",
    "ENVELOPE_FINGERPRINT_LEN",
    "ENVELOPE_VERSION",
    "HPKE_CIPHERSUITE",
    "HPKE_INFO",
    "HPKE_KEM_ALG",
    "KEYSET_ENDPOINT_PATH",
    "KEYSET_FETCH_TIMEOUT_S",
    "KEYSET_STORAGE_KEY",
    "KEYSET_STORAGE_VERSION",
    "KeysetEndpointUnsupported",
    "KeysetVerificationError",
    "REQUEST_NONCE_LEN",
    "ROOT_PUBKEYS_PINNED",
    "SealedEnvelope",
    "SecretEnvelopeError",
    "VerifiedKeyset",
    "canonical_json",
    "fetch_signed_keyset",
    "load_or_refresh_keyset",
    "open_envelope_for_test",
    "production_root_anchors_available",
    "seal_compile_secret_envelope",
    "verify_signed_keyset",
]
