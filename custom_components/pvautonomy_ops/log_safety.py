"""Exception text that is safe to log (FIX-C1, #317 #322 #323 #326).

A log call that renders an exception, through ``exc_info``, a traceback or
``str(exc)``, renders whatever the author of that exception put into its
message. At the credential-handling sites of this integration that author
can be Home Assistant, the ESPHome integration, a library or an external
script. Their messages cannot be decided from this repository (log traceback
audit #325, class U).

A caller therefore logs a fixed message plus :func:`safe_exc_text`, and never
``exc_info``. The function returns the message only for the exception types
below and the type name for every other exception.

The allowlist holds exact types; a subclass is not trusted. A type is on it
because every place that constructs it passes fixed text or text from a
non-credential source. Reviewed at ``8613a52``:

- ``flash_uploader.OTAError``. Raised in ``flash_uploader`` by:

  - ``_recv_exactly`` and ``_check_response``: a fixed stage label, byte
    counts, a protocol response byte, or an entry of the fixed
    ``_ERROR_MESSAGES`` table;
  - ``ota_upload``: fixed text or the protocol version or auth-type byte.
    For an ``OSError`` or ``ConnectionError`` of the connection block, the
    message is that error's own text: errno and strerror, and for a failed
    connect the host and port;
  - ``ota_upload_with_retry``: the text of another ``OTAError``.

  Also raised in ``button.PVAutonomyOpsFlashButton._execute_flash`` as
  ``Cannot resolve IP for device: <device id>``.
- ``esphome_secrets.SecretsFileUnreadableError``. Message
  ``<path> could not be read: <reason>``. ``_read_secrets_strict`` passes the
  file's path and one of four fixed reasons.
- ``credential_rotation.CredentialRotationError``. Every raise in
  ``credential_rotation`` passes one of three:

  - fixed text;
  - a fixed field label (``active API key``, ``pending OTA password``, ...);
  - the fixed reason of a ``SecretsFileUnreadableError``.

A new raise site of one of these types must keep this property, or the type
leaves the allowlist. ``tests/test_log_safety.py`` counts the raise sites, so
a new one fails the tests until it has been reviewed.
"""

from __future__ import annotations

from .credential_rotation import CredentialRotationError
from .esphome_secrets import SecretsFileUnreadableError
from .flash_uploader import OTAError

_MESSAGE_SAFE_TYPES: frozenset[type[BaseException]] = frozenset(
    {OTAError, SecretsFileUnreadableError, CredentialRotationError}
)


def safe_exc_text(exc: BaseException) -> str:
    """Return the message of an allowlisted exception, else its type name."""
    if type(exc) in _MESSAGE_SAFE_TYPES:
        return str(exc)
    return type(exc).__qualname__
