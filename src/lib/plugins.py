"""Lazy plugin loaders. Replaces the old `lib.registry` package walk —
each plugin module is imported only when its name actually appears in the
TOML, and each module is expected to expose a `Plugin` symbol pointing at
its concrete `Source` / `Interface` / `AgentType` subclass."""

from __future__ import annotations

import functools
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.agent import AgentType
    from lib.source import InputSettings, Source


@functools.cache
def load_input(kind: str, name: str) -> type["Source"]:
    """Import the plugin module and return its `Plugin` symbol. `kind` is
    `"interfaces"` or `"sources"`; `name` is the section key. Interfaces
    live at `sources.interfaces.<name>` (an Interface is a Source that
    can also send), sources at `sources.<name>`."""
    path = f"sources.interfaces.{name}" if kind == "interfaces" else f"sources.{name}"
    return importlib.import_module(path).Plugin


@functools.cache
def load_input_settings(kind: str, name: str) -> type["InputSettings"]:
    """RootConfig class for the input plugin — used by the dashboard for
    introspection (editable fields, REQUIRED_ENV) without instantiating."""
    return load_input(kind, name).settings_cls


@functools.cache
def load_agent_type(name: str) -> type["AgentType"]:
    """Import `agent_types.<name>` and return its `Plugin` symbol."""
    return importlib.import_module(f"agent_types.{name}").Plugin
