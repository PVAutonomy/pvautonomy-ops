"""Persistent notification helper for setup progress UX.

Creates/updates a single persistent notification per config entry to show
build+flash progress. Uses HA's native async_create/async_dismiss API
(not service calls) for speed and stability.

Stage mapping (Planner-defined, Customer Setup UX Pack):
  preflight   -> 10%  "Readiness wird geprueft..."
  building    -> 20%  "Firmware wird kompiliert..."
  flashing    -> 80%  "OTA-Update laeuft..."
  verifying   -> 90%  "Geraet startet neu..."
  success     -> 100% "Setup abgeschlossen!"
  error       ->  --  "Fehler: ..." + actionable steps

Ref: WORKER-PROMPT-CUSTOMER-SETUP-UX-PACK.md
"""

import logging

from homeassistant.components.persistent_notification import (
    async_create,
    async_dismiss,
)
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# ── Link to integration detail page (visible in notification body) ─────
_INTEGRATION_LINK = (
    "[Einstellungen öffnen](/config/integrations/integration/pvautonomy_ops)"
)

# ── Internal flash_stage → user-facing notification stage ──────────────
# Only known stages get a notification. Unknown stages are silently ignored.
FLASH_STAGE_TO_NOTIFICATION: dict[str, str] = {
    "init": "preflight",
    "preflight": "preflight",
    "metadata": "preflight",
    "build": "building",
    "upload": "flashing",
    # EPIC-015 P3-04: factory-installer managed-pull stages
    "set_manifest_url": "flashing",
    "trigger_update": "flashing",
    "waiting_reboot": "verifying",
    "postcheck": "verifying",
    "complete": "success",
    "failed": "error",
}

# ── Notification stage → (percent, de_text, en_text) ──────────────────
_STAGE_TEXT: dict[str, tuple[int, str, str]] = {
    "preflight": (10, "Readiness wird geprüft…", "Checking readiness…"),
    "building": (20, "Firmware wird kompiliert…", "Compiling firmware…"),
    "flashing": (80, "OTA-Update läuft…", "OTA update in progress…"),
    "verifying": (90, "Gerät startet neu / wird geprüft…", "Device rebooting / verifying…"),
    "success": (100, "Setup abgeschlossen!", "Setup complete!"),
}

# ── Progress bar helper ────────────────────────────────────────────────
_BAR_WIDTH = 20


def _progress_bar(percent: int) -> str:
    """Render a simple text progress bar: [========>           ] 40%"""
    filled = int(_BAR_WIDTH * percent / 100)
    empty = _BAR_WIDTH - filled
    arrow = ">" if 0 < percent < 100 else ""
    bar = "=" * max(filled - 1, 0) + arrow + " " * empty
    return f"[{bar}] {percent}%"


def _notification_id(entry_id: str) -> str:
    """Stable notification ID per config entry."""
    return f"pvautonomy_setup_{entry_id[:8]}"


async def notify_setup_progress(
    hass: HomeAssistant,
    entry_id: str,
    device_label: str,
    stage: str,
    *,
    error: str | None = None,
) -> None:
    """Create or update a persistent notification for setup progress.

    Args:
        hass: Home Assistant instance.
        entry_id: Config entry ID (for stable notification ID).
        device_label: Human-readable device label (e.g. "Growatt MIC600 Garage 01").
        stage: Notification stage key (from FLASH_STAGE_TO_NOTIFICATION values).
        error: Error message (only for stage="error").
    """
    title = f"PVAutonomy Setup — {device_label}"
    nid = _notification_id(entry_id)

    _LOGGER.debug(
        "notify_setup_progress called: stage=%s, device=%s, nid=%s",
        stage, device_label, nid,
    )

    if stage == "error":
        body = f"**Fehler:** {error or 'Unbekannter Fehler'}\n\n"
        body += _error_suggestion(error)
        body += f"\n\n{_INTEGRATION_LINK}"
        async_create(hass, body, title=title, notification_id=nid)
        return

    info = _STAGE_TEXT.get(stage)
    if not info:
        _LOGGER.debug("notify_setup_progress: unknown stage '%s' — skipping", stage)
        return

    percent, de_text, _en_text = info
    body = f"{_progress_bar(percent)}\n\n{de_text}"

    if stage == "success":
        body += "\n\nGerät ist online und einsatzbereit."
    elif stage == "preflight":
        body += (
            "\n\n**Hinweis:** Diese Benachrichtigung wird automatisch "
            "aktualisiert, sobald ein neuer Schritt erreicht wird."
        )
    else:
        body += "\n\nFortschritt wird hier automatisch aktualisiert."

    body += f"\n\n{_INTEGRATION_LINK}"
    async_create(hass, body, title=title, notification_id=nid)


async def dismiss_setup_notification(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Dismiss the setup notification for an entry."""
    async_dismiss(hass, _notification_id(entry_id))


def _error_suggestion(error: str | None) -> str:
    """Return actionable suggestion based on error message."""
    if not error:
        return "Bitte erneut versuchen."

    err_lower = error.lower()

    if "offline" in err_lower or "unreachable" in err_lower or "timeout" in err_lower:
        return (
            "**Was tun:**\n"
            "1. Prüfen Sie, ob das Gerät eingeschaltet und im WLAN ist.\n"
            "2. Warten Sie 30 Sekunden und versuchen Sie es erneut.\n"
            "3. Falls das Problem bestehen bleibt: Gerät neu starten."
        )

    if "ota" in err_lower or "password" in err_lower or "auth" in err_lower:
        return (
            "**Was tun:**\n"
            "1. Prüfen Sie das OTA-Passwort in der ESPHome-Konfiguration.\n"
            "2. Stellen Sie sicher, dass die Firmware-Version kompatibel ist."
        )

    if "build" in err_lower or "compile" in err_lower or "dispatch" in err_lower:
        return (
            "**Was tun:**\n"
            "1. Prüfen Sie die Proxy-Verbindung (Einstellungen > PVAutonomy).\n"
            "2. Versuchen Sie es in einigen Minuten erneut."
        )

    if "guard" in err_lower or "gate" in err_lower or "blocked" in err_lower:
        return (
            "**Was tun:**\n"
            "1. Prüfen Sie den Status-Sensor für Details.\n"
            "2. Beheben Sie die gemeldeten Probleme und versuchen Sie es erneut."
        )

    return "Bitte prüfen Sie die Logs und versuchen Sie es erneut."
