"""Lazy plugin loaders.

A plugin module exposes one Service or Provider subclass. The loader finds
it by inspection; a module with several can mark the right one with
`Plugin = ClassName`.
"""

from __future__ import annotations

import functools
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from providers.base_provider import Provider
    from services.base_service import Service


def _find_plugin(module_name: str, base: type) -> type:
    """The `Plugin` override if set, else the unique `base` subclass defined
    in the module itself."""
    mod = importlib.import_module(module_name)
    if hasattr(mod, "Plugin"):
        return mod.Plugin
    found = [
        obj for obj in vars(mod).values()
        if isinstance(obj, type) and issubclass(obj, base)
        and obj is not base and obj.__module__ == mod.__name__
    ]
    if len(found) != 1:
        raise ImportError(
            f"{module_name} defines {len(found)} {base.__name__} subclasses; "
            "expected one, or set `Plugin = ClassName`"
        )
    return found[0]


@functools.cache
def load_service(name: str) -> type["Service"]:
    from services.base_service import Service
    return _find_plugin(f"services.{name}", Service)


@functools.cache
def load_provider(name: str) -> type["Provider"]:
    from providers.base_provider import Provider
    return _find_plugin(f"providers.{name}", Provider)
