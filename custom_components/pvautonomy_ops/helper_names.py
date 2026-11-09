"""Entity-ID prefix of the integration's own helper entities (#364).

``switch.py`` and ``time.py`` create five helpers per controller: the Load
First and Battery First activate switches, the Grid First schedule draft and
the two Grid First slot times. The device dashboard looks them up, and its
Activate button writes the drafts, under the dashboard's ``device_name``.

On every path a user builds that dashboard by, the name is the slug of the
entry's device with ``-`` replaced by ``_``: the setup and adoption wizard
(``config_flow.py``), the button **Maintenance → Refresh device dashboard**
(``device_name`` = ``get_device_slug_from_entry(entry)``,
``dashboard_builder.py``, then ``lookup`` and ``ensure_slug()`` in
``__init__.py``) and the refresh after an installation. The helpers take the
same name:

1. identifier = ``get_device_slug_from_entry(entry)``, without ``hass``, as
   the Maintenance page calls it;
2. only when the entry has no slug: ``options[selected_device]``;
3. ``metadata_store.lookup(identifier)``, name =
   ``ensure_slug().replace("-", "_")``.

No other record is tried. When no name results (no identifier, no store,
nothing found, an error, an empty slug), the chain used up to 0.5.1 names the
helpers as before and ``previous`` is ``None``, so nothing is deleted.
Otherwise ``previous`` is what that chain returns, when it differs: the
prefix this entry's helpers had up to 0.5.1.

Bench 05: slug ``sph10k-bench-05`` and ``selected_device``
``edge101_sph10k_bench_05`` give the name ``sph10k_bench_05`` and the
previous name ``edge101_sph10k_bench_05``.

Only the integration's own helpers are named here. Firmware entities keep
their names; resolving them through the registry belongs to the C2 identity
design (#364).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from .config_flow import get_device_slug_from_entry
from .const import CONF_SELECTED_DEVICE, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class HelperDeviceNames(NamedTuple):
    """The prefix for the helper entity IDs, and the one used before it.

    ``previous`` is ``None`` when it equals ``name`` or is unknown.
    """

    name: str | None
    previous: str | None


def normalize_device_name(device_name: str | None) -> str | None:
    """Normalize a selected device name to entity-id slug form."""
    if not device_name:
        return None
    normalized = device_name.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


async def _async_dashboard_name(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the ``device_name`` the user-path dashboards build for *entry*."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    metadata_store = entry_data.get("metadata_store")
    if metadata_store is None:
        return None
    try:
        identifier = get_device_slug_from_entry(entry)
        if identifier is None:
            identifier = entry.options.get(CONF_SELECTED_DEVICE) or ""
        identifier = identifier.strip()
        if not identifier:
            return None
        metadata = await metadata_store.lookup(identifier)
        if metadata is None:
            return None
        slug = metadata.ensure_slug()
    except Exception:  # noqa: BLE001 — defensive: fall back to the 0.5.1 chain
        return None
    return slug.replace("-", "_") if slug else None


async def _async_selected_device_name(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> str | None:
    """Return ``selected_device`` from runtime data, storage or options."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    selected = normalize_device_name(entry_data.get("selected_device"))
    if selected:
        return selected

    input_reader = entry_data.get("input_reader")
    if input_reader is not None:
        try:
            stored = await input_reader.get_selected_device_from_storage(entry.entry_id)
        except Exception:  # noqa: BLE001 — defensive: storage read is best-effort
            stored = None
        selected = normalize_device_name(stored)
        if selected:
            return selected

    return normalize_device_name(entry.options.get(CONF_SELECTED_DEVICE))


async def _async_name_up_to_0_5_1(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> str | None:
    """Return the name the chain up to 0.5.1 gives, unchanged.

    ``selected_device`` first, then the slug of the first stored record with
    the entry's ``ha_device_id``.
    """
    selected = await _async_selected_device_name(hass, entry)
    if selected:
        return selected

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    metadata_store = entry_data.get("metadata_store")
    ha_device_id = entry.options.get("ha_device_id") or entry.data.get("ha_device_id")
    if metadata_store is not None and ha_device_id:
        try:
            for metadata in await metadata_store.get_all():
                if metadata.ha_device_id == ha_device_id:
                    return normalize_device_name(metadata.ensure_slug())
        except Exception:  # noqa: BLE001 — defensive: store read is best-effort
            return None

    return None


async def async_resolve_helper_device_names(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> HelperDeviceNames:
    """Resolve the helper prefix of *entry*: the name of its dashboard.

    See the module docstring for the rule. When the dashboard's name cannot
    be resolved, the 0.5.1 chain names the helpers and nothing is deleted.
    """
    up_to_0_5_1 = await _async_name_up_to_0_5_1(hass, entry)
    name = await _async_dashboard_name(hass, entry)
    if not name:
        return HelperDeviceNames(name=up_to_0_5_1, previous=None)
    return HelperDeviceNames(
        name=name,
        previous=up_to_0_5_1 if up_to_0_5_1 != name else None,
    )
