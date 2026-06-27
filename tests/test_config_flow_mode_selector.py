"""fix/#128 — Config-flow Build Service mode selector.

Verifies:
 1. managed_build routes to managed_key step (not proxy).
 2. adopt_direct skips proxy, routes to manufacturer (via adopt_direct method).
 3. local_esphome routes to local_esphome_guide.
 4. advanced_proxy routes to proxy step.
 5. managed_key shows Build-Key field, not proxy_base_url.
 6. managed_key uses DEFAULT_PROXY_BASE_URL internally.
 7. adopt_direct creates entry without pva_ key requirement (proxy fields empty).
 8. adopt_direct does not call _preflight_compile_secret_key.
 9. local_esphome path routes adopt_mode=True, mode=local_esphome.
10. self-hosted path sets mode=self_hosted before proxy step.
11. managed mode abort in progress_build uses managed_build_not_configured,
    not compile_secret_missing_or_invalid.
12. setup_new backward-compat handler redirects to managed_build.
13. adopt_existing backward-compat handler redirects to adopt_direct.
14. async_step_user menu has the four new options.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
from custom_components.pvautonomy_ops.const import (
    BUILD_SERVICE_LOCAL_ESPHOME,
    BUILD_SERVICE_MANAGED,
    BUILD_SERVICE_SELF_HOSTED,
    DEFAULT_PROXY_BASE_URL,
    MENU_OPTION_ADOPT_DIRECT,
    MENU_OPTION_ADVANCED_PROXY,
    MENU_OPTION_LOCAL_ESPHOME,
    MENU_OPTION_MANAGED_BUILD,
)


def _run(coro):
    """Run a coroutine synchronously using a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_flow() -> PVAutonomyOpsConfigFlow:
    """Return a minimal PVAutonomyOpsConfigFlow for mode-selector tests.

    Uses __new__ to bypass __init__ (DeviceMetadata import) and sets all
    attributes that the routing steps need.
    """
    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)
    flow.hass = MagicMock()
    flow._build_task = None
    flow._build_result = None
    flow._flash_error = None
    flow._display_name = "test-device"
    flow._adopt_mode = False
    flow._build_service_mode = BUILD_SERVICE_MANAGED
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""
    flow._get_compile_secret_key = AsyncMock(return_value=None)
    return flow


# ---------------------------------------------------------------------------
# 14. async_step_user — menu shape
# ---------------------------------------------------------------------------


def test_step_user_shows_four_options():
    """async_step_user must expose exactly the four new menu options."""
    flow = _make_flow()
    result = _run(flow.async_step_user())
    assert result["type"] == "menu"
    options = result["menu_options"]
    assert MENU_OPTION_MANAGED_BUILD in options
    assert MENU_OPTION_ADOPT_DIRECT in options
    assert MENU_OPTION_LOCAL_ESPHOME in options
    assert MENU_OPTION_ADVANCED_PROXY in options
    # Old options must NOT appear
    assert "setup_new" not in options
    assert "adopt_existing" not in options


# ---------------------------------------------------------------------------
# 1. managed_build routing
# ---------------------------------------------------------------------------


def test_managed_build_sets_mode_and_not_adopt():
    """async_step_managed_build sets mode=managed and adopt_mode=False."""
    flow = _make_flow()
    # managed_key will call async_show_form — intercept it
    with patch.object(flow, "async_step_managed_key", new=AsyncMock(
        return_value={"type": "form", "step_id": "managed_key"}
    )):
        result = _run(flow.async_step_managed_build())
    assert flow._build_service_mode == BUILD_SERVICE_MANAGED
    assert flow._adopt_mode is False
    assert result["step_id"] == "managed_key"


# ---------------------------------------------------------------------------
# 5+6. managed_key — form shape and DEFAULT_PROXY_BASE_URL
# ---------------------------------------------------------------------------


def test_managed_key_form_has_api_key_only():
    """managed_key form must contain proxy_api_key and NOT proxy_base_url."""
    flow = _make_flow()
    result = _run(flow.async_step_managed_key())
    assert result["type"] == "form"
    assert result["step_id"] == "managed_key"
    schema_keys = list(result["data_schema"].schema.keys())
    key_names = [k.schema if hasattr(k, "schema") else str(k) for k in schema_keys]
    assert "proxy_api_key" in key_names
    assert "proxy_base_url" not in key_names


def test_managed_key_sets_default_proxy_url_on_submit():
    """On submit, managed_key must hard-code proxy_base_url = DEFAULT_PROXY_BASE_URL."""
    flow = _make_flow()

    async def _fake_manufacturer():
        return {"type": "form", "step_id": "manufacturer"}

    flow.async_step_manufacturer = _fake_manufacturer

    # Simulate a build backend that succeeds
    mock_backend = MagicMock()
    mock_backend.health_check = AsyncMock()
    mock_backend.whoami = AsyncMock(return_value={"customer_id": "cust-001"})
    mock_backend.close = AsyncMock()

    with patch(
        "custom_components.pvautonomy_ops.build_backend.ProxyRemoteBuildBackend",
        return_value=mock_backend,
    ):
        # Patch the import inside async_step_managed_key
        with patch.dict(
            "sys.modules",
            {},
        ):
            with patch(
                "custom_components.pvautonomy_ops.config_flow.PVAutonomyOpsConfigFlow"
                "._find_inherited_customer_id",
                return_value=None,
            ):
                with patch(
                    "custom_components.pvautonomy_ops.config_flow."
                    "ProxyRemoteBuildBackend",
                    return_value=mock_backend,
                    create=True,
                ):
                    _run(
                        flow.async_step_managed_key(
                            user_input={"proxy_api_key": "pva_test_key"}
                        )
                    )

    assert flow._proxy_base_url == DEFAULT_PROXY_BASE_URL
    assert flow._proxy_api_key == "pva_test_key"


