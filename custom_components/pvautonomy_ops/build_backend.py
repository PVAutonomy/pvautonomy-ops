"""Build Backend abstraction for firmware compilation (P3-12-001).

Defines the interface for compiling device-specific ESPHome firmware.
Concrete implementations:
- BuilderAddonBackend        — Builder App HTTP API (WP3: future)
- EsphomeDashboardBackend   — ESPHome Dashboard App bridge (WP2: active)
- SimulatedBuildBackend      — deterministic test double (WP1: active)
- ManualBuildBackend         — developer-mode fallback

Ref: WORKER-PROMPT-P3-12-001, Phase A3a.
     WORK-ITEM-WP1-SIMULATED-BUILD-BACKEND.md
     WORK-ITEM-WP2-ESPHOME-DASHBOARD-BACKEND.md
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .const import (
    BUILD_CONTRACT_YAML_AUTHORITY,
    BUILD_STATE_COMPILING,
    BUILD_STATE_FAILED,
    BUILD_STATE_QUEUED,
    BUILD_STATE_SUCCESS,
    BUILDER_ADDON_DEFAULT_PORT,
    BUILDER_ADDON_TIMEOUT,
    BUILDER_POLL_INTERVAL,
    ESPHOME_ADDON_SLUG,
    ESPHOME_BUILD_DIR,
    ESPHOME_COMPILE_TIMEOUT,
    ESPHOME_COMPILE_WS_PATH,
    ESPHOME_CONFIG_DIR,
    ESPHOME_DOWNLOAD_PATH,
    ESPHOME_EDIT_PATH,
    ESPHOME_SUPERVISOR_URL,
    PROXY_API_TIMEOUT,
    PROXY_ARTIFACT_CACHE_DIR,
    PROXY_DEFAULT_BASE_URL,
    PROXY_DEFAULT_TIMEOUT,
    PROXY_DOWNLOAD_TIMEOUT,
    PROXY_POLL_INTERVAL,
    PROXY_STATUS_MAP,
    PROXY_TERMINAL_NO_ARTIFACT,
    SIMULATED_ARTIFACT_SIZE,
    SIMULATED_BUILD_DURATION_S,
    SIMULATED_FAILURE_COMPILE,
    SIMULATED_FAILURE_MISSING_ARTIFACT,
    SIMULATED_FAILURE_NONE,
    SIMULATED_FAILURE_TIMEOUT,
)
from .secret_envelope import (
    BuildBackendKeysetCache,
    EnvelopeRequestContext,
    EnvelopeSealError,
    KeysetEndpointUnsupported,
    KeysetVerificationError,
    ROOT_PUBKEYS_PINNED,
    SealedEnvelope,
    load_or_refresh_keyset,
    seal_compile_secret_envelope,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class BuildState(str, Enum):
    """Build lifecycle states."""

    QUEUED = BUILD_STATE_QUEUED
    COMPILING = BUILD_STATE_COMPILING
    SUCCESS = BUILD_STATE_SUCCESS
    FAILED = BUILD_STATE_FAILED


@dataclass
class BuildStatus:
    """Status of a firmware build job."""

    state: BuildState
    progress_pct: int = 0
    logs_tail: str = ""
    artifact_names: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Return True if the build has finished (success or failed)."""
        return self.state in (BuildState.SUCCESS, BuildState.FAILED)


class BuildError(Exception):
    """Build failed fatally."""


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class BuildBackend(ABC):
    """Abstract interface for firmware compilation.

    Concrete implementations:
    - BuilderAddonBackend  (production — Builder App HTTP API)
    - ManualBuildBackend   (developer-mode fallback, NOT for customers)
    """

    @abstractmethod
    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        """Submit a firmware build job.

        Args:
            device_id: Deterministic device identifier (e.g. ``edge101_sph10k_haus_02``).
            yaml_content: Complete ESPHome YAML (as string) to compile.
            channel: Release channel (``stable`` / ``beta``).
            esphome_version: Pin ESPHome version (optional — default uses App's pinned version).

        Returns:
            Tuple of (job_id, cache_hit):
            - job_id: Opaque identifier used to poll status / fetch artifacts.
            - cache_hit: True if the build was served from proxy cache (EPIC-007).

        Raises:
            BuildError: If the build could not be queued.
        """

    @abstractmethod
    async def get_status(self, job_id: str) -> BuildStatus:
        """Poll current build status.

        Returns a :class:`BuildStatus` with state, progress, and log tail.

        Raises:
            BuildError: If the status query fails fatally.
        """

    @abstractmethod
    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        """Download a build artifact.

        Args:
            artifact_name: File name (e.g. ``firmware.ota.bin``, ``manifest.json``).

        Returns:
            Raw bytes of the artifact.

        Raises:
            BuildError: If the artifact is not available.
        """

    async def health_check(self) -> dict[str, Any]:
        """Check if the backend is reachable and healthy.

        Returns:
            Dict with at least ``{"status": "ok"}`` or raises.

        Raises:
            BuildError: If backend is unreachable.
        """
        raise BuildError("health_check not implemented")

    # ------------------------------------------------------------------
    # Convenience: poll until terminal state
    # ------------------------------------------------------------------

    async def wait_for_completion(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        progress_callback: Any | None = None,
    ) -> BuildStatus:
        """Block until the build reaches a terminal state.

        Args:
            timeout: Max seconds to wait (default: BUILDER_ADDON_TIMEOUT).
            poll_interval: Seconds between polls (default: BUILDER_POLL_INTERVAL).
            progress_callback: Optional ``async def cb(status: BuildStatus)`` called on each poll.

        Returns:
            Final :class:`BuildStatus`.

        Raises:
            BuildError: On timeout or fatal error.
        """
        _timeout = timeout or BUILDER_ADDON_TIMEOUT
        _interval = poll_interval or BUILDER_POLL_INTERVAL
        elapsed = 0.0

        while elapsed < _timeout:
            status = await self.get_status(job_id)
            if progress_callback:
                await progress_callback(status)
            if status.is_terminal:
                return status
            await asyncio.sleep(_interval)
            elapsed += _interval

        raise BuildError(
            f"Build {job_id} timed out after {_timeout}s (last state: {status.state})"
        )


# ---------------------------------------------------------------------------
# Concrete: Builder Add-on HTTP backend
# ---------------------------------------------------------------------------


