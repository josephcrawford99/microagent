import importlib
import pkgutil
from lib.interface import Interface

INTERFACES: dict[str, type[Interface]] = {}

for _finder, module_name, _ in pkgutil.iter_modules(__path__):
    module = importlib.import_module(f".{module_name}", __package__)
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, Interface) and attr is not Interface:
            INTERFACES[attr.name] = attr