# ---------------------------------------------------------------------------
# 2. adopt_direct — proxy skipped
# ---------------------------------------------------------------------------


def test_adopt_direct_sets_adopt_mode_and_clears_proxy():
    """adopt_direct must set adopt_mode=True, mode=local_esphome, clear proxy fields."""
    flow = _make_flow()
    flow._proxy_base_url = "https://some-proxy.example.com"
    flow._proxy_api_key = "pva_old"
    flow._proxy_customer_id = "old-cust"

    with patch.object(flow, "async_step_manufacturer", new=AsyncMock(
        return_value={"type": "form", "step_id": "manufacturer"}
    )):
        result = _run(flow.async_step_adopt_direct())

    assert flow._adopt_mode is True
    assert flow._build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME
    assert flow._proxy_base_url == ""
    assert flow._proxy_api_key == ""
    assert flow._proxy_customer_id == ""
    assert result["step_id"] == "manufacturer"


# ---------------------------------------------------------------------------
# 8. adopt_direct does NOT call _preflight_compile_secret_key
# ---------------------------------------------------------------------------


def test_adopt_direct_does_not_call_preflight():
    """adopt_direct must not touch _preflight_compile_secret_key."""
    flow = _make_flow()
    flow._preflight_compile_secret_key = AsyncMock()

    with patch.object(flow, "async_step_manufacturer", new=AsyncMock(
        return_value={"type": "form", "step_id": "manufacturer"}
    )):
        _run(flow.async_step_adopt_direct())

    flow._preflight_compile_secret_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. local_esphome routing
# ---------------------------------------------------------------------------


def test_local_esphome_sets_adopt_mode_and_routes_to_guide():
    """local_esphome must set adopt_mode=True, mode=local_esphome, route to guide."""
    flow = _make_flow()

    with patch.object(flow, "async_step_local_esphome_guide", new=AsyncMock(
        return_value={"type": "form", "step_id": "local_esphome_guide"}
    )):
        result = _run(flow.async_step_local_esphome())

    assert flow._adopt_mode is True
    assert flow._build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME
    assert flow._proxy_base_url == ""
    assert result["step_id"] == "local_esphome_guide"


def test_local_esphome_guide_form():
    """local_esphome_guide shows a form; submitting advances to manufacturer."""
    flow = _make_flow()

    # No input → form
    result = _run(flow.async_step_local_esphome_guide())
    assert result["type"] == "form"
    assert result["step_id"] == "local_esphome_guide"

    # With input → manufacturer
    with patch.object(flow, "async_step_manufacturer", new=AsyncMock(
        return_value={"type": "form", "step_id": "manufacturer"}
    )):
        result2 = _run(flow.async_step_local_esphome_guide(user_input={}))
    assert result2["step_id"] == "manufacturer"


# ---------------------------------------------------------------------------
# 4. advanced_proxy routing
# ---------------------------------------------------------------------------


def test_advanced_proxy_sets_self_hosted_and_routes_to_proxy():
    """advanced_proxy must set mode=self_hosted, adopt_mode=False, route to proxy."""
    flow = _make_flow()

    with patch.object(flow, "async_step_proxy", new=AsyncMock(
        return_value={"type": "form", "step_id": "proxy"}
    )):
        result = _run(flow.async_step_advanced_proxy())

    assert flow._build_service_mode == BUILD_SERVICE_SELF_HOSTED
    assert flow._adopt_mode is False
    assert result["step_id"] == "proxy"


# ---------------------------------------------------------------------------
# 11. managed mode abort in progress_build uses managed_build_not_configured
# ---------------------------------------------------------------------------


def test_managed_mode_abort_uses_managed_reason():
    """Missing key in managed mode → abort with 'managed_build_not_configured'."""
    flow = _make_flow()
    flow._build_service_mode = BUILD_SERVICE_MANAGED
    result = _run(flow.async_step_progress_build())
    assert result["type"] == "abort"
    assert result["reason"] == "managed_build_not_configured"


def test_non_managed_mode_abort_uses_compile_secret_reason():
    """Missing key in non-managed mode → abort with 'compile_secret_missing_or_invalid'."""
    flow = _make_flow()
    flow._build_service_mode = BUILD_SERVICE_SELF_HOSTED
    result = _run(flow.async_step_progress_build())
    assert result["type"] == "abort"
    assert result["reason"] == "compile_secret_missing_or_invalid"


# ---------------------------------------------------------------------------
# 12+13. Backward-compat redirects
# ---------------------------------------------------------------------------


def test_setup_new_redirects_to_managed_build():
    """async_step_setup_new must redirect to managed_build path."""
    flow = _make_flow()
    with patch.object(flow, "async_step_managed_build", new=AsyncMock(
        return_value={"type": "form", "step_id": "managed_key"}
    )) as mock_mb:
        _run(flow.async_step_setup_new())
    mock_mb.assert_awaited_once()


def test_adopt_existing_redirects_to_adopt_direct():
    """async_step_adopt_existing must redirect to adopt_direct path."""
    flow = _make_flow()
    with patch.object(flow, "async_step_adopt_direct", new=AsyncMock(
        return_value={"type": "form", "step_id": "manufacturer"}
    )) as mock_ad:
        _run(flow.async_step_adopt_existing())
    mock_ad.assert_awaited_once()
