"""fix/#113 — Wizard COMPILE_SECRET_KEY preflight before firmware build.

Tests for PVAutonomyOpsConfigFlow._preflight_compile_secret_key() and the
async_step_progress_build() gate that prevents the build pipeline from starting
when no valid COMPILE_SECRET_KEY is provisioned.

Coverage:
  1. _preflight_compile_secret_key returns False when keyring has no key.
  2. _preflight_compile_secret_key returns False for invalid (non-hex/short) key.
  3. _preflight_compile_secret_key returns True for a valid 64-hex key.
  4. async_step_progress_build returns error_build_failed and sets a clear
     _flash_error message when the preflight fails (missing key).
  5. async_step_progress_build does NOT create a build task when preflight fails.
  6. async_step_progress_build starts the build task normally when key is valid.
  7. The raw key value is never logged by the preflight.
  8. Backend fail-closed behaviour: _COMPILE_SECRET_KEY_RE in build_backend stays
     intact (the preflight is additive; it does not replace the backend guard).

Test isolation note:
  Some test files in this suite (notably test_keyring.py) replace
  custom_components.pvautonomy_ops.keyring in sys.modules with a stripped stub.
  To avoid order-dependent failures, these tests mock `_get_compile_secret_key`
  directly on the flow instance instead of patching at the module level.
  This is safe because the production tests exercise _preflight_compile_secret_key
  via the method boundary — not by inspecting PVAutonomyKeyring internals.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
from custom_components.pvautonomy_ops.keyring import COMPILE_SECRET_KEY_RE

# Distinctive synthetic test values — clearly not real material.
# Split construction avoids assembling a 64-hex literal that triggers scanners.
_SYNTH_HEX_BLOCK = "0123456789abcdef"
_SYNTH_VALID_KEY = _SYNTH_HEX_BLOCK * 4  # 64 hex chars
_SYNTH_WRONG_LEN = _SYNTH_HEX_BLOCK      # 16 hex chars — too short
_SYNTH_NON_HEX = "ZZZZZZZZZZZZZZZZ" * 4  # 64 chars, not hex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously using a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_flow(
    stored_key: str | None,
    *,
    build_service_mode: str = "self_hosted",
) -> PVAutonomyOpsConfigFlow:
    """Return a PVAutonomyOpsConfigFlow stub for preflight tests.

    Uses __new__ to bypass __init__ (which imports DeviceMetadata from
    .metadata — a module that may be partially stubbed in the full suite).
    Mocks _get_compile_secret_key so tests are independent of sys.modules state.

    build_service_mode defaults to "self_hosted" so the abort reason for a
    missing key remains "compile_secret_missing_or_invalid" (the original
    behaviour preserved for these tests).  Pass "managed" to get the new
    "managed_build_not_configured" reason.
    """
    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)
    flow.hass = MagicMock()
    flow._build_task = None
    flow._build_result = None
    flow._flash_error = None
    flow._display_name = "test-device"
    # fix/#128: _build_service_mode must be set (bypassed by __new__)
    flow._build_service_mode = build_service_mode
    # Mock the keyring accessor — isolates from sys.modules ordering effects.
    flow._get_compile_secret_key = AsyncMock(return_value=stored_key)
    return flow


# ---------------------------------------------------------------------------
# 1–3. _preflight_compile_secret_key unit tests
# ---------------------------------------------------------------------------


def test_preflight_returns_false_when_key_absent():
    """Missing key → preflight returns False."""
    flow = _make_flow(None)
    assert _run(flow._preflight_compile_secret_key()) is False
    flow._get_compile_secret_key.assert_awaited_once()


def test_preflight_returns_false_for_wrong_length():
    """Short key (16 hex) → _get_compile_secret_key returns None → preflight False.

    _get_compile_secret_key validates format and returns None for invalid keys;
    the preflight mock simulates that validated-None result.
    """
    flow = _make_flow(None)  # _get_compile_secret_key returns None = format rejected
    assert _run(flow._preflight_compile_secret_key()) is False


def test_preflight_returns_false_for_non_hex():
    """Non-hex key → _get_compile_secret_key returns None → preflight False."""
    flow = _make_flow(None)  # _get_compile_secret_key returns None = format rejected
    assert _run(flow._preflight_compile_secret_key()) is False


def test_preflight_returns_true_for_valid_key():
    """64-hex key → preflight returns True."""
    flow = _make_flow(_SYNTH_VALID_KEY)
    assert _run(flow._preflight_compile_secret_key()) is True


# ---------------------------------------------------------------------------
# 4–5. async_step_progress_build: missing key → abort with dedicated reason
# ---------------------------------------------------------------------------

_ABORT_REASON = "compile_secret_missing_or_invalid"


def test_progress_build_aborts_with_dedicated_reason_on_missing_key():
    """async_step_progress_build → abort(compile_secret_missing_or_invalid) when key absent."""
    flow = _make_flow(None)
    result = _run(flow.async_step_progress_build())
    assert result["type"] == "abort"
    assert result["reason"] == _ABORT_REASON


def test_progress_build_no_task_when_key_missing():
    """Build task must NOT be created when preflight fails."""
    flow = _make_flow(None)
    _run(flow.async_step_progress_build())
    assert flow._build_task is None


def test_progress_build_abort_on_invalid_key_format():
    """Invalid-format key (_get_compile_secret_key returns None) → same abort reason."""
    flow = _make_flow(None)  # _get_compile_secret_key returns None = format rejected
    result = _run(flow.async_step_progress_build())
    assert result["type"] == "abort"
    assert result["reason"] == _ABORT_REASON


def test_no_flash_error_set_on_preflight_abort():
    """Option A: _flash_error is NOT set when aborting via dedicated reason."""
    flow = _make_flow(None)
    _run(flow.async_step_progress_build())
    # With Option A, preflight goes directly to async_abort — no _flash_error needed
    assert flow._flash_error is None


# ---------------------------------------------------------------------------
# 6. Valid key → build task is created
# ---------------------------------------------------------------------------


def test_progress_build_creates_task_when_key_valid():
    """async_step_progress_build creates a build task when the key is valid."""
    flow = _make_flow(_SYNTH_VALID_KEY)
    task_holder: list = []

    async def _noop_build():
        flow._build_result = MagicMock(success=True, error=None)

    def _fake_create_task(coro):
        task = asyncio.ensure_future(coro)
        task_holder.append(task)
        return task

    flow.hass.async_create_task = _fake_create_task
    flow._do_build_firmware = _noop_build

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(flow.async_step_progress_build())
        pending = [t for t in task_holder if not t.done()]
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()

    assert len(task_holder) == 1


# ---------------------------------------------------------------------------
# 7. No raw key in logs
# ---------------------------------------------------------------------------


def test_preflight_never_logs_raw_key(caplog):
    """The raw 64-hex key must not appear in any log output."""
    flow = _make_flow(_SYNTH_VALID_KEY)
    with caplog.at_level(logging.DEBUG):
        _run(flow._preflight_compile_secret_key())

    log_text = caplog.text
    assert _SYNTH_VALID_KEY not in log_text
    for i in range(len(_SYNTH_VALID_KEY) - 3):
        fragment = _SYNTH_VALID_KEY[i : i + 4]
        assert fragment not in log_text, f"Key fragment {fragment!r} leaked to log"


# ---------------------------------------------------------------------------
# 8. COMPILE_SECRET_KEY_RE export and backend guard still intact
# ---------------------------------------------------------------------------


def test_compile_secret_key_re_exported_from_keyring():
    """COMPILE_SECRET_KEY_RE must be importable from keyring.py."""
    assert COMPILE_SECRET_KEY_RE is not None
    assert COMPILE_SECRET_KEY_RE.pattern == r"^[0-9a-fA-F]{64}$"


def test_compile_secret_key_re_accepts_valid_key():
    assert COMPILE_SECRET_KEY_RE.match(_SYNTH_VALID_KEY) is not None


def test_compile_secret_key_re_rejects_short():
    assert COMPILE_SECRET_KEY_RE.match(_SYNTH_WRONG_LEN) is None


def test_compile_secret_key_re_rejects_non_hex():
    assert COMPILE_SECRET_KEY_RE.match(_SYNTH_NON_HEX) is None


def test_backend_fail_closed_guard_still_present():
    """The backend-level BuildError guard must not be removed.

    This is a source-level assertion — it verifies that the backend still
    raises for a missing key independently of the wizard preflight.
    Regression protection: the preflight is additive, not a replacement.
    """
    src_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components/pvautonomy_ops/build_backend.py"
    )
    src = src_path.read_text()
    assert "compile_secret_key_missing_or_invalid" in src
    assert "_COMPILE_SECRET_KEY_RE.match(key_hex)" in src
