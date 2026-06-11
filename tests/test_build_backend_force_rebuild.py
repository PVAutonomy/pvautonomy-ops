"""[ISSUE-16] force_rebuild must keep the yaml_authority contract.

Regression tests for the silent degradation fixed in issue #16:
``build_firmware`` with ``force_rebuild: true`` used to omit
``payload.yaml_hash``, which also dropped ``build_contract`` and
``yaml_content`` — the GHA runner then regenerated the device YAML from its
own stale registry/generator instead of compiling the YAML HA supplied
(real-world failure 2026-06-11, precision-0 MIC firmware, EPIC-009 P1j).

Proven against pvautonomy-proxy main: the proxy has no build cache (every
POST /build dispatches a fresh workflow run), so force_rebuild needs no
payload change at all to get a cold build.
"""

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMP_DIR = _REPO_ROOT / "custom_components" / "pvautonomy_ops"

# Minimal package bootstrap — load build_backend WITHOUT importing the
# package __init__ (which needs Home Assistant).
_pkg = types.ModuleType("custom_components")
_pkg.__path__ = [str(_REPO_ROOT / "custom_components")]
sys.modules.setdefault("custom_components", _pkg)
_sub = types.ModuleType("custom_components.pvautonomy_ops")
_sub.__path__ = [str(_COMP_DIR)]
sys.modules.setdefault("custom_components.pvautonomy_ops", _sub)


def _load_module(name: str, filename: str):
    full_name = f"custom_components.pvautonomy_ops.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, _COMP_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    setattr(_sub, name, mod)
    spec.loader.exec_module(mod)
    return mod


_load_module("const", "const.py")
_load_module("secret_envelope", "secret_envelope.py")
build_backend = _load_module("build_backend", "build_backend.py")

YAML_CONTENT = "esphome:\n  name: mic600-test\n"
YAML_HASH = hashlib.sha256(YAML_CONTENT.encode()).hexdigest()


class _FakeResponse:
    status = 201

    async def text(self):
        return json.dumps({"build_id": "test-build-1", "status": "dispatched"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    closed = False

    def __init__(self):
        self.posted = []

    def post(self, url, *, json=None, headers=None):
        self.posted.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse()


def _start_build(*, force_rebuild, yaml_content, build_contract="yaml_authority"):
    """Run ProxyRemoteBuildBackend.start_build against a fake session and
    return the posted wire payload."""
    backend = build_backend.ProxyRemoteBuildBackend(
        base_url="https://proxy.test",
        api_key="test-key",
        customer_id="cust-1",
    )
    session = _FakeSession()
    backend._session = session
    ctx = {
        "registry_file": "growatt/mic/mic600.json",
        "device_name": "mic600-test",
        "device_key": "17e9c4",
        "model": "edge101",
        "build_profile": "production",
    }
    if build_contract:
        ctx["build_contract"] = build_contract
    backend.set_build_context(**ctx)
    backend.set_force_rebuild(force_rebuild)

    build_id, cache_hit = asyncio.run(
        backend.start_build("17e9c4", yaml_content)
    )
    assert build_id == "test-build-1"
    assert cache_hit is False
    assert len(session.posted) == 1
    return session.posted[0]["json"]


# ---------------------------------------------------------------------------
# (a) force_rebuild + yaml_content keeps the full yaml_authority contract
# ---------------------------------------------------------------------------

def test_force_rebuild_keeps_yaml_authority_contract():
    payload = _start_build(force_rebuild=True, yaml_content=YAML_CONTENT)
    inner = payload["payload"]
    assert inner["yaml_hash"] == YAML_HASH
    assert inner["build_contract"] == "yaml_authority"
    assert base64.b64decode(inner["yaml_content"]).decode() == YAML_CONTENT


def test_force_rebuild_payload_identical_to_normal_build():
    """force_rebuild is a pure no-op on the wire when yaml_content exists —
    the cold build is guaranteed by the proxy (fresh dispatch per request)."""
    forced = _start_build(force_rebuild=True, yaml_content=YAML_CONTENT)
    normal = _start_build(force_rebuild=False, yaml_content=YAML_CONTENT)
    assert forced == normal


# ---------------------------------------------------------------------------
# (b) behavior WITHOUT yaml_content (true legacy callers) is unchanged
# ---------------------------------------------------------------------------

def test_legacy_caller_without_yaml_content_unchanged():
    forced = _start_build(
        force_rebuild=True, yaml_content="", build_contract=None
    )
    normal = _start_build(
        force_rebuild=False, yaml_content="", build_contract=None
    )
    assert forced == normal
    inner = forced["payload"]
    assert "yaml_hash" not in inner
    assert "build_contract" not in inner
    assert "yaml_content" not in inner
    assert inner["registry_file"] == "inverters/growatt/mic/mic600.json"


# ---------------------------------------------------------------------------
# (c) no degradation path remains reachable with yaml_content present
# ---------------------------------------------------------------------------

def test_no_degradation_log_with_yaml_content(caplog):
    with caplog.at_level("DEBUG"):
        _start_build(force_rebuild=True, yaml_content=YAML_CONTENT)
    degradation_markers = ("yaml_hash omitted", "extras suppressed",
                           "fall back to registry-regeneration")
    for record in caplog.records:
        msg = record.getMessage()
        for marker in degradation_markers:
            assert marker not in msg, f"degradation log resurfaced: {msg}"
    assert any(
        "yaml_authority contract kept" in r.getMessage() for r in caplog.records
    ), "expected forced-cold-build INFO log"


def test_degradation_strings_removed_from_module():
    source = (_COMP_DIR / "build_backend.py").read_text()
    assert "yaml_hash omitted" not in source
    assert "extras suppressed" not in source
