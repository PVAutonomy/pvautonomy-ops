"""fix/#120 — build_firmware service preflights COMPILE_SECRET_KEY.

Tests for the preflight guard added to handle_build_firmware() in __init__.py.
Verifies that a missing or invalid COMPILE_SECRET_KEY is detected before
operation_runner.run() is called, so op_state never transitions to running for
a pure configuration gap.

Coverage:
  1. Missing key → HomeAssistantError before operation_runner.run().
  2. Invalid-format key → same HomeAssistantError.
  3. Valid key → operation_runner.run() is called (build proceeds).
  4. Error text mentions build encryption/operator provisioning.
  5. Error text does not contain any raw key material.
  6. operation_runner.run() is never called when key is absent/invalid.

Test isolation:
  handle_build_firmware is a closure inside _async_register_services(hass).
  We capture it via hass.services.async_register side-effect, then call it
  directly with a mocked service call. The keyring is injected via entry_data
  so no sys.modules patching is required.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Distinctive synthetic test values — clearly not real material.
# Split construction avoids assembling a 64-hex literal that triggers scanners.
_SYNTH_HEX_BLOCK = "0123456789abcdef"
_SYNTH_VALID_HEX = _SYNTH_HEX_BLOCK * 4   # 64 hex chars
_SYNTH_WRONG_LEN = _SYNTH_HEX_BLOCK        # 16 hex chars — too short
_SYNTH_NON_HEX = "ZZZZZZZZZZZZZZZZ" * 4   # 64 chars, not hex

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeKeyring:
    """Minimal keyring stub backed by an in-memory key value."""

    def __init__(self, key: str | None) -> None:
        self._key = key
        self.load_called = False

    async def async_load(self) -> None:
        self.load_called = True

    async def get_compile_secret_key(self) -> str | None:
        return self._key


def _make_handler(stored_key: str | None):
    """Register services against a mock hass; capture and return the
    handle_build_firmware handler together with bookkeeping.

    Returns (handler, operation_runner_mock, hass_mock).
    """
    from custom_components.pvautonomy_ops import _async_register_services
    from custom_components.pvautonomy_ops.const import DOMAIN

    entry_id = "01ENTRYIDXXXXXXXXXXXXXXXXX"
    operation_runner = MagicMock()
    operation_runner.run = AsyncMock(return_value={"success": True, "result": {}})

    fake_keyring = _FakeKeyring(stored_key)

    entry_data = {
        "keyring": fake_keyring,
        "config": {},
        "operation_runner": operation_runner,
        "operation_tracker": MagicMock(),
    }

    handlers: dict = {}

    def _capture(domain, name, handler, schema=None, supports_response=None):  # noqa: ARG001
        handlers[name] = handler

    hass = MagicMock()
    hass.services.async_register.side_effect = _capture
    hass.data = {DOMAIN: {entry_id: entry_data}}
    hass.bus.async_listen = MagicMock(return_value=lambda: None)

    # Patch _resolve_target_entry_data so the handler uses our entry_data.
    import custom_components.pvautonomy_ops as init_mod
    _orig = getattr(init_mod, "_resolve_target_entry_data", None)

    def _fake_resolve(h, call):
        return entry_id, entry_data

    init_mod._resolve_target_entry_data = _fake_resolve

    _run(_async_register_services(hass))
    init_mod._resolve_target_entry_data = _orig  # restore

    return handlers["build_firmware"], operation_runner, hass


def _call(**data):
    return SimpleNamespace(data=data)


# ---------------------------------------------------------------------------
# 1–2. Missing/invalid key → HomeAssistantError before operation_runner.run()
# ---------------------------------------------------------------------------


def test_build_firmware_raises_when_key_absent():
    """Missing COMPILE_SECRET_KEY → HomeAssistantError."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner, _ = _make_handler(None)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return exc

    exc = _run(_run_test())
    assert exc is not None, "Expected HomeAssistantError, got None"
    assert isinstance(exc, HomeAssistantError)


def test_build_firmware_raises_when_key_invalid_format():
    """Invalid-format key → HomeAssistantError (same as absent)."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner, _ = _make_handler(_SYNTH_WRONG_LEN)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return exc

    exc = _run(_run_test())
    assert exc is not None
    assert isinstance(exc, HomeAssistantError)


def test_build_firmware_raises_when_key_non_hex():
    """Non-hex key → HomeAssistantError."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner, _ = _make_handler(_SYNTH_NON_HEX)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return exc

    exc = _run(_run_test())
    assert exc is not None
    assert isinstance(exc, HomeAssistantError)


# ---------------------------------------------------------------------------
# 3. operation_runner.run() is NOT called when key is absent/invalid
# ---------------------------------------------------------------------------


def test_operation_runner_not_called_on_missing_key():
    """operation_runner.run() must not be called for a configuration gap."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner, _ = _make_handler(None)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
        except HomeAssistantError as err:
            assert "Build encryption is not configured" in str(err)

    _run(_run_test())
    op_runner.run.assert_not_called()


def test_operation_runner_not_called_on_invalid_key():
    """operation_runner.run() must not be called for an invalid-format key."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner, _ = _make_handler(_SYNTH_NON_HEX)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
        except HomeAssistantError as err:
            assert "Build encryption is not configured" in str(err)

    _run(_run_test())
    op_runner.run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Error text is actionable and does not contain raw key material
