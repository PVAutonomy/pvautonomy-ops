"""Build Pipeline orchestrator — Auto Configure → Compile → Publish (P3-12-001).

Chains the YAML generator, Builder Add-on, and artifact management into a
single async pipeline that fires HA events for UI progress tracking.

Usage from button.py or a dashboard service call:

    result = await run_build_pipeline(
        hass, model="sph10k", site="haus", number="02",
        registry_file="growatt/sph/sph10k.json",
        mac_suffix="2eb1e4",
    )

Ref: WORKER-PROMPT-P3-12-001, Phase A3c.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .build_backend import (
    BuildBackend,
    BuilderAddonBackend,
    BuildError,
    BuildState,
    BuildStatus,
    EsphomeDashboardBackend,
    ManualBuildBackend,
    ProxyRemoteBuildBackend,
    SimulatedBuildBackend,
)
from .const import (
    BUILD_BACKEND_ESPHOME_DASHBOARD,
    BUILD_BACKEND_MANUAL,
    BUILD_BACKEND_PROXY_REMOTE,
    BUILD_BACKEND_SIMULATED,
    BUILD_CONTRACT_YAML_AUTHORITY,
    BUILDER_ADDON_DEFAULT_PORT,
    BUILDER_ADDON_TIMEOUT,
    BUILDER_POLL_INTERVAL,
    DEFAULT_HARDWARE_MODEL,
    DOMAIN,
    ESPHOME_COMPILE_TIMEOUT,
    PROXY_DEFAULT_TIMEOUT,
    SIMULATED_FAILURE_NONE,
    SUPPORTED_HARDWARE_MODELS,
    TIER_STANDARD,
)
from .device_id import compute_device_id, compute_node_name
from .yaml_generator import YamlGenerationError, generate_device_yaml

_LOGGER = logging.getLogger(__name__)

# HA event names
EVENT_BUILD_STAGE = f"{DOMAIN}_build_stage"

# Local artifact storage (before Phase B publishes to GitHub Pages)
ARTIFACT_DIR = Path("/config/pvautonomy/builds")


@dataclass
class PipelineResult:
    """Result of a full build pipeline run."""

    success: bool
    device_id: str
    node_name: str
    stage: str = "init"
    error: str | None = None
    artifact_path: Path | None = None
    artifact_bytes: bytes | None = None
    firmware_size: int = 0
    build_job_id: str | None = None
    duration_s: float = 0.0
    build_backend: str = ""
    is_simulated: bool = False
    generated_yaml: str | None = None  # ESPHome Builder sync (not serialized)
    cache_hit: bool = False  # EPIC-007: proxy build cache hit
    force_rebuild: bool = False  # EPIC-004: proxy cache bypass requested

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "device_id": self.device_id,
            "node_name": self.node_name,
            "stage": self.stage,
            "error": self.error,
            "artifact_path": str(self.artifact_path) if self.artifact_path else None,
            "firmware_size": self.firmware_size,
            "build_job_id": self.build_job_id,
            "duration_s": round(self.duration_s, 1),
            "build_backend": self.build_backend,
            "is_simulated": self.is_simulated,
            "cache_hit": self.cache_hit,
            "force_rebuild": self.force_rebuild,
        }


async def run_build_pipeline(
    hass: HomeAssistant,
    *,
    model: str,
    site: str,
    number: str | int,
    registry_file: str,
    mac_suffix: str | None = None,
    version: str | None = None,
    channel: str = "stable",
    developer_mode: bool = False,
    build_backend: str | None = None,
    simulated_failure_mode: str = SIMULATED_FAILURE_NONE,
    proxy_config: dict | None = None,
    selected_tier: str = TIER_STANDARD,
    modbus_version: int | None = None,
    map_confirmed: bool = False,
    force_rebuild: bool = False,
    entry_id: str | None = None,
) -> PipelineResult:
    """Run the full Auto Configure → Compile → (local store) pipeline.

    Args:
        hass: Home Assistant instance.
        model: Inverter model slug (e.g. "sph10k").
        site: Installation location (e.g. "haus").
        number: Device number (e.g. "02").
        registry_file: Relative path to registry JSON.
        mac_suffix: 6-char hex MAC suffix for secrets.
        version: Firmware version string.
        channel: Release channel (stable/beta).
        developer_mode: DEPRECATED — use build_backend instead. Kept for backward compat.
        build_backend: Backend to use: "simulated"|"builder_addon"|"esphome_dashboard"|"manual".
                       If None, reads from hass.data[DOMAIN]["config"]["build_backend"].
        simulated_failure_mode: Failure mode for simulated backend.
        proxy_config: Optional proxy credentials dict for entry-free operation
                      (e.g. during Config Flow). Keys: proxy_base_url, proxy_api_key,
                      proxy_customer_id. When provided, bypasses get_integration_data().
        force_rebuild: EPIC-004 — when True and the build backend is the proxy,
                       bypass the proxy artifact cache (cold build) so a
                       registry-only update produces fresh firmware. Has no
                       effect on non-proxy backends. The generated YAML bytes
                       are unchanged; only the proxy cache lookup is bypassed.

    Returns:
        PipelineResult with artifact bytes on success.
    """
    device_id = compute_device_id(model, site, number)
    node_name = compute_node_name(model, site, number)
    result = PipelineResult(
        success=False,
        device_id=device_id,
        node_name=node_name,
        force_rebuild=force_rebuild,
    )
    start_time = time.monotonic()

    def _fire_event(stage: str, progress: int, **extra: Any) -> None:
        result.stage = stage
        hass.bus.async_fire(
            EVENT_BUILD_STAGE,
            {
                "entry_id": entry_id,  # EPIC-015 P2-02
                "stage": stage,
                "progress": progress,
                "device_id": device_id,
                "node_name": node_name,
                "registry_file": registry_file,
                "build_backend": result.build_backend,
                "is_simulated": result.is_simulated,
                "force_rebuild": force_rebuild,
                **extra,
            },
        )

    _fire_event("init", 0)
    _LOGGER.info("Build pipeline started for %s", device_id)

    # ---------------------------------------------------------------
    # Stage 1: Auto Configure — generate device-specific YAML
    # ---------------------------------------------------------------
    _fire_event("auto_configure", 5, detail="Generating device YAML")

    try:
        yaml_content = await hass.async_add_executor_job(
            _generate_yaml_sync,
            model, site, number, registry_file, mac_suffix, version,
            selected_tier, modbus_version, map_confirmed,
        )
    except YamlGenerationError as exc:
        result.error = f"YAML generation failed: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire_event("failed", 100, error=result.error)
        _LOGGER.error(result.error)
        return result

    yaml_lines = yaml_content.count("\n")
    result.generated_yaml = yaml_content  # Preserve for ESPHome Builder sync
    _fire_event("auto_configure_done", 15, yaml_lines=yaml_lines)
    _LOGGER.info("Auto Configure done: %d lines of YAML", yaml_lines)

    # ---------------------------------------------------------------
    # Stage 2: Compile — submit to build backend
    # ---------------------------------------------------------------
    # Resolve which backend to use (FR6: proper propagation)
    if proxy_config:
        config = proxy_config
    else:
        # EPIC-015 P1-04: entry-scoped config lookup (no first-entry fallback)
        from . import get_integration_data
        config = get_integration_data(hass, entry_id).get("config", {})
    effective_backend = build_backend
    if not effective_backend:
        effective_backend = config.get("build_backend", BUILD_BACKEND_SIMULATED)
    # Legacy compat: developer_mode=True → manual
    if developer_mode and not build_backend:
        effective_backend = BUILD_BACKEND_MANUAL

    is_simulated = effective_backend == BUILD_BACKEND_SIMULATED
    result.build_backend = effective_backend
    result.is_simulated = is_simulated
    _fire_event("compile_start", 20, detail=f"Submitting build ({effective_backend})")

    backend: BuildBackend
    if effective_backend == BUILD_BACKEND_SIMULATED:
        # Read failure mode from args or config
        fm = simulated_failure_mode
        if fm == SIMULATED_FAILURE_NONE:
            fm = config.get("simulated_failure_mode", SIMULATED_FAILURE_NONE)
        backend = SimulatedBuildBackend(failure_mode=fm)
        _LOGGER.info("Using SimulatedBuildBackend (failure_mode=%s)", fm)
    elif effective_backend == BUILD_BACKEND_ESPHOME_DASHBOARD:
        _LOGGER.info("Using EsphomeDashboardBackend (WP2 production bridge)")
        backend = EsphomeDashboardBackend()
    elif effective_backend == BUILD_BACKEND_PROXY_REMOTE:
        from .const import CONF_PROXY_API_KEY, CONF_PROXY_BASE_URL, CONF_PROXY_CUSTOMER_ID
        from .const import CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT

        # Defense-in-depth (C): refuse to call the proxy with an empty
        # customer_id. Without this guard the proxy returns
        # ``HTTP 400 Missing or invalid customer_id`` only after a full
        # build attempt, with no actionable message in the dashboard.
        from homeassistant.exceptions import HomeAssistantError

        proxy_customer_id = (config.get(CONF_PROXY_CUSTOMER_ID) or "").strip()
        if not proxy_customer_id:
            raise HomeAssistantError(
                "Proxy customer_id fehlt. Bitte PVAutonomy Eintrag "
                "konfigurieren oder Setup erneut ausführen. / Proxy "
                "customer_id is missing. Please configure the PVAutonomy "
                "entry or re-run setup."
            )

        _LOGGER.info("Using ProxyRemoteBuildBackend (EPIC-005-D1)")
        backend = ProxyRemoteBuildBackend(
            base_url=config.get(CONF_PROXY_BASE_URL, ""),
            api_key=config.get(CONF_PROXY_API_KEY, ""),
            customer_id=proxy_customer_id,
            entry_id=config.get("_entry_id", ""),
        )
        # EPIC-006-WP3 D2: propagate auto-refresh setting from Options Flow
        auto_refresh = config.get(CONF_PROXY_AUTO_REFRESH_ON_TIMEOUT, True)
        backend.set_auto_refresh(bool(auto_refresh))
        # EPIC-004: build_firmware service cache bypass for registry-only updates
        if force_rebuild:
            backend.set_force_rebuild(True)
        # TASK-20260520 Phase 2 (A′): resolve the repo-wide AES-256
        # COMPILE_SECRET_KEY from the keyring and hand it to the backend so
        # it can encrypt the legacy `encrypted_secrets` payload HA-side.
        # The key is provisioned out-of-band by the operator; if absent the
        # backend fails closed only when a build actually needs to send
        # compile secrets (factory/no-secret builds are unaffected). The
        # value is never logged here.
        #
        # fix/wizard-compile-key-global-fallback: the compile_secret_key is a
        # single repo-wide value in the global keyring store
        # (STORAGE_KEY="pvautonomy_ops_keyring"), NOT per-entry. Prefer the
        # entry-scoped keyring when an entry context exists (service / button
        # build path). The Config Flow wizard runs entry-free — proxy_config
        # carries no "_entry_id", so hass.data has no entry-scoped keyring and
        # the key was previously never loaded, making the backend fail closed
        # with compile_secret_key_missing_or_invalid before the /build POST.
        # Fall back to a fresh keyring instance bound to the same global store
        # so a provisioned key is found in both paths. The key value is never
        # logged: only the resolution source and a redacted marker are emitted.
        from .keyring import PVAutonomyKeyring, mask_key

        entry_id = config.get("_entry_id", "")
        keyring = (
            hass.data.get(DOMAIN, {}).get(entry_id, {}).get("keyring")
            if entry_id
            else None
        )
        compile_key_source = "entry"
        if keyring is None:
            keyring = PVAutonomyKeyring(hass)
            await keyring.async_load()
            compile_key_source = "global-fallback"

        compile_secret_key = await keyring.get_compile_secret_key()
        if compile_secret_key:
            backend.set_compile_secret_key(compile_secret_key)
        else:
            compile_key_source = "not-found"
        _LOGGER.info(
            "Proxy build compile_key resolution: source=%s key=%s",
            compile_key_source,
            mask_key(compile_secret_key),
        )
        # Set proxy-specific build context
        # "model" → hardware platform (e.g. "edge101"), NOT inverter slug ("mic600").
        # Proxy interprets "model" as compile target / board family.
        # Inverter identity travels via registry_file + device_name.
        hardware_model = DEFAULT_HARDWARE_MODEL
        if hardware_model not in SUPPORTED_HARDWARE_MODELS:
            raise BuildError(
                f"Unsupported hardware model '{hardware_model}' "
                f"(supported: {', '.join(SUPPORTED_HARDWARE_MODELS)})"
            )
        _LOGGER.info(
            "Proxy build context: hardware_model=%s, inverter_slug=%s, "
            "device_key=%s, registry=%s",
            hardware_model, model, mac_suffix or "(none)", registry_file,
        )
        # Auto-provision missing ESPHome secrets (first-install flow).
        # Must run BEFORE secret resolution so the generated values are
        # available to resolve_noise_psk_from_secrets / get_ota_password.
        if mac_suffix:
            from .esphome_secrets import ensure_device_secrets

            provision = await ensure_device_secrets(hass, mac_suffix)
            if provision.ok:
                if provision.api_key_created or provision.ota_key_created:
                    _LOGGER.info(
                        "Auto-provisioned ESPHome secrets for %s "
                        "(api_key=%s, ota=%s)",
                        mac_suffix,
                        "new" if provision.api_key_created else "existing",
                        "new" if provision.ota_key_created else "existing",
                    )
            else:
                _LOGGER.warning(
                    "Secret provisioning issues: %s", provision.errors,
                )

        # EPIC-011: Resolve device-specific api encryption key for
        # compile-time injection.  The key is sent per-build (not stored
        # in GHA) so each build gets exactly the one key it needs.
        compile_secrets: dict[str, str] = {}
        if mac_suffix:
            from .keyring import resolve_noise_psk_from_secrets

            api_key = await hass.async_add_executor_job(
                resolve_noise_psk_from_secrets, hass, mac_suffix,
            )
            if api_key:
                compile_secrets[f"edge101_api_key_{mac_suffix}"] = api_key
                _LOGGER.info(
                    "Compile secret resolved: edge101_api_key_%s (value masked)",
                    mac_suffix,
                )

        # SEC-010 D1+D2: Resolve OTA password for proxy builds
        from .flash_uploader import get_ota_password
        ota_pw_result = await hass.async_add_executor_job(
            get_ota_password, hass, mac_suffix or ""
        )
        if ota_pw_result is None:
            result.error = (
                "SEC-010: OTA password missing — build aborted. "
                "Expected 'edge101_ota_password_{suffix}' (per-device) "
                "in esphome/secrets.yaml. "
                "Auto-provisioning should have created this — "
                "check logs for provisioning errors. "
                "(Legacy 'ota_password' is accepted locally but is not "
                "supported end-to-end by the remote build workflow.)"
            )
            result.duration_s = time.monotonic() - start_time
            _fire_event("failed", 100, error=result.error)
            _LOGGER.error(result.error)
            return result
        _LOGGER.info(
            "SEC-010: OTA password resolved (key=%s, scope=%s, source=%s)",
            ota_pw_result.key_name,
            ota_pw_result.scope,
            Path(ota_pw_result.source_file).name,
        )
        backend.set_ota_required(True)
        # Forward OTA password as compile secret so GHA compiles the
        # same value that the local flash_uploader will use.  Uses the
        # same AES-256-GCM encrypted channel as the API key.
        # The secret name must match the YAML generator output:
        #   !secret edge101_ota_password_{mac_suffix}
        compile_secrets[ota_pw_result.key_name] = ota_pw_result.password
        _LOGGER.info(
            "Compile secret added: %s (scope=%s, value masked)",
            ota_pw_result.key_name,
            ota_pw_result.scope,
        )

        # Set build context AFTER all compile_secrets are collected
        # (API key + OTA password).
        #
        # EPIC-006-B7: default the customer proxy path to the
        # ``yaml_authority`` build contract. The GHA runner compiles
        # HA's exact yaml_content bytes and verifies sha256 against
        # payload.yaml_hash before compile, eliminating the silent
        # divergence between embedded and remote registry snapshots
        # (see project-docs/PLANNING/active/epics/EPIC-006/tasks/
        # TASK-20260519-EPIC006-B7-YAML-AUTHORITY-CROSS-REPO-CONTRACT.md
        # § Smoke Verification — 2026-05-19).
        # ``force_rebuild`` suppresses yaml_hash in start_build(); when
        # that happens the non-envelope yaml_authority extras are also
        # suppressed (proxy/workflow require both), so the cache-bypass
        # path keeps working unchanged.
        backend.set_build_context(
            device_key=mac_suffix or "",
            model=hardware_model,
            build_profile="production",
            registry_file=registry_file,
            device_name=node_name,
            compile_secrets=compile_secrets,
            build_contract=BUILD_CONTRACT_YAML_AUTHORITY,
        )
    elif effective_backend == BUILD_BACKEND_MANUAL:
        _LOGGER.warning("Developer mode: using ManualBuildBackend (NOT for customers)")
        backend = ManualBuildBackend()
    else:
        # builder_addon (WP3: future)
        backend = BuilderAddonBackend(port=BUILDER_ADDON_DEFAULT_PORT)

    try:
        # Health check first
        try:
            health = await backend.health_check()
            _LOGGER.info("Builder App healthy: %s", health)
        except BuildError as exc:
            result.error = f"Builder App not available: {exc}"
            result.duration_s = time.monotonic() - start_time
            _fire_event("failed", 100, error=result.error)
            _LOGGER.error(result.error)
            return result

        # Start build
        job_id, cache_hit = await backend.start_build(
            device_id, yaml_content, channel=channel
        )
        result.build_job_id = job_id
        result.cache_hit = cache_hit
        if cache_hit:
            _fire_event("compile_cached", 25, job_id=job_id, cache_hit=True)
        else:
            _fire_event("compile_queued", 25, job_id=job_id)

        # Poll until complete
        async def _progress_cb(status: BuildStatus) -> None:
            # Map build progress (0-100) to pipeline progress (25-75)
            mapped = 25 + int(status.progress_pct * 0.5)
            _fire_event(
                "compiling",
                mapped,
                build_progress=status.progress_pct,
                build_state=status.state.value,
                logs_tail=(status.logs_tail or "")[-500:],
            )

        # Select appropriate timeout for the backend
        if effective_backend == BUILD_BACKEND_ESPHOME_DASHBOARD:
            compile_timeout = ESPHOME_COMPILE_TIMEOUT
        elif effective_backend == BUILD_BACKEND_PROXY_REMOTE:
            compile_timeout = PROXY_DEFAULT_TIMEOUT
        else:
            compile_timeout = BUILDER_ADDON_TIMEOUT

        final_status = await backend.wait_for_completion(
            job_id,
            timeout=compile_timeout,
            poll_interval=BUILDER_POLL_INTERVAL,
            progress_callback=_progress_cb,
        )

        if final_status.state != BuildState.SUCCESS:
            # EPIC-006-D2: actionable error for proxy terminal-no-artifact
            if (
                effective_backend == BUILD_BACKEND_PROXY_REMOTE
                and not final_status.artifact_names
            ):
                result.error = (
                    f"Build {final_status.state.value}: "
                    f"{final_status.error or 'no artifact available'}. "
                    f"Start a new build or contact support with Build-ID: {job_id}"
                )
            else:
                result.error = f"Compile failed: {final_status.error or (final_status.logs_tail or '')[-500:]}"
            result.duration_s = time.monotonic() - start_time
            _fire_event("failed", 100, error=result.error)
            _LOGGER.error("Compile FAILED for %s: %s", device_id, result.error)
            return result

        _fire_event("compile_done", 75, detail="Compile successful")
        _LOGGER.info("Compile SUCCESS for %s (job %s)", device_id, job_id)

        # ---------------------------------------------------------------
        # Stage 3: Fetch artifact
        # ---------------------------------------------------------------
        _fire_event("fetch_artifact", 80, detail="Downloading firmware binary")

        artifact_bytes = await backend.fetch_artifact(job_id, "firmware.ota.bin")
        result.artifact_bytes = artifact_bytes
        result.firmware_size = len(artifact_bytes)

        # Hard-fail D8: minimum firmware size gate (reject stub/corrupt binaries)
        MIN_OTA_SIZE = 300 * 1024  # 300 KB
        if not is_simulated and result.firmware_size < MIN_OTA_SIZE:
            result.error = (
                f"Compiled firmware too small: {result.firmware_size} bytes "
                f"(minimum {MIN_OTA_SIZE} bytes). Likely a compile stub, not real firmware."
            )
            result.success = False
            result.duration_s = time.monotonic() - start_time
            _fire_event("failed", 100, error=result.error)
            _LOGGER.error(result.error)
            return result

        _LOGGER.info(
            "Artifact fetched: firmware.ota.bin (%d bytes)", result.firmware_size
        )

        # Store locally for flash step
        device_dir = ARTIFACT_DIR / device_id
        device_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = device_dir / "firmware.ota.bin"
        await hass.async_add_executor_job(
            _write_artifact, artifact_path, artifact_bytes
        )
        result.artifact_path = artifact_path

        _fire_event("artifact_stored", 90, path=str(artifact_path), size=result.firmware_size)

        # EPIC-015 P2-04: Prune old cached builds for this device.
        # Cache key must match the key used by ProxyRemoteBuildBackend for
        # write/read (device_key = mac_suffix), NOT the full device_id.
        # Retention uses configured cache_keep_builds from entry data.
        if effective_backend == BUILD_BACKEND_PROXY_REMOTE and mac_suffix:
            try:
                from .cache import prune_cache
                from .const import PROXY_ARTIFACT_CACHE_DIR
                cache_base = Path("/config") / PROXY_ARTIFACT_CACHE_DIR
                # Read configured retention from entry-scoped data (P2-01)
                from . import get_integration_data
                _entry_data = get_integration_data(hass, entry_id)
                keep_n = _entry_data.get("cache_keep_builds", 10)
                pruned = await hass.async_add_executor_job(
                    prune_cache, cache_base, mac_suffix, keep_n
                )
                if pruned:
                    _LOGGER.info(
                        "Pruned %d old cached builds for %s (keep=%d)",
                        pruned, mac_suffix, keep_n,
                    )
            except Exception as prune_exc:
                _LOGGER.warning("Cache prune failed (non-fatal): %s", prune_exc)

    except BuildError as exc:
        result.error = f"Build pipeline error: {exc}"
        result.duration_s = time.monotonic() - start_time
        _fire_event("failed", 100, error=result.error)
        _LOGGER.error(result.error)
        return result
    finally:
        if isinstance(backend, (BuilderAddonBackend, EsphomeDashboardBackend, ProxyRemoteBuildBackend)):
            await backend.close()

    # ---------------------------------------------------------------
    # Stage 4: Complete
    # ---------------------------------------------------------------
    result.success = True
    result.duration_s = time.monotonic() - start_time
    _fire_event(
        "complete",
        100,
        firmware_size=result.firmware_size,
        duration_s=result.duration_s,
        cache_hit=result.cache_hit,
        job_id=result.build_job_id,
        artifact_path=str(result.artifact_path) if result.artifact_path else None,
    )
    _LOGGER.info(
        "Build pipeline COMPLETE for %s: %d bytes in %.1fs (cache_hit=%s)",
        device_id,
        result.firmware_size,
        result.duration_s,
        result.cache_hit,
    )
    return result


# ---------------------------------------------------------------------------
# Sync helpers (run in executor)
# ---------------------------------------------------------------------------


def _generate_yaml_sync(
    model: str,
    site: str,
    number: str | int,
    registry_file: str,
    mac_suffix: str | None,
    version: str | None,
    selected_tier: str = TIER_STANDARD,
    modbus_version: int | None = None,
    map_confirmed: bool = False,
) -> str:
    """Synchronous wrapper for YAML generation (called via async_add_executor_job)."""
    return generate_device_yaml(
        model=model,
        site=site,
        number=number,
        registry_file=registry_file,
        mac_suffix=mac_suffix,
        version=version,
        selected_tier=selected_tier,
        modbus_version=modbus_version,
        map_confirmed=map_confirmed,
    )


def _write_artifact(path: Path, data: bytes) -> None:
    """Write binary artifact to disk."""
    path.write_bytes(data)
