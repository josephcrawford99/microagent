"""Package-scan plugin registry.

Both `interfaces/` and `agent_types/` used to have identical
pkgutil-iter-modules code. This helper is that code, once.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import TypeVar

T = TypeVar("T")


def discover(pkg: ModuleType, base_cls: type[T]) -> dict[str, type[T]]:
    """Import every module in `pkg` and return a `{cls.name: cls}` map of the
    base_cls subclasses defined in them. Subclasses must set a `name`
    attribute; the base class itself is excluded."""
    out: dict[str, type[T]] = {}
    for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f".{module_name}", pkg.__name__)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, base_cls)
                and attr is not base_cls
            ):
                name = getattr(attr, "name", None)
                if isinstance(name, str) and name:
                    out[name] = attr
    return out
