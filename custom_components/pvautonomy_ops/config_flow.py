"""Config and Options Flow for PVAutonomy Ops (P3-6-001).

Provides UI-based setup and options editing for the integration.
No YAML editing required after initial migration.

Contract: ops-contract-v1.md (v1.0.0) — no new entities.
Directive: D-ADDON-002, P3-6-001
"""
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ============================================================================
# Default values (aligned with current production)
# ============================================================================
DEFAULT_NAME = "PVAutonomy Ops"
DEFAULT_POLL_INTERVAL = 60

DEFAULT_ARTIFACT_CHANNEL = "stable"
DEFAULT_ARTIFACT_HW_FAMILY = "edge101"
DEFAULT_ARTIFACT_OWNER = "PVAutonomy"
DEFAULT_ARTIFACT_REPO = "pvautonomy-firmware"
DEFAULT_FLASH_MIN_SIZE_KB = 300
DEFAULT_GATES_FRESHNESS_MIN = 10
DEFAULT_STRICT_GATES = True

# Option keys
CONF_POLL_INTERVAL = "poll_interval_sec"
CONF_ARTIFACT_CHANNEL = "artifact_channel"
CONF_ARTIFACT_HW_FAMILY = "artifact_hw_family_default"
CONF_ARTIFACT_OWNER = "artifact_owner"
CONF_ARTIFACT_REPO = "artifact_repo"
CONF_FLASH_MIN_SIZE_KB = "flash_min_firmware_size_kb"
CONF_GATES_FRESHNESS_MIN = "gates_freshness_minutes"
CONF_STRICT_GATES = "strict_gates_required"


class PVAutonomyOpsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PVAutonomy Ops."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step (UI)."""
        # Only allow one instance
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", DEFAULT_NAME),
                data={},
                options={
                    CONF_POLL_INTERVAL: user_input.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional("name", default=DEFAULT_NAME): str,
                    vol.Optional(
                        CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL
                    ): vol.All(int, vol.Range(min=10, max=300)),
                }
            ),
        )

    async def async_step_import(
        self, import_config: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle YAML import (one-time migration).

        Called when `pvautonomy_ops:` is present in configuration.yaml.
        Creates a config entry so the integration works via ConfigEntry lifecycle.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        _LOGGER.info(
            "Importing PVAutonomy Ops from YAML configuration (one-time migration)"
        )

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={},
            options={
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "PVAutonomyOpsOptionsFlow":
        """Get the options flow handler."""
        return PVAutonomyOpsOptionsFlow(config_entry)


class PVAutonomyOpsOptionsFlow(config_entries.OptionsFlow):
    """Handle options for PVAutonomy Ops."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POLL_INTERVAL,
                        default=options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_ARTIFACT_CHANNEL,
                        default=options.get(
                            CONF_ARTIFACT_CHANNEL, DEFAULT_ARTIFACT_CHANNEL
                        ),
                    ): vol.In(["stable", "beta"]),
                    vol.Optional(
                        CONF_ARTIFACT_HW_FAMILY,
                        default=options.get(
                            CONF_ARTIFACT_HW_FAMILY, DEFAULT_ARTIFACT_HW_FAMILY
                        ),
                    ): str,
                    vol.Optional(
                        CONF_ARTIFACT_OWNER,
                        default=options.get(
                            CONF_ARTIFACT_OWNER, DEFAULT_ARTIFACT_OWNER
                        ),
                    ): str,
                    vol.Optional(
                        CONF_ARTIFACT_REPO,
                        default=options.get(
                            CONF_ARTIFACT_REPO, DEFAULT_ARTIFACT_REPO
                        ),
                    ): str,
                    vol.Optional(
                        CONF_FLASH_MIN_SIZE_KB,
                        default=options.get(
                            CONF_FLASH_MIN_SIZE_KB, DEFAULT_FLASH_MIN_SIZE_KB
                        ),
                    ): vol.All(int, vol.Range(min=50, max=5000)),
                    vol.Optional(
                        CONF_GATES_FRESHNESS_MIN,
                        default=options.get(
                            CONF_GATES_FRESHNESS_MIN, DEFAULT_GATES_FRESHNESS_MIN
                        ),
                    ): vol.All(int, vol.Range(min=1, max=60)),
                    vol.Optional(
                        CONF_STRICT_GATES,
                        default=options.get(
                            CONF_STRICT_GATES, DEFAULT_STRICT_GATES
                        ),
                    ): bool,
                }
            ),
        )
