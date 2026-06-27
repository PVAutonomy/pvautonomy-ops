"""Local pytest harness — faithful lightweight Home Assistant stub.

The original test harness for ``pvautonomy_ops`` relied on a ``conftest.py``
that stubbed the Home Assistant import surface (incl. a ``_StubConfigFlow``
base class). That conftest was NOT carried over when this product surface was
extracted from ``gshubi/home-assistant-config``, so every test that imports
the integration package (e.g. ``tests/test_config_flow.py`` with the Adopt
tests) currently fails to even collect with ``ModuleNotFoundError:
homeassistant``.

This module reconstructs that stub locally. It is **test infrastructure
only** — no product code is touched. It installs a ``sys.meta_path`` finder
that satisfies any ``import homeassistant.*`` with an auto-stub module, and
pre-seeds the few symbols the tests depend on *semantically*:

* ``homeassistant.config_entries.ConfigFlow`` / ``OptionsFlow`` — real,
  subclassable base classes whose flow-result emitters
  (``async_show_form`` / ``async_show_menu`` / ``async_create_entry`` /
  ``async_abort`` / ``async_show_progress[_done]``) return plain ``dict``
  payloads, faithful to the assertions the existing flow tests make
  (``result["type"] == "form"``, ``result["step_id"] == ...``);
* ``homeassistant.core.callback`` — a pass-through decorator;
* ``homeassistant.exceptions.HomeAssistantError`` — a real ``Exception``;
* ``homeassistant.data_entry_flow.FlowResult`` / ``FlowResultType``.

Self-stubbing test modules (which set their own ``sys.modules['homeassistant']``
entries before importing) keep working: ``import`` consults ``sys.modules``
before the meta-path finder, so their explicit stubs take precedence.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types
from unittest.mock import MagicMock


# ── Semantic stubs ───────────────────────────────────────────────────


def _callback(func=None, **_kwargs):
    """Pass-through stand-in for ``homeassistant.core.callback``."""
    if func is None:
        def _wrap(f):
            return f
        return _wrap
    return func


class _HomeAssistant:  # noqa: D401 - simple stand-in
    """Stand-in for ``homeassistant.core.HomeAssistant``."""


class _ConfigEntry:
    """Stand-in for ``homeassistant.config_entries.ConfigEntry``."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class HomeAssistantError(Exception):
    """Stand-in for ``homeassistant.exceptions.HomeAssistantError``.

    Mirrors the real signature (HA ≥ 2023.8): positional message plus the
    optional translation kwargs used by translation_key-based exceptions
    (P1.3-GO-A). The kwargs are stored verbatim so tests can assert them.
    """

    def __init__(
        self,
        *args,
        translation_domain=None,
        translation_key=None,
        translation_placeholders=None,
    ):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class _FlowResultType:
    """Stand-in for ``homeassistant.data_entry_flow.FlowResultType`` (StrEnum-like)."""

    FORM = "form"
    MENU = "menu"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"
    SHOW_PROGRESS = "progress"
    SHOW_PROGRESS_DONE = "progress_done"
    EXTERNAL_STEP = "external"


class _StubFlowBase:
    """Common flow-result emitters returning plain dicts (faithful design)."""

    def __init_subclass__(cls, **kwargs):  # swallow ConfigFlow(domain=...)
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id=None, *, raise_on_progress=True):
        self._unique_id = unique_id
        return None

    def _async_current_entries(self, include_ignore=False):
        return []

    def _abort_if_unique_id_configured(self, *a, **k):
        return None

    def async_show_form(self, *, step_id, data_schema=None, errors=None,
                        description_placeholders=None, last_step=None, **kw):
        return {
            "type": "form",
            "step_id": step_id,
            "errors": errors or {},
            "data_schema": data_schema,
            "description_placeholders": description_placeholders,
        }

    def async_show_menu(self, *, step_id, menu_options,
                        description_placeholders=None, **kw):
        return {
            "type": "menu",
            "step_id": step_id,
            "menu_options": menu_options,
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(self, *, title, data=None, options=None, **kw):
        return {
            "type": "create_entry",
            "title": title,
            "data": data or {},
            "options": options or {},
        }

    def async_abort(self, *, reason, description_placeholders=None, **kw):
        return {"type": "abort", "reason": reason}

    def async_show_progress(self, *, step_id, progress_action=None,
                            progress_task=None, description_placeholders=None, **kw):
        return {
            "type": "progress",
            "step_id": step_id,
            "progress_action": progress_action,
            "progress_task": progress_task,
        }

    def async_show_progress_done(self, *, next_step_id, **kw):
        return {"type": "progress_done", "next_step_id": next_step_id}


class _StubConfigFlow(_StubFlowBase):
    """Stand-in base for ``config_entries.ConfigFlow``."""

    hass = None

    @property
    def unique_id(self):
        return getattr(self, "_unique_id", None)


class _StubOptionsFlow(_StubFlowBase):
    """Stand-in base for ``config_entries.OptionsFlow``.

    ``config_entry`` is a read-only property (mirrors modern HA), so the
    regression that motivated the existing options-flow tests stays covered.
    """

    @property
    def config_entry(self):
        return getattr(self, "_config_entry", None)


# ── Auto-stub module + meta-path finder ──────────────────────────────


class _FakeHAModule(types.ModuleType):
    """A ``homeassistant.*`` module that yields MagicMocks for unknown attrs."""

    __path__: list = []  # treat as a package so submodules import

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        val = MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, val)
        return val


class _HAFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            spec = importlib.machinery.ModuleSpec(fullname, self)
            spec.submodule_search_locations = []  # mark as package
            return spec
        return None

    def create_module(self, spec):
        mod = _FakeHAModule(spec.name)
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        return None


def _seed(name, **attrs):
    """Register/seed a stub module and link it onto its parent package."""
    mod = sys.modules.get(name)
    if not isinstance(mod, types.ModuleType):
        mod = _FakeHAModule(name)
        mod.__path__ = []
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    if "." in name:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if isinstance(parent, types.ModuleType):
            setattr(parent, child, mod)
    return mod


def _install_homeassistant_stub():
    if any(isinstance(f, _HAFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _HAFinder())

    _seed("homeassistant")
    _seed(
        "homeassistant.core",
        HomeAssistant=_HomeAssistant,
        callback=_callback,
        CALLBACK_TYPE=object,
    )
    _seed("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    _seed(
        "homeassistant.config_entries",
        ConfigFlow=_StubConfigFlow,
        OptionsFlow=_StubOptionsFlow,
        ConfigEntry=_ConfigEntry,
        SOURCE_USER="user",
        SOURCE_IMPORT="import",
    )
    _seed(
        "homeassistant.data_entry_flow",
        FlowResult=dict,
        FlowResultType=_FlowResultType,
        AbortFlow=type("AbortFlow", (Exception,), {}),
    )
    _seed(
        "homeassistant.const",
        EVENT_HOMEASSISTANT_STARTED="homeassistant_started",
    )
    # Helper namespace packages used by the import chain (auto-stubbed attrs).
    _seed("homeassistant.helpers")
    _seed("homeassistant.helpers.event")
    _seed("homeassistant.helpers.typing", ConfigType=dict)
    _seed("homeassistant.helpers.device_registry", CONNECTION_NETWORK_MAC="mac")
    _seed("homeassistant.helpers.entity_registry")
    _seed("homeassistant.helpers.storage")
    _seed("homeassistant.util")
    _seed("homeassistant.util.dt")


_install_homeassistant_stub()
