"""fix/#130 — Local ESPHome YAML export from config flow.

Verifies:
 1. Route: local_esphome → guide → model → location → local_yaml_ready
    (not target_device) when build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME.
 2. local_yaml_ready shows no credential fields (empty schema).
 3. Submitting local_yaml_ready aborts with "local_yaml_exported".
 4. Generated YAML contains "!secret api_encryption_key" and "!secret ota_password".
 5. Generated YAML does NOT contain COMPILE_SECRET_KEY, "pva_", "customer_id",
    or "hpke" tokens.
 6. File is written to the configured path under hass.config.path().
 7. managed_build path is unaffected (still goes to managed_key, not yaml ready).
 8. adopt_direct path is unaffected (goes straight to manufacturer).
 9. No _preflight_compile_secret_key call in the local path.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
from custom_components.pvautonomy_ops.const import (
    BUILD_SERVICE_LOCAL_ESPHOME,
    BUILD_SERVICE_MANAGED,
)

# Save the real yaml_generator symbols before any stub can replace the module.
# _make_yaml_generator_stub() replaces sys.modules so that generate_device_yaml
# can be mocked; it must carry over functions added after fix/#134 so that
# subsequent test files that import derive_required_entity_names still work.
try:
    from custom_components.pvautonomy_ops.yaml_generator import (
        derive_required_entity_names as _real_derive_required_entity_names,
    )
except ImportError:
    _real_derive_required_entity_names = None  # type: ignore[assignment]


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
    *,
    build_service_mode: str = BUILD_SERVICE_LOCAL_ESPHOME,
    model_slug: str = "sph10k",
    site: str = "home",
    number: int = 1,
    registry_file: str = "inverter-registry/sph10k.yaml",
    local_yaml_mode: bool = False,
    adopt_mode: bool = True,
) -> PVAutonomyOpsConfigFlow:
    """Return a PVAutonomyOpsConfigFlow stub for local YAML export tests.

    Uses __new__ to bypass __init__ (which imports DeviceMetadata).
    hass.config.path returns a string suitable for Path() wrapping.

    local_yaml_mode: True for local_esphome (YAML export), False for adopt_direct.
    """
    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)

    hass_mock = MagicMock()
    hass_mock.config.path.side_effect = lambda *parts: str(Path("/tmp/ha_test_config") / Path(*parts))
    flow.hass = hass_mock

    flow._build_task = None
    flow._build_result = None
    flow._flash_error = None
    flow._display_name = "test-device"
    flow._adopt_mode = adopt_mode
    flow._local_yaml_mode = local_yaml_mode
    flow._build_service_mode = build_service_mode
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""
    flow._model_slug = model_slug
    flow._site = site
    flow._number = number
    flow._registry_file = registry_file
    flow._get_compile_secret_key = AsyncMock(return_value=None)
    return flow


def _make_yaml_generator_stub(yaml_content: str = "placeholder_yaml: true"):
    """Create and install a sys.modules stub for yaml_generator.

    Stubs generate_device_yaml so callers see a controlled return value.
    Carries over derive_required_entity_names (real function saved at import
    time) so test modules that import it after this stub is installed still
    get a working implementation (#134 compatibility).
    """
    mod = types.ModuleType("custom_components.pvautonomy_ops.yaml_generator")

    class YamlGenerationError(Exception):
        pass

    mod.YamlGenerationError = YamlGenerationError
    mod.generate_device_yaml = MagicMock(return_value=yaml_content)
    if _real_derive_required_entity_names is not None:
        mod.derive_required_entity_names = _real_derive_required_entity_names
    sys.modules["custom_components.pvautonomy_ops.yaml_generator"] = mod
    return mod


# ---------------------------------------------------------------------------
# 1. Route: location → local_yaml_ready in local_esphome mode
# ---------------------------------------------------------------------------


def test_location_routes_to_local_yaml_ready_in_local_mode():
    """In LOCAL_ESPHOME mode with _local_yaml_mode=True, location must call local_yaml_ready."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME, local_yaml_mode=True)
    _make_yaml_generator_stub("api_encryption_key: !secret api_encryption_key\nota_password: !secret ota_password")

    with patch.object(flow, "async_step_local_yaml_ready", new=AsyncMock(
        return_value={"type": "form", "step_id": "local_yaml_ready"}
    )) as mock_local:
        result = _run(flow.async_step_location(user_input={
            "site_preset": "custom",
            "site": "home",
            "number": 1,
        }))

    mock_local.assert_awaited_once()
    assert result["step_id"] == "local_yaml_ready"


def test_location_routes_to_target_device_in_managed_mode():
    """In MANAGED mode, async_step_location must call async_step_target_device."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_MANAGED)
    flow._adopt_mode = False

    with patch.object(flow, "async_step_target_device", new=AsyncMock(
        return_value={"type": "form", "step_id": "target_device"}
    )) as mock_target:
        result = _run(flow.async_step_location(user_input={
            "site_preset": "custom",
            "site": "home",
            "number": 1,
        }))

    mock_target.assert_awaited_once()
    assert result["step_id"] == "target_device"


# ---------------------------------------------------------------------------
# 2. local_yaml_ready shows empty schema (no credential fields)
# ---------------------------------------------------------------------------


def test_local_yaml_ready_form_has_no_credential_fields(tmp_path):
    """local_yaml_ready must show an empty schema — no api_key or credential fields."""
    flow = _make_flow()
    _make_yaml_generator_stub("api_encryption_key: !secret api_encryption_key")

    with patch.object(
        flow,
        "_generate_and_save_local_yaml",
        new=AsyncMock(return_value=(str(tmp_path / "home_sph10k_01.yaml"), "sph10k_home_01")),
    ):
        result = _run(flow.async_step_local_yaml_ready())

    assert result["type"] == "form"
    assert result["step_id"] == "local_yaml_ready"
    # Schema must be empty — no fields for credentials
    schema = result["data_schema"]
    schema_keys = list(schema.schema.keys()) if schema else []
    assert len(schema_keys) == 0, f"Expected no schema fields, got: {schema_keys}"


# ---------------------------------------------------------------------------
# 3. Submitting local_yaml_ready aborts with "local_yaml_exported"
# ---------------------------------------------------------------------------


def test_local_yaml_ready_submit_aborts_with_correct_reason(tmp_path):
    """On submit (user_input={}), local_yaml_ready must abort with local_yaml_exported."""
    flow = _make_flow()

    with patch.object(
        flow,
        "_generate_and_save_local_yaml",
        new=AsyncMock(return_value=(str(tmp_path / "home_sph10k_01.yaml"), "sph10k_home_01")),
    ):
        result = _run(flow.async_step_local_yaml_ready(user_input={}))

    assert result["type"] == "abort"
    assert result["reason"] == "local_yaml_exported"


# ---------------------------------------------------------------------------
# 4. Generated YAML contains !secret placeholders
# ---------------------------------------------------------------------------


def test_generate_and_save_local_yaml_uses_secret_placeholders(tmp_path):
    """_generate_and_save_local_yaml must call generator with mac_suffix=None.

    mac_suffix=None instructs the generator to emit !secret placeholders instead
    of device-specific key names.
    """
    expected_yaml = (
        "api:\n"
        "  encryption:\n"
        "    key: !secret api_encryption_key\n"
        "ota:\n"
        "  - platform: esphome\n"
        "    password: !secret ota_password\n"
    )
    mod = _make_yaml_generator_stub(expected_yaml)

    flow = _make_flow()
    flow.hass.config.path.side_effect = lambda *parts: str(tmp_path / Path(*parts))

    _run(flow._generate_and_save_local_yaml())

    call_kwargs = mod.generate_device_yaml.call_args.kwargs
    # mac_suffix must be None so the generator emits !secret placeholders
    assert call_kwargs.get("mac_suffix") is None, "mac_suffix must be None for !secret placeholders"


def test_generated_yaml_content_has_secret_api_key_marker(tmp_path):
    """YAML content returned by the generator must include !secret api_encryption_key."""
    yaml_with_secrets = (
        "api:\n"
        "  encryption:\n"
        "    key: !secret api_encryption_key\n"
        "ota:\n"
        "  - platform: esphome\n"
        "    password: !secret ota_password\n"
    )
    _make_yaml_generator_stub(yaml_with_secrets)

    flow = _make_flow()
    flow.hass.config.path.side_effect = lambda *parts: str(tmp_path / Path(*parts))

    yaml_path, node_name = _run(flow._generate_and_save_local_yaml())

    # Verify the written file contains the secret markers
    written_file = Path(yaml_path)
    assert written_file.exists(), f"YAML file not written at {yaml_path}"
    content = written_file.read_text(encoding="utf-8")
    assert "!secret api_encryption_key" in content
    assert "!secret ota_password" in content


# ---------------------------------------------------------------------------
# 5. Generated YAML must NOT contain managed-service tokens
# ---------------------------------------------------------------------------


def test_generated_yaml_has_no_managed_service_tokens(tmp_path):
    """YAML must not contain COMPILE_SECRET_KEY, pva_, customer_id, or hpke tokens."""
    safe_yaml = (
        "api:\n"
        "  encryption:\n"
        "    key: !secret api_encryption_key\n"
        "ota:\n"
        "  - platform: esphome\n"
        "    password: !secret ota_password\n"
    )
    _make_yaml_generator_stub(safe_yaml)

    flow = _make_flow()
    flow.hass.config.path.side_effect = lambda *parts: str(tmp_path / Path(*parts))

    yaml_path, _ = _run(flow._generate_and_save_local_yaml())
    content = Path(yaml_path).read_text(encoding="utf-8")

    assert "COMPILE_SECRET_KEY" not in content
    assert "pva_" not in content
    assert "customer_id" not in content
    assert "hpke" not in content


# ---------------------------------------------------------------------------
# 6. File is written to correct path under hass.config.path()
# ---------------------------------------------------------------------------


def test_yaml_file_written_to_config_dir(tmp_path):
    """_generate_and_save_local_yaml must write YAML under pvautonomy/generated/."""
    _make_yaml_generator_stub("test: yaml")

    flow = _make_flow(site="home", number=1, model_slug="sph10k")
    flow.hass.config.path.side_effect = lambda *parts: str(tmp_path / Path(*parts))

    yaml_path, node_name = _run(flow._generate_and_save_local_yaml())

    expected_dir = tmp_path / "pvautonomy" / "generated"
    expected_file = expected_dir / f"{node_name}.yaml"
    assert Path(yaml_path) == expected_file
    assert expected_file.exists()


# ---------------------------------------------------------------------------
# 7. managed_build path unaffected — still routes to managed_key
# ---------------------------------------------------------------------------


def test_managed_build_still_routes_to_managed_key():
    """managed_build mode must route to managed_key, not local_yaml_ready."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_MANAGED)
    flow._adopt_mode = False

    with patch.object(flow, "async_step_managed_key", new=AsyncMock(
        return_value={"type": "form", "step_id": "managed_key"}
    )) as mock_managed:
        result = _run(flow.async_step_managed_build())

    mock_managed.assert_awaited_once()
    assert result["step_id"] == "managed_key"


# ---------------------------------------------------------------------------
# 8. adopt_direct path unaffected — goes straight to manufacturer
# ---------------------------------------------------------------------------


def test_adopt_direct_still_routes_to_manufacturer():
    """adopt_direct must route to manufacturer, not local_yaml_ready."""
    flow = _make_flow()
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""

    with patch.object(flow, "async_step_manufacturer", new=AsyncMock(
        return_value={"type": "form", "step_id": "manufacturer"}
    )) as mock_mfr:
        result = _run(flow.async_step_adopt_direct())

    mock_mfr.assert_awaited_once()
    assert result["step_id"] == "manufacturer"


# ---------------------------------------------------------------------------
# 9. No _preflight_compile_secret_key in local path
# ---------------------------------------------------------------------------


def test_local_yaml_ready_does_not_call_preflight(tmp_path):
    """local_yaml_ready must not invoke _preflight_compile_secret_key."""
    flow = _make_flow()
    flow._preflight_compile_secret_key = AsyncMock(return_value=True)

    with patch.object(
        flow,
        "_generate_and_save_local_yaml",
        new=AsyncMock(return_value=(str(tmp_path / "sph10k_home_01.yaml"), "sph10k_home_01")),
    ):
        _run(flow.async_step_local_yaml_ready())

    flow._preflight_compile_secret_key.assert_not_awaited()


def test_location_local_mode_does_not_call_preflight():
    """async_step_location in local mode must not call _preflight_compile_secret_key."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME, local_yaml_mode=True)
    flow._preflight_compile_secret_key = AsyncMock(return_value=True)
    _make_yaml_generator_stub("placeholder: true")

    with patch.object(flow, "async_step_local_yaml_ready", new=AsyncMock(
        return_value={"type": "form", "step_id": "local_yaml_ready"}
    )):
        _run(flow.async_step_location(user_input={
            "site_preset": "custom",
            "site": "home",
            "number": 1,
        }))

    flow._preflight_compile_secret_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# Integration: full local flow description_placeholders contains yaml_path
# ---------------------------------------------------------------------------


def test_local_yaml_ready_form_description_placeholders_contain_path(tmp_path):
    """local_yaml_ready form must include yaml_path in description_placeholders."""
    flow = _make_flow()
    expected_path = str(tmp_path / "pvautonomy" / "generated" / "sph10k_home_01.yaml")

    with patch.object(
        flow,
        "_generate_and_save_local_yaml",
        new=AsyncMock(return_value=(expected_path, "sph10k_home_01")),
    ):
        result = _run(flow.async_step_local_yaml_ready())

    placeholders = result.get("description_placeholders") or {}
    assert "yaml_path" in placeholders
    assert placeholders["yaml_path"] == expected_path
    assert "node_name" in placeholders


def test_local_yaml_ready_graceful_on_generation_failure(tmp_path):
    """If _generate_and_save_local_yaml raises, local_yaml_ready shows fallback path."""
    flow = _make_flow()

    with patch.object(flow, "_generate_and_save_local_yaml", new=AsyncMock(side_effect=RuntimeError("registry not found"))):
        result = _run(flow.async_step_local_yaml_ready())

    assert result["type"] == "form"
    placeholders = result.get("description_placeholders") or {}
    # Fallback placeholder must be set when generation fails
    assert placeholders.get("yaml_path") == "(generation failed — check logs)"


# ---------------------------------------------------------------------------
# fix/#136 — Adopt Direct location-step routing
# ---------------------------------------------------------------------------


def test_location_routes_to_local_yaml_ready_when_local_yaml_mode_true():
    """local_esphome path (_local_yaml_mode=True) must route location → local_yaml_ready.

    Regression guard: the fix must not break the Local ESPHome YAML export path.
    """
    flow = _make_flow(build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME, local_yaml_mode=True)
    _make_yaml_generator_stub()

    with patch.object(
        flow, "async_step_local_yaml_ready",
        new=AsyncMock(return_value={"type": "form", "step_id": "local_yaml_ready"})
    ) as mock_local:
        result = _run(flow.async_step_location(user_input={
            "site_preset": "custom",
            "site": "home",
            "number": 1,
        }))

    mock_local.assert_awaited_once()
    assert result["step_id"] == "local_yaml_ready"


def test_location_routes_to_target_device_when_local_yaml_mode_false():
    """adopt_direct path (_local_yaml_mode=False) must route location → target_device.

    This is the #136 fix: adopt_direct sets _local_yaml_mode=False so the
    location step goes to target_device instead of local_yaml_ready.
    """
    flow = _make_flow(
        build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME,
        local_yaml_mode=False,
        adopt_mode=True,
    )

    with patch.object(
        flow, "async_step_target_device",
        new=AsyncMock(return_value={"type": "form", "step_id": "target_device"})
    ) as mock_target:
        result = _run(flow.async_step_location(user_input={
            "site_preset": "custom",
            "site": "home",
            "number": 1,
        }))

    mock_target.assert_awaited_once()
    assert result["step_id"] == "target_device"


def test_adopt_direct_step_sets_local_yaml_mode_false():
    """async_step_adopt_direct must leave _local_yaml_mode=False (default).

    adopt_direct never sets _local_yaml_mode, so the routing uses False and
    routes location → target_device instead of local_yaml_ready.
    """
    from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
    from custom_components.pvautonomy_ops.const import BUILD_SERVICE_LOCAL_ESPHOME

    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)
    flow.hass = MagicMock()
    flow._adopt_mode = False
    flow._local_yaml_mode = False
    flow._build_service_mode = ""
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""

    with patch.object(
        flow, "async_step_manufacturer",
        new=AsyncMock(return_value={"type": "form", "step_id": "manufacturer"})
    ):
        _run(flow.async_step_adopt_direct())

    # adopt_direct must NOT set _local_yaml_mode=True
    assert flow._local_yaml_mode is False, "adopt_direct must not set _local_yaml_mode=True"
    assert flow._adopt_mode is True
    assert flow._build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME


def test_local_esphome_step_sets_local_yaml_mode_true():
    """async_step_local_esphome must set _local_yaml_mode=True.

    This ensures the YAML export routing fires when the user chose the
    Local ESPHome path (not Adopt Direct).
    """
    from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
    from custom_components.pvautonomy_ops.const import BUILD_SERVICE_LOCAL_ESPHOME

    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)
    flow.hass = MagicMock()
    flow._adopt_mode = False
    flow._local_yaml_mode = False
    flow._build_service_mode = ""
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""

    with patch.object(
        flow, "async_step_local_esphome_guide",
        new=AsyncMock(return_value={"type": "form", "step_id": "local_esphome_guide"})
    ):
        _run(flow.async_step_local_esphome())

    assert flow._local_yaml_mode is True, "local_esphome must set _local_yaml_mode=True"
    assert flow._adopt_mode is True
    assert flow._build_service_mode == BUILD_SERVICE_LOCAL_ESPHOME