class BuilderAddonBackend(BuildBackend):
    """Calls the PVAutonomy Builder App HTTP API.

    Expected App API (see Worker Prompt):
      POST /build              → {job_id, status: "queued"}
      GET  /build/<id>         → {status, progress_pct, logs_tail, artifacts}
      GET  /build/<id>/artifact/<name> → binary download
      GET  /health             → {status: "ok", esphome_version}
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = BUILDER_ADDON_DEFAULT_PORT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._external_session = session is not None
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session (if we created it)."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    # -- BuildBackend interface --

    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        session = await self._get_session()
        payload: dict[str, Any] = {
            "device_id": device_id,
            "yaml_content": yaml_content,
            "channel": channel,
        }
        if esphome_version:
            payload["esphome_version"] = esphome_version

        _LOGGER.info("Starting build for %s via Builder App", device_id)
        try:
            async with session.post(
                f"{self._base_url}/build", json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Builder App rejected build: HTTP {resp.status} — {body}"
                    )
                data = await resp.json()
                job_id = data.get("job_id")
                if not job_id:
                    raise BuildError(f"Builder App returned no job_id: {data}")
                _LOGGER.info("Build queued: job_id=%s", job_id)
                return job_id, False
        except aiohttp.ClientError as exc:
            raise BuildError(f"Builder App unreachable: {exc}") from exc

    async def get_status(self, job_id: str) -> BuildStatus:
        session = await self._get_session()
        try:
            async with session.get(
                f"{self._base_url}/build/{job_id}"
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Status query failed: HTTP {resp.status} — {body}"
                    )
                data = await resp.json()
                return BuildStatus(
                    state=BuildState(data.get("status", BUILD_STATE_FAILED)),
                    progress_pct=data.get("progress_pct", 0),
                    logs_tail=data.get("logs_tail", ""),
                    artifact_names=data.get("artifacts", []),
                    error=data.get("error"),
                )
        except aiohttp.ClientError as exc:
            raise BuildError(f"Builder App unreachable: {exc}") from exc

    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        session = await self._get_session()
        url = f"{self._base_url}/build/{job_id}/artifact/{artifact_name}"
        _LOGGER.info("Fetching artifact: %s", url)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Artifact download failed: HTTP {resp.status} — {body}"
                    )
                return await resp.read()
        except aiohttp.ClientError as exc:
            raise BuildError(f"Builder Add-on unreachable: {exc}") from exc

    async def health_check(self) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(f"{self._base_url}/health") as resp:
                if resp.status != 200:
                    raise BuildError(f"Builder Add-on unhealthy: HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise BuildError(f"Builder Add-on unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# Concrete: Developer-only manual backend (NOT for customers)
# ---------------------------------------------------------------------------


class ManualBuildBackend(BuildBackend):
    """Developer-mode fallback: user compiles manually via ESPHome Dashboard.

    This implementation does *not* compile anything automatically.  It records
    the build request and waits for the user to acknowledge completion.
    It MUST NOT be used as the standard customer path (Planner decision
    2026-02-16).

    Usage: activated only when ``input_boolean.pvautonomy_developer_mode`` is
    ``on``.
    """

    _STATUS = "manual_pending"

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        import uuid

        job_id = f"manual-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {
            "device_id": device_id,
            "channel": channel,
            "yaml_content": yaml_content,
        }
        _LOGGER.warning(
            "Manual build requested for %s. Job %s — "
            "DEVELOPER MUST compile via ESPHome Dashboard and acknowledge.",
            device_id,
            job_id,
        )
        return job_id, False

    async def get_status(self, job_id: str) -> BuildStatus:
        if job_id not in self._jobs:
            raise BuildError(f"Unknown job: {job_id}")
        return BuildStatus(
            state=BuildState.QUEUED,
            progress_pct=0,
            logs_tail="⚠️ Waiting for developer to compile manually via ESPHome Dashboard.",
        )

    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        raise BuildError(
            "ManualBuildBackend cannot fetch artifacts — "
            "developer must supply the compiled binary."
        )

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "manual_developer_only"}


# ---------------------------------------------------------------------------
# Concrete: ESPHome Dashboard backend (WP2 — production bridge)
# ---------------------------------------------------------------------------


class EsphomeDashboardBackend(BuildBackend):
    """Compile firmware via the existing ESPHome Dashboard Add-on.

    Uses the Supervisor API + Dashboard's WebSocket/REST endpoints to:
    1. Write generated YAML to /config/esphome/<device>.yaml
    2. Trigger compile via WebSocket (/compile)
    3. Stream compilation logs
    4. Retrieve compiled artifact from shared filesystem or REST endpoint

    Feature-flagged: only active when ``build_backend == "esphome_dashboard"``.
    Per WP2 risk assessment: artifact retrieval is best-effort with truthful
    fallback if not possible.

    Ref: WORK-ITEM-WP2-ESPHOME-DASHBOARD-BACKEND.md
    """

    def __init__(
        self,
        *,
        addon_slug: str = ESPHOME_ADDON_SLUG,
        supervisor_url: str = ESPHOME_SUPERVISOR_URL,
        config_dir: str = ESPHOME_CONFIG_DIR,
        build_dir: str = ESPHOME_BUILD_DIR,
        compile_timeout: float = ESPHOME_COMPILE_TIMEOUT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._addon_slug = addon_slug
        self._supervisor_url = supervisor_url
        self._config_dir = Path(config_dir)
        self._build_dir = Path(build_dir)
        self._compile_timeout = compile_timeout
        self._external_session = session is not None
        self._session = session
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._ingress_token: str | None = None
        self._jobs: dict[str, dict[str, Any]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    def _supervisor_headers(self) -> dict[str, str]:
        """Build HTTP headers for Supervisor API calls."""
        return {"Authorization": f"Bearer {self._supervisor_token}"}

    async def _get_addon_info(self) -> dict[str, Any]:
        """Fetch ESPHome add-on info from Supervisor API."""
        session = await self._get_session()
        url = f"{self._supervisor_url}/addons/{self._addon_slug}/info"
        try:
            async with session.get(url, headers=self._supervisor_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Supervisor API error checking ESPHome add-on: "
                        f"HTTP {resp.status} — {body}"
                    )
                data = await resp.json()
                return data.get("data", {})
        except aiohttp.ClientError as exc:
            raise BuildError(
                f"Supervisor API unreachable: {exc}. "
                "Is this running inside Home Assistant OS/Supervised?"
            ) from exc

    async def _get_ingress_session(self) -> tuple[str, str]:
        """Create an ingress session and return (session_token, ingress_token).

        Returns:
            Tuple of (ingress_session_cookie, ingress_token).
            The ingress_token is the raw token used with the Supervisor's
            ``/ingress/{token}/{path}`` route — NOT the HA Core URL.

        Raises:
            BuildError: If session creation fails.
        """
        session = await self._get_session()

        # Get ingress token from add-on info
        if not self._ingress_token:
            info = await self._get_addon_info()
            ingress_entry = info.get("ingress_entry", "")
            if not ingress_entry:
                raise BuildError(
                    "ESPHome add-on has no ingress entry. "
                    "Ensure the add-on is started and ingress is enabled."
                )
            # ingress_entry is /api/hassio_ingress/{token} (HA Core URL).
            # Extract the raw token for the Supervisor's /ingress/{token} route.
            self._ingress_token = ingress_entry.replace(
                "/api/hassio_ingress/", ""
            )

        # Create ingress session via Supervisor API
        url = f"{self._supervisor_url}/ingress/session"
        try:
            async with session.post(
                url, headers=self._supervisor_headers(), json={}
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Failed to create ingress session: HTTP {resp.status} — {body}. "
                        "The Supervisor may not grant ingress access to this context."
                    )
                data = await resp.json()
                session_token = data.get("data", {}).get("session", "")
                if not session_token:
                    raise BuildError(f"Ingress session response missing token: {data}")
                return session_token, self._ingress_token
        except aiohttp.ClientError as exc:
            raise BuildError(f"Supervisor API unreachable for ingress: {exc}") from exc

    async def _write_yaml_to_esphome(
        self, filename: str, yaml_content: str
    ) -> None:
        """Write YAML config to the ESPHome config directory.

        Uses direct filesystem write since HA Core and ESPHome share /config.
        Falls back to the Dashboard's REST API (/edit) if filesystem is not writable.
        """
        filepath = self._config_dir / filename
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: filepath.parent.mkdir(parents=True, exist_ok=True)
            )
            await loop.run_in_executor(
                None, lambda: filepath.write_text(yaml_content, encoding="utf-8")
            )
            _LOGGER.info(
                "Wrote YAML to ESPHome config: %s (%d bytes)",
                filepath, len(yaml_content),
            )
        except OSError as exc:
            _LOGGER.warning(
                "Direct filesystem write failed (%s), trying REST API", exc
            )
            await self._write_yaml_via_api(filename, yaml_content)

    async def _write_yaml_via_api(
        self, filename: str, yaml_content: str
    ) -> None:
        """Write YAML via ESPHome Dashboard's /edit REST endpoint."""
        try:
            session_token, ingress_token = await self._get_ingress_session()
        except BuildError:
            raise BuildError(
                f"Cannot write YAML to ESPHome: filesystem and API both failed. "
                f"Ensure {self._config_dir} is writable or ingress is available."
            )

        session = await self._get_session()
        url = (
            f"{self._supervisor_url}"
            f"/ingress/{ingress_token}/{ESPHOME_EDIT_PATH}"
            f"?configuration={filename}"
        )
        cookies = {"ingress_session": session_token}
        try:
            async with session.post(
                url, data=yaml_content.encode("utf-8"), cookies=cookies,
                headers={"Content-Type": "application/yaml"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"ESPHome /edit API failed: HTTP {resp.status} — {body}"
                    )
            _LOGGER.info("Wrote YAML via ESPHome API: %s", filename)
        except aiohttp.ClientError as exc:
            raise BuildError(f"ESPHome API unreachable for YAML write: {exc}") from exc

    async def close(self) -> None:
        """Close the HTTP session (if we created it)."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    # -- BuildBackend interface --

    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        """Write YAML and prepare for compile.

        The actual compile is triggered in wait_for_completion() via WebSocket.
        """
        import uuid

        job_id = f"esphome-{uuid.uuid4().hex[:8]}"

        # Derive filename from device_id (e.g., edge101_sph10k_haus_01 → edge101-sph10k-haus-01.device.yaml)
        # Convert underscores to hyphens for ESPHome naming convention
        config_filename = device_id.replace("_", "-") + ".device.yaml"

        # Write YAML to ESPHome config directory
        await self._write_yaml_to_esphome(config_filename, yaml_content)

        self._jobs[job_id] = {
            "device_id": device_id,
            "config_filename": config_filename,
            "channel": channel,
            "state": BuildState.QUEUED,
            "progress": 0,
            "logs": [],
            "error": None,
            "artifact_path": None,
        }

        _LOGGER.info(
            "ESPHome Dashboard build queued: job=%s, device=%s, config=%s",
            job_id, device_id, config_filename,
        )
        return job_id, False

    async def get_status(self, job_id: str) -> BuildStatus:
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown ESPHome Dashboard job: {job_id}")
        return BuildStatus(
            state=job["state"],
            progress_pct=job["progress"],
            logs_tail="\n".join(job["logs"][-30:]),
            artifact_names=(
                ["firmware.ota.bin"] if job["state"] == BuildState.SUCCESS else []
            ),
            error=job.get("error"),
        )

    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        """Fetch compiled firmware binary.

        Strategy:
        1. Try reading from shared filesystem (.esphome/build/<device>/)
        2. Try ESPHome Dashboard REST API (/download.bin)
        3. Truthful fallback error if neither works (WP2 Non-DoD per Planner)
        """
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown ESPHome Dashboard job: {job_id}")

        if job["state"] != BuildState.SUCCESS:
            raise BuildError(
                f"Cannot fetch artifact: build state is {job['state']}"
            )

        config_filename = job["config_filename"]
        device_name = config_filename.replace(".device.yaml", "").replace(".yaml", "")

        # Strategy 1: Direct filesystem read from ESPHome build directory
        firmware_path = self._build_dir / device_name / "firmware.ota.bin"
        if firmware_path.exists():
            try:
                data = firmware_path.read_bytes()
                _LOGGER.info(
                    "Artifact fetched from filesystem: %s (%d bytes)",
                    firmware_path, len(data),
                )
                return data
            except OSError as exc:
                _LOGGER.warning(
                    "Filesystem read failed for %s: %s", firmware_path, exc
                )

        # Strategy 2: Also try without .device suffix
        alt_path = self._build_dir / device_name.replace(".device", "") / "firmware.ota.bin"
        if alt_path.exists():
            try:
                data = alt_path.read_bytes()
                _LOGGER.info(
                    "Artifact fetched from filesystem (alt): %s (%d bytes)",
                    alt_path, len(data),
                )
                return data
            except OSError as exc:
                _LOGGER.warning("Filesystem read failed for %s: %s", alt_path, exc)

        # Strategy 3: Try REST API download
        try:
            data = await self._download_artifact_via_api(config_filename, artifact_name)
            if data:
                return data
        except BuildError as exc:
            _LOGGER.warning("REST API artifact download failed: %s", exc)

        # Truthful fallback (WP2 Planner annotation: artifact retrieval is Non-DoD)
        raise BuildError(
            f"Firmware compiled successfully but artifact retrieval failed. "
            f"Tried filesystem paths: {firmware_path}, {alt_path}. "
            f"The ESPHome Dashboard may store artifacts in a different location. "
            f"You can download the firmware manually from the ESPHome Dashboard UI "
            f"or switch to the Builder Add-on backend for reliable artifact delivery."
        )

    async def _download_artifact_via_api(
        self, config_filename: str, artifact_name: str
    ) -> bytes | None:
        """Try downloading artifact via ESPHome Dashboard REST API."""
        try:
            session_token, ingress_token = await self._get_ingress_session()
        except BuildError:
            return None

        session = await self._get_session()
        url = (
            f"{self._supervisor_url}"
            f"/ingress/{ingress_token}/{ESPHOME_DOWNLOAD_PATH}"
            f"?configuration={config_filename}&file={artifact_name}"
        )
        cookies = {"ingress_session": session_token}
        try:
            async with session.get(url, cookies=cookies) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                _LOGGER.info(
                    "Artifact fetched via REST API: %s (%d bytes)",
                    artifact_name, len(data),
                )
                return data
        except aiohttp.ClientError:
            return None

    async def health_check(self) -> dict[str, Any]:
        """Check if ESPHome Dashboard Add-on is running and healthy.

        Uses Supervisor API to verify add-on state.
        """
        if not self._supervisor_token:
            raise BuildError(
                "SUPERVISOR_TOKEN not available. "
                "The ESPHome Dashboard backend requires Home Assistant OS "
                "or a Supervised installation. "
                "Switch to 'simulated' or 'manual' backend for development."
            )

        info = await self._get_addon_info()
        state = info.get("state", "unknown")
        version = info.get("version", "unknown")

        if state != "started":
            raise BuildError(
                f"ESPHome add-on is not running (state: {state}). "
                "Start the ESPHome add-on from Settings → Add-ons, "
                "or switch to a different build backend."
            )

        return {
            "status": "ok",
            "mode": "esphome_dashboard",
            "addon_slug": self._addon_slug,
            "addon_version": version,
            "addon_state": state,
            "update_available": info.get("update_available", False),
            "ingress_entry": info.get("ingress_entry", ""),
        }

    async def wait_for_completion(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        progress_callback: Any | None = None,
    ) -> BuildStatus:
        """Trigger compile via WebSocket and stream logs until completion.

        The ESPHome Dashboard's /compile endpoint is a WebSocket handler:
        - Client connects and sends: {"configuration": "<filename>"}
        - Server streams log lines as text messages
        - Connection closes when compile finishes
        - Exit code / success is determined from log output
        """
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown ESPHome Dashboard job: {job_id}")

        config_filename = job["config_filename"]
        _timeout = timeout or self._compile_timeout

        job["state"] = BuildState.COMPILING
        job["progress"] = 5
        job["logs"].append(f"Triggering compile for {config_filename}…")

        if progress_callback:
            await progress_callback(await self.get_status(job_id))

        # Try WebSocket compile via Supervisor ingress
        try:
            await self._compile_via_websocket(job_id, config_filename, _timeout, progress_callback)
        except BuildError:
            raise
        except Exception as exc:
            # If WebSocket fails, provide actionable error
            job["state"] = BuildState.FAILED
            job["error"] = (
                f"Compile trigger failed: {exc}. "
                "The ESPHome Dashboard WebSocket API may not be accessible "
                "from this context. Try compiling manually via the ESPHome "
                "Dashboard UI, or switch to the 'simulated' backend for testing."
            )
            job["logs"].append(f"ERROR: {job['error']}")
            _LOGGER.error("ESPHome compile trigger failed: %s", exc)

        return await self.get_status(job_id)

    async def _compile_via_websocket(
        self,
        job_id: str,
        config_filename: str,
        timeout: float,
        progress_callback: Any | None,
    ) -> None:
        """Connect to ESPHome Dashboard WebSocket and run compile."""
        job = self._jobs[job_id]

        try:
            session_token, ingress_token = await self._get_ingress_session()
        except BuildError as exc:
            # Ingress session not available — try direct WebSocket on host
            _LOGGER.warning(
                "Ingress session failed (%s), trying direct WebSocket", exc
            )
            await self._compile_via_direct_ws(job_id, config_filename, timeout, progress_callback)
            return

        session = await self._get_session()

        # Build WebSocket URL via Supervisor ingress proxy
        # The Supervisor routes /ingress/{token}/{path} to the add-on.
        # Note: /api/hassio_ingress/ is an HA Core URL, NOT a Supervisor route.
        ws_url = (
            f"ws://supervisor"
            f"/ingress/{ingress_token}/{ESPHOME_COMPILE_WS_PATH}"
        )
        cookies = {"ingress_session": session_token}

        _LOGGER.info("Connecting to ESPHome compile WebSocket: %s", ws_url)
        job["logs"].append("Connecting to ESPHome Dashboard…")

        await self._run_ws_compile(
            session, ws_url, cookies, config_filename,
            job_id, timeout, progress_callback,
        )

    async def _compile_via_direct_ws(
        self,
        job_id: str,
        config_filename: str,
        timeout: float,
        progress_callback: Any | None,
    ) -> None:
        """Fallback: try direct WebSocket to ESPHome add-on."""
        job = self._jobs[job_id]

        # Get add-on info to find ingress port
        info = await self._get_addon_info()
        ingress_port = info.get("ingress_port")
        if not ingress_port:
            raise BuildError("ESPHome add-on ingress port not available")

        session = await self._get_session()
        ws_url = f"ws://localhost:{ingress_port}/{ESPHOME_COMPILE_WS_PATH}"

        _LOGGER.info(
            "Trying direct WebSocket to ESPHome (port %s): %s",
            ingress_port, ws_url,
        )
        job["logs"].append(f"Connecting directly to ESPHome (port {ingress_port})…")

        await self._run_ws_compile(
            session, ws_url, {}, config_filename,
            job_id, timeout, progress_callback,
        )

    async def _run_ws_compile(
        self,
        session: aiohttp.ClientSession,
        ws_url: str,
        cookies: dict[str, str],
        config_filename: str,
        job_id: str,
        timeout: float,
        progress_callback: Any | None,
    ) -> None:
        """Execute the WebSocket compile protocol.

        Protocol (ESPHome Dashboard EsphomeCommandWebSocket):
        1. Connect WebSocket
        2. Send JSON: {"configuration": "<filename>", "type": "spawn"}
        3. Receive text messages (log lines) with progress
        4. Receive JSON: {"event": "exit", "code": 0|1}
        5. Connection closes
        """
        job = self._jobs[job_id]
        compile_start = asyncio.get_event_loop().time()

        # Pass ingress_session as Cookie header — ws_connect() does not
        # support the cookies= parameter in all aiohttp versions.
        ws_headers = {}
        if cookies and "ingress_session" in cookies:
            ws_headers["Cookie"] = f"ingress_session={cookies['ingress_session']}"

        try:
            async with session.ws_connect(
                ws_url,
                timeout=aiohttp.ClientWSTimeout(ws_close=timeout),
                headers=ws_headers or None,
            ) as ws:
                # Send compile request
                msg = json.dumps({
                    "type": "spawn",
                    "configuration": config_filename,
                })
                await ws.send_str(msg)
                _LOGGER.info("Compile request sent for %s", config_filename)
                job["logs"].append(f"Compile started for {config_filename}")
                job["progress"] = 10

                if progress_callback:
                    await progress_callback(await self.get_status(job_id))

                # Read streaming output
                line_count = 0
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        text = ws_msg.data.strip()
                        if not text:
                            continue

                        # Check for exit event (JSON); non-JSON lines fall through as log lines.
                        with contextlib.suppress(json.JSONDecodeError, TypeError):
                            event_data = json.loads(text)
                            if event_data.get("event") == "exit":
                                exit_code = event_data.get("code", -1)
                                if exit_code == 0:
                                    job["state"] = BuildState.SUCCESS
                                    job["progress"] = 100
                                    job["logs"].append("Compile finished successfully.")
                                    _LOGGER.info(
                                        "ESPHome compile SUCCESS for %s (%.1fs)",
                                        config_filename,
                                        asyncio.get_event_loop().time() - compile_start,
                                    )
                                else:
                                    job["state"] = BuildState.FAILED
                                    job["error"] = (
                                        f"Compile failed with exit code {exit_code}. "
                                        "Check the logs above for details."
                                    )
                                    job["logs"].append(
                                        f"FATAL: Compile failed (exit code {exit_code})"
                                    )
                                    _LOGGER.error(
                                        "ESPHome compile FAILED for %s (exit %d)",
                                        config_filename, exit_code,
                                    )
                                break
                            continue

                        # Regular log line
                        job["logs"].append(text)
                        line_count += 1

                        # Estimate progress from log patterns
                        progress = self._estimate_progress(text, line_count)
                        if progress > job["progress"]:
                            job["progress"] = progress

                        if progress_callback and line_count % 5 == 0:
                            await progress_callback(await self.get_status(job_id))

                    elif ws_msg.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break

                    # Check timeout
                    elapsed = asyncio.get_event_loop().time() - compile_start
                    if elapsed > timeout:
                        job["state"] = BuildState.FAILED
                        job["error"] = (
                            f"Compile timed out after {timeout}s. "
                            "The build may still be running in the ESPHome add-on. "
                            "Check the ESPHome Dashboard for status."
                        )
                        job["logs"].append(f"TIMEOUT after {timeout}s")
                        _LOGGER.error(
                            "ESPHome compile TIMEOUT for %s after %.0fs",
                            config_filename, timeout,
                        )
                        break

                # If we exited the loop without setting a terminal state
                if not job["state"].is_terminal if hasattr(job["state"], "is_terminal") else job["state"] not in (BuildState.SUCCESS, BuildState.FAILED):
                    # WebSocket closed without exit event — assume failure
                    job["state"] = BuildState.FAILED
                    job["error"] = (
                        "WebSocket connection closed unexpectedly without exit event. "
                        "The compile may have been interrupted."
                    )
                    job["logs"].append("WARNING: Connection closed without result")

        except aiohttp.ClientError as exc:
            job["state"] = BuildState.FAILED
            job["error"] = (
                f"WebSocket connection failed: {exc}. "
                "Ensure the ESPHome add-on is running and accessible."
            )
            job["logs"].append(f"ERROR: WebSocket connection failed: {exc}")
            raise BuildError(job["error"]) from exc

        if progress_callback:
            await progress_callback(await self.get_status(job_id))

    @staticmethod
    def _estimate_progress(log_line: str, line_count: int) -> int:
        """Estimate compile progress from log output patterns.

        Returns progress percentage (10-95). Start at 10, finish set to 100 by caller.
        """
        line_lower = log_line.lower()

        # Known ESPHome compile phases
        if "resolving" in line_lower or "reading" in line_lower:
            return 15
        if "generating c++" in line_lower:
            return 20
        if "compiling" in line_lower and "esp-idf" in line_lower:
            return 30
        if "compiling" in line_lower:
            return max(35, min(35 + line_count // 10, 70))
        if "linking" in line_lower:
            return 75
        if "building firmware" in line_lower or "creating" in line_lower:
            return 80
        if "successfully" in line_lower:
            return 95
        if "creating esp32" in line_lower or "image" in line_lower:
            return 85

        # Fallback: increment slowly based on line count
        return min(10 + line_count // 5, 90)


# ---------------------------------------------------------------------------
# Concrete: Simulated build backend (WP1 — E2E test double)
# ---------------------------------------------------------------------------


class SimulatedBuildBackend(BuildBackend):
    """Deterministic test double that simulates a full build cycle.

    Produces realistic progress/logs and a dummy artifact so the wizard
    (P3-13-001) can be tested end-to-end without a real Builder Add-on.

    The UI MUST clearly display **TEST MODE — simulated build** when this
    backend is active.

    Failure modes (configurable via Options Flow ``simulated_failure_mode``):
    - ``none``              — happy path, build succeeds
    - ``fail_compile``      — build fails mid-compile with error
    - ``timeout``           — build stays "compiling" forever (until caller times out)
    - ``missing_artifact``  — build "succeeds" but artifact fetch raises BuildError

    Ref: WORK-ITEM-WP1, FR2–FR5.
    """

    # Simulated log lines emitted at each progress step
    _SIM_LOGS: list[tuple[int, str]] = [
        (0, "Starting compile for {device_id}…"),
        (10, "INFO Reading YAML configuration…"),
        (20, "INFO Resolving component dependencies…"),
        (35, "INFO Compiling ESP-IDF framework…"),
        (50, "INFO Compiling ESPHome components…"),
        (70, "INFO Linking firmware binary…"),
        (85, "INFO Generating OTA partition image…"),
        (95, "INFO Writing firmware.ota.bin…"),
        (100, "INFO Compile finished successfully."),
    ]

    _SIM_FAIL_LOGS: list[tuple[int, str]] = [
        (0, "Starting compile for {device_id}…"),
        (10, "INFO Reading YAML configuration…"),
        (20, "INFO Resolving component dependencies…"),
        (35, "INFO Compiling ESP-IDF framework…"),
        (45, "ERROR src/main.cpp:42: undefined reference to 'missing_symbol'"),
        (45, "FATAL Compile failed with 1 error(s)."),
    ]

    def __init__(
        self,
        *,
        failure_mode: str = SIMULATED_FAILURE_NONE,
        build_duration_s: float = SIMULATED_BUILD_DURATION_S,
        artifact_size: int = SIMULATED_ARTIFACT_SIZE,
    ) -> None:
        """Initialize SimulatedBuildBackend.

        Args:
            failure_mode: One of SIMULATED_FAILURE_* constants.
            build_duration_s: Total simulated compile wall-clock time.
            artifact_size: Size of the dummy artifact in bytes.
        """
        self._failure_mode = failure_mode
        self._build_duration_s = build_duration_s
        self._artifact_size = artifact_size
        self._jobs: dict[str, dict[str, Any]] = {}

    @property
    def failure_mode(self) -> str:
        """Current failure mode."""
        return self._failure_mode

    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        import uuid

        job_id = f"sim-{uuid.uuid4().hex[:8]}"
        self._jobs[job_id] = {
            "device_id": device_id,
            "channel": channel,
            "started": True,
            "progress": 0,
            "state": BuildState.QUEUED,
            "logs": [],
        }
        _LOGGER.info(
            "[SIMULATED] Build queued: job_id=%s, device=%s, failure_mode=%s",
            job_id, device_id, self._failure_mode,
        )
        return job_id, False

    async def get_status(self, job_id: str) -> BuildStatus:
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown simulated job: {job_id}")
        return BuildStatus(
            state=job["state"],
            progress_pct=job["progress"],
            logs_tail="\n".join(job["logs"][-10:]),
            artifact_names=["firmware.ota.bin"] if job["state"] == BuildState.SUCCESS else [],
            error=job.get("error"),
        )

    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown simulated job: {job_id}")

        if self._failure_mode == SIMULATED_FAILURE_MISSING_ARTIFACT:
            raise BuildError(
                "[SIMULATED] Artifact not available — failure mode: missing_artifact. "
                "The build reported success but the artifact store is empty. "
                "In production this indicates a Builder Add-on storage error."
            )

        if job["state"] != BuildState.SUCCESS:
            raise BuildError(
                f"[SIMULATED] Cannot fetch artifact: build state is {job['state']}"
            )

        # Generate deterministic dummy firmware binary
        device_id = job["device_id"]
        header = f"SIMULATED_OTA|{device_id}|{artifact_name}|".encode("utf-8")
        padding = os.urandom(max(0, self._artifact_size - len(header)))
        _LOGGER.info(
            "[SIMULATED] Artifact fetched: %s (%d bytes, DUMMY — not real firmware)",
            artifact_name, len(header) + len(padding),
        )
        return header + padding

    async def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "simulated",
            "failure_mode": self._failure_mode,
            "build_duration_s": self._build_duration_s,
            "artifact_size": self._artifact_size,
        }

    async def wait_for_completion(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        progress_callback: Any | None = None,
    ) -> BuildStatus:
        """Simulate a deterministic build with realistic timing.

        Overrides the base polling loop to produce controlled progress steps.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise BuildError(f"Unknown simulated job: {job_id}")

        device_id = job["device_id"]

        # Select log sequence based on failure mode
        if self._failure_mode == SIMULATED_FAILURE_COMPILE:
            log_steps = self._SIM_FAIL_LOGS
        else:
            log_steps = self._SIM_LOGS

        # Timeout mode: stay compiling until caller times out
        if self._failure_mode == SIMULATED_FAILURE_TIMEOUT:
            job["state"] = BuildState.COMPILING
            job["progress"] = 35
            job["logs"].append(f"Starting compile for {device_id}…")
            job["logs"].append("INFO Reading YAML configuration…")
            job["logs"].append("INFO Resolving component dependencies…")
            job["logs"].append("INFO Compiling ESP-IDF framework… (stalled)")

            if progress_callback:
                await progress_callback(await self.get_status(job_id))

            _LOGGER.warning(
                "[SIMULATED] Timeout mode active — build will stall until timeout"
            )
            # Delegate to base class polling (will eventually hit timeout)
            return await super().wait_for_completion(
                job_id,
                timeout=timeout,
                poll_interval=poll_interval,
                progress_callback=progress_callback,
            )

        # Normal / fail_compile / missing_artifact: step through logs
        n_steps = len(log_steps)
        step_delay = self._build_duration_s / max(n_steps, 1)

        job["state"] = BuildState.COMPILING

        for pct, log_line in log_steps:
            formatted = log_line.format(device_id=device_id)
            job["progress"] = pct
            job["logs"].append(formatted)

            if progress_callback:
                await progress_callback(await self.get_status(job_id))

            await asyncio.sleep(step_delay)

        # Determine final state
        if self._failure_mode == SIMULATED_FAILURE_COMPILE:
            job["state"] = BuildState.FAILED
            job["error"] = "Compile error: undefined reference to 'missing_symbol'"
            _LOGGER.warning("[SIMULATED] Build FAILED (failure_mode=fail_compile)")
        else:
            # SUCCESS (including missing_artifact — artifact fetch will fail later)
            job["state"] = BuildState.SUCCESS
            job["progress"] = 100
            _LOGGER.info("[SIMULATED] Build SUCCESS for %s", device_id)

        return await self.get_status(job_id)


# ---------------------------------------------------------------------------
# Concrete: Proxy Remote Build Backend (EPIC-005-D1)
# ---------------------------------------------------------------------------

INVERTERS_PREFIX = "inverters/"


def _ensure_inverters_prefix(registry_file: str) -> str:
    """Ensure registry_file has the inverters/ prefix for the GitHub workflow."""
    if not registry_file:
        return registry_file
    if registry_file.startswith(INVERTERS_PREFIX):
        return registry_file
    return f"{INVERTERS_PREFIX}{registry_file}"


# TASK-20260520 Phase 2 (A′): AES-256 key is exactly 64 hex chars (32 bytes).
_COMPILE_SECRET_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _encrypt_compile_secrets(plaintext: str, key_hex: str) -> str:
    """AES-256-GCM-encrypt the legacy compile-secret payload for the workflow.

    Produces ``base64( nonce[12] || ciphertext || tag )`` — byte-compatible
    with the inverter-registry workflow decrypt:
        ``AESGCM(bytes.fromhex(key)).decrypt(blob[:12], blob[12:], None)``
    (the cryptography library appends the GCM tag to the ciphertext, so
    ``nonce + AESGCM.encrypt(...)`` is exactly ``nonce ‖ ciphertext ‖ tag``).

    The caller is responsible for validating ``key_hex`` and failing closed
    when it is missing/invalid; this helper assumes a 64-hex key. Neither
    the key nor the plaintext is logged.
    """
    key = bytes.fromhex(key_hex)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


class ProxyRemoteBuildBackend(BuildBackend):
    """Remote build backend that delegates to the PVAutonomy Proxy (Cloudflare Worker).

    The proxy dispatches GitHub Actions builds and streams artifacts from the
    private ``inverter-registry`` repo.  Customer HA systems never hold a
    GitHub PAT — all auth goes through the proxy API key.

    API contract (pvautonomy-proxy):
      POST /build              → {build_id, status, ...}
      GET  /build/<id>         → {build_id, status, progress, artifact, ...}
      GET  /build/<id>/artifact/<name> → binary stream
      GET  /health             → {status: "ok", ...}

    Ref: EPIC-005-D1, keen-booping-aho.md
    """

    def __init__(
        self,
        *,
        base_url: str = PROXY_DEFAULT_BASE_URL,
        api_key: str = "",
        customer_id: str = "",
        entry_id: str = "",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # customer_id from /whoami or user input; never fall back to entry_id
        # (entry_id is an internal HA UUID, not a valid proxy customer identifier)
        self._customer_id = customer_id
        self._external_session = session is not None
        self._session = session
        # Per-build artifact info, populated by get_status() on success
        self._artifact_info: dict[str, dict[str, Any]] = {}
        # Build context set by pipeline before start_build()
        self._build_context: dict[str, Any] = {}
        # SEC-010: OTA required flag for proxy builds
        self._ota_required: bool = False
        # EPIC-004 build_firmware service: when True the proxy artifact cache
        # is bypassed by omitting payload.yaml_hash so the proxy's existing
        # missing-hash cold-build path applies (registry-only update scenario).
        self._force_rebuild: bool = False
        # EPIC-006-D2: track which builds already had a refresh=1 attempt
        self._refresh_attempted: set[str] = set()
        # EPIC-006-WP3-D2: auto-refresh gating (can be disabled via Options Flow)
        self._auto_refresh_enabled: bool = True
        # EPIC-006 PR-3: HPKE compile_secret_envelope (Phase-2 opt-in).
        # Default is the legacy plaintext-secrets-then-proxy-encrypts path.
        # Envelope mode activates only when set_envelope_mode(enabled=True)
        # has been called with a usable keyset cache and root anchors.
        self._envelope_mode_enabled: bool = False
        self._envelope_keyset_cache: BuildBackendKeysetCache | None = None
        self._envelope_root_pubkeys: Mapping[str, bytes] | None = None
        # TASK-20260520 Phase 2 (A′): repo-wide AES-256 compile-secret key
        # (64 hex chars), resolved by the pipeline from PVAutonomyKeyring and
        # set before start_build(). Used to encrypt the legacy
        # `encrypted_secrets` payload so the workflow can decrypt it. None
        # until provisioned; start_build() fails closed if secrets must be
        # sent without it.
        self._compile_secret_key: str | None = None

    def set_compile_secret_key(self, key_hex: str | None) -> None:
        """Provide the repo-wide AES-256 compile-secret key (64 hex chars).

        Called by the pipeline with the value resolved from
        PVAutonomyKeyring.get_compile_secret_key(). Passing None (no key
        provisioned) is allowed; start_build() will fail closed only if the
        build actually needs to send compile secrets. The key is never
        logged.
        """
        self._compile_secret_key = key_hex

    def set_auto_refresh(self, enabled: bool) -> None:
        """Enable or disable D2 auto-refresh on terminal-no-artifact states.

        When disabled, the proxy will NOT retry with ?refresh=1 on timeout/failed
        builds. Useful for debugging or when refresh causes unwanted side effects.

        EPIC-006-WP3, Deliverable E.
        """
        self._auto_refresh_enabled = enabled

    def set_build_context(self, **kwargs: Any) -> None:
        """Set proxy-specific build parameters before start_build().

        Called by pipeline.py to pass context that doesn't fit the
        BuildBackend.start_build() ABC signature.

        Expected kwargs:
            device_key: 6-char hex MAC suffix (from canonical_mac_last6)
            model: **hardware** platform model (e.g. "edge101"), NOT inverter
                   slug. The Proxy uses this as compile target / board family.
                   Inverter identity is carried by registry_file + device_name.
            build_profile: "production" | "factory"
            registry_file: path in inverter-registry (e.g. "growatt/sph/sph10k.json")
            device_name: ESPHome node name (e.g. "sph10k-haus-03")
        """
        self._build_context = dict(kwargs)

    def set_ota_required(self, required: bool = True) -> None:
        """Flag that this build requires OTA password (SEC-010).

        The proxy forwards this flag to GHA, which reads the actual password
        from its own Repository Secret.  No secret crosses trust boundaries.
        """
        self._ota_required = required

    def set_force_rebuild(self, force: bool = True) -> None:
        """Request a proxy artifact-cache bypass for this build (EPIC-004).

        When set, ``start_build()`` omits ``payload.yaml_hash`` so the proxy's
        existing "missing yaml hash → cold build" path applies and a stale
        cached firmware artifact cannot be returned.  This is required for
        registry-only update scenarios where the local generated YAML is
        unchanged but the external inverter-registry has new content.

        The generated YAML bytes themselves are not modified — only the
        cache-lookup key is suppressed.
        """
        self._force_rebuild = bool(force)

    def set_envelope_mode(
        self,
        *,
        enabled: bool,
        keyset_cache: BuildBackendKeysetCache | None = None,
        root_pubkeys: Mapping[str, bytes] | None = None,
    ) -> None:
        """Enable / disable Phase-2 ``compile_secret_envelope`` mode.

        Envelope mode activates only when ALL of these hold at start_build():
        * ``enabled=True``;
        * ``keyset_cache`` is a :class:`BuildBackendKeysetCache`;
        * ``root_pubkeys`` is a non-empty mapping of pinned Ed25519 roots
          (defaults to :data:`ROOT_PUBKEYS_PINNED`, which is empty in this
          PR — production envelope mode therefore stays unavailable until a
          Judge-approved root-key ceremony task adds an anchor);
        * the inbound build is on the ``yaml_authority`` build_contract;
        * a usable signed keyset is reachable (or 404/405 falls back legacy).

        Anything else falls back to legacy or fails closed per ADR §6.1.
        """
        self._envelope_mode_enabled = bool(enabled)
        self._envelope_keyset_cache = keyset_cache
        self._envelope_root_pubkeys = (
            root_pubkeys if root_pubkeys is not None else ROOT_PUBKEYS_PINNED
        )

    @property
    def envelope_mode_active(self) -> bool:
        """True only when the runtime is wired to seal an envelope.

        Reads false until set_envelope_mode(enabled=True) is called with both
        a cache and at least one pinned root anchor. The build path also has
        to be ``yaml_authority`` and the proxy has to support the keyset
        endpoint; that is checked at start_build() time.
        """
        return (
            self._envelope_mode_enabled
            and self._envelope_keyset_cache is not None
            and bool(self._envelope_root_pubkeys)
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(connect=10, total=PROXY_API_TIMEOUT),
            )
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # ------------------------------------------------------------------
    # EPIC-006 PR-3: HPKE compile_secret_envelope (Phase-2 opt-in)
    # ------------------------------------------------------------------

    async def _maybe_attach_envelope(
        self,
        *,
        payload: dict[str, Any],
        yaml_content: str,
        yaml_hash: str,
        secrets_payload: str,
        ctx: Mapping[str, Any],
    ) -> bool:
        """If envelope mode is active and the build is envelope-eligible,
        seal an HPKE envelope onto ``payload`` and return True.

        Returns False to mean "fall back to legacy plaintext path" — that
        decision is taken ONLY for the safe conditions enumerated below.
        Any unsafe failure (invalid keyset, seal error, missing required
        compile secret) raises :class:`BuildError` and aborts before the
        request goes out, per ADR §6.1.

        Fallback to legacy is permitted only when:
        * envelope mode is not enabled (Phase 1, default), OR
        * no usable cache / no real root anchors are configured, OR
        * the build is not on ``yaml_authority``, OR
        * the proxy's ``/build-backend/keys`` endpoint returns 404 / 405.
        """
        if not self.envelope_mode_active:
            return False

        build_contract = ctx.get("build_contract")
        if build_contract != BUILD_CONTRACT_YAML_AUTHORITY:
            # ADR §6.3.1: only yaml_authority can produce a finalized
            # yaml_hash that the runner can re-derive byte-for-byte.
            # Other paths use legacy without envelope; not a fail-closed
            # case because envelope was never required for them.
            return False

        if not yaml_content or not yaml_hash:
            return False

        if not secrets_payload:
            # Envelope only protects compile secrets — without them the
            # legacy plaintext path is also a no-op. Fall through to
            # legacy (which will also emit nothing).
            return False

        device_key = ctx.get("device_key", "")
        if not device_key:
            raise BuildError(
                "envelope_mode_requires_device_key: build context missing device_key"
            )

        compile_secrets: Mapping[str, str] = ctx.get("compile_secrets", {})
        api_key_name = f"edge101_api_key_{device_key}"
        if api_key_name not in compile_secrets or not compile_secrets[api_key_name]:
            raise BuildError(
                "envelope_mode_missing_api_key: "
                f"compile secrets must contain {api_key_name}"
            )
        if self._ota_required:
            ota_name = f"edge101_ota_password_{device_key}"
            if ota_name not in compile_secrets or not compile_secrets[ota_name]:
                raise BuildError(
                    "envelope_mode_missing_ota_password: "
                    f"compile secrets must contain {ota_name} when ota_required"
                )

        registry_file = _ensure_inverters_prefix(ctx.get("registry_file", ""))
        device_name = ctx.get("device_name", "")
        build_profile = ctx.get("build_profile", "production")

        cache = self._envelope_keyset_cache
        assert cache is not None  # guarded by envelope_mode_active

        session = await self._get_session()
        try:
            verified = await load_or_refresh_keyset(
                session=session,
                cache=cache,
                base_url=self._base_url,
                api_key=self._api_key,
                root_pubkeys=self._envelope_root_pubkeys,
            )
        except KeysetEndpointUnsupported:
            # The single legitimate fallback condition (ADR §6.1).
            _LOGGER.info(
                "Build-backend keys endpoint unsupported; using legacy path"
            )
            return False
        except KeysetVerificationError as exc:
            # Endpoint reachable but keyset bad: fail-closed, no fallback.
            raise BuildError(
                f"envelope_invalid_keyset: code={exc.code}"
            ) from exc

        try:
            sealed: SealedEnvelope = seal_compile_secret_envelope(
                secrets_payload.encode("utf-8"),
                active_key=verified.active_key,
                keyset_serial=verified.keyset_serial,
                context=EnvelopeRequestContext(
                    build_profile=build_profile,
                    registry_file=registry_file,
                    device_name=device_name,
                    device_key=device_key,
                    yaml_hash=yaml_hash,
                ),
            )
        except EnvelopeSealError as exc:
            # ADR §6.1: HA-side seal error must abort, never fall back.
            raise BuildError(
                "envelope_seal_failed: aborting before /build POST"
            ) from exc

        # Emit envelope path. Never carry both fields.
        payload["payload"].pop("encrypted_secrets", None)
        payload["payload"].pop("secret_context_hash", None)
        # Wire contract: compile_secret_envelope rides as a JSON *string*,
        # not a nested object — the proxy validates it as a string and the
        # GitHub Actions dispatch can only carry string inputs. Serialize
        # deterministically (sorted keys, compact separators) so the bytes
        # are stable for hashing/auditing across runs.
        payload["payload"]["compile_secret_envelope"] = json.dumps(
            sealed.payload, sort_keys=True, separators=(",", ":")
        )
        payload["payload"]["build_contract"] = BUILD_CONTRACT_YAML_AUTHORITY
        # ADR §6.3.1: yaml_authority requires HA to ship the byte-stable
        # YAML so the runner can hash exactly what it will compile.
        payload["payload"]["yaml_content"] = base64.b64encode(
            yaml_content.encode("utf-8")
        ).decode("ascii")

        # Audit trail — log-safe values only.
        _LOGGER.info(
            "Sealed compile_secret_envelope: key_id=%s keyset_serial=%s fingerprint=%s",
            sealed.key_id, sealed.keyset_serial, sealed.envelope_fingerprint,
        )
        try:
            await cache.async_record_envelope_used(
                key_id=sealed.key_id,
                keyset_serial=sealed.keyset_serial,
            )
        except Exception:  # pragma: no cover - audit save is best-effort
            _LOGGER.debug("Failed to persist envelope audit record", exc_info=True)

        return True

    async def close(self) -> None:
        """Close the HTTP session (if we created it)."""
        if not self._external_session and self._session and not self._session.closed:
            await self._session.close()

    # -- BuildBackend interface --

    async def start_build(
        self,
        device_id: str,
        yaml_content: str,
        *,
        channel: str = "stable",
        esphome_version: str | None = None,
    ) -> tuple[str, bool]:
        """Submit a build request to the proxy.

        The proxy forwards to GitHub Actions. The wire shape of the request
        is selected by the combination of ``build_contract`` (from
        :meth:`set_build_context`) and envelope readiness:

        * **Legacy path** (``build_contract`` is empty/registry):
          ``payload.yaml_content`` is NOT sent — the GHA workflow reads
          from the inverter-registry. ``payload.yaml_hash`` is sent as a
          cache key, and compile secrets ride in
          ``payload.encrypted_secrets`` — AES-256-GCM ciphertext (see the
          yaml_authority bullet for the encryption detail).
        * **Non-envelope yaml_authority path** (EPIC-006-B7, default for
          customer proxy builds; envelope mode off or unsupported but
          ``build_contract == "yaml_authority"``): HA ships base64-encoded
          ``payload.yaml_content`` plus ``payload.build_contract`` plus
          ``payload.yaml_hash`` so the GHA runner compiles exactly the
          bytes HA generated and fails closed on sha256 mismatch (per
          inverter-registry build-firmware-on-demand.yml). Compile
          secrets ride in ``payload.encrypted_secrets`` as
          ``base64(nonce[12] ‖ ciphertext ‖ tag)`` — AES-256-GCM-encrypted
          HA-side (TASK-20260520 Phase 2 A′) with the repo-wide
          COMPILE_SECRET_KEY resolved from PVAutonomyKeyring; the proxy
          forwards it unchanged and the workflow decrypts. If no valid key
          is provisioned, start_build() raises BuildError before the POST
          (never sends plaintext). The YAML source-of-truth and the
          secret-transport are orthogonal.
        * **Envelope path** (EPIC-006 PR-3, ``yaml_authority`` plus active
          envelope mode plus reachable signed keyset): same yaml_authority
          fields as above, but compile secrets are sealed into
          ``payload.compile_secret_envelope`` instead of
          ``payload.encrypted_secrets``. Both secret paths never coexist.

        ``force_rebuild`` suppresses ``payload.yaml_hash`` to defeat the
        proxy build cache; in that case the non-envelope yaml_authority
        extras are also suppressed (proxy/workflow require yaml_hash with
        yaml_authority), so the request degrades to the legacy path for
        that single build.

        Build context from ``set_build_context()`` provides the proxy payload.

        Returns:
            Tuple of (build_id, cache_hit). cache_hit is True when the proxy
            returned status="cached" (EPIC-007 build cache).
        """
        session = await self._get_session()

        # Build proxy payload from context
        ctx = self._build_context
        payload: dict[str, Any] = {
            "customer_id": self._customer_id,
            "device_key": ctx.get("device_key", device_id),
            "model": ctx.get("model", "edge101"),
            "build_profile": ctx.get("build_profile", "production"),
            "payload": {
                # GitHub workflow expects inverters/ prefix on registry paths
                "registry_file": _ensure_inverters_prefix(ctx.get("registry_file", "")),
                "device_name": ctx.get("device_name", device_id),
            },
        }

        if esphome_version:
            payload["payload"]["version"] = esphome_version

        # EPIC-006: Content-addressed cache key — hash the final generated YAML
        # so registry/template changes invalidate the proxy build cache.
        #
        # EPIC-004: when force_rebuild was requested, the yaml_hash is
        # intentionally suppressed so the proxy treats this as a cold build
        # and cannot serve a stale cached artifact (registry-only update).
        yaml_hash = ""
        if yaml_content and not self._force_rebuild:
            yaml_hash = hashlib.sha256(yaml_content.encode()).hexdigest()
            payload["payload"]["yaml_hash"] = yaml_hash
        elif self._force_rebuild:
            _LOGGER.info(
                "Proxy build cache bypass (force_rebuild): yaml_hash omitted "
                "from /build payload — proxy will cold-build"
            )

        # SEC-010: Flag that build requires OTA authentication
        if self._ota_required:
            payload["payload"]["ota_required"] = True

        # EPIC-011: Forward device_key (MAC suffix) for GHA context.
        device_key = ctx.get("device_key", "")
        if device_key:
            payload["payload"]["device_key"] = device_key

        # EPIC-011: Send per-build compile secrets (api encryption key).
        # Each build gets exactly the secrets it needs — no bulk secret
        # store on GHA.  The proxy encrypts before dispatching to GHA.
        # Format: "key1=value1\nkey2=value2" (newline-separated key=value).
        compile_secrets: dict[str, str] = ctx.get("compile_secrets", {})
        secrets_payload = ""
        if compile_secrets:
            secrets_payload = "\n".join(
                f"{k}={v}" for k, v in sorted(compile_secrets.items())
            )

        # EPIC-006 PR-3: emit ONE secret path — never both. Legacy is the
        # default; envelope only kicks in when all preconditions are met.
        envelope_emitted = await self._maybe_attach_envelope(
            payload=payload,
            yaml_content=yaml_content,
            yaml_hash=yaml_hash,
            secrets_payload=secrets_payload,
            ctx=ctx,
        )
        if not envelope_emitted and secrets_payload:
            # TASK-20260520 Phase 2 (A′): encrypt the legacy compile-secret
            # payload HA-side with AES-256-GCM before it leaves HA. The proxy
            # forwards `encrypted_secrets` unchanged; the workflow decrypts
            # with the matching COMPILE_SECRET_KEY. Fail closed if no valid
            # 64-hex key is provisioned — never emit plaintext compile
            # secrets onto the wire.
            key_hex = self._compile_secret_key
            if not key_hex or not _COMPILE_SECRET_KEY_RE.match(key_hex):
                raise BuildError(
                    "compile_secret_key_missing_or_invalid: a 64-hex AES-256 "
                    "COMPILE_SECRET_KEY must be provisioned in the keyring "
                    "(matching the GitHub Actions repo secret) before "
                    "encrypted compile secrets can be sent. Aborting before "
                    "the /build POST so no plaintext secret is transmitted."
                )
            payload["payload"]["encrypted_secrets"] = _encrypt_compile_secrets(
                secrets_payload, key_hex
            )
            # secret_context_hash stays sha256(PLAINTEXT secrets_payload):
            # it is a cache-invalidation key (not raw material), unchanged by
            # the encryption — same value HA/proxy already use for caching.
            payload["payload"]["secret_context_hash"] = (
                hashlib.sha256(secrets_payload.encode()).hexdigest()
            )

        # EPIC-006-B7: non-envelope ``yaml_authority`` path.
        # When the envelope wasn't sealed (envelope mode off, no root pin,
        # or keyset endpoint unsupported) but the caller asked for
        # ``yaml_authority``, still ship the byte-stable YAML and hash so
        # the GHA runner compiles HA's bytes and verifies sha256 before
        # compile (inverter-registry build-firmware-on-demand.yml
        # ``yaml_authority`` branch, fail-closed on hash mismatch).
        #
        # Both yaml_content and yaml_hash must be present — the proxy
        # rejects yaml_authority requests missing either field (see
        # pvautonomy-proxy src/guards/validation.ts EPIC-006-B7 guards).
        # ``force_rebuild`` suppresses yaml_hash above; in that case we
        # also suppress the build_contract/yaml_content extras so the
        # request degrades cleanly to the legacy registry-regeneration
        # path and the proxy cache-bypass intent is honored.
        if (
            not envelope_emitted
            and ctx.get("build_contract") == BUILD_CONTRACT_YAML_AUTHORITY
            and yaml_content
            and yaml_hash
        ):
            payload["payload"]["build_contract"] = BUILD_CONTRACT_YAML_AUTHORITY
            payload["payload"]["yaml_content"] = base64.b64encode(
                yaml_content.encode("utf-8")
            ).decode("ascii")
        elif (
            not envelope_emitted
            and ctx.get("build_contract") == BUILD_CONTRACT_YAML_AUTHORITY
            and self._force_rebuild
        ):
            _LOGGER.info(
                "force_rebuild requested with build_contract=yaml_authority; "
                "yaml_authority extras suppressed so proxy cache-bypass "
                "(missing yaml_hash) keeps working — workflow will fall back "
                "to registry-regeneration for this build only"
            )

        _LOGGER.info(
            "Starting proxy build for %s (customer=%s, ota_required=%s, envelope=%s)",
            device_id, self._customer_id, self._ota_required, envelope_emitted,
        )
        try:
            async with session.post(
                f"{self._base_url}/build",
                json=payload,
                headers=self._auth_headers(),
            ) as resp:
                body = await resp.text()
                if resp.status == 401:
                    raise BuildError(
                        "Proxy API key is invalid. "
                        "Check Settings > Integrations > PVAutonomy > Configure."
                    )
                if resp.status == 403:
                    raise BuildError(
                        "Customer ID does not match API key."
                    )
                if resp.status == 409:
                    raise BuildError(
                        "Build already in progress. Wait for current build to finish."
                    )
                if resp.status == 429:
                    raise BuildError(
                        "Daily build limit exceeded. Try again tomorrow."
                    )
                if resp.status not in (200, 201):
                    raise BuildError(
                        f"Proxy rejected build: HTTP {resp.status} — {body}"
                    )
                data = json.loads(body)
                build_id = data.get("build_id")
                if not build_id:
                    raise BuildError(f"Proxy returned no build_id: {data}")
                cache_hit = data.get("status") == "cached"
                if cache_hit:
                    _LOGGER.info(
                        "Proxy build cache HIT: build_id=%s", build_id
                    )
                else:
                    _LOGGER.info(
                        "Proxy build queued: build_id=%s", build_id
                    )
                return build_id, cache_hit
        except aiohttp.ClientError as exc:
            raise BuildError(
                f"Proxy unreachable — check network connection. ({exc})"
            ) from exc

    async def get_status(self, job_id: str) -> BuildStatus:
        """Poll proxy for build status, map to BuildState.

        EPIC-006-D2: If status is terminal (timeout/failed) with no artifact
        and we haven't tried yet, automatically retry once with ?refresh=1
        to let the proxy re-poll GitHub and correct stale records.
        """
        data = await self._poll_proxy(job_id)

        proxy_status = data.get("status", "failed")
        artifact = data.get("artifact")

        # D2: auto-refresh on terminal-no-artifact (one attempt per build_id)
        # Gated by _auto_refresh_enabled (EPIC-006-WP3, Deliverable E)
        if (
            self._auto_refresh_enabled
            and proxy_status in PROXY_TERMINAL_NO_ARTIFACT
            and artifact is None
            and job_id not in self._refresh_attempted
        ):
            self._refresh_attempted.add(job_id)
            _LOGGER.info(
                "Proxy refresh=1 attempted for build_id=%s (was %s, no artifact)",
                job_id,
                proxy_status,
            )
            try:
                refreshed = await self._poll_proxy(job_id, refresh=True)
                refreshed_status = refreshed.get("status", proxy_status)
                if refreshed_status != proxy_status:
                    _LOGGER.info(
                        "Proxy refresh corrected build_id=%s: %s → %s",
                        job_id,
                        proxy_status,
                        refreshed_status,
                    )
                    data = refreshed
                    proxy_status = refreshed_status
                    artifact = refreshed.get("artifact")
                else:
                    _LOGGER.warning(
                        "Refresh did not resolve terminal state; "
                        "build %s remains %s",
                        job_id,
                        proxy_status,
                    )
            except (BuildError, aiohttp.ClientError):
                _LOGGER.warning(
                    "Proxy refresh=1 failed for build_id=%s; "
                    "returning cached terminal state",
                    job_id,
                )

        # Map proxy status → BuildState
        mapped = PROXY_STATUS_MAP.get(proxy_status, BUILD_STATE_FAILED)
        state = BuildState(mapped)

        # Store artifact info for later fetch_artifact() call
        if artifact and state == BuildState.SUCCESS:
            self._artifact_info[job_id] = artifact

        return BuildStatus(
            state=state,
            progress_pct=data.get("progress", 0),
            logs_tail=data.get("run_url", ""),
            artifact_names=(
                ["firmware.ota.bin", "manifest.json"]
                if state == BuildState.SUCCESS and artifact
                else []
            ),
            error=data.get("error"),
        )

    async def _poll_proxy(
        self, job_id: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Raw HTTP poll to proxy GET /build/{job_id}[?refresh=1]."""
        session = await self._get_session()
        url = f"{self._base_url}/build/{job_id}"
        if refresh:
            url += "?refresh=1"
        try:
            async with session.get(url, headers=self._auth_headers()) as resp:
                if resp.status == 404:
                    raise BuildError(f"Build not found: {job_id}")
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Proxy status query failed: HTTP {resp.status} — {body}"
                    )
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise BuildError(
                f"Proxy unreachable — check network connection. ({exc})"
            ) from exc

    async def fetch_artifact(self, job_id: str, artifact_name: str) -> bytes:
        """Download artifact via proxy stream with hash verification + cache.

        Streams chunks while computing hash incrementally (never holds full
        binary in RAM during hashing).  Verifies hash + size against artifact
        info from get_status().

        EPIC-006-A5: Uses atomic ``.part`` writes and per-device cache.
        If a verified file already exists in cache, it is reused without
        re-downloading.
        """
        import hashlib

        from .cache import ensure_cache_dir, get_cached_firmware, write_atomic

        artifact_info = self._artifact_info.get(job_id)
        if not artifact_info:
            raise BuildError(
                f"No artifact info for build {job_id}. "
                "Was the build successful? Call get_status() first."
            )

        expected_hash = artifact_info.get("hash", "")
        hash_alg = artifact_info.get("hash_alg", "sha256")
        expected_size = artifact_info.get("size_bytes", 0)

        # EPIC-006-A5: Check cache before downloading
        device_id = self._build_context.get("device_key", "unknown")
        cache_base = Path("/config") / PROXY_ARTIFACT_CACHE_DIR
        cached = get_cached_firmware(cache_base, device_id, job_id)
        if cached:
            _LOGGER.info(
                "Cache hit: reusing verified firmware from %s", cached
            )
            return cached.read_bytes()

        # Select hasher
        if hash_alg == "sha256":
            hasher = hashlib.sha256()
        elif hash_alg == "md5":
            hasher = hashlib.md5()
        else:
            _LOGGER.warning(
                "Unknown hash_alg '%s', falling back to sha256", hash_alg
            )
            hasher = hashlib.sha256()
            hash_alg = "sha256"

        # Download via proxy stream endpoint
        download_timeout = aiohttp.ClientTimeout(
            connect=10, total=PROXY_DOWNLOAD_TIMEOUT
        )
        session = await self._get_session()

        url = f"{self._base_url}/build/{job_id}/artifact/{artifact_name}"
        _LOGGER.info("Fetching artifact via proxy: %s", url)

        try:
            async with session.get(
                url,
                headers=self._auth_headers(),
                timeout=download_timeout,
            ) as resp:
                if resp.status == 409:
                    raise BuildError(
                        "Build is not complete (status check first)."
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise BuildError(
                        f"Proxy artifact download failed: HTTP {resp.status} — {body}"
                    )

                # Pre-check: Content-Length vs expected size
                content_length = resp.headers.get("Content-Length")
                if content_length and expected_size:
                    cl = int(content_length)
                    if cl != expected_size:
                        raise BuildError(
                            f"Content-Length mismatch: proxy reports {cl} bytes, "
                            f"expected {expected_size} bytes. Do NOT flash."
                        )

                # Stream chunks, compute hash incrementally
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    hasher.update(chunk)
                    chunks.append(chunk)
                    total_bytes += len(chunk)

        except aiohttp.ClientError as exc:
            raise BuildError(
                f"Proxy unreachable during artifact download — "
                f"check network connection. ({exc})"
            ) from exc

        # Verify size
        if expected_size and total_bytes != expected_size:
            raise BuildError(
                f"Firmware size mismatch: received {total_bytes} bytes, "
                f"expected {expected_size} bytes. Do NOT flash. Retry the build."
            )

        # Verify hash
        computed_hash = hasher.hexdigest()
        if expected_hash and computed_hash != expected_hash:
            raise BuildError(
                f"Firmware integrity check FAILED. "
                f"{hash_alg} mismatch: computed {computed_hash[:16]}…, "
                f"expected {expected_hash[:16]}…. Do NOT flash. Retry the build."
            )

        _LOGGER.info(
            "Artifact verified: %s (%d bytes, %s=%s)",
            artifact_name, total_bytes, hash_alg, computed_hash[:16],
        )

        # EPIC-006-A5: Store verified artifact in cache (atomic write)
        # Run blocking file I/O in executor to avoid event-loop warnings.
        firmware_data = b"".join(chunks)

        def _cache_to_disk() -> Path:
            import json as _json

            cache_dir = ensure_cache_dir(cache_base, device_id, job_id)
            fw_path = cache_dir / "firmware.ota.bin"
            write_atomic(
                fw_path, firmware_data,
                expected_hash=expected_hash,
                expected_size=expected_size,
                hash_alg=hash_alg,
            )
            manifest = {
                "build_id": job_id,
                "device_id": device_id,
                "artifact_name": artifact_name,
                "hash_alg": hash_alg,
                "hash": expected_hash,
                "size_bytes": expected_size,
                "computed_hash": computed_hash,
            }
            (cache_dir / "manifest.json").write_text(
                _json.dumps(manifest, indent=2)
            )
            return fw_path

        try:
            fw_path = await asyncio.get_event_loop().run_in_executor(
                None, _cache_to_disk
            )
            _LOGGER.info("Artifact cached: %s", fw_path)
        except Exception as cache_exc:
            # Cache write failure is non-fatal — firmware data is already verified
            _LOGGER.warning("Failed to cache artifact: %s", cache_exc)

        return firmware_data

    async def health_check(self) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(f"{self._base_url}/health") as resp:
                if resp.status != 200:
                    raise BuildError(f"Proxy unhealthy: HTTP {resp.status}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise BuildError(
                f"Proxy unreachable — check network connection. ({exc})"
            ) from exc

    async def whoami(self) -> dict[str, Any] | None:
        """Call /whoami to derive customer_id from API key.

        Returns the whoami response dict on success, or None if the
        endpoint is not available (404) or the call fails.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{self._base_url}/whoami",
                headers=self._auth_headers(),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                _LOGGER.debug("/whoami returned HTTP %s (not available yet)", resp.status)
                return None
        except aiohttp.ClientError:
            _LOGGER.debug("/whoami call failed (endpoint not available)")
            return None

    async def wait_for_completion(
        self,
        job_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        progress_callback: Any | None = None,
    ) -> BuildStatus:
        """Poll proxy until build reaches terminal state.

        Uses proxy-specific defaults for timeout and poll interval.
        """
        return await super().wait_for_completion(
            job_id,
            timeout=timeout or PROXY_DEFAULT_TIMEOUT,
            poll_interval=poll_interval or PROXY_POLL_INTERVAL,
            progress_callback=progress_callback,
        )