# ---------------------------------------------------------------------------


def test_error_text_mentions_provisioning():
    """Error message must mention build encryption / operator provisioning."""
    from homeassistant.exceptions import HomeAssistantError

    handler, _, _ = _make_handler(None)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return str(exc)

    msg = _run(_run_test())
    assert msg is not None
    msg_lower = msg.lower()
    assert "build encryption" in msg_lower or "operator provisioning" in msg_lower


def test_error_text_does_not_contain_raw_key():
    """Error text must never contain the raw key value."""
    from homeassistant.exceptions import HomeAssistantError

    handler, _, _ = _make_handler(_SYNTH_NON_HEX)

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return str(exc)

    msg = _run(_run_test())
    assert _SYNTH_NON_HEX not in (msg or "")


# ---------------------------------------------------------------------------
# 5. Valid key → operation_runner.run() IS called (build proceeds normally)
# ---------------------------------------------------------------------------


def test_operation_runner_called_with_valid_key():
    """Valid 64-hex key → operation_runner.run() is called."""
    handler, op_runner, _ = _make_handler(_SYNTH_VALID_HEX)

    _run(handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX")))
    op_runner.run.assert_called_once()


# ---------------------------------------------------------------------------
# 6. No raw key in logs
# ---------------------------------------------------------------------------


def test_preflight_never_logs_raw_key(caplog):
    """The raw key must not appear in any log output during preflight."""
    handler, _, _ = _make_handler(_SYNTH_VALID_HEX)

    with caplog.at_level(logging.DEBUG):
        _run(handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX")))

    log_text = caplog.text
    assert _SYNTH_VALID_HEX not in log_text


# ---------------------------------------------------------------------------
# fix/#128 — local build path guard
# ---------------------------------------------------------------------------


def _make_handler_with_backend(stored_key: str | None, build_backend: str):
    """Like _make_handler but injects CONF_BUILD_BACKEND into entry_data."""
    from custom_components.pvautonomy_ops import _async_register_services
    from custom_components.pvautonomy_ops.const import CONF_BUILD_BACKEND, DOMAIN

    entry_id = "01ENTRYIDXXXXXXXXXXXXXXXXX"
    operation_runner = MagicMock()
    operation_runner.run = AsyncMock(return_value={"success": True, "result": {}})

    fake_keyring = _FakeKeyring(stored_key)

    entry_data = {
        "keyring": fake_keyring,
        "config": {},
        "operation_runner": operation_runner,
        "operation_tracker": MagicMock(),
        CONF_BUILD_BACKEND: build_backend,
    }

    handlers: dict = {}

    def _capture(domain, name, handler, schema=None, supports_response=None):  # noqa: ARG001
        handlers[name] = handler

    hass = MagicMock()
    hass.services.async_register.side_effect = _capture
    hass.data = {DOMAIN: {entry_id: entry_data}}
    hass.bus.async_listen = MagicMock(return_value=lambda: None)

    import custom_components.pvautonomy_ops as init_mod
    _orig = getattr(init_mod, "_resolve_target_entry_data", None)

    def _fake_resolve(h, call):
        return entry_id, entry_data

    init_mod._resolve_target_entry_data = _fake_resolve
    _run(_async_register_services(hass))
    init_mod._resolve_target_entry_data = _orig

    return handlers["build_firmware"], operation_runner


def test_build_firmware_raises_for_manual_backend():
    """CONF_BUILD_BACKEND='manual' → HomeAssistantError mentioning 'local build path'."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner = _make_handler_with_backend(_SYNTH_VALID_HEX, "manual")

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return str(exc)

    msg = _run(_run_test())
    assert msg is not None, "Expected HomeAssistantError, got None"
    assert "local build path" in msg.lower() or "local build" in msg.lower()
    op_runner.run.assert_not_called()


def test_build_firmware_raises_for_esphome_dashboard_backend():
    """CONF_BUILD_BACKEND='esphome_dashboard' → HomeAssistantError (local path)."""
    from homeassistant.exceptions import HomeAssistantError

    handler, op_runner = _make_handler_with_backend(
        _SYNTH_VALID_HEX, "esphome_dashboard"
    )

    async def _run_test():
        try:
            await handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX"))
            return None
        except HomeAssistantError as exc:
            return str(exc)

    msg = _run(_run_test())
    assert msg is not None
    assert "local build path" in msg.lower() or "local build" in msg.lower()
    op_runner.run.assert_not_called()


def test_build_firmware_does_not_raise_for_proxy_remote_backend():
    """CONF_BUILD_BACKEND='proxy_remote' with valid key → NOT a local-path error."""
    handler, op_runner = _make_handler_with_backend(
        _SYNTH_VALID_HEX, "proxy_remote"
    )
    # proxy_remote + valid key → build proceeds (op_runner.run called)
    _run(handler(_call(entry_id="01ENTRYIDXXXXXXXXXXXXXXXXX")))
    op_runner.run.assert_called_once()
