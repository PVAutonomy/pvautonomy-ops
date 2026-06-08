"""Factory → Production Managed Pull Installer (P3-11-001 / C2).

Implements the Factory→Production firmware transition via ESPHome
managed pull update (ota: platform: http_request).

This is fundamentally different from the Push OTA (espota2) used for
Production→Production updates. Factory firmware uses device-initiated
download from a manifest URL — no OTA password required.

Directive: D-OPS-FLASH-MODE-SPLIT-001
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Any, NamedTuple

from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    ARTIFACTS_PAGES_BASE_URL,
)
from .artifacts import get_latest_version

_LOGGER = logging.getLogger(__name__)


class FactoryInstallError(Exception):
    """Factory install operation failed."""

    def __init__(self, message: str, *, recovery: str = "") -> None:
        super().__init__(message)
        self.recovery = recovery


class _FactoryUpdateDiagnostic(NamedTuple):
    """Internal structured diagnostic for factory update/install failures.

    Stable categories enable consistent operator-facing error reporting and
    support triage without resorting to substring matching over ``str(exc)``.
    Not part of any public API — leading underscore is intentional.
    """

    category: str
    summary: str
    recovery: str


def _diagnose_factory_update_failure(exc: BaseException) -> _FactoryUpdateDiagnostic:
    """Classify a factory update/install exception into a stable category.

    Uses structured checks only — exception type and HTTP-style status
    attributes. Does not inspect ``str(exc)`` to avoid brittle/AI-style
    substring heuristics.

    Categories:
      - ``missing_artifact``: HTTP-style ``status`` / ``status_code`` /
        ``code`` attribute equal to ``404`` or ``410``.
      - ``network_connectivity``: ``ConnectionError``, ``TimeoutError``,
        or ``asyncio.TimeoutError`` (the latter aliases ``TimeoutError``
        in Python 3.11+ but is listed explicitly for intent).
      - ``invalid_manifest``: ``json.JSONDecodeError`` or an exception
        class whose name is in a small explicit allowlist of clearly-named
        manifest parse/format classes.
      - ``unknown_update_install_failure``: everything else.

    Summaries include only the exception class name (and the HTTP status
    code for ``missing_artifact``). No secret-bearing values are included.
    """
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if value in (404, 410):
            return _FactoryUpdateDiagnostic(
                category="missing_artifact",
                summary=f"Firmware artifact not found (HTTP {value})",
                recovery=(
                    "Verify the manifest path and that firmware.ota.bin is "
                    "published at the manifest URL."
                ),
            )

    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return _FactoryUpdateDiagnostic(
            category="network_connectivity",
            summary=f"Network/connectivity failure: {type(exc).__name__}",
            recovery=(
                "Check device WiFi/DNS/internet and that the ESPHome API "
                "connection is up."
            ),
        )

    cls_name = type(exc).__name__
    if isinstance(exc, json.JSONDecodeError) or cls_name in {
        "ManifestParseError",
        "InvalidManifestError",
    }:
        return _FactoryUpdateDiagnostic(
            category="invalid_manifest",
            summary=f"Invalid manifest format: {cls_name}",
            recovery=(
                "Manifest must be valid ESP-Web-Tools JSON "
                "(builds[].chipFamily + ota.md5 + ota.path)."
            ),
        )

    return _FactoryUpdateDiagnostic(
        category="unknown_update_install_failure",
        summary=f"Unknown failure: {cls_name}",
        recovery=(
            "Inspect device and HA logs; capture debug output for support."
        ),
    )


def _slugify(text: str) -> str:
    """Convert text to HA entity slug format."""
    return text.lower().replace(" ", "_").replace("-", "_")


def _build_manifest_url(
    version: str,
    hw_family: str = "edge101",
    channel: str = "stable",
    device_id: str | None = None,
) -> str:
    """Build manifest URL for Factory Managed Pull update.

    Uses GitHub Pages (no redirects, ESP32-safe) with ESP-Web-Tools
    compatible manifest format.

    URL scheme (generic — Factory pull):
        https://{org}.github.io/{repo}/firmware/{hw_family}/{channel}/manifest.json

    URL scheme (per-device — P3-12-001):
        https://{org}.github.io/{repo}/firmware/{hw_family}/{channel}/{device_id}/manifest.json

    Args:
        version: Firmware version (unused currently — manifest always latest)
        hw_family: Hardware family (e.g., "edge101")
        channel: Release channel (stable|beta|dev)
        device_id: Optional device identifier for per-device manifest (P3-12-001)

    Returns:
        Full manifest URL on GitHub Pages
    """
    if device_id:
        url = f"{ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}/{channel}/{device_id}/manifest.json"
    else:
        url = f"{ARTIFACTS_PAGES_BASE_URL}/firmware/{hw_family}/{channel}/manifest.json"
    _LOGGER.info("Built manifest URL (Pages): %s", url)
    return url


def _derive_esphome_node_slug(device_name: str) -> str | None:
    """Derive the ESPHome node slug from device display name.

    The factory firmware uses:
      name: pvautonomy-edge101
      name_add_mac_suffix: true

    So the HA device name "PVAutonomy Modbus Bridge 2eb1e4" maps to
    ESPHome node "pvautonomy-edge101-2eb1e4" → slug "pvautonomy_edge101_2eb1e4".

    For devices with standard naming, we extract the MAC suffix and
    construct the expected node slug.

    Args:
        device_name: HA device display name

    Returns:
        ESPHome node slug (e.g., "pvautonomy_edge101_2eb1e4") or None
    """
    from .mac_utils import InvalidMACError, canonical_mac_last6

    try:
        mac_suffix = canonical_mac_last6(device_name)
    except InvalidMACError:
        _LOGGER.warning(
            "Cannot derive ESPHome node slug from device name: '%s'",
            device_name,
        )
        return None

    slug = f"pvautonomy_edge101_{mac_suffix}"
    _LOGGER.debug(
        "Derived ESPHome node slug: '%s' → '%s'",
        device_name, slug,
    )
    return slug


def _find_entity_by_pattern(
    hass: HomeAssistant,
    domain: str,
    device_slug: str,
    keywords: list[str],
) -> str | None:
    """Find an entity by domain + device slug + keyword matching.

    Args:
        hass: Home Assistant instance
        domain: Entity domain (e.g., "update", "button")
        device_slug: Slugified device name
        keywords: Keywords to match in entity_id

    Returns:
        Entity ID or None
    """
    for entity_id in hass.states.async_entity_ids(domain):
        if device_slug not in entity_id:
            continue
        entity_lower = entity_id.lower()
        if any(kw in entity_lower for kw in keywords):
            _LOGGER.debug("Found %s entity: %s", domain, entity_id)
            return entity_id

    return None


async def install_production_from_factory(
    hass: HomeAssistant,
    device_name: str,
    progress_cb=None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Install production firmware on a factory device via managed pull.

    Steps:
        1. Resolve ESPHome service name from device name
        2. Build manifest URL from artifacts scheme
        3. Set manifest URL on device via ESPHome API service
        4. Trigger firmware update via update entity
        5. Wait for device reboot (offline → online)
        6. Return result

    Args:
        hass: Home Assistant instance
        device_name: Device display name (e.g., "PVAutonomy Modbus Bridge 2eb1e4")
        progress_cb: Optional async callback(stage: str, progress: int)

    Returns:
        Dict with install results

    Raises:
        FactoryInstallError: If any step fails
    """

    # EPIC-015 P3-04: mutable container so firmware_version propagates to events
    # once computed (after manifest URL build).
    _flash_info: dict[str, str | None] = {"version": None}

    async def _update_progress(stage: str, pct: int):
        if progress_cb:
            result = progress_cb(stage, pct)
            if asyncio.iscoroutine(result):
                await result
        _LOGGER.info("Factory install stage: %s (%d%%)", stage, pct)
        hass.bus.async_fire(
            f"{DOMAIN}_flash_stage",
            {
                "entry_id": entry_id,  # EPIC-015 P2-02
                "stage": stage,
                "progress": pct,
                "target_device": device_name,
                "version": _flash_info["version"],
                "build_job_id": None,  # factory install has no CI build job
                "error": None,
            },
        )

    # ── STAGE 1: init (0%) ──────────────────────────────────────
    await _update_progress("init", 0)

    device_slug = _slugify(device_name)
    node_slug = _derive_esphome_node_slug(device_name)

    if not node_slug:
        raise FactoryInstallError(
            f"Cannot determine ESPHome node name for '{device_name}'. "
            f"Ensure device has a 6-char hex MAC suffix in its name."
        )

    # ── STAGE 2: preflight (10%) ────────────────────────────────
    await _update_progress("preflight", 10)

    # Verify ESPHome service exists
    service_name = f"{node_slug}_set_manifest_url"
    esphome_services = hass.services.async_services().get("esphome", {})

    if service_name not in esphome_services:
        # Fallback: try with device_slug (in case node name differs)
        alt_service = f"{device_slug}_set_manifest_url"
        if alt_service in esphome_services:
            service_name = alt_service
            _LOGGER.info("Using fallback service name: esphome.%s", service_name)
        else:
            available = [s for s in esphome_services if "manifest" in s.lower()]
            raise FactoryInstallError(
                f"ESPHome service 'esphome.{service_name}' not found. "
                f"Factory firmware must expose set_manifest_url API service. "
                f"Available manifest services: {available or 'none'}"
            )

    # Find update entity for firmware install trigger
    update_entity = _find_entity_by_pattern(
        hass, "update", device_slug, ["firmware", "update"],
    )
    if not update_entity:
        # Try node slug
        update_entity = _find_entity_by_pattern(
            hass, "update", node_slug, ["firmware", "update"],
        )
    if not update_entity:
        raise FactoryInstallError(
            f"Firmware update entity not found for device '{device_name}'. "
            f"Factory firmware must include 'update: platform: http_request' component."
        )

    _LOGGER.info(
        "Preflight passed: service=esphome.%s, update=%s",
        service_name, update_entity,
    )

    # ── STAGE 3: set manifest URL (30%) ─────────────────────────
    await _update_progress("set_manifest_url", 30)

    # EPIC-015 P2-02: entry-scoped config lookup
    from . import get_integration_data
    config = get_integration_data(hass, entry_id).get("config", {})
    hw_family = config.get("artifact_hw_family_default", "edge101")
    channel = config.get("artifact_channel", "stable")
    # P3-12-001: per-device manifest URL (if device_id is configured)
    configured_device_id = config.get("device_id")

    firmware_version = get_latest_version(hw_family, channel)
    _flash_info["version"] = firmware_version  # EPIC-015 P3-04: propagate to events
    manifest_url = _build_manifest_url(
        version=firmware_version,
        hw_family=hw_family,
        channel=channel,
        device_id=configured_device_id,
    )

    _LOGGER.info(
        "Setting manifest URL on device: %s → %s",
        device_name, manifest_url,
    )

    try:
        await hass.services.async_call(
            "esphome",
            service_name,
            {"url": manifest_url},
            blocking=True,
        )
    except Exception as e:
        diag = _diagnose_factory_update_failure(e)
        raise FactoryInstallError(
            f"Failed to set manifest URL on device [{diag.category}]: "
            f"{diag.summary}. "
            f"Verify device is online and API is connected."
        ) from e

    _LOGGER.info("Manifest URL set successfully on device")

    # ── STAGE 4: trigger firmware update (50%) ──────────────────
    await _update_progress("trigger_update", 50)

    # Wait for device to fetch and parse the new manifest.
    # The update component needs to:
    #   1. Process set_source_url() (instant)
    #   2. Fetch manifest.json from GitHub Pages (1-5s)
    #   3. Parse ESP-Web-Tools format and set state to "on" (update available)
    # We poll the update entity state, waiting for it to become "on".
    _LOGGER.info(
        "Waiting for device to fetch manifest and detect update (%s)...",
        update_entity,
    )

    # Force HA to refresh the update entity state (best-effort)
    with contextlib.suppress(Exception):
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": update_entity},
            blocking=True,
        )

    manifest_check_timeout = 60  # seconds
    manifest_poll_interval = 3
    manifest_elapsed = 0
    update_available = False

    while manifest_elapsed < manifest_check_timeout:
        await asyncio.sleep(manifest_poll_interval)
        manifest_elapsed += manifest_poll_interval

        # Update progress: 50→60% during manifest wait
        pct = 50 + int((manifest_elapsed / manifest_check_timeout) * 10)
        await _update_progress("trigger_update", min(pct, 59))

        cur_state = hass.states.get(update_entity)
        if cur_state and cur_state.state == "on":
            update_available = True
            _LOGGER.info(
                "Update available detected (%s state=on, elapsed=%ds)",
                update_entity, manifest_elapsed,
            )
            break

        # Periodically force refresh (best-effort; hot poll path, no log)
        if manifest_elapsed % 10 == 0:
            with contextlib.suppress(Exception):
                await hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": update_entity},
                    blocking=True,
                )

        _LOGGER.debug(
            "Waiting for update entity (state=%s, elapsed=%ds/%ds)",
            cur_state.state if cur_state else "missing",
            manifest_elapsed, manifest_check_timeout,
        )

    if not update_available:
        # Device couldn't parse manifest or couldn't reach URL
        cur_state = hass.states.get(update_entity)
        raise FactoryInstallError(
            f"Device did not detect update within {manifest_check_timeout}s "
            f"({update_entity} state={cur_state.state if cur_state else 'missing'}). "
            f"Possible causes: (1) Device cannot reach manifest URL "
            f"(check device WiFi/DNS/internet), (2) Manifest format incorrect, "
            f"(3) firmware.ota.bin not found at manifest path. "
            f"Manifest URL: {manifest_url}"
        )

    # ── STAGE 4b: trigger install with retry (50→90%) ─────────────
    # ESPHome BUG WORKAROUND: HttpContainerArduino::read() returns 0
    # when TCP buffer is momentarily empty (WiFiClient::available()==0),
    # and the OTA download loop treats this as end-of-stream → MD5 mismatch.
    # On ESP32 without PSRAM the TCP window is only 5.7KB, making this
    # race condition almost certain on large firmware downloads (~1MB).
    # After OTA_ERROR the update entity returns to state=AVAILABLE,
    # so we can safely retry. Timing jitter between attempts means
    # retries have a reasonable chance of passing the stall point.
    # See: ESPHome http_request_arduino.cpp:read() + ota_http_request.cpp
    max_install_attempts = 5
    reboot_detected = False
    install_error = None
    last_update_install_diag: _FactoryUpdateDiagnostic | None = None
    ota_attempt_log: list[dict[str, Any]] = []

    # Find monitoring sensor (uptime preferred) — needed for reboot detection
    monitor_entity = _find_entity_by_pattern(
        hass, "sensor", device_slug, ["uptime"],
    )
    if not monitor_entity:
        monitor_entity = _find_entity_by_pattern(
            hass, "sensor", node_slug, ["uptime"],
        )

    for attempt in range(1, max_install_attempts + 1):
        attempt_start = time.monotonic()
        _LOGGER.info(
            "OTA install attempt %d/%d via %s",
            attempt, max_install_attempts, update_entity,
        )

        # State-aware gating: if update entity reports in_progress, wait
        # for the current install to finish before firing another one.
        cur_upd_check = hass.states.get(update_entity)
        if cur_upd_check:
            in_prog = cur_upd_check.attributes.get("in_progress", False)
            if in_prog is not False:
                _LOGGER.info(
                    "Update entity in_progress=%s (attempt %d) — "
                    "waiting up to 60s for idle",
                    in_prog, attempt,
                )
                _gating_limit = 60
                _gating_elapsed = 0
                while _gating_elapsed < _gating_limit:
                    await asyncio.sleep(3)
                    _gating_elapsed += 3
                    _chk = hass.states.get(update_entity)
                    if (
                        not _chk
                        or _chk.attributes.get("in_progress", False) is False
                    ):
                        _LOGGER.info(
                            "Update entity idle after %ds gating (attempt %d)",
                            _gating_elapsed, attempt,
                        )
                        break
                else:
                    _LOGGER.warning(
                        "Update entity still in_progress after %ds "
                        "(attempt %d) — proceeding anyway",
                        _gating_limit, attempt,
                    )

        # Trigger the install
        try:
            await hass.services.async_call(
                "update",
                "install",
                {"entity_id": update_entity},
                blocking=True,
            )
            _LOGGER.info("Firmware update triggered via %s", update_entity)
        except Exception as e:
            err_msg = str(e).lower()
            if "no update available" in err_msg:
                raise FactoryInstallError(
                    f"Device cannot install update: {e}. "
                    f"The manifest at {manifest_url} is either unreachable "
                    f"or not in ESP-Web-Tools format (requires builds[].chipFamily "
                    f"+ ota.md5 + ota.path). Check device logs."
                ) from e
            # Other errors during reboot are expected (device drops connection).
            # Classify with structured diagnostic; record for final failure
            # output. Original exception preserved at debug level for support.
            last_update_install_diag = _diagnose_factory_update_failure(e)
            _LOGGER.warning(
                "update.install error (may be expected during reboot) [%s]: %s",
                last_update_install_diag.category,
                last_update_install_diag.summary,
            )
            _LOGGER.debug(
                "update.install original exception detail (attempt %d)",
                attempt,
                exc_info=True,
            )

        # Wait for reboot — shorter timeout per attempt since we retry
        reboot_wait = 30  # seconds per attempt (device reboots in ~5-10s if OTA succeeds)
        poll_interval = 3
        elapsed = 0
        offline_detected = False
        initial_uptime = None

        if monitor_entity:
            init_state = hass.states.get(monitor_entity)
            if init_state and init_state.state not in ("unavailable", "unknown"):
                with contextlib.suppress(ValueError, TypeError):
                    initial_uptime = float(init_state.state)

        # Progress: distribute across attempts (50→90%)
        base_pct = 50 + int(((attempt - 1) / max_install_attempts) * 40)
        await _update_progress("waiting_reboot", base_pct)

        _LOGGER.info(
            "Waiting for device reboot (attempt %d, monitor: %s)...",
            attempt, monitor_entity or "none",
        )

        while elapsed < reboot_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            pct = base_pct + int((elapsed / reboot_wait) * (40 // max_install_attempts))
            await _update_progress("waiting_reboot", min(pct, 90))

            if not monitor_entity:
                continue

            cur_state = hass.states.get(monitor_entity)
            if not cur_state:
                continue

            cur_available = cur_state.state not in ("unavailable", "unknown")

            if not offline_detected and not cur_available:
                offline_detected = True
                _LOGGER.info(
                    "Device offline detected (attempt %d, elapsed=%ds)",
                    attempt, elapsed,
                )

            if cur_available and initial_uptime is not None:
                with contextlib.suppress(ValueError, TypeError):
                    cur_uptime = float(cur_state.state)
                    if cur_uptime < initial_uptime:
                        _LOGGER.info(
                            "Uptime reset: %.1fs→%.1fs (reboot confirmed, "
                            "attempt %d, elapsed=%ds)",
                            initial_uptime, cur_uptime, attempt, elapsed,
                        )
                        reboot_detected = True
                        break

            if offline_detected and cur_available:
                _LOGGER.info(
                    "Device back online (attempt %d, elapsed=%ds)",
                    attempt, elapsed,
                )
                reboot_detected = True
                break

        if reboot_detected:
            break

        if offline_detected:
            # Device went offline = OTA likely succeeded (production firmware
            # has different identity, so it won't reconnect as the same entity)
            _LOGGER.info(
                "Device went offline and did not reconnect (attempt %d). "
                "This is EXPECTED for Factory→Production transition.",
                attempt,
            )
            reboot_detected = True
            break

        # OTA stalled — record attempt metrics, then decide: retry or abort
        attempt_elapsed = time.monotonic() - attempt_start
        cur_upd = hass.states.get(update_entity)
        cur_upd_state_str = cur_upd.state if cur_upd else "missing"
        cur_upd_in_prog = (
            cur_upd.attributes.get("in_progress", False) if cur_upd else "n/a"
        )

        attempt_record = {
            "attempt": attempt,
            "elapsed_s": round(attempt_elapsed, 1),
            "update_state": cur_upd_state_str,
            "in_progress": cur_upd_in_prog,
            "offline_seen": offline_detected,
        }
        ota_attempt_log.append(attempt_record)

        _LOGGER.warning(
            "OTA attempt %d/%d stalled: elapsed=%.1fs update_state=%s "
            "in_progress=%s offline_seen=%s",
            attempt, max_install_attempts,
            attempt_elapsed, cur_upd_state_str, cur_upd_in_prog,
            offline_detected,
        )

        if cur_upd and cur_upd.state == "on":
            install_error = (
                f"OTA download aborted on attempt {attempt}/{max_install_attempts} "
                f"(ESPHome Arduino HTTP client race condition, "
                f"elapsed={attempt_elapsed:.1f}s)"
            )
        elif cur_upd:
            install_error = (
                f"Update entity in unexpected state '{cur_upd_state_str}' "
                f"after attempt {attempt} (elapsed={attempt_elapsed:.1f}s)"
            )
        else:
            _LOGGER.warning(
                "Update entity not found after attempt %d — aborting retry",
                attempt,
            )
            break

        # Exponential backoff with jitter before next attempt
        backoff = min(5 * (2 ** (attempt - 1)), 60)
        jitter = random.uniform(0.0, min(backoff * 0.3, 10.0))
        total_backoff = backoff + jitter
        _LOGGER.info(
            "Backoff before attempt %d: %.1fs (base=%ds jitter=%.1fs)",
            attempt + 1, total_backoff, backoff, jitter,
        )
        await asyncio.sleep(total_backoff)

    if not reboot_detected and not offline_detected:
        await _update_progress("failed", 0)
        attempt_summary = "; ".join(
            f"#{r['attempt']} elapsed={r['elapsed_s']}s state={r['update_state']}"
            for r in ota_attempt_log
        ) or "no attempts recorded"
        diag_part = (
            f"Diagnostic: [{last_update_install_diag.category}] "
            f"{last_update_install_diag.summary}. "
            if last_update_install_diag is not None
            else ""
        )
        raise FactoryInstallError(
            f"No reboot detected after {max_install_attempts} OTA attempts "
            f"(~{max_install_attempts * 35}s total). "
            f"Attempt log: [{attempt_summary}]. "
            f"Root cause: ESPHome Arduino HTTP client bug — "
            f"HttpContainerArduino::read() returns 0 when TCP buffer is "
            f"momentarily empty, OTA loop treats this as end-of-stream. "
            f"{diag_part}"
            f"Last error: {install_error or 'unknown'}. "
            f"Manifest: {manifest_url}",
            recovery=(
                "Try: (1) Retry — timing jitter may resolve the TCP race. "
                "(2) Use a device with PSRAM (larger TCP window). "
                "(3) Switch ESPHome to ESP-IDF framework. "
                "(4) Use Push OTA (ota: platform: esphome). "
                "USB flash is the guaranteed fallback."
            ),
        )

    # ── STAGE 6: postcheck (95%) ────────────────────────────────
    await _update_progress("postcheck", 95)

    # Brief pause for device to stabilize
    await asyncio.sleep(5)

    # Post-reboot: For Factory→Production, we can't check firmware version
    # because the old entity is offline (production firmware = new identity).
    # Checking for "Connection requires encryption" in HA logs would be ideal,
    # but we rely on the offline→didn't-come-back pattern as evidence of success.
    post_version = None
    post_note = None

    # Try to check version (works if device came back under same identity)
    version_entity = _find_entity_by_pattern(
        hass, "sensor", device_slug, ["esphome_version", "version"],
    )
    if version_entity:
        ver_state = hass.states.get(version_entity)
        if ver_state and ver_state.state not in ("unavailable", "unknown"):
            post_version = ver_state.state
            _LOGGER.info("Post-reboot firmware version: %s", post_version)

    if offline_detected and post_version is None:
        post_note = (
            "Device rebooted with production firmware (expected). "
            "It needs to be re-adopted in HA's ESPHome integration "
            "with the production API encryption key."
        )
        _LOGGER.info("Post-install note: %s", post_note)

    # ── STAGE 7: complete (100%) ────────────────────────────────
    await _update_progress("complete", 100)

    _LOGGER.info(
        "Factory install complete: device=%s, version=%s, manifest=%s",
        device_name, firmware_version, manifest_url,
    )

    return {
        "success": True,  # EPIC-015 P3-01: canonical success signal for callers
        "result": "success",
        "device_name": device_name,
        "firmware_version": firmware_version,
        "manifest_url": manifest_url,
        "install_method": "managed_pull",
        "stage": "complete",
        "post_reboot_version": post_version,
        "post_note": post_note,
    }
