"""fix/#134 — Adopt Direct entity-surface validator.

Verifies:
 1. adopt_confirm GET shows form with empty missing_entities placeholder.
 2. adopt_confirm POST with complete entity surface creates entry.
 3. adopt_confirm POST with missing active_power_rate_device blocks adoption.
 4. adopt_confirm POST with missing battery_soc_device blocks adoption.
 5. Error key is missing_required_entities; error dict has base error.
 6. Missing-entity list appears in description_placeholders.
 7. Long missing-entity list is truncated to 5 + count suffix.
 8. Adopt Direct requires no Build-Key (proxy_api_key is empty on entry).
 9. managed_build path is unaffected (does not go through adopt_confirm).
10. local_esphome path is unaffected (aborts at local_yaml_exported).
11. advanced_proxy path is unaffected (goes to proxy step, not adopt_confirm).
12. disabled_by_default entities are not required (do not block adoption).
13. Validator skips validation when ha_device_id is absent (fail-open).
14. Validator skips when registry cannot be loaded (fail-open).
15. derive_required_entity_names returns standard-tier enabled entities.
16. derive_required_entity_names excludes disabled_by_default entities.
17. derive_required_entity_names excludes generator_skip entities.
18. derive_required_entity_names returns empty list on bad registry (fail-open).
19. Translation/error key exists in strings.json.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.pvautonomy_ops.config_flow import PVAutonomyOpsConfigFlow
from custom_components.pvautonomy_ops.const import (
    BUILD_SERVICE_LOCAL_ESPHOME,
    BUILD_SERVICE_MANAGED,
    TIER_STANDARD,
)


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
    ha_device_id: str = "test-ha-device-id",
    model_slug: str = "sph10k",
    site: str = "home",
    number: int = 1,
    registry_file: str = "growatt/sph/sph10k.json",
    build_service_mode: str = BUILD_SERVICE_LOCAL_ESPHOME,
) -> PVAutonomyOpsConfigFlow:
    """Return a PVAutonomyOpsConfigFlow stub ready for adopt_confirm tests."""
    flow = PVAutonomyOpsConfigFlow.__new__(PVAutonomyOpsConfigFlow)

    flow.hass = MagicMock()
    flow._build_task = None
    flow._build_result = None
    flow._flash_error = None
    flow._adopt_mode = True
    flow._build_service_mode = build_service_mode
    flow._proxy_base_url = ""
    flow._proxy_api_key = ""
    flow._proxy_customer_id = ""
    flow._manufacturer = "growatt"
    flow._model_slug = model_slug
    flow._site = site
    flow._number = number
    flow._registry_file = registry_file
    flow._ha_device_id = ha_device_id
    flow._mac_suffix = "aabbcc"
    flow._selected_tier = TIER_STANDARD
    flow._modbus_version = None
    flow._map_status = "unknown"
    flow._replacing_entry = None
    flow._device_id = ""
    flow._summary_title = ""
    flow._display_name = ""
    return flow


def _make_entity_entry(original_name: str, disabled_by=None):
    """Return a mock entity registry entry."""
    ent = MagicMock()
    ent.original_name = original_name
    ent.disabled_by = disabled_by
    return ent


def _patch_entity_registry(entries: list):
    """Context-manager pair: patch HA entity registry to return *entries*."""
    mock_reg = MagicMock()

    # We need to patch the er module as it is imported inside _validate_entity_surface.
    # The conftest stubs homeassistant.helpers.entity_registry as a _FakeHAModule;
    # we override the two functions our code calls.
    import homeassistant.helpers.entity_registry as er_mod

    er_mod.async_get = MagicMock(return_value=mock_reg)
    er_mod.async_entries_for_device = MagicMock(return_value=entries)
    return er_mod


def _sph10k_full_surface() -> list:
    """Return a minimal mock entity surface that satisfies the SPH10K standard contract.

    Uses the known required entity names from the bundled registry.
    """
    from custom_components.pvautonomy_ops.yaml_generator import derive_required_entity_names

    required = derive_required_entity_names("growatt/sph/sph10k.json", TIER_STANDARD)
    return [_make_entity_entry(name) for name in required]


# ---------------------------------------------------------------------------
# 1. GET form shows empty missing_entities placeholder
# ---------------------------------------------------------------------------


def test_adopt_confirm_get_shows_form_with_empty_missing_entities():
    """adopt_confirm GET must show form with missing_entities='' placeholder."""
    flow = _make_flow()
    result = _run(flow.async_step_adopt_confirm(user_input=None))

    assert result["type"] == "form"
    assert result["step_id"] == "adopt_confirm"
    placeholders = result.get("description_placeholders") or {}
    assert "missing_entities" in placeholders, "missing_entities placeholder must always be present"
    assert placeholders["missing_entities"] == "", "GET form must have empty missing_entities"
    assert not result.get("errors"), "GET form must have no errors"


# ---------------------------------------------------------------------------
# 2. POST with complete surface → entry created
# ---------------------------------------------------------------------------


def test_adopt_confirm_post_with_complete_surface_creates_entry():
    """adopt_confirm POST with full entity surface must create a config entry."""
    flow = _make_flow()
    _patch_entity_registry(_sph10k_full_surface())

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "create_entry", f"Expected create_entry, got {result}"
    options = result.get("options", {})
    init = options.get("_initial_device", {})
    assert init.get("_setup_state") == "adopted"
    assert init.get("ha_device_id") == "test-ha-device-id"


# ---------------------------------------------------------------------------
# 3. POST with missing active_power_rate_device → blocked
# ---------------------------------------------------------------------------


def test_adopt_confirm_blocks_when_active_power_rate_missing():
    """Adoption must be blocked when active_power_rate_device is absent."""
    flow = _make_flow()
    surface = [
        e for e in _sph10k_full_surface()
        if e.original_name != "active_power_rate_device"
    ]
    _patch_entity_registry(surface)

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "form", "Adoption must be blocked (form re-shown)"
    assert result["step_id"] == "adopt_confirm"
    errors = result.get("errors") or {}
    assert errors.get("base") == "missing_required_entities"
    placeholders = result.get("description_placeholders") or {}
    assert "active_power_rate_device" in placeholders.get("missing_entities", "")


# ---------------------------------------------------------------------------
# 4. POST with missing battery_soc_device → blocked
# ---------------------------------------------------------------------------


def test_adopt_confirm_blocks_when_battery_soc_missing():
    """Adoption must be blocked when battery_soc_device is absent."""
    flow = _make_flow()
    surface = [
        e for e in _sph10k_full_surface()
        if e.original_name != "battery_soc_device"
    ]
    _patch_entity_registry(surface)

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "form"
    errors = result.get("errors") or {}
    assert errors.get("base") == "missing_required_entities"
    placeholders = result.get("description_placeholders") or {}
    assert "battery_soc_device" in placeholders.get("missing_entities", "")


# ---------------------------------------------------------------------------
# 5. Error key is missing_required_entities
# ---------------------------------------------------------------------------


def test_adopt_confirm_error_key_is_missing_required_entities():
    """The error key must be exactly 'missing_required_entities'."""
    flow = _make_flow()
    _patch_entity_registry([])  # empty surface → all required entities missing

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "form"
    errors = result.get("errors") or {}
    assert "base" in errors
    assert errors["base"] == "missing_required_entities"


# ---------------------------------------------------------------------------
# 6. Missing entity list in description_placeholders
# ---------------------------------------------------------------------------


def test_adopt_confirm_error_includes_missing_entities_in_placeholders():
    """description_placeholders must include the missing entity names on error."""
    flow = _make_flow()
    _patch_entity_registry([])  # all required entities missing

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    placeholders = result.get("description_placeholders") or {}
    missing_str = placeholders.get("missing_entities", "")
    assert missing_str, "missing_entities placeholder must be non-empty on error"
    # Must include at least one known required entity name
    assert "device" in missing_str, "Missing entity names must contain '_device' suffix"


# ---------------------------------------------------------------------------
# 7. Long missing list truncated to 5 + count suffix
# ---------------------------------------------------------------------------


def test_adopt_confirm_truncates_long_missing_list():
    """When more than 5 entities are missing the placeholder is truncated."""
    flow = _make_flow()
    _patch_entity_registry([])  # all required entities missing

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    placeholders = result.get("description_placeholders") or {}
    missing_str = placeholders.get("missing_entities", "")
    # The SPH10K standard surface has many required entities; truncation fires.
    if "more)" in missing_str:
        # Truncated: must show "(+N more)"
        assert "(+" in missing_str
    # Count of commas in the entity portion should be <= 4 (i.e. max 5 shown)
    entity_part = missing_str.split("…")[0] if "…" in missing_str else missing_str
    entity_names = [e.strip() for e in entity_part.split(",") if e.strip()]
    assert len(entity_names) <= 5, "At most 5 entity names shown before truncation"


# ---------------------------------------------------------------------------
# 8. Adopt Direct requires no Build-Key
# ---------------------------------------------------------------------------


def test_adopt_direct_no_build_key_required():
    """Adopt Direct entry must have empty proxy_api_key."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME)
    _patch_entity_registry(_sph10k_full_surface())

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "create_entry"
    options = result.get("options", {})
    assert options.get("proxy_api_key", "") == "", "Adopt Direct must not require a Build-Key"


