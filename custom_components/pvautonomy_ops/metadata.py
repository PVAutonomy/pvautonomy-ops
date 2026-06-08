"""Device Metadata Store for PVAutonomy Ops.

Persistently stores device identity and build parameters (model, site,
number, registry_file) per device.  Used by Config Flow wizard (new device)
and Flash button (re-flash existing device).

Storage: /config/.storage/pvautonomy_ops_devices
Pattern: Same as keyring.py (HA Store API).

Ref: EPIC-006 WP3, Deliverable B.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEVICE_HW_PREFIX, MODEL_REGISTRY_MAP
from .device_id import compute_device_id, compute_node_name, parse_device_id

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "pvautonomy_ops_devices"
STORAGE_VERSION = 1
_SINGLETON_KEY = "pvautonomy_ops_metadata_store"


async def async_get_metadata_store(hass: HomeAssistant) -> "PVAutonomyMetadataStore":
    """Return the domain-global metadata store singleton.

    EPIC-015 P3-03: All callers share one instance backed by the same HA
    storage key, preventing stale in-memory snapshots when multiple callers
    (runtime setup, config flow) create independent store objects.

    Creates and loads the store on first call.  Subsequent calls return
    the cached instance.
    """
    store = hass.data.get(_SINGLETON_KEY)
    if store is not None:
        return store

    store = PVAutonomyMetadataStore(hass)
    await store.async_load()
    hass.data[_SINGLETON_KEY] = store
    return store


@dataclass
class DeviceMetadata:
    """Persistent metadata for one managed device.

    Immutable identity: device_id, ha_device_id, mac_suffix.
    Mutable assignment: site, number (via Relocate).
    """

    device_id: str  # e.g. "edge101_sph10k_haus_02"
    mac_suffix: str  # Last 6 hex chars WiFi MAC (e.g. "2eb1e4")
    manufacturer: str  # e.g. "growatt"
    model_slug: str  # e.g. "sph10k", "mic600"
    site: str  # e.g. "garage", "home"
    number: int  # e.g. 1, 3
    registry_file: str  # e.g. "growatt/sph/sph10k.json"
    created_at: str  # ISO 8601
    updated_at: str  # ISO 8601
    ha_device_id: str = ""  # HA Device Registry UUID (binds to physical device)
    esphome_yaml_filename: str = ""  # e.g. "edge101-mic600-garage-06.device.yaml"
    device_slug: str = ""  # Immutable slug (ADR-003, EPIC-011): e.g. "sph10k-haus-02"

    def ensure_slug(self) -> str:
        """Return device_slug, computing from components if missing (R5 compat)."""
        if self.device_slug:
            return self.device_slug
        return compute_node_name(self.model_slug, self.site, self.number)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceMetadata:
        valid_keys = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid_keys})


class PVAutonomyMetadataStore:
    """Persistent device metadata store using HA Store API.

    Thread-safe via HA Store's internal locking.
    Pattern follows keyring.py exactly.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] = {"version": 1, "devices": {}}

    async def async_load(self) -> None:
        """Load metadata from persistent storage. Call once during setup."""
        stored = await self._store.async_load()
        if stored and isinstance(stored, dict):
            self._data = stored
        else:
            self._data = {"version": 1, "devices": {}}
        count = len(self._data.get("devices", {}))
        _LOGGER.info("Metadata store loaded: %d device(s)", count)

    async def _async_save(self) -> None:
        """Persist current metadata state."""
        await self._store.async_save(self._data)

    async def put(self, metadata: DeviceMetadata) -> None:
        """Store or update device metadata. Keyed by device_id."""
        self._data.setdefault("devices", {})
        metadata.updated_at = datetime.now(timezone.utc).isoformat()
        self._data["devices"][metadata.device_id] = metadata.to_dict()
        await self._async_save()
        _LOGGER.info(
            "Metadata stored: %s (model=%s, site=%s, number=%d)",
            metadata.device_id,
            metadata.model_slug,
            metadata.site,
            metadata.number,
        )

    async def get_by_device_id(self, device_id: str) -> DeviceMetadata | None:
        """Retrieve metadata by device_id (exact match)."""
        entry = self._data.get("devices", {}).get(device_id)
        if entry:
            return DeviceMetadata.from_dict(entry)
        return None

    async def get_by_mac_suffix(self, mac_suffix: str) -> DeviceMetadata | None:
        """Retrieve metadata by MAC suffix (linear scan)."""
        for entry in self._data.get("devices", {}).values():
            if entry.get("mac_suffix") == mac_suffix:
                return DeviceMetadata.from_dict(entry)
        return None

    async def lookup(self, identifier: str) -> DeviceMetadata | None:
        """Pure read-only metadata lookup — no persistence side effects.

        EPIC-015 P2-05: Safe for active runtime callers (flash, gates, sensors)
        that need metadata without risking implicit writes.

        Resolution order (same as resolve steps 1-3, NO step 4):
        1. Direct device_id lookup (exact match)
        2. device_id with hw_prefix prepended + display-name normalization
        3. MAC suffix lookup (linear scan)

        Returns:
            DeviceMetadata or None if not found.
        """
        # 1. Direct device_id lookup
        result = await self.get_by_device_id(identifier)
        if result:
            return result

        # 2. Try with hw_prefix prepended
        if not identifier.startswith(f"{DEVICE_HW_PREFIX}_"):
            prefixed = f"{DEVICE_HW_PREFIX}_{identifier}"
            result = await self.get_by_device_id(prefixed)
            if result:
                return result

        # 2b. Normalize display name
        normalized = identifier.lower().replace(" ", "_").replace("-", "_")
        if normalized != identifier:
            result = await self.get_by_device_id(normalized)
            if result:
                return result
            if not normalized.startswith(f"{DEVICE_HW_PREFIX}_"):
                prefixed = f"{DEVICE_HW_PREFIX}_{normalized}"
                result = await self.get_by_device_id(prefixed)
                if result:
                    return result

        # 3. MAC suffix lookup
        result = await self.get_by_mac_suffix(identifier)
        if result:
            return result

        return None

    async def resolve(self, identifier: str) -> DeviceMetadata | None:
        """Resolve metadata by device_id, mac_suffix, or name-parsing fallback.

        WARNING: Step 4 (name-parsing fallback) has a persistence side effect —
        it auto-creates and persists metadata if the identifier parses as a
        valid device name. Use ``lookup()`` for read-only runtime paths.

        Resolution order:
        1. Direct device_id lookup (exact match)
        2. device_id with hw_prefix prepended
        3. MAC suffix lookup (linear scan)
        4. Name-parsing fallback (e.g. "sph10k_haus_03") → auto-persist

        Args:
            identifier: device_id, mac_suffix, or parseable device name.

        Returns:
            DeviceMetadata or None if not resolvable.
        """
        # Steps 1-3: delegate to read-only lookup()
        result = await self.lookup(identifier)
        if result:
            return result

        # 4. Name-parsing fallback → auto-persist on success
        parsed = parse_device_id(identifier)
        if not parsed and not identifier.startswith(f"{DEVICE_HW_PREFIX}_"):
            parsed = parse_device_id(f"{DEVICE_HW_PREFIX}_{identifier}")

        if parsed and parsed["model"] in MODEL_REGISTRY_MAP:
            model_info = MODEL_REGISTRY_MAP[parsed["model"]]
            device_id = compute_device_id(
                parsed["model"], parsed["site"], parsed["number"]
            )
            now = datetime.now(timezone.utc).isoformat()
            metadata = DeviceMetadata(
                device_id=device_id,
                mac_suffix="",  # Unknown from name-parsing
                manufacturer=model_info["manufacturer"],
                model_slug=parsed["model"],
                site=parsed["site"],
                number=int(parsed["number"]),
                registry_file=model_info["registry_file"],
                created_at=now,
                updated_at=now,
            )
            await self.put(metadata)
            _LOGGER.info(
                "Auto-migrated metadata from name-parsing: %s", device_id
            )
            return metadata

        return None

    async def get_all(self) -> list[DeviceMetadata]:
        """Return all stored device metadata entries."""
        return [
            DeviceMetadata.from_dict(v)
            for v in self._data.get("devices", {}).values()
        ]

    async def delete(self, device_id: str) -> bool:
        """Delete device metadata by device_id. Returns True if found."""
        devices = self._data.get("devices", {})
        if device_id in devices:
            del devices[device_id]
            await self._async_save()
            _LOGGER.info("Metadata deleted: %s", device_id)
            return True
        return False

    async def update_location(
        self, device_id: str, site: str, number: int
    ) -> DeviceMetadata | None:
        """Update site/number for a device (Relocate operation).

        Returns updated metadata or None if device not found.
        """
        metadata = await self.get_by_device_id(device_id)
        if not metadata:
            return None

        # Compute new device_id from updated location
        new_device_id = compute_device_id(
            metadata.model_slug, site, number
        )

        # Delete old entry if device_id changed
        if new_device_id != device_id:
            await self.delete(device_id)

        metadata.site = site
        metadata.number = number
        metadata.device_id = new_device_id
        await self.put(metadata)

        _LOGGER.info(
            "Metadata relocated: %s → %s (site=%s, number=%d)",
            device_id,
            new_device_id,
            site,
            number,
        )
        return metadata
