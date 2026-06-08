"""Post-Install UX Stepper — unified wizard state machine (P3-13-001).

Manages three lifecycle flows through a single stepper interface:
  A) Initial Setup:  Factory → Production (managed pull)
  B) Re-configure:   Production → Production (push OTA, identity switch)
  C) Factory Reset:  Production → Factory (push OTA)

Each flow has ordered stages.  The stepper exposes its state via a
dedicated HA sensor and accepts commands via HA services.

Ref: WORKER-PROMPT-P3-13-001, Sections 1–5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WizardFlow(str, Enum):
    """Which lifecycle transition is active."""

    NONE = "none"
    INITIAL_SETUP = "initial_setup"
    RECONFIGURE = "reconfigure"
    FACTORY_RESET = "factory_reset"


class WizardStage(str, Enum):
    """All possible stepper stages across all flows."""

    # Common
    IDLE = "idle"
    ERROR = "error"

    # ── Initial Setup (Factory → Production) ──────────────────────────
    SETUP_SELECT_DEVICE = "setup_select_device"
    SETUP_PREFLIGHT = "setup_preflight"
    SETUP_CONFIGURE = "setup_configure"
    SETUP_BUILD = "setup_build"
    SETUP_INSTALL = "setup_install"
    SETUP_REBOOTING = "setup_rebooting"
    SETUP_RECONNECT = "setup_reconnect"
    SETUP_SAVE_KEY = "setup_save_key"
    SETUP_VERIFY = "setup_verify"
    SETUP_COMPLETE = "setup_complete"

    # ── Re-configure (Production → Production) ───────────────────────
    RECONF_SELECT_DEVICE = "reconf_select_device"
    RECONF_CONFIRM = "reconf_confirm"
    RECONF_CONFIGURE = "reconf_configure"
    RECONF_BUILD = "reconf_build"
    RECONF_PUSH_OTA = "reconf_push_ota"
    RECONF_IDENTITY_SWITCH = "reconf_identity_switch"
    RECONF_READOPT = "reconf_readopt"
    RECONF_ENTITY_RESET = "reconf_entity_reset"
    RECONF_VERIFY = "reconf_verify"
    RECONF_COMPLETE = "reconf_complete"

    # ── Factory Reset (Production → Factory) ─────────────────────────
    RESET_SELECT_DEVICE = "reset_select_device"
    RESET_CONFIRM = "reset_confirm"
    RESET_PUSH_OTA = "reset_push_ota"
    RESET_REBOOTING = "reset_rebooting"
    RESET_CLEANUP = "reset_cleanup"
    RESET_COMPLETE = "reset_complete"


# Stage ordering per flow (for progress calculation & "next" logic)
_SETUP_STAGES = [
    WizardStage.SETUP_SELECT_DEVICE,
    WizardStage.SETUP_PREFLIGHT,
    WizardStage.SETUP_CONFIGURE,
    WizardStage.SETUP_BUILD,
    WizardStage.SETUP_INSTALL,
    WizardStage.SETUP_REBOOTING,
    WizardStage.SETUP_RECONNECT,
    WizardStage.SETUP_SAVE_KEY,
    WizardStage.SETUP_VERIFY,
    WizardStage.SETUP_COMPLETE,
]

_RECONF_STAGES = [
    WizardStage.RECONF_SELECT_DEVICE,
    WizardStage.RECONF_CONFIRM,
    WizardStage.RECONF_CONFIGURE,
    WizardStage.RECONF_BUILD,
    WizardStage.RECONF_PUSH_OTA,
    WizardStage.RECONF_IDENTITY_SWITCH,
    WizardStage.RECONF_READOPT,
    WizardStage.RECONF_ENTITY_RESET,
    WizardStage.RECONF_VERIFY,
    WizardStage.RECONF_COMPLETE,
]

_RESET_STAGES = [
    WizardStage.RESET_SELECT_DEVICE,
    WizardStage.RESET_CONFIRM,
    WizardStage.RESET_PUSH_OTA,
    WizardStage.RESET_REBOOTING,
    WizardStage.RESET_CLEANUP,
    WizardStage.RESET_COMPLETE,
]

FLOW_STAGES: dict[WizardFlow, list[WizardStage]] = {
    WizardFlow.INITIAL_SETUP: _SETUP_STAGES,
    WizardFlow.RECONFIGURE: _RECONF_STAGES,
    WizardFlow.FACTORY_RESET: _RESET_STAGES,
}


# ---------------------------------------------------------------------------
# Stage metadata — human-readable labels + actionable messages
# ---------------------------------------------------------------------------

@dataclass
class StageInfo:
    """UI metadata for a wizard stage."""

    label: str
    description: str
    is_manual: bool = False       # Requires user action to advance
    is_terminal: bool = False     # Wizard stops here (success or error)


STAGE_INFO: dict[WizardStage, StageInfo] = {
    WizardStage.IDLE: StageInfo("Ready", "No wizard active. Start a setup, re-configure, or factory reset."),

    # Initial Setup
    WizardStage.SETUP_SELECT_DEVICE: StageInfo(
        "Select Factory Device",
        "Choose the Factory device to set up.",
        is_manual=True,
    ),
    WizardStage.SETUP_PREFLIGHT: StageInfo(
        "Pre-Flight Checks",
        "Running quality gates and verifying device readiness…",
    ),
    WizardStage.SETUP_CONFIGURE: StageInfo(
        "Configure Device",
        "Set inverter model, location, and device number.",
        is_manual=True,
    ),
    WizardStage.SETUP_BUILD: StageInfo(
        "Building Firmware",
        "Generating device-specific YAML and compiling firmware…",
    ),
    WizardStage.SETUP_INSTALL: StageInfo(
        "Installing Firmware",
        "Device is downloading and installing production firmware…",
    ),
    WizardStage.SETUP_REBOOTING: StageInfo(
        "Rebooting",
        "Device is rebooting — offline is expected. Please wait…",
    ),
    WizardStage.SETUP_RECONNECT: StageInfo(
        "Reconnecting",
        "Searching for the new Production device…",
    ),
    WizardStage.SETUP_SAVE_KEY: StageInfo(
        "Save Encryption Key",
        "Copy and save the API encryption key. You will need it to manage this device.",
        is_manual=True,
    ),
    WizardStage.SETUP_VERIFY: StageInfo(
        "Verifying",
        "Running sanity checks — confirming sensors respond…",
    ),
    WizardStage.SETUP_COMPLETE: StageInfo(
        "Setup Complete",
        "Device is configured and operational. All sanity checks passed.",
        is_terminal=True,
    ),

    # Re-configure
    WizardStage.RECONF_SELECT_DEVICE: StageInfo(
        "Select Device",
        "Choose the Production device to re-configure.",
        is_manual=True,
    ),
    WizardStage.RECONF_CONFIRM: StageInfo(
        "Confirm Changes",
        "Review current vs. new configuration. This will replace the existing firmware.",
        is_manual=True,
    ),
    WizardStage.RECONF_CONFIGURE: StageInfo(
        "Configure New Settings",
        "Set the new inverter model, location, and device number.",
        is_manual=True,
    ),
    WizardStage.RECONF_BUILD: StageInfo(
        "Building New Firmware",
        "Compiling firmware with updated configuration…",
    ),
    WizardStage.RECONF_PUSH_OTA: StageInfo(
        "Pushing OTA Update",
        "Uploading new firmware to the device via OTA…",
    ),
    WizardStage.RECONF_IDENTITY_SWITCH: StageInfo(
        "Identity Switch",
        "Old device going offline, waiting for new identity to appear…",
    ),
    WizardStage.RECONF_READOPT: StageInfo(
        "Re-Adopt Device",
        "New device found. Confirming connection…",
    ),
    WizardStage.RECONF_ENTITY_RESET: StageInfo(
        "Entity Reset",
        "Resetting entity registry for correct naming. Device-ID stays stable.",
    ),
    WizardStage.RECONF_VERIFY: StageInfo(
        "Verifying",
        "Running sanity checks on re-configured device…",
    ),
    WizardStage.RECONF_COMPLETE: StageInfo(
        "Re-Configure Complete",
        "Device re-configured successfully. Old entries cleaned up.",
        is_terminal=True,
    ),

    # Factory Reset
    WizardStage.RESET_SELECT_DEVICE: StageInfo(
        "Select Device",
        "Choose the Production device to factory-reset.",
        is_manual=True,
    ),
    WizardStage.RESET_CONFIRM: StageInfo(
        "Confirm Reset",
        "All device-specific configuration will be erased. This cannot be undone.",
        is_manual=True,
    ),
    WizardStage.RESET_PUSH_OTA: StageInfo(
        "Pushing Factory Firmware",
        "Uploading factory firmware via OTA…",
    ),
    WizardStage.RESET_REBOOTING: StageInfo(
        "Rebooting to Factory",
        "Device is rebooting into factory mode — offline is expected…",
    ),
    WizardStage.RESET_CLEANUP: StageInfo(
        "Cleaning Up",
        "Removing old Production entries and orphaned entities…",
    ),
    WizardStage.RESET_COMPLETE: StageInfo(
        "Factory Reset Complete",
        "Device reset to factory state. Run Initial Setup to configure again.",
        is_terminal=True,
    ),

    # Error (any flow)
    WizardStage.ERROR: StageInfo(
        "Error",
        "An error occurred. See details below.",
        is_terminal=True,
    ),
}


# ---------------------------------------------------------------------------
# Wizard State
# ---------------------------------------------------------------------------

@dataclass
class WizardState:
    """Complete runtime state of the wizard."""

    flow: WizardFlow = WizardFlow.NONE
    stage: WizardStage = WizardStage.IDLE
    progress: int = 0               # 0–100
    message: str = ""               # Most recent actionable message
    error: str = ""                 # Error message (empty if none)
    started_at: float = 0.0         # time.time()
    finished_at: float = 0.0

    # Flow-specific context
    device_id: str = ""             # Current/selected device (display name)
    ha_device_id: str = ""          # HA Device Registry UUID (resolved from display name)
    new_device_id: str = ""         # Target device (re-configure)
    node_name: str = ""             # Current ESPHome node name
    new_node_name: str = ""         # Target node name
    encryption_key: str = ""        # API encryption key (internal; redacted in to_dict)
    key_confirmed: bool = False     # User confirmed key saved
    log_lines: list[str] = field(default_factory=list)  # Last N log lines

    # Sanity check results
    sanity_passed: bool = False
    sanity_results: dict[str, Any] = field(default_factory=dict)

    # Orphaned entities (for cleanup display)
    orphaned_entities: list[str] = field(default_factory=list)

    # Build backend info (WP1: TEST MODE indicator)
    build_backend: str = ""         # "simulated"|"builder_addon"|etc.
    is_simulated: bool = False      # True when using SimulatedBuildBackend

    def to_dict(self) -> dict[str, Any]:
        """Export as attribute dict for HA sensor."""
        info = STAGE_INFO.get(self.stage, STAGE_INFO[WizardStage.IDLE])
        stages = FLOW_STAGES.get(self.flow, [])
        total = len(stages)
        current_idx = stages.index(self.stage) + 1 if self.stage in stages else 0

        return {
            "wizard_flow": self.flow.value,
            "wizard_stage": self.stage.value,
            "wizard_stage_label": info.label,
            "wizard_stage_description": info.description,
            "wizard_is_manual": info.is_manual,
            "wizard_is_terminal": info.is_terminal,
            "wizard_progress": self.progress,
            "wizard_step_current": current_idx,
            "wizard_step_total": total,
            "wizard_message": self.message,
            "wizard_error": self.error,
            "wizard_device_id": self.device_id,
            "wizard_ha_device_id": self.ha_device_id,
            "wizard_new_device_id": self.new_device_id,
            "wizard_node_name": self.node_name,
            "wizard_new_node_name": self.new_node_name,
            "wizard_encryption_key": "",
            "wizard_key_confirmed": self.key_confirmed,
            "wizard_sanity_passed": self.sanity_passed,
            "wizard_sanity_results": self.sanity_results,
            "wizard_orphaned_entities": self.orphaned_entities,
            "wizard_log": self.log_lines[-30:],  # Cap at 30 lines
            "wizard_started_at": self.started_at,
            "wizard_duration_s": round(
                (self.finished_at or time.time()) - self.started_at, 1
            ) if self.started_at else 0,
            "wizard_build_backend": self.build_backend,
            "wizard_is_simulated": self.is_simulated,
        }


# ---------------------------------------------------------------------------
# Wizard Engine
# ---------------------------------------------------------------------------

MAX_LOG_LINES = 50


class WizardEngine:
    """Stepper state machine for post-install UX.

    The engine manages transitions and delegates actual work to the
    existing modules (factory_installer, lifecycle, pipeline, etc.).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str | None = None) -> None:
        self._hass = hass
        self._entry_id = entry_id  # EPIC-015 P1-05: entry-scoped runtime lookup
        self._state = WizardState()
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []

    # ── Properties ────────────────────────────────────────────────────

    @property
    def state(self) -> WizardState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state.flow != WizardFlow.NONE and not STAGE_INFO.get(
            self._state.stage, STAGE_INFO[WizardStage.IDLE]
        ).is_terminal

    # ── Listener management ───────────────────────────────────────────

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change listener. Returns unsubscribe callable."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def _notify(self) -> None:
        """Notify all listeners of state change."""
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                _LOGGER.exception("Wizard listener error")

    # ── Stage transitions ─────────────────────────────────────────────

    def _set_stage(
        self,
        stage: WizardStage,
        *,
        message: str = "",
        progress: int | None = None,
    ) -> None:
        """Update current stage and compute progress."""
        self._state.stage = stage
        if message:
            self._state.message = message
            self._log(message)

        if progress is not None:
            self._state.progress = progress
        else:
            # Auto-compute from stage position
            stages = FLOW_STAGES.get(self._state.flow, [])
            if stages and stage in stages:
                idx = stages.index(stage)
                self._state.progress = int((idx / max(len(stages) - 1, 1)) * 100)
            elif stage == WizardStage.ERROR:
                pass  # Keep previous progress

        # Fire HA event for Lovelace reactivity (thread-safe for HA 2026.2+)
        # EPIC-015 P3-05: include entry_id for multi-entry scoping
        event_data = {
            "entry_id": self._entry_id,
            "flow": self._state.flow.value,
            "stage": stage.value,
            "progress": self._state.progress,
            "message": self._state.message,
        }
        try:
            asyncio.get_running_loop()
            # We're in the event loop — safe to call async_fire directly
            self._hass.bus.async_fire(f"{DOMAIN}_wizard_stage", event_data)
        except RuntimeError:
            # Called from a non-event-loop thread — schedule safely
            self._hass.loop.call_soon_threadsafe(
                self._hass.bus.async_fire, f"{DOMAIN}_wizard_stage", event_data
            )
        self._notify()

    def _set_error(self, error: str, *, recovery: str = "") -> None:
        """Transition to ERROR stage."""
        self._state.error = error
        full_msg = f"Error: {error}"
        if recovery:
            full_msg += f" — {recovery}"
        self._state.finished_at = time.time()
        self._set_stage(WizardStage.ERROR, message=full_msg)

    def _log(self, msg: str) -> None:
        """Append to the wizard message log (capped)."""
        self._state.log_lines.append(msg)
        if len(self._state.log_lines) > MAX_LOG_LINES:
            self._state.log_lines = self._state.log_lines[-MAX_LOG_LINES:]

    # ── Flow Starters ─────────────────────────────────────────────────

    def _read_build_backend_config(self) -> tuple[str, bool]:
        """Read build_backend + is_simulated from integration config.

        Returns:
            (build_backend_name, is_simulated) tuple.
        """
        from .const import BUILD_BACKEND_SIMULATED
        from . import get_integration_data
        # EPIC-015 P1-05: entry-scoped config lookup
        config = get_integration_data(self._hass, self._entry_id).get("config", {})
        backend = config.get("build_backend", BUILD_BACKEND_SIMULATED)
        return backend, backend == BUILD_BACKEND_SIMULATED

    async def start_initial_setup(
        self,
        *,
        device_id: str = "",
    ) -> bool:
        """Start Factory → Production wizard."""
        if self.is_active:
            _LOGGER.warning("Wizard already active (flow=%s)", self._state.flow)
            return False

        bb, sim = self._read_build_backend_config()
        self._state = WizardState(
            flow=WizardFlow.INITIAL_SETUP,
            started_at=time.time(),
            device_id=device_id,
            build_backend=bb,
            is_simulated=sim,
        )
        self._set_stage(
            WizardStage.SETUP_SELECT_DEVICE,
            message="Select a Factory device to begin initial setup.",
        )
        if sim:
            self._log("⚠️ TEST MODE — simulated build backend active. No real firmware will be compiled.")
        _LOGGER.info("Wizard started: Initial Setup (device=%s, backend=%s)", device_id, bb)
        return True

    async def start_reconfigure(
        self,
        *,
        device_id: str = "",
    ) -> bool:
        """Start Production → Production re-configure wizard."""
        if self.is_active:
            _LOGGER.warning("Wizard already active (flow=%s)", self._state.flow)
            return False

        bb, sim = self._read_build_backend_config()
        self._state = WizardState(
            flow=WizardFlow.RECONFIGURE,
            started_at=time.time(),
            device_id=device_id,
            build_backend=bb,
            is_simulated=sim,
        )
        self._set_stage(
            WizardStage.RECONF_SELECT_DEVICE,
            message="Select a Production device to re-configure.",
        )
        if sim:
            self._log("⚠️ TEST MODE — simulated build backend active. No real firmware will be compiled.")
        _LOGGER.info("Wizard started: Re-configure (device=%s, backend=%s)", device_id, bb)
        return True

    async def start_factory_reset(
        self,
        *,
        device_id: str = "",
    ) -> bool:
        """Start Production → Factory reset wizard."""
        if self.is_active:
            _LOGGER.warning("Wizard already active (flow=%s)", self._state.flow)
            return False

        bb, sim = self._read_build_backend_config()
        self._state = WizardState(
            flow=WizardFlow.FACTORY_RESET,
            started_at=time.time(),
            device_id=device_id,
            build_backend=bb,
            is_simulated=sim,
        )
        self._set_stage(
            WizardStage.RESET_SELECT_DEVICE,
            message="Select a Production device to factory-reset.",
        )
        _LOGGER.info("Wizard started: Factory Reset (device=%s, backend=%s)", device_id, bb)
        return True

    # ── User Actions (advance manual stages) ──────────────────────────

    async def advance(self, *, context: dict[str, Any] | None = None) -> bool:
        """Advance the wizard to the next stage.

        For manual stages, this is called when the user clicks "Next"
        or "Confirm". For async stages, the background task auto-advances.
        The *context* dict carries user-supplied data (device selection,
        config params, key confirmation, etc.).
        """
        ctx = context or {}
        st = self._state
        stage = st.stage
        flow = st.flow

        if flow == WizardFlow.NONE or STAGE_INFO.get(stage, STAGE_INFO[WizardStage.IDLE]).is_terminal:
            _LOGGER.debug("advance() called on idle/terminal stage — ignored")
            return False

        # ── Initial Setup advances ────────────────────────────────────
        if flow == WizardFlow.INITIAL_SETUP:
            return await self._advance_initial_setup(stage, ctx)

        # ── Re-configure advances ─────────────────────────────────────
        if flow == WizardFlow.RECONFIGURE:
            return await self._advance_reconfigure(stage, ctx)

        # ── Factory Reset advances ────────────────────────────────────
        if flow == WizardFlow.FACTORY_RESET:
            return await self._advance_factory_reset(stage, ctx)

        return False

    async def abort(self) -> bool:
        """Abort the active wizard (works from any non-idle state, including error)."""
        if self._state.flow == WizardFlow.NONE:
            return False

        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

        _LOGGER.info("Wizard aborted (flow=%s, stage=%s)", self._state.flow, self._state.stage)
        self._state.finished_at = time.time()
        self._set_stage(WizardStage.IDLE, message="Wizard aborted by user.")
        self._state.flow = WizardFlow.NONE
        self._notify()
        return True

    async def confirm_key_saved(self) -> bool:
        """User confirms the encryption key has been saved."""
        if self._state.stage != WizardStage.SETUP_SAVE_KEY:
            _LOGGER.warning("confirm_key_saved called in wrong stage: %s", self._state.stage)
            return False

        self._state.key_confirmed = True
        self._log("Encryption key confirmed as saved.")
        # Auto-advance to verification
        self._set_stage(
            WizardStage.SETUP_VERIFY,
            message="Key confirmed. Running sanity checks…",
        )
        # Start sanity check in background
        self._task = self._hass.async_create_task(
            self._run_sanity_checks()
        )
        return True

    # ── Flow-specific advance logic ───────────────────────────────────

    async def _advance_initial_setup(
        self, stage: WizardStage, ctx: dict[str, Any]
    ) -> bool:
        """Advance Initial Setup flow."""
        if stage == WizardStage.SETUP_SELECT_DEVICE:
            device_id = ctx.get("device_id", "")
            device_kind = ctx.get("device_kind", "")

            if not device_id:
                self._log("No device selected.")
                return False

            # Auto-resolve device_kind from InputReader when not provided
            if not device_kind:
                from . import get_integration_data
                # EPIC-015 P1-05: entry-scoped lookup
                domain_data = get_integration_data(self._hass, self._entry_id)
                input_reader = domain_data.get("input_reader")
                if input_reader:
                    device_kind = await input_reader.get_selected_device_kind(
                        device_name=device_id
                    ) or ""

            # Guard: Initial Setup is Factory → Production only.
            # Production devices must use Reconfigure flow.
            if device_kind == "production":
                self._set_error(
                    f"'{device_id}' is already a Production device. "
                    "Initial Setup is for Factory devices only.",
                    recovery="Abort this wizard and use Re-configure instead.",
                )
                return True
            self._state.device_id = device_id
            self._set_stage(
                WizardStage.SETUP_PREFLIGHT,
                message=f"Running pre-flight checks for {device_id}…",
            )
            self._task = self._hass.async_create_task(
                self._run_preflight()
            )
            return True

        if stage == WizardStage.SETUP_PREFLIGHT:
            # Auto-advanced by background task
            return False

        if stage == WizardStage.SETUP_CONFIGURE:
            # User provided config params
            model = ctx.get("model", "")
            site = ctx.get("site", "")
            number = ctx.get("number", "")
            registry_file = ctx.get("registry_file", "")
            mac_suffix = ctx.get("mac_suffix", "")
            encryption_key = ctx.get("encryption_key", "")

            if not all([model, site, number, registry_file]):
                self._log("Missing configuration parameters.")
                return False

            # Compute identities
            from .device_id import compute_device_id, compute_node_name
            self._state.new_device_id = compute_device_id(model, site, number)
            self._state.new_node_name = compute_node_name(model, site, number)
            self._state.encryption_key = encryption_key

            self._set_stage(
                WizardStage.SETUP_BUILD,
                message=f"Building firmware for {self._state.new_node_name}…",
            )
            self._task = self._hass.async_create_task(
                self._run_initial_setup_build(model, site, number, registry_file, mac_suffix)
            )
            return True

        if stage == WizardStage.SETUP_SAVE_KEY:
            # Must call confirm_key_saved() instead
            self._log("Please confirm you saved the encryption key.")
            return False

        return False

    async def _advance_reconfigure(
        self, stage: WizardStage, ctx: dict[str, Any]
    ) -> bool:
        """Advance Re-configure flow."""
        if stage == WizardStage.RECONF_SELECT_DEVICE:
            device_id = ctx.get("device_id", "")
            if not device_id:
                self._log("No device selected.")
                return False
            self._state.device_id = device_id

            # Resolve HA Device Registry UUID from display name
            from .flash_uploader import resolve_ha_device_id_by_name
            ha_dev_id = resolve_ha_device_id_by_name(self._hass, device_id)
            if ha_dev_id:
                self._state.ha_device_id = ha_dev_id
                self._log(f"Resolved HA device: {ha_dev_id[:8]}…")
            else:
                self._log("Warning: Could not resolve HA device UUID — will use fallback resolution.")

            self._set_stage(
                WizardStage.RECONF_CONFIRM,
                message=f"Selected device: {device_id}. Confirm to proceed.",
            )
            return True

        if stage == WizardStage.RECONF_CONFIRM:
            confirmed = ctx.get("confirmed", False)
            if not confirmed:
                self._log("Please confirm the re-configuration.")
                return False
            self._set_stage(
                WizardStage.RECONF_CONFIGURE,
                message="Enter new device configuration.",
            )
            return True

        if stage == WizardStage.RECONF_CONFIGURE:
            model = ctx.get("model", "")
            site = ctx.get("site", "")
            number = ctx.get("number", "")
            registry_file = ctx.get("registry_file", "")
            if not all([model, site, number, registry_file]):
                self._log("Missing configuration parameters.")
                return False

            from .device_id import compute_device_id, compute_node_name
            new_device_id = compute_device_id(model, site, number)
            new_node_name = compute_node_name(model, site, number)

            # Edge case: same config → no-op
            if new_device_id == self._state.device_id:
                self._log("New configuration is identical to current. No changes needed.")
                self._state.new_device_id = new_device_id
                self._state.new_node_name = new_node_name
                self._state.finished_at = time.time()
                self._set_stage(
                    WizardStage.RECONF_COMPLETE,
                    message="Configuration unchanged — no re-configure needed.",
                )
                return True

            self._state.new_device_id = new_device_id
            self._state.new_node_name = new_node_name

            self._set_stage(
                WizardStage.RECONF_BUILD,
                message=f"Building firmware for {new_node_name}…",
            )
            self._task = self._hass.async_create_task(
                self._run_reconfigure(model, site, number, registry_file)
            )
            return True

        return False

    async def _advance_factory_reset(
        self, stage: WizardStage, ctx: dict[str, Any]
    ) -> bool:
        """Advance Factory Reset flow."""
        if stage == WizardStage.RESET_SELECT_DEVICE:
            device_id = ctx.get("device_id", "")
            device_kind = ctx.get("device_kind", "")
            if not device_id:
                self._log("No device selected.")
                return False

            # Auto-resolve device_kind from InputReader when not provided
            if not device_kind:
                from . import get_integration_data
                # EPIC-015 P1-05: entry-scoped lookup
                domain_data = get_integration_data(self._hass, self._entry_id)
                input_reader = domain_data.get("input_reader")
                if input_reader:
                    device_kind = await input_reader.get_selected_device_kind(
                        device_name=device_id
                    ) or ""

            # Reject if already Factory
            if device_kind == "factory":
                self._set_error(
                    "Cannot factory-reset a device that is already in Factory mode.",
                    recovery="Select a Production device instead.",
                )
                return True
            self._state.device_id = device_id

            # Resolve HA Device Registry UUID from display name
            from .flash_uploader import resolve_ha_device_id_by_name
            ha_dev_id = resolve_ha_device_id_by_name(self._hass, device_id)
            if ha_dev_id:
                self._state.ha_device_id = ha_dev_id

            self._set_stage(
                WizardStage.RESET_CONFIRM,
                message=f"About to factory-reset {device_id}. All device-specific config will be erased.",
            )
            return True

        if stage == WizardStage.RESET_CONFIRM:
            confirmed = ctx.get("confirmed", False)
            if not confirmed:
                self._log("Please confirm the factory reset.")
                return False

            self._set_stage(
                WizardStage.RESET_PUSH_OTA,
                message="Pushing factory firmware via OTA…",
            )
            self._task = self._hass.async_create_task(
                self._run_factory_reset()
            )
            return True

        return False

    # ── Background task implementations ───────────────────────────────

    async def _run_preflight(self) -> None:
        """Run pre-flight checks (gates) for Initial Setup."""
        try:
            from .gates import QualityGateChecker

            from . import get_integration_data
            # EPIC-015 P1-05: entry-scoped lookup
            domain_data = get_integration_data(self._hass, self._entry_id)
            input_reader = domain_data.get("input_reader")

            if not input_reader:
                self._set_error("Integration not ready — input reader unavailable.")
                return

            checker = QualityGateChecker(self._hass, input_reader)
            summary = await checker.run_all_gates()

            gate_status = summary.get("overall", "unknown")
            if gate_status == "fail":
                fail_names = summary.get("failed_gates", [])
                # Include evidence from failed gates so the user sees what went wrong
                details = summary.get("details", {})
                evidence_parts = []
                for gid in fail_names:
                    gate_detail = details.get(gid, {})
                    evidence = gate_detail.get("evidence", "")
                    if evidence:
                        evidence_parts.append(f"{gid}: {evidence}")
                evidence_msg = "; ".join(evidence_parts) if evidence_parts else ", ".join(fail_names)
                self._set_error(
                    f"Pre-flight gates failed: {evidence_msg}",
                    recovery="Fix the failing gates and try again.",
                )
                return

            self._log(f"Pre-flight checks: {gate_status}")
            self._set_stage(
                WizardStage.SETUP_CONFIGURE,
                message="Pre-flight checks passed. Configure the device.",
            )
        except Exception as exc:
            self._set_error(f"Pre-flight check failed: {exc}")

    async def _run_initial_setup_build(
        self,
        model: str,
        site: str,
        number: str,
        registry_file: str,
        mac_suffix: str,
    ) -> None:
        """Build firmware + install via Factory managed pull."""
        try:
            # Stage: SETUP_BUILD — compile firmware
            from .factory_installer import (
                FactoryInstallError,
                install_production_from_factory,
            )

            self._log(f"Building device config: model={model}, site={site}, number={number}")

            # The factory installer handles the full flow:
            #   set_manifest_url → trigger_update → wait_reboot → postcheck
            def progress_cb(stage: str, pct: int, msg: str = "") -> None:
                """Map installer progress to wizard stages."""
                if msg:
                    self._log(msg)

                if stage == "set_manifest_url":
                    self._set_stage(WizardStage.SETUP_INSTALL, message=msg or "Setting manifest URL…")
                elif stage == "trigger_update":
                    self._set_stage(WizardStage.SETUP_INSTALL, message=msg or "Triggering firmware update…")
                elif stage == "waiting_reboot":
                    self._set_stage(WizardStage.SETUP_REBOOTING, message=msg or "Device rebooting…")
                elif stage == "postcheck":
                    self._set_stage(WizardStage.SETUP_RECONNECT, message=msg or "Checking device…")
                elif stage == "failed":
                    self._log("OTA install failed — all retry attempts exhausted.")

            result = await install_production_from_factory(
                self._hass,
                self._state.device_id,
                progress_cb=progress_cb,
                entry_id=self._entry_id,  # EPIC-015 P2-02
            )

            # EPIC-015 P3-01: install_production_from_factory returns
            # {"success": True, ...} on success; raises FactoryInstallError on failure.
            if not result.get("success", False):
                error_msg = result.get("error", "Unknown failure")
                if "never returned" in error_msg.lower() or "timeout" in error_msg.lower():
                    self._set_error(
                        error_msg,
                        recovery="Check device power and WiFi. If unreachable, try USB flash.",
                    )
                else:
                    self._set_error(error_msg)
                return

            # Install success → attempt deterministic noise_psk auto-reauth
            # (D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001)
            self._state.node_name = self._state.new_node_name
            await self._attempt_auto_reauth(mac_suffix)

        except FactoryInstallError as exc:
            self._set_error(
                f"OTA install failed after all retries: {exc}",
                recovery=exc.recovery or (
                    "Check device WiFi. Retry the wizard or "
                    "use USB flash as last resort."
                ),
            )
        except Exception as exc:
            self._set_error(f"Build/install failed: {exc}")

    async def _attempt_auto_reauth(self, mac_suffix: str) -> bool:
        """Attempt deterministic noise_psk auto-reauth after Factory→Production install.

        D-OPS-ESPHOME-NOISE-PSK-DETERMINISTIC-001:
          1. Resolve noise_psk from esphome/secrets.yaml (per-device only)
          2. Store in keyring
          3. Apply to ESPHome config entry (two-phase reload, AD-1 verification)
          4. On success: skip SETUP_SAVE_KEY → go to SETUP_VERIFY
          5. On failure: fall back to SETUP_SAVE_KEY (manual flow)

        AD-2: No plaintext keys in logs or wizard attributes during auto-reauth.

        Returns:
            True if auto-reauth succeeded and wizard advanced to SETUP_VERIFY.
        """
        if not mac_suffix:
            self._log("No MAC suffix available — skipping auto-reauth.")
            self._transition_to_manual_key_save(
                "MAC suffix missing — auto-reauth skipped."
            )
            return False

        try:
            from .keyring import (
                apply_noise_psk_to_esphome_entry,
                mask_key,
                resolve_noise_psk_from_secrets,
            )

            from . import get_integration_data
            # EPIC-015 P1-05: entry-scoped lookup
            domain_data = get_integration_data(self._hass, self._entry_id)
            keyring = domain_data.get("keyring")

            # ── Step 1: Resolve noise_psk from secrets ──
            self._set_stage(
                WizardStage.SETUP_RECONNECT,
                message="Resolving API encryption key…",
            )
            noise_psk = await self._hass.async_add_executor_job(
                resolve_noise_psk_from_secrets, self._hass, mac_suffix
            )

            if not noise_psk:
                self._log(
                    f"API encryption key not found in secrets for "
                    f"mac_suffix={mac_suffix}. Manual key entry required."
                )
                self._transition_to_manual_key_save(
                    "API encryption key not found in secrets."
                )
                return False

            self._log(
                f"API encryption key resolved (key={mask_key(noise_psk)})"
            )

            # ── Step 2: Store in keyring ──
            if keyring:
                await keyring.set_production_noise_psk(mac_suffix, noise_psk)
                self._log("Key stored in persistent keyring.")

            # ── Step 3: Resolve full device MAC for entry lookup ──
            device_mac = self._resolve_device_mac(mac_suffix)

            # ── Step 4: Apply to ESPHome config entry ──
            self._set_stage(
                WizardStage.SETUP_RECONNECT,
                message="Applying encryption key to ESPHome integration…",
            )

            applied = await apply_noise_psk_to_esphome_entry(
                self._hass,
                noise_psk,
                new_node_name=self._state.new_node_name,
                device_mac=device_mac,
                ha_device_id=self._state.ha_device_id,
                device_names=[
                    self._state.device_id,       # old Factory name
                    self._state.new_node_name,    # new Production node
                ],
            )

            if applied:
                self._log("ESPHome connection verified with encryption key.")
                # Skip SETUP_SAVE_KEY → go directly to SETUP_VERIFY
                self._set_stage(
                    WizardStage.SETUP_VERIFY,
                    message="Encryption key applied automatically. Running sanity checks…",
                )
                self._task = self._hass.async_create_task(
                    self._run_sanity_checks()
                )
                return True

            self._log(
                "Auto-reauth could not verify ESPHome connection. "
                "Falling back to manual key save."
            )
            self._transition_to_manual_key_save(
                "Auto-reauth could not verify the ESPHome connection."
            )
            return False

        except Exception:
            _LOGGER.warning(
                "Auto-reauth failed unexpectedly — falling back to manual flow",
                exc_info=True,
            )
            self._log("Auto-reauth error — falling back to manual key save.")
            self._transition_to_manual_key_save(
                "Auto-reauth error — manual fallback required."
            )
            return False

    def _resolve_device_mac(self, mac_suffix: str) -> str:
        """Resolve full device MAC from Device Registry using mac_suffix.

        Searches all devices for a MAC address ending with the given suffix.

        Returns:
            Full MAC address (colon-separated) or empty string.
        """
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self._hass)
        suffix_lower = mac_suffix.lower()

        for device_entry in dev_reg.devices.values():
            for conn_type, conn_id in device_entry.connections:
                if (
                    conn_type == dr.CONNECTION_NETWORK_MAC
                    and conn_id.lower().replace(":", "").endswith(suffix_lower)
                ):
                    _LOGGER.debug(
                        "Resolved full MAC for suffix %s: %s",
                        mac_suffix,
                        conn_id,
                    )
                    return conn_id
        _LOGGER.debug("Could not resolve full MAC for suffix %s", mac_suffix)
        return ""

    def _transition_to_manual_key_save(self, reason: str) -> None:
        """Fail closed to manual key save/reauth.

        Used by every _attempt_auto_reauth fallback path (missing
        mac_suffix, missing noise_psk, failed apply/verify, unexpected
        exception). Always lands on SETUP_SAVE_KEY and never schedules
        sanity checks — those run only after successful auto-reauth or
        after the user confirms via confirm_key_saved().

        ``reason`` is a short operator-facing summary; it must not
        contain key material.
        """
        self._set_stage(
            WizardStage.SETUP_SAVE_KEY,
            message=(
                f"{reason} "
                f"Please save the encryption key and confirm before "
                f"verification."
            ),
        )

    async def _run_reconfigure(
        self,
        model: str,
        site: str,
        number: str,
        registry_file: str,
    ) -> None:
        """Run the full re-configure flow (build + push + identity switch)."""
        try:
            from .lifecycle import reconfigure_device, EVENT_LIFECYCLE_STAGE

            # Map lifecycle events to wizard stages (lifecycle uses event bus, not callbacks)
            stage_map = {
                "building": (WizardStage.RECONF_BUILD, "Building…"),
                "build_done": (WizardStage.RECONF_BUILD, "Build complete."),
                "pushing_ota": (WizardStage.RECONF_PUSH_OTA, "Pushing OTA…"),
                "ota_done": (WizardStage.RECONF_PUSH_OTA, "OTA push complete."),
                "identity_switch": (WizardStage.RECONF_IDENTITY_SWITCH, "Waiting for identity switch…"),
                "cleanup": (WizardStage.RECONF_READOPT, "Cleaning up old device…"),
            }

            def _on_lifecycle_event(event) -> None:
                data = event.data or {}
                if data.get("transition") != "reconfigure":
                    return
                # EPIC-015 P2-02: ignore events from other entries
                evt_entry_id = data.get("entry_id")
                if evt_entry_id and self._entry_id and evt_entry_id != self._entry_id:
                    return
                lc_stage = data.get("stage", "")
                detail = data.get("detail", "")
                mapping = stage_map.get(lc_stage)
                if mapping:
                    wiz_stage, default_msg = mapping
                    self._set_stage(wiz_stage, message=detail or default_msg)
                if detail:
                    self._log(detail)

            unsub = self._hass.bus.async_listen(EVENT_LIFECYCLE_STAGE, _on_lifecycle_event)

            try:
                result = await reconfigure_device(
                    self._hass,
                    current_device_id=self._state.device_id,
                    ha_device_id=self._state.ha_device_id or None,
                    new_model=model,
                    new_site=site,
                    new_number=number,
                    registry_file=registry_file,
                    entry_id=self._entry_id,  # EPIC-015 P2-02
                )
            finally:
                unsub()

            if not result.success:
                recovery = ""
                if "unreachable" in (result.error or "").lower():
                    recovery = "Device may be offline. Try Factory Reset or USB flash as last resort."
                self._set_error(result.error or "Re-configure failed", recovery=recovery)
                return

            self._state.orphaned_entities = result.orphaned_entities or []
            self._state.node_name = result.new_node_name or self._state.new_node_name

            # ── Entity Registry Reset ──
            # After reconfigure, ESPHome entities keep old HA entity_ids
            # because HA matches by unique_id. We must unload → remove → reload
            # to force fresh entity IDs matching the new friendly_name prefix.
            self._set_stage(
                WizardStage.RECONF_ENTITY_RESET,
                message="Resetting entity registry for correct naming…",
            )

            try:
                from .entity_reset import (
                    reset_entity_registry,
                    _resolve_esphome_config_entry,
                )

                # Build expected prefix from new node name
                new_node = result.new_node_name or self._state.new_node_name or ""
                expected_prefix = new_node.replace("-", "_") + "_" if new_node else ""

                # Resolve config_entry_id early — pass it directly for reliable scoping
                esphome_entry_id = None
                if self._state.ha_device_id:
                    esphome_entry_id = _resolve_esphome_config_entry(
                        self._hass, self._state.ha_device_id,
                    )

                async def _reset_progress(stage: str, pct: int, detail: str = "") -> None:
                    self._log(f"Entity reset [{stage}]: {detail}" if detail else f"Entity reset: {stage}")

                reset_result = await reset_entity_registry(
                    self._hass,
                    config_entry_id=esphome_entry_id,
                    device_id=self._state.ha_device_id or None,
                    expected_prefix=expected_prefix,
                    mode="full_reset",
                    dry_run=False,
                    warmup_timeout_s=60,
                    min_entities=5,
                    progress_cb=_reset_progress,
                )

                if reset_result.success:
                    self._log(
                        f"Entity reset complete: {reset_result.removed_count} removed, "
                        f"{reset_result.re_registered_count} re-registered, "
                        f"device_id stable={reset_result.device_id_stable}"
                    )
                else:
                    self._log(f"Entity reset warning: {reset_result.error}")
                    # Non-fatal — continue to verify stage

            except Exception as exc:
                self._log(f"Entity reset failed (non-fatal): {exc}")
                _LOGGER.warning("Entity reset failed: %s", exc, exc_info=True)

            # ── Update device selector dropdown to new identity ──
            # After identity switch, the old device name is no longer valid.
            # Update input_select so HA doesn't log "option no longer valid".
            try:
                from .const import ENTITY_DEVICE_SELECTOR
                # Derive display name from node name: "sph10k-haus-02" → "Sph10K Haus 02"
                new_node = result.new_node_name or self._state.new_node_name or ""
                new_display_name = new_node.replace("-", " ").title() if new_node else ""
                if new_display_name:
                    # Refresh dropdown options from discovery
                    # EPIC-015 P1-05: entry-scoped lookup
                    from . import get_integration_data as _get_data
                    _domain_data = _get_data(self._hass, self._entry_id)
                    input_reader = _domain_data.get("input_reader")
                    if input_reader:
                        dropdown_items = await input_reader.get_all_devices_for_dropdown()
                        options = ["none"] + [item["value"] for item in dropdown_items]
                        if new_display_name not in options:
                            options.append(new_display_name)
                        await self._hass.services.async_call(
                            "input_select", "set_options",
                            {"entity_id": ENTITY_DEVICE_SELECTOR, "options": options},
                        )
                    await self._hass.services.async_call(
                        "input_select", "select_option",
                        {"entity_id": ENTITY_DEVICE_SELECTOR, "option": new_display_name},
                    )
                    self._log(f"Device selector updated to: {new_display_name}")
            except Exception as exc:
                _LOGGER.debug("Could not update device selector: %s", exc)

            # Verify
            self._set_stage(
                WizardStage.RECONF_VERIFY,
                message="Re-configure done. Running sanity checks…",
            )
            await self._run_sanity_checks()

        except Exception as exc:
            self._set_error(
                f"Re-configure failed: {exc}",
                recovery="Try Factory Reset or USB flash as last resort.",
            )

    async def _run_factory_reset(self) -> None:
        """Run the full factory reset flow."""
        try:
            from .lifecycle import factory_reset_device, EVENT_LIFECYCLE_STAGE

            # Map lifecycle events to wizard stages
            stage_map = {
                "download_factory": (WizardStage.RESET_PUSH_OTA, "Downloading factory firmware…"),
                "pushing_ota": (WizardStage.RESET_PUSH_OTA, "Pushing factory firmware…"),
                "ota_done": (WizardStage.RESET_PUSH_OTA, "OTA push complete."),
                "waiting_factory": (WizardStage.RESET_REBOOTING, "Rebooting to factory…"),
                "cleanup": (WizardStage.RESET_CLEANUP, "Cleaning up…"),
            }

            def _on_lifecycle_event(event) -> None:
                data = event.data or {}
                if data.get("transition") != "factory_reset":
                    return
                # EPIC-015 P2-02: ignore events from other entries
                evt_entry_id = data.get("entry_id")
                if evt_entry_id and self._entry_id and evt_entry_id != self._entry_id:
                    return
                lc_stage = data.get("stage", "")
                detail = data.get("detail", "")
                mapping = stage_map.get(lc_stage)
                if mapping:
                    wiz_stage, default_msg = mapping
                    self._set_stage(wiz_stage, message=detail or default_msg)
                if detail:
                    self._log(detail)

            unsub = self._hass.bus.async_listen(EVENT_LIFECYCLE_STAGE, _on_lifecycle_event)

            try:
                result = await factory_reset_device(
                    self._hass,
                    current_device_id=self._state.device_id,
                    ha_device_id=self._state.ha_device_id or None,
                    entry_id=self._entry_id,  # EPIC-015 P2-02
                )
            finally:
                unsub()

            if not result.success:
                recovery = ""
                if "unreachable" in (result.error or "").lower():
                    recovery = "Device may be offline. Try USB flash as last resort."
                self._set_error(result.error or "Factory reset failed", recovery=recovery)
                return

            self._state.orphaned_entities = result.orphaned_entities or []
            self._state.finished_at = time.time()
            self._set_stage(
                WizardStage.RESET_COMPLETE,
                message="Device reset to factory state. Run Initial Setup to configure again.",
            )

        except Exception as exc:
            self._set_error(
                f"Factory reset failed: {exc}",
                recovery="Try USB flash as last resort.",
            )

    async def _run_sanity_checks(self) -> None:
        """Run post-install sanity checks."""
        try:
            from .sanity import run_sanity_checks

            self._log("Running sanity checks…")
            results = await run_sanity_checks(
                self._hass,
                node_name=self._state.node_name or self._state.new_node_name,
            )
            self._state.sanity_results = results
            self._state.sanity_passed = results.get("passed", False)

            if self._state.sanity_passed:
                self._log("Sanity checks passed.")
            else:
                failures = results.get("failures", [])
                for f in failures:
                    self._log(f"  ✗ {f}")
                self._log("Some sanity checks failed — device may still be starting up.")

            # Move to complete stage
            self._state.finished_at = time.time()
            flow = self._state.flow
            if flow == WizardFlow.INITIAL_SETUP:
                self._set_stage(
                    WizardStage.SETUP_COMPLETE,
                    message="Setup complete." if self._state.sanity_passed
                    else "Setup finished with warnings — some sensors not yet responding.",
                )
            elif flow == WizardFlow.RECONFIGURE:
                self._set_stage(
                    WizardStage.RECONF_COMPLETE,
                    message="Re-configure complete." if self._state.sanity_passed
                    else "Re-configure finished with warnings — some sensors not yet responding.",
                )
        except Exception as exc:
            _LOGGER.warning("Sanity checks errored: %s", exc)
            self._state.sanity_passed = False
            self._state.sanity_results = {"error": str(exc)}
            self._state.finished_at = time.time()

            flow = self._state.flow
            if flow == WizardFlow.INITIAL_SETUP:
                self._set_stage(
                    WizardStage.SETUP_COMPLETE,
                    message=f"Setup complete but sanity checks errored: {exc}",
                )
            elif flow == WizardFlow.RECONFIGURE:
                self._set_stage(
                    WizardStage.RECONF_COMPLETE,
                    message=f"Re-configure complete but sanity checks errored: {exc}",
                )