# ---------------------------------------------------------------------------
# 9. managed_build path is unaffected
# ---------------------------------------------------------------------------


def test_managed_build_not_affected_by_surface_validator():
    """managed_build path must not call adopt_confirm."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_MANAGED)
    flow._adopt_mode = False

    # managed_build routes to managed_key, not adopt_confirm
    with patch.object(
        flow, "async_step_managed_key",
        new=AsyncMock(return_value={"type": "form", "step_id": "managed_key"})
    ) as mock_managed:
        result = _run(flow.async_step_managed_build())

    mock_managed.assert_awaited_once()
    assert result["step_id"] == "managed_key"


# ---------------------------------------------------------------------------
# 10. local_esphome path is unaffected
# ---------------------------------------------------------------------------


def test_local_esphome_does_not_reach_adopt_confirm():
    """local_esphome path must abort at local_yaml_exported, not adopt_confirm."""
    flow = _make_flow(build_service_mode=BUILD_SERVICE_LOCAL_ESPHOME)

    with patch.object(
        flow, "_generate_and_save_local_yaml",
        new=AsyncMock(return_value=("/tmp/test.yaml", "sph10k_home_01")),
    ):
        result = _run(flow.async_step_local_yaml_ready(user_input={}))

    assert result["type"] == "abort"
    assert result["reason"] == "local_yaml_exported"


# ---------------------------------------------------------------------------
# 11. advanced_proxy path is unaffected
# ---------------------------------------------------------------------------


def test_advanced_proxy_routes_to_proxy_step_not_adopt_confirm():
    """advanced_proxy routes to proxy step, not adopt_confirm."""
    flow = _make_flow()
    flow._adopt_mode = False

    with patch.object(
        flow, "async_step_proxy",
        new=AsyncMock(return_value={"type": "form", "step_id": "proxy"})
    ) as mock_proxy:
        result = _run(flow.async_step_advanced_proxy())

    mock_proxy.assert_awaited_once()
    assert result["step_id"] == "proxy"


# ---------------------------------------------------------------------------
# 12. disabled_by_default entities are not required
# ---------------------------------------------------------------------------


def test_disabled_by_default_entities_not_required():
    """Entities with disabled_by set must not block adoption."""
    flow = _make_flow()
    # Build full surface but mark one required entity as HA-disabled
    full_surface = _sph10k_full_surface()
    for ent in full_surface:
        if ent.original_name == "pv3_voltage_device":
            ent.disabled_by = "integration"  # simulate disabled_by_default
    _patch_entity_registry(full_surface)

    # pv3_voltage_device has enabled_by_default=False in registry — not required.
    # The surface must still pass even if the entity is present but disabled.
    result = _run(flow.async_step_adopt_confirm(user_input={}))

    # Should pass (pv3_voltage_device is not required, so disabling it is fine)
    assert result["type"] == "create_entry", (
        f"Disabled non-required entity must not block adoption; got {result}"
    )


# ---------------------------------------------------------------------------
# 13. Validator skips when ha_device_id is absent (fail-open)
# ---------------------------------------------------------------------------


def test_surface_validator_skips_when_no_ha_device_id():
    """_validate_entity_surface must return empty list when ha_device_id is absent."""
    flow = _make_flow()
    flow._ha_device_id = ""  # absent

    missing = _run(flow._validate_entity_surface())

    assert missing == [], "Validator must skip (fail-open) when ha_device_id is absent"


# ---------------------------------------------------------------------------
# 14. Validator skips when registry cannot be loaded (fail-open)
# ---------------------------------------------------------------------------


def test_surface_validator_returns_none_on_bad_registry():
    """_validate_entity_surface must return None when registry load fails (fail-closed).

    None signals that the entity-surface contract could not be derived and that
    the caller (async_step_adopt_confirm) must block adoption rather than silently
    allowing an unvalidated entry.
    """
    flow = _make_flow(registry_file="nonexistent/bad/path.json")
    _patch_entity_registry([])

    missing = _run(flow._validate_entity_surface())

    assert missing is None, (
        "Validator must return None (fail-closed) when registry cannot be loaded"
    )


# ---------------------------------------------------------------------------
# 15. derive_required_entity_names returns standard-tier enabled entities
# ---------------------------------------------------------------------------


def test_derive_required_entity_names_standard_entities():
    """derive_required_entity_names must include core standard-tier enabled entities."""
    from custom_components.pvautonomy_ops.yaml_generator import derive_required_entity_names

    required = derive_required_entity_names("growatt/sph/sph10k.json", TIER_STANDARD)

    assert "active_power_rate_device" in required
    assert "battery_soc_device" in required
    assert "grid_power_device" in required
    assert "pv_total_power_device" in required
    assert "local_load_power_device" in required
    assert "inverter_status_device" in required
    assert "ac_power_total_device" in required
    assert "modbus_version_device" in required


# ---------------------------------------------------------------------------
# 16. derive_required_entity_names excludes disabled_by_default entities
# ---------------------------------------------------------------------------


def test_derive_required_entity_names_excludes_disabled_by_default():
    """derive_required_entity_names must not include enabled_by_default=False entities."""
    from custom_components.pvautonomy_ops.yaml_generator import derive_required_entity_names

    required = derive_required_entity_names("growatt/sph/sph10k.json", TIER_STANDARD)

    # These are standard-tier but enabled_by_default=False in the SPH10K registry
    assert "pv3_voltage_device" not in required
    assert "pv3_current_device" not in required
    assert "pv3_power_device" not in required
    assert "battery_first_stop_soc_alias_device" not in required


# ---------------------------------------------------------------------------
# 17. derive_required_entity_names excludes generator_skip entities
# ---------------------------------------------------------------------------


def test_derive_required_entity_names_excludes_generator_skip():
    """derive_required_entity_names must exclude entities with generator_skip=True."""
    from custom_components.pvautonomy_ops.yaml_generator import derive_required_entity_names

    stub_registry = {
        "registers": {
            "sensors": [
                {
                    "id": "normal_sensor",
                    "address": 1,
                    "tier": "standard",
                    "enabled_by_default": True,
                },
                {
                    "id": "skip_sensor",
                    "address": 2,
                    "tier": "standard",
                    "enabled_by_default": True,
                    "generator_skip": True,
                },
            ],
            "numbers": [],
        }
    }
    # Patch _load_registry in the real function's own globals dict so it works
    # even when sys.modules['yaml_generator'] has been replaced by a test stub.
    with patch.dict(
        derive_required_entity_names.__globals__,
        {"_load_registry": MagicMock(return_value=stub_registry)},
    ):
        result = derive_required_entity_names("any.json", TIER_STANDARD)

    assert "normal_sensor_device" in result
    assert "skip_sensor_device" not in result, "generator_skip entity must be excluded"


# ---------------------------------------------------------------------------
# 18. derive_required_entity_names returns empty list on bad registry
# ---------------------------------------------------------------------------


def test_derive_required_entity_names_returns_none_on_load_failure():
    """derive_required_entity_names must return None when registry load fails.

    None (not []) signals a load failure so callers can distinguish "registry
    loaded but empty" from "registry could not be loaded at all".  Callers
    must treat None as a hard failure, not as a pass-through.
    """
    from custom_components.pvautonomy_ops.yaml_generator import derive_required_entity_names

    result = derive_required_entity_names("nonexistent/bad.json", TIER_STANDARD)

    assert result is None, (
        "derive_required_entity_names must return None (not []) on registry load failure"
    )


# ---------------------------------------------------------------------------
# 19. Translation/error keys exist in strings.json
# ---------------------------------------------------------------------------


def test_strings_json_has_missing_required_entities_key():
    """strings.json must define the missing_required_entities error key."""
    strings_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components" / "pvautonomy_ops" / "strings.json"
    )
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    errors = strings.get("config", {}).get("error", {})
    assert "missing_required_entities" in errors, (
        "strings.json config.error must include 'missing_required_entities'"
    )
    assert errors["missing_required_entities"], "Error message must not be empty"


def test_strings_json_has_entity_surface_validation_failed_key():
    """strings.json must define the entity_surface_validation_failed error key."""
    strings_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components" / "pvautonomy_ops" / "strings.json"
    )
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    errors = strings.get("config", {}).get("error", {})
    assert "entity_surface_validation_failed" in errors, (
        "strings.json config.error must include 'entity_surface_validation_failed'"
    )
    assert errors["entity_surface_validation_failed"], "Error message must not be empty"


# ---------------------------------------------------------------------------
# 20. adopt_confirm blocks with entity_surface_validation_failed when
#     validator returns None (broken installation / registry unavailable)
# ---------------------------------------------------------------------------


def test_adopt_confirm_blocks_with_validation_failed_error_when_validator_returns_none():
    """adopt_confirm POST with None from validator must show entity_surface_validation_failed.

    None from _validate_entity_surface means the entity-surface contract could
    not be derived (e.g. bundled registry missing).  Adoption must be blocked
    with the specific error key, not silently allowed.
    """
    from unittest.mock import AsyncMock as _AsyncMock

    flow = _make_flow()
    flow._validate_entity_surface = _AsyncMock(return_value=None)

    result = _run(flow.async_step_adopt_confirm(user_input={}))

    assert result["type"] == "form", "Adoption must be blocked when validator returns None"
    assert result["step_id"] == "adopt_confirm"
    errors = result.get("errors") or {}
    assert errors.get("base") == "entity_surface_validation_failed", (
        "Error key must be entity_surface_validation_failed when validator returns None"
    )
    placeholders = result.get("description_placeholders") or {}
    assert "missing_entities" in placeholders
    assert placeholders["missing_entities"] == "", "missing_entities must be empty for this error"
